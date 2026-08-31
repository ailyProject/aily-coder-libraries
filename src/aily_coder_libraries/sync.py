from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import os
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import quote, urlsplit

from .index import (
    OUTPUT_FILENAME,
    IndexBuildError,
    Package,
    ReleaseCandidate,
    build_index,
    build_release_entry,
    canonical_repository_url,
    normalise_version,
    read_repository_urls,
    repository_list_digest,
    version_sort_key,
)
from .scanner import ScanResult, TagUpdate, scan_repository
from .state import (
    STATE_FILENAME,
    LoadedState,
    StateError,
    new_state,
    parse_state,
    serialise_state,
)
from .storage import S3Target, StorageConfigError, load_target_settings

LOGGER = logging.getLogger("aily-coder-libraries")
MAX_REPOSITORY_SCAN_ATTEMPTS = 3
MAX_SCAN_WORKERS = 4
MAX_ARCHIVE_SOURCE_BYTES = 512 * 1024 * 1024
RUSTFS_TARGET_NAME = "rustfs"
R2_TARGET_NAME = "cloudflare-r2"
INDEX_TARGET_NAMES = (RUSTFS_TARGET_NAME, R2_TARGET_NAME)


class SyncError(RuntimeError):
    """Raised when an independent registry run cannot finish safely."""


class RegistryTarget(Protocol):
    name: str
    index_key: str
    state_key: str
    stores_state: bool

    def upload_package(self, package: Package, path: Path) -> bool: ...

    def document_matches(self, key: str, size: int, sha256: str) -> bool: ...

    def read_document_bytes(self, key: str, *, max_bytes: int) -> bytes | None: ...

    def upload_document_bytes(
        self,
        key: str,
        data: bytes,
        *,
        sha256: str,
        content_type: str,
        cache_control: str,
    ) -> None: ...


ScanFunction = Callable[..., ScanResult]


@dataclass(frozen=True, slots=True)
class SyncSummary:
    scanned_repository_count: int
    failed_repository_count: int
    discovered_tag_count: int
    added_release_count: int
    release_count: int
    uploaded_package_object_count: int
    uploaded_document_object_count: int
    next_cursor: int
    bootstrap_complete: bool
    index_published: bool


def serialise_index(document: Mapping[str, Any]) -> bytes:
    try:
        text = json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SyncError("索引包含不能序列化为 JSON 的值") from exc
    return (text + "\n").encode("utf-8")


def write_index(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary_path.write_bytes(data)
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _validated_public_base_url(value: str, variable_name: str) -> str:
    base = value.strip().rstrip("/")
    parsed = urlsplit(base)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise IndexBuildError(
            f"{variable_name} 必须是无凭据、查询参数和片段的 HTTP(S) URL"
        )
    return base


def _validated_index_configuration(
    output_paths: Mapping[str, Path],
    public_download_base_urls: Mapping[str, str],
) -> tuple[dict[str, Path], dict[str, str]]:
    expected_names = set(INDEX_TARGET_NAMES)
    if set(output_paths) != expected_names:
        raise SyncError("本地索引输出必须同时配置 RustFS 和 Cloudflare R2")
    if set(public_download_base_urls) != expected_names:
        raise SyncError("公开下载基址必须同时配置 RustFS 和 Cloudflare R2")

    outputs = {name: Path(output_paths[name]) for name in INDEX_TARGET_NAMES}
    if outputs[RUSTFS_TARGET_NAME] == outputs[R2_TARGET_NAME]:
        raise SyncError("RustFS 和 Cloudflare R2 的本地索引输出路径不能相同")

    bases = {
        RUSTFS_TARGET_NAME: _validated_public_base_url(
            public_download_base_urls[RUSTFS_TARGET_NAME],
            "RUSTFS_PUBLIC_DOWNLOAD_BASE_URL",
        ),
        R2_TARGET_NAME: _validated_public_base_url(
            public_download_base_urls[R2_TARGET_NAME],
            "R2_PUBLIC_DOWNLOAD_BASE_URL",
        ),
    }
    if bases[RUSTFS_TARGET_NAME] == bases[R2_TARGET_NAME]:
        raise SyncError("RustFS 和 Cloudflare R2 的公开下载基址不能相同")
    return outputs, bases


def _index_for_public_base_url(
    document: Mapping[str, Any], public_download_base_url: str
) -> dict[str, Any]:
    variant = copy.deepcopy(document)
    for entry in variant["libraries"]:
        archive_file_name = entry["archiveFileName"]
        entry["url"] = (
            f"{public_download_base_url}/"
            f"{quote(archive_file_name, safe='-._~')}"
        )
    return variant


def _repository_items(repository_urls: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    items: list[tuple[str, str]] = []
    seen: set[str] = set()
    for repository_url in repository_urls:
        repository_key = canonical_repository_url(repository_url)
        if repository_key in seen:
            raise IndexBuildError(
                f"repositories.txt 包含规范化后重复的仓库: {repository_url}"
            )
        seen.add(repository_key)
        items.append((repository_key, repository_url))
    return tuple(items)


def _load_state(
    state_owner: RegistryTarget,
    registry_digest: str,
    max_state_bytes: int,
) -> LoadedState:
    payload = state_owner.read_document_bytes(
        state_owner.state_key,
        max_bytes=max_state_bytes,
    )
    if payload is None:
        return new_state(registry_digest)
    try:
        return parse_state(payload)
    except StateError as exc:
        raise StateError(f"{state_owner.name} 的状态无效：{exc}") from exc


def _prepare_working_state(
    loaded: LoadedState,
    *,
    registry_digest: str,
    repository_items: tuple[tuple[str, str], ...],
    public_download_base_url: str,
) -> dict[str, Any]:
    document = copy.deepcopy(loaded.document)
    if document["registryDigest"] != registry_digest:
        document["registryDigest"] = registry_digest
        document["cursor"] = 0
        document["bootstrapComplete"] = False
        document["retryRepositories"] = {}

    cursor = document["cursor"]
    if cursor > len(repository_items):
        raise SyncError("持久状态 cursor 超出 repositories.txt 范围")

    current_urls = dict(repository_items)
    base = _validated_public_base_url(
        public_download_base_url,
        "R2_PUBLIC_DOWNLOAD_BASE_URL",
    )
    for release in document["releases"]:
        repository_key = release["repositoryKey"]
        entry = release["entry"]
        if repository_key in current_urls:
            entry["repository"] = current_urls[repository_key]
        archive_file_name = entry["archiveFileName"]
        entry["url"] = f"{base}/{quote(archive_file_name, safe='-._~')}"
    return document


def _select_batch(
    repository_items: tuple[tuple[str, str], ...],
    cursor: int,
    max_repositories: int,
    retry_repository_keys: set[str],
    *,
    bootstrap_complete: bool,
) -> tuple[tuple[tuple[str, str], ...], int, bool]:
    if not repository_items:
        raise SyncError("repositories.txt 中没有仓库")
    repository_keys = {repository_key for repository_key, _url in repository_items}
    unknown_retry_keys = retry_repository_keys - repository_keys
    if unknown_retry_keys:
        raise SyncError(
            "重试仓库已不在 repositories.txt 中: "
            + ", ".join(sorted(unknown_retry_keys))
        )
    if cursor == len(repository_items) and bootstrap_complete:
        cursor = 0

    batch: list[tuple[str, str]] = []
    for repository_key, repository_url in repository_items:
        if repository_key not in retry_repository_keys:
            continue
        batch.append((repository_key, repository_url))
        if max_repositories and len(batch) == max_repositories:
            return tuple(batch), cursor, cursor == len(repository_items)

    position = cursor
    while position < len(repository_items) and (
        not max_repositories or len(batch) < max_repositories
    ):
        repository_key, repository_url = repository_items[position]
        position += 1
        if repository_key in retry_repository_keys:
            continue
        batch.append((repository_key, repository_url))

    return tuple(batch), position, position == len(repository_items)


def _scan_batch(
    batch: tuple[tuple[str, str], ...],
    repositories_state: dict[str, Any],
    temporary_root: Path,
    *,
    workers: int,
    timeout_seconds: int,
    max_source_bytes: int,
    scan_function: ScanFunction,
) -> tuple[dict[str, ScanResult], tuple[str, ...]]:
    def scan_with_retry(
        repository_key: str,
        repository_url: str,
        known_tags: Mapping[str, object],
    ) -> ScanResult:
        first_error: Exception | None = None
        for _attempt in range(2):
            try:
                return scan_function(
                    repository_key,
                    repository_url,
                    known_tags,
                    temporary_root,
                    timeout_seconds=timeout_seconds,
                    max_source_bytes=max_source_bytes,
                )
            except Exception as exc:
                if first_error is not None:
                    raise exc from first_error
                first_error = exc
        raise AssertionError("unreachable")

    results: dict[str, ScanResult] = {}
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures: dict[Future[ScanResult], tuple[str, str]] = {}
        for repository_key, repository_url in batch:
            repository_state = repositories_state.setdefault(
                repository_key,
                {"url": repository_url, "name": "", "tags": {}},
            )
            repository_state["url"] = repository_url
            futures[
                executor.submit(
                    scan_with_retry,
                    repository_key,
                    repository_url,
                    repository_state["tags"],
                )
            ] = (repository_key, repository_url)

        for future in as_completed(futures):
            repository_key, repository_url = futures[future]
            try:
                results[repository_key] = future.result()
            except Exception as exc:
                failed.append(repository_key)
                LOGGER.warning(
                    "仓库扫描失败，将在下一轮巡检周期重试：%s (%s: %s)",
                    repository_url,
                    type(exc).__name__,
                    exc,
                )
    return results, tuple(failed)


def _tag_document(update: TagUpdate, *, archive_file_name: str | None = None) -> dict[str, Any]:
    return {
        "refOid": update.ref_oid,
        "commitOid": update.commit_oid,
        "archiveFileName": (
            update.archive_file_name
            if archive_file_name is None
            else archive_file_name
        ),
    }


def _invalid_tag_document(candidate: ReleaseCandidate) -> dict[str, Any]:
    return {
        "refOid": candidate.tag_ref_oid,
        "commitOid": candidate.tag_commit_oid,
        "archiveFileName": None,
    }


def _catalog_maps(
    document: Mapping[str, Any],
) -> tuple[
    dict[str, str],
    dict[tuple[str, str], dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    name_owners: dict[str, str] = {}
    for repository_key, repository in document["repositories"].items():
        name = repository["name"]
        if not name:
            continue
        previous = name_owners.setdefault(name.casefold(), repository_key)
        if previous != repository_key:
            raise SyncError(f"持久状态中库名所有权冲突: {name}")

    versions: dict[tuple[str, str], dict[str, Any]] = {}
    archives: dict[str, dict[str, Any]] = {}
    for record in document["releases"]:
        entry = record["entry"]
        identity = (entry["name"].casefold(), normalise_version(entry["version"]))
        previous = versions.setdefault(identity, record)
        if previous is not record:
            raise SyncError(
                f"持久状态中库版本冲突: {entry['name']} {entry['version']}"
            )
        archive_file_name = entry["archiveFileName"]
        previous_archive = archives.setdefault(archive_file_name, record)
        if previous_archive is not record:
            raise SyncError(f"持久状态中归档文件名冲突: {archive_file_name}")
    return name_owners, versions, archives


def _reject_candidates(
    tags: dict[str, Any],
    candidates: list[ReleaseCandidate],
    message: str,
) -> None:
    for candidate in candidates:
        tags[candidate.tag] = _invalid_tag_document(candidate)
        LOGGER.warning(
            "%s，跳过 %s tag %s",
            message,
            candidate.repository_url,
            candidate.tag,
        )


def _merge_scan_results(
    document: dict[str, Any],
    batch: tuple[tuple[str, str], ...],
    scan_results: Mapping[str, ScanResult],
    public_download_base_url: str,
) -> tuple[ReleaseCandidate, ...]:
    name_owners, versions, archives = _catalog_maps(document)
    accepted: list[ReleaseCandidate] = []
    releases: list[dict[str, Any]] = document["releases"]
    releases_by_tag = {
        (release["repositoryKey"], release["tag"]): release
        for release in releases
    }

    for repository_key, repository_url in batch:
        result = scan_results.get(repository_key)
        if result is None:
            continue
        repository = document["repositories"][repository_key]
        tags: dict[str, Any] = repository["tags"]

        for tag, update in sorted(result.tag_updates.items()):
            tags[tag] = _tag_document(update)
            release = releases_by_tag.get((repository_key, tag))
            if release is not None:
                if (
                    update.commit_oid != release["tagCommitOid"]
                    or update.archive_file_name
                    != release["entry"]["archiveFileName"]
                ):
                    raise SyncError(
                        f"scanner 尝试改写已发布 tag: {repository_url} {tag}"
                    )
                # An annotated tag object may be recreated without changing
                # its peeled commit or package. Keep state provenance aligned.
                release["tagRefOid"] = update.ref_oid
        for issue in result.issues:
            LOGGER.warning(
                "%s tag %s 未发布：%s",
                repository_url,
                issue.tag,
                issue.message,
            )

        candidates = sorted(
            result.candidates,
            key=lambda candidate: (
                version_sort_key(candidate.metadata.version),
                candidate.metadata.version,
                candidate.tag,
            ),
            reverse=True,
        )
        if not candidates:
            continue

        locked_name = repository["name"]
        if not locked_name:
            locked_name = candidates[0].metadata.name
            owner = name_owners.get(locked_name.casefold())
            if owner is not None and owner != repository_key:
                _reject_candidates(
                    tags,
                    candidates,
                    f"库名 {locked_name!r} 已由其他仓库占用",
                )
                continue
            repository["name"] = locked_name
            name_owners[locked_name.casefold()] = repository_key

        matching: list[ReleaseCandidate] = []
        for candidate in candidates:
            if candidate.metadata.name != locked_name:
                tags[candidate.tag] = _invalid_tag_document(candidate)
                LOGGER.warning(
                    "%s tag %s 的库名 %r 与锁定名称 %r 不一致",
                    repository_url,
                    candidate.tag,
                    candidate.metadata.name,
                    locked_name,
                )
            else:
                matching.append(candidate)

        grouped: dict[tuple[str, str], list[ReleaseCandidate]] = {}
        for candidate in matching:
            identity = (
                candidate.metadata.name.casefold(),
                normalise_version(candidate.metadata.version),
            )
            grouped.setdefault(identity, []).append(candidate)

        for identity in sorted(
            grouped,
            key=lambda item: (item[0], version_sort_key(item[1]), item[1]),
        ):
            group = sorted(grouped[identity], key=lambda item: item.tag)
            existing = versions.get(identity)
            if existing is not None:
                existing_commit = existing["tagCommitOid"]
                existing_archive = existing["entry"]["archiveFileName"]
                for candidate in group:
                    if (
                        existing["repositoryKey"] == repository_key
                        and existing_commit == candidate.tag_commit_oid
                    ):
                        tags[candidate.tag] = {
                            "refOid": candidate.tag_ref_oid,
                            "commitOid": candidate.tag_commit_oid,
                            "archiveFileName": existing_archive,
                        }
                    else:
                        tags[candidate.tag] = _invalid_tag_document(candidate)
                        LOGGER.warning(
                            "%s tag %s 与已发布的 %s %s commit 冲突",
                            repository_url,
                            candidate.tag,
                            candidate.metadata.name,
                            candidate.metadata.version,
                        )
                continue

            commits = {candidate.tag_commit_oid for candidate in group}
            if len(commits) != 1:
                _reject_candidates(
                    tags,
                    group,
                    (
                        f"{group[0].metadata.name} {group[0].metadata.version} "
                        "由多个不同 commit 声明"
                    ),
                )
                continue

            chosen = group[0]
            archive_file_name = chosen.package.archive_file_name
            if archive_file_name in archives:
                _reject_candidates(
                    tags,
                    group,
                    f"扁平归档文件名 {archive_file_name!r} 已被占用",
                )
                continue

            entry = build_release_entry(
                chosen.metadata,
                repository_url,
                chosen.package,
                public_download_base_url,
            )
            release_record = {
                "repositoryKey": repository_key,
                "tag": chosen.tag,
                "tagRefOid": chosen.tag_ref_oid,
                "tagCommitOid": chosen.tag_commit_oid,
                "entry": entry,
            }
            releases.append(release_record)
            releases_by_tag[(repository_key, chosen.tag)] = release_record
            versions[identity] = release_record
            archives[archive_file_name] = release_record
            for candidate in group:
                tags[candidate.tag] = {
                    "refOid": candidate.tag_ref_oid,
                    "commitOid": candidate.tag_commit_oid,
                    "archiveFileName": archive_file_name,
                }
            accepted.append(chosen)

    releases.sort(
        key=lambda record: (
            record["repositoryKey"],
            record["entry"]["name"].casefold(),
            record["entry"]["version"],
            record["tag"],
        )
    )
    return tuple(accepted)


def _upload_one_package(
    candidate: ReleaseCandidate,
    targets: tuple[RegistryTarget, ...],
) -> int:
    uploaded = 0
    for target in targets:
        if target.upload_package(candidate.package, candidate.archive_path):
            uploaded += 1
    return uploaded


def _upload_packages(
    candidates: tuple[ReleaseCandidate, ...],
    targets: tuple[RegistryTarget, ...],
    *,
    workers: int,
) -> int:
    if not candidates:
        return 0
    uploaded = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_upload_one_package, candidate, targets): candidate
            for candidate in candidates
        }
        try:
            for future in as_completed(futures):
                uploaded += future.result()
        except Exception:
            for future in futures:
                future.cancel()
            raise
    return uploaded


def _publish_documents(
    targets: tuple[RegistryTarget, ...],
    state_owner: RegistryTarget,
    state_data: bytes,
    index_data_by_target: Mapping[str, bytes] | None,
) -> int:
    state_sha256 = hashlib.sha256(state_data).hexdigest()

    state_matches = state_owner.document_matches(
        state_owner.state_key,
        len(state_data),
        state_sha256,
    )
    index_uploads: list[tuple[RegistryTarget, bytes, str]] = []
    if index_data_by_target is not None:
        for target in targets:
            index_data = index_data_by_target[target.name]
            index_sha256 = hashlib.sha256(index_data).hexdigest()
            if not target.document_matches(
                target.index_key,
                len(index_data),
                index_sha256,
            ):
                index_uploads.append((target, index_data, index_sha256))

    # The single durable state is written before either public index copy.
    uploaded = 0
    if not state_matches:
        state_owner.upload_document_bytes(
            state_owner.state_key,
            state_data,
            sha256=state_sha256,
            content_type="application/json; charset=utf-8",
            cache_control="no-store",
        )
        uploaded += 1
    for target, index_data, index_sha256 in index_uploads:
        target.upload_document_bytes(
            target.index_key,
            index_data,
            sha256=index_sha256,
            content_type="application/json; charset=utf-8",
            cache_control="no-store, no-cache, must-revalidate, max-age=0",
        )
        uploaded += 1
    return uploaded


def synchronise(
    repository_urls: tuple[str, ...],
    targets: tuple[RegistryTarget, ...],
    output_paths: Mapping[str, Path],
    public_download_base_urls: Mapping[str, str],
    *,
    workers: int = 4,
    max_repositories: int = 250,
    timeout_seconds: int = 120,
    max_source_bytes: int = MAX_ARCHIVE_SOURCE_BYTES,
    max_state_bytes: int = 512 * 1024 * 1024,
    dry_run: bool = False,
    scan_function: ScanFunction = scan_repository,
) -> SyncSummary:
    """Scan a bounded batch, replicate packages, and publish target-specific indexes."""
    if workers <= 0:
        raise SyncError("workers 必须大于 0")
    if workers > MAX_SCAN_WORKERS:
        raise SyncError(f"workers 不能大于 {MAX_SCAN_WORKERS}")
    if max_repositories < 0:
        raise SyncError("max_repositories 不能小于 0")
    if min(timeout_seconds, max_source_bytes, max_state_bytes) <= 0:
        raise SyncError("超时和大小限制必须大于 0")
    if max_source_bytes > MAX_ARCHIVE_SOURCE_BYTES:
        raise SyncError(
            f"MAX_ARCHIVE_SOURCE_BYTES 不能大于 {MAX_ARCHIVE_SOURCE_BYTES}"
        )
    if dry_run:
        if targets:
            raise SyncError("dry-run 不应配置对象存储目标")
    elif len(targets) != 2:
        raise SyncError("同步必须恰好配置 RustFS 和 Cloudflare R2 两个目标")
    if not dry_run:
        target_names = [target.name for target in targets]
        if set(target_names) != set(INDEX_TARGET_NAMES) or len(set(target_names)) != 2:
            raise SyncError("同步目标必须分别是 RustFS 和 Cloudflare R2")
        if any(target.index_key != OUTPUT_FILENAME for target in targets):
            raise SyncError(f"两端索引对象名必须统一为 {OUTPUT_FILENAME}")
    state_owners = tuple(target for target in targets if target.stores_state)
    if not dry_run and len(state_owners) != 1:
        raise SyncError("同步必须恰好配置一个 state 存储目标")

    local_outputs, download_bases = _validated_index_configuration(
        output_paths,
        public_download_base_urls,
    )
    # State schema v1 requires one entry.url. Keep the state owner's R2 URL as
    # the canonical value, then derive each public index without mutating state.
    state_download_base = download_bases[
        state_owners[0].name if state_owners else R2_TARGET_NAME
    ]

    repository_items = _repository_items(repository_urls)
    registry_digest = repository_list_digest(repository_urls)
    loaded = (
        new_state(registry_digest)
        if dry_run
        else _load_state(state_owners[0], registry_digest, max_state_bytes)
    )
    document = _prepare_working_state(
        loaded,
        registry_digest=registry_digest,
        repository_items=repository_items,
        public_download_base_url=state_download_base,
    )
    batch, next_cursor, reached_end = _select_batch(
        repository_items,
        document["cursor"],
        max_repositories,
        set(document["retryRepositories"]),
        bootstrap_complete=document["bootstrapComplete"],
    )
    LOGGER.info(
        "本轮扫描 %d 个仓库，cursor=%d/%d",
        len(batch),
        document["cursor"],
        len(repository_items),
    )

    retry_repositories: dict[str, int] = document["retryRepositories"]
    scanned_repository_count = 0
    failed_repository_count = 0
    discovered_tag_count = 0
    added_release_count = 0
    uploaded_packages = 0

    # A completed scan may retain its generated ZIPs until upload. Process at
    # most one worker-sized window at a time so temporary disk use is bounded
    # by concurrency rather than MAX_REPOSITORIES_PER_RUN.
    for start in range(0, len(batch), workers):
        window = batch[start : start + workers]
        with tempfile.TemporaryDirectory(
            prefix="aily-coder-libraries-"
        ) as directory:
            scan_results, failed = _scan_batch(
                window,
                document["repositories"],
                Path(directory),
                workers=workers,
                timeout_seconds=timeout_seconds,
                max_source_bytes=max_source_bytes,
                scan_function=scan_function,
            )
            scanned_repository_count += len(scan_results)
            failed_repository_count += len(failed)
            discovered_tag_count += sum(
                result.remote_tag_count for result in scan_results.values()
            )

            for repository_key in scan_results:
                retry_repositories.pop(repository_key, None)
            for repository_key in sorted(failed):
                failure_count = retry_repositories.get(repository_key, 0) + 1
                if failure_count >= MAX_REPOSITORY_SCAN_ATTEMPTS:
                    retry_repositories.pop(repository_key, None)
                    LOGGER.warning(
                        "仓库连续 %d 轮扫描失败，本轮视为已评估；后续稳态巡检仍会重试：%s",
                        MAX_REPOSITORY_SCAN_ATTEMPTS,
                        repository_key,
                    )
                else:
                    retry_repositories[repository_key] = failure_count
            candidates = _merge_scan_results(
                document,
                window,
                scan_results,
                state_download_base,
            )
            added_release_count += len(candidates)
            if not dry_run:
                uploaded_packages += _upload_packages(
                    candidates, targets, workers=workers
                )

    if document["bootstrapComplete"]:
        document["cursor"] = 0 if reached_end else next_cursor
    else:
        document["cursor"] = next_cursor
        if reached_end and not retry_repositories:
            document["bootstrapComplete"] = True
            document["cursor"] = 0
    next_cursor = document["cursor"]
    document["generation"] = loaded.document["generation"] + 1
    document["parentDigest"] = loaded.digest
    state_data = serialise_state(document)
    # Parse our own output before any remote write so state schema failures
    # cannot leave a package-referencing public index.
    parse_state(state_data)

    active_repository_keys = {key for key, _url in repository_items}
    index_document = build_index(
        document["releases"],
        active_repository_keys=active_repository_keys,
    )
    index_data_by_target = {
        target_name: serialise_index(
            _index_for_public_base_url(index_document, download_bases[target_name])
        )
        for target_name in INDEX_TARGET_NAMES
    }
    for target_name in INDEX_TARGET_NAMES:
        write_index(local_outputs[target_name], index_data_by_target[target_name])

    if (
        document["bootstrapComplete"]
        and not index_document["libraries"]
        and repository_items
    ):
        raise SyncError("bootstrap 完成但没有任何可发布版本，拒绝发布空索引")

    uploaded_documents = 0
    if not dry_run:
        public_index_data_by_target = (
            index_data_by_target if document["bootstrapComplete"] else None
        )
        uploaded_documents = _publish_documents(
            targets,
            state_owners[0],
            state_data,
            public_index_data_by_target,
        )

    if not document["bootstrapComplete"]:
        LOGGER.info(
            "bootstrap 尚未完成；状态已保存，公开索引保持不变，next cursor=%d",
            next_cursor,
        )
    return SyncSummary(
        scanned_repository_count=scanned_repository_count,
        failed_repository_count=failed_repository_count,
        discovered_tag_count=discovered_tag_count,
        added_release_count=added_release_count,
        release_count=len(index_document["libraries"]),
        uploaded_package_object_count=uploaded_packages,
        uploaded_document_object_count=uploaded_documents,
        next_cursor=next_cursor,
        bootstrap_complete=document["bootstrapComplete"],
        index_published=(not dry_run and document["bootstrapComplete"]),
    )


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return parsed


def _non_negative_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("不能小于 0")
    return parsed


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="独立扫描 Git tag 并同步 Aily Coder Arduino 库索引"
    )
    parser.add_argument(
        "--repositories",
        default=os.environ.get("REPOSITORIES_FILE", "repositories.txt"),
        help="仓库 URL 列表",
    )
    parser.add_argument(
        "--output-directory",
        default=os.environ.get("OUTPUT_DIRECTORY", "dist"),
        help="本地生成的双端候选索引目录",
    )
    parser.add_argument(
        "--rustfs-public-download-base-url",
        default=os.environ.get("RUSTFS_PUBLIC_DOWNLOAD_BASE_URL", ""),
        help="RustFS package bucket 根目录的公开 URL",
    )
    parser.add_argument(
        "--r2-public-download-base-url",
        default=os.environ.get("R2_PUBLIC_DOWNLOAD_BASE_URL", ""),
        help="Cloudflare R2 package bucket 根目录的公开 URL",
    )
    parser.add_argument(
        "--workers",
        type=_positive_integer,
        default=os.environ.get("SCAN_WORKERS", "4"),
        help="并行仓库扫描和包上传数",
    )
    parser.add_argument(
        "--max-repositories",
        type=_non_negative_integer,
        default=os.environ.get("MAX_REPOSITORIES_PER_RUN", "250"),
        help="本轮最多扫描的仓库数；0 表示从 cursor 到列表末尾",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="从空状态扫描候选批次并写本地索引，不访问对象存储",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _build_argument_parser().parse_args(argv)
    try:
        if not args.rustfs_public_download_base_url.strip():
            raise IndexBuildError("缺少 RUSTFS_PUBLIC_DOWNLOAD_BASE_URL")
        if not args.r2_public_download_base_url.strip():
            raise IndexBuildError("缺少 R2_PUBLIC_DOWNLOAD_BASE_URL")

        timeout_seconds = int(os.environ.get("GIT_TIMEOUT_SECONDS", "120"))
        max_source_bytes = int(
            os.environ.get(
                "MAX_ARCHIVE_SOURCE_BYTES", str(MAX_ARCHIVE_SOURCE_BYTES)
            )
        )
        max_state_bytes = int(
            os.environ.get("MAX_STATE_BYTES", str(512 * 1024 * 1024))
        )
        if min(timeout_seconds, max_source_bytes, max_state_bytes) <= 0:
            raise ValueError("超时和大小限制必须大于 0")

        repository_urls = read_repository_urls(Path(args.repositories))
        if args.dry_run:
            targets: tuple[RegistryTarget, ...] = ()
        else:
            settings = load_target_settings()
            targets = tuple(
                S3Target.create(
                    item,
                    max_pool_connections=max(10, args.workers * 2),
                )
                for item in settings
            )

        output_directory = Path(args.output_directory)
        output_paths = {
            RUSTFS_TARGET_NAME: output_directory / RUSTFS_TARGET_NAME / OUTPUT_FILENAME,
            R2_TARGET_NAME: output_directory / "r2" / OUTPUT_FILENAME,
        }
        public_download_base_urls = {
            RUSTFS_TARGET_NAME: args.rustfs_public_download_base_url,
            R2_TARGET_NAME: args.r2_public_download_base_url,
        }
        summary = synchronise(
            repository_urls,
            targets,
            output_paths,
            public_download_base_urls,
            workers=args.workers,
            max_repositories=args.max_repositories,
            timeout_seconds=timeout_seconds,
            max_source_bytes=max_source_bytes,
            max_state_bytes=max_state_bytes,
            dry_run=args.dry_run,
        )
        LOGGER.info(
            "完成：扫描仓库 %d（失败 %d），发现 tag %d，新增版本 %d，"
            "包对象上传 %d，文档对象上传 %d，索引版本 %d，bootstrap=%s，公开索引=%s",
            summary.scanned_repository_count,
            summary.failed_repository_count,
            summary.discovered_tag_count,
            summary.added_release_count,
            summary.uploaded_package_object_count,
            summary.uploaded_document_object_count,
            summary.release_count,
            "完成" if summary.bootstrap_complete else "进行中",
            "已就绪" if summary.index_published else "未更新",
        )
        return 0
    except (
        IndexBuildError,
        StateError,
        StorageConfigError,
        SyncError,
        OSError,
        ValueError,
    ) as exc:
        LOGGER.error("同步失败：%s", exc)
        return 1
    except Exception as exc:
        # SDK exception classes differ between RustFS and R2. Never include
        # configured credential values in the error path.
        LOGGER.error("同步或对象存储操作失败：%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
