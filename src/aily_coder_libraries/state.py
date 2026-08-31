from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Any, Mapping

from .index import (
    IndexBuildError,
    LibraryMetadata,
    archive_stem,
    normalise_version,
)


STATE_FILENAME = "aily_coder_library_state.json"
STATE_SCHEMA_VERSION = 1

_TOP_LEVEL_KEYS = {
    "schemaVersion",
    "generation",
    "parentDigest",
    "registryDigest",
    "cursor",
    "bootstrapComplete",
    "retryRepositories",
    "repositories",
    "releases",
}
_REPOSITORY_KEYS = {"url", "name", "tags"}
_TAG_KEYS = {"refOid", "commitOid", "archiveFileName"}
_RELEASE_KEYS = {
    "repositoryKey",
    "tag",
    "tagRefOid",
    "tagCommitOid",
    "entry",
}
_ENTRY_REQUIRED_KEYS = {
    "name",
    "version",
    "author",
    "maintainer",
    "sentence",
    "architectures",
    "repository",
    "url",
    "archiveFileName",
    "size",
    "checksum",
}
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_CHECKSUM_PATTERN = re.compile(r"^SHA-256:[0-9a-f]{64}$")


class StateError(ValueError):
    """Raised when a persisted synchronisation state is invalid or divergent."""


@dataclass(frozen=True, slots=True)
class LoadedState:
    document: dict[str, Any]
    digest: str
    data: bytes


def _require_exact_keys(
    value: Mapping[str, Any], expected: set[str], location: str
) -> None:
    actual = set(value)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(repr(item) for item in actual - expected)
    details: list[str] = []
    if missing:
        details.append(f"缺少 {', '.join(missing)}")
    if extra:
        details.append(f"包含未知字段 {', '.join(extra)}")
    raise StateError(f"{location} 字段无效：{'；'.join(details)}")


def _require_non_empty_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise StateError(f"{location} 必须是非空字符串")
    return value


def _validate_sha256(value: Any, location: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        suffix = "或 null" if nullable else ""
        raise StateError(f"{location} 必须是小写 SHA-256 哈希{suffix}")


def _validate_git_oid(value: Any, location: str) -> None:
    if not isinstance(value, str) or not _GIT_OID_PATTERN.fullmatch(value):
        raise StateError(f"{location} 必须是 40 或 64 位小写 Git OID")


def _validate_archive_file_name(value: Any, location: str) -> str:
    name = _require_non_empty_string(value, location)
    if (
        name in {".", ".."}
        or "/" in name
        or "\\" in name
        or PureWindowsPath(name).name != name
        or not name.casefold().endswith(".zip")
        or any(ord(character) < 32 for character in name)
    ):
        raise StateError(f"{location} 必须是无路径的 ZIP 文件名")
    return name


def _validate_string_list(value: Any, location: str) -> None:
    if not isinstance(value, list) or not value:
        raise StateError(f"{location} 必须是非空字符串数组")
    for index, item in enumerate(value):
        _require_non_empty_string(item, f"{location}[{index}]")


def _validate_entry(entry: Any, location: str) -> str:
    if not isinstance(entry, dict):
        raise StateError(f"{location} 必须是 JSON 对象")
    missing = sorted(_ENTRY_REQUIRED_KEYS - set(entry))
    if missing:
        raise StateError(f"{location} 缺少基本字段：{', '.join(missing)}")

    for field in (
        "name",
        "version",
        "author",
        "maintainer",
        "sentence",
        "repository",
        "url",
    ):
        _require_non_empty_string(entry[field], f"{location}.{field}")
    _validate_string_list(entry["architectures"], f"{location}.architectures")
    archive_file_name = _validate_archive_file_name(
        entry["archiveFileName"], f"{location}.archiveFileName"
    )
    try:
        normalised_version = normalise_version(entry["version"])
    except IndexBuildError as exc:
        raise StateError(f"{location}.version 无效：{exc}") from exc
    if entry["version"] != normalised_version:
        raise StateError(
            f"{location}.version 必须使用规范化版本 {normalised_version!r}"
        )
    try:
        metadata = LibraryMetadata(
            name=entry["name"],
            version=normalised_version,
            author=entry["author"],
            maintainer=entry["maintainer"],
            sentence=entry["sentence"],
        )
        expected_archive_file_name = f"{archive_stem(metadata)}.zip"
    except IndexBuildError as exc:
        raise StateError(f"{location} 无法生成归档文件名：{exc}") from exc
    if archive_file_name != expected_archive_file_name:
        raise StateError(
            f"{location}.archiveFileName 必须是 {expected_archive_file_name!r}"
        )

    size = entry["size"]
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise StateError(f"{location}.size 必须是正整数")
    checksum = entry["checksum"]
    if not isinstance(checksum, str) or not _CHECKSUM_PATTERN.fullmatch(checksum):
        raise StateError(
            f"{location}.checksum 必须是 SHA-256: 后接 64 位小写十六进制"
        )
    return archive_file_name


def _validate_document(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise StateError("状态根节点必须是 JSON 对象")
    _require_exact_keys(document, _TOP_LEVEL_KEYS, "状态根节点")

    if document["schemaVersion"] != STATE_SCHEMA_VERSION or isinstance(
        document["schemaVersion"], bool
    ):
        raise StateError(f"不支持的状态 schemaVersion: {document['schemaVersion']!r}")

    generation = document["generation"]
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise StateError("generation 必须是非负整数")
    _validate_sha256(document["registryDigest"], "registryDigest")
    _validate_sha256(document["parentDigest"], "parentDigest", nullable=True)
    if generation == 0 and document["parentDigest"] is not None:
        raise StateError("generation 为 0 时 parentDigest 必须是 null")
    if generation > 0 and document["parentDigest"] is None:
        raise StateError("generation 大于 0 时 parentDigest 不能为空")

    cursor = document["cursor"]
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        raise StateError("cursor 必须是非负整数")
    if not isinstance(document["bootstrapComplete"], bool):
        raise StateError("bootstrapComplete 必须是布尔值")

    repositories = document["repositories"]
    if not isinstance(repositories, dict):
        raise StateError("repositories 必须是 JSON 对象")
    for repository_key, repository in repositories.items():
        _require_non_empty_string(repository_key, "repositories 的 key")
        location = f"repositories[{repository_key!r}]"
        if not isinstance(repository, dict):
            raise StateError(f"{location} 必须是 JSON 对象")
        _require_exact_keys(repository, _REPOSITORY_KEYS, location)
        _require_non_empty_string(repository["url"], f"{location}.url")
        if not isinstance(repository["name"], str):
            raise StateError(f"{location}.name 必须是字符串")
        tags = repository["tags"]
        if not isinstance(tags, dict):
            raise StateError(f"{location}.tags 必须是 JSON 对象")
        for tag_name, tag in tags.items():
            _require_non_empty_string(tag_name, f"{location}.tags 的 key")
            tag_location = f"{location}.tags[{tag_name!r}]"
            if not isinstance(tag, dict):
                raise StateError(f"{tag_location} 必须是 JSON 对象")
            _require_exact_keys(tag, _TAG_KEYS, tag_location)
            _validate_git_oid(tag["refOid"], f"{tag_location}.refOid")
            commit_oid = tag["commitOid"]
            if commit_oid is not None:
                _validate_git_oid(commit_oid, f"{tag_location}.commitOid")
            archive_file_name = tag["archiveFileName"]
            if archive_file_name is not None:
                _validate_archive_file_name(
                    archive_file_name, f"{tag_location}.archiveFileName"
                )
            if commit_oid is None and archive_file_name is not None:
                raise StateError(
                    f"{tag_location}.commitOid 为 null 时不能关联 archiveFileName"
                )

    retry_repositories = document["retryRepositories"]
    if not isinstance(retry_repositories, dict):
        raise StateError("retryRepositories 必须是 JSON 对象")
    for repository_key, failure_count in retry_repositories.items():
        _require_non_empty_string(repository_key, "retryRepositories 的 key")
        if repository_key not in repositories:
            raise StateError(
                f"retryRepositories[{repository_key!r}] 不存在于 repositories"
            )
        if (
            isinstance(failure_count, bool)
            or not isinstance(failure_count, int)
            or failure_count <= 0
        ):
            raise StateError(
                f"retryRepositories[{repository_key!r}] 必须是正整数"
            )

    releases = document["releases"]
    if not isinstance(releases, list):
        raise StateError("releases 必须是 JSON 数组")
    release_identities: set[tuple[str, str]] = set()
    archive_owners: dict[str, tuple[str, str]] = {}
    library_versions: set[tuple[str, str]] = set()
    library_owners: dict[str, str] = {}
    for index, release in enumerate(releases):
        location = f"releases[{index}]"
        if not isinstance(release, dict):
            raise StateError(f"{location} 必须是 JSON 对象")
        _require_exact_keys(release, _RELEASE_KEYS, location)
        repository_key = _require_non_empty_string(
            release["repositoryKey"], f"{location}.repositoryKey"
        )
        tag_name = _require_non_empty_string(release["tag"], f"{location}.tag")
        _validate_git_oid(release["tagRefOid"], f"{location}.tagRefOid")
        _validate_git_oid(release["tagCommitOid"], f"{location}.tagCommitOid")
        archive_file_name = _validate_entry(release["entry"], f"{location}.entry")

        identity = (repository_key, tag_name)
        if identity in release_identities:
            raise StateError(f"{location} 与已有 release 重复")
        release_identities.add(identity)

        repository = repositories.get(repository_key)
        if not isinstance(repository, dict):
            raise StateError(f"{location}.repositoryKey 不存在于 repositories")
        tag = repository["tags"].get(tag_name)
        if not isinstance(tag, dict):
            raise StateError(f"{location}.tag 不存在于对应 repository.tags")
        if tag["refOid"] != release["tagRefOid"]:
            raise StateError(f"{location}.tagRefOid 与 repository tag 不一致")
        if tag["commitOid"] != release["tagCommitOid"]:
            raise StateError(f"{location}.tagCommitOid 与 repository tag 不一致")
        if tag["archiveFileName"] != archive_file_name:
            raise StateError(f"{location} 的 archiveFileName 与 repository tag 不一致")

        owner = archive_owners.setdefault(archive_file_name, identity)
        if owner != identity:
            raise StateError(f"{location} 的 archiveFileName 与已有 release 冲突")
        entry = release["entry"]
        if repository["name"] != entry["name"]:
            raise StateError(f"{location}.entry.name 与 repository 锁定名称不一致")
        library_key = entry["name"].casefold()
        library_owner = library_owners.setdefault(library_key, repository_key)
        if library_owner != repository_key:
            raise StateError(f"{location}.entry.name 已由其他 repository 占用")
        library_version = (library_key, entry["version"])
        if library_version in library_versions:
            raise StateError(f"{location} 的库名和版本与已有 release 重复")
        library_versions.add(library_version)

    return document


def serialise_state(document: dict[str, Any]) -> bytes:
    """Validate and deterministically serialise a state document."""
    _validate_document(document)
    try:
        text = json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        data = (text + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise StateError("状态包含不能序列化为 JSON 的值") from exc
    return data


def _loaded_state(document: dict[str, Any]) -> LoadedState:
    data = serialise_state(document)
    return LoadedState(
        document=document,
        digest=hashlib.sha256(data).hexdigest(),
        data=data,
    )


def new_state(registry_digest: str) -> LoadedState:
    """Create an empty generation-zero state for a repository registry."""
    _validate_sha256(registry_digest, "registryDigest")
    return _loaded_state(
        {
            "schemaVersion": STATE_SCHEMA_VERSION,
            "generation": 0,
            "parentDigest": None,
            "registryDigest": registry_digest,
            "cursor": 0,
            "bootstrapComplete": False,
            "retryRepositories": {},
            "repositories": {},
            "releases": [],
        }
    )


def _reject_json_constant(value: str) -> None:
    raise StateError(f"状态包含无效 JSON 数值 {value}")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StateError(f"状态包含重复 JSON 字段 {key!r}")
        result[key] = value
    return result


def parse_state(data: bytes) -> LoadedState:
    """Parse, validate, and canonicalise persisted state bytes."""
    if not isinstance(data, bytes) or not data:
        raise StateError("状态内容必须是非空 bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StateError("状态不是有效的 UTF-8") from exc
    try:
        document = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except StateError:
        raise
    except json.JSONDecodeError as exc:
        raise StateError("状态不是有效的 JSON") from exc
    return _loaded_state(_validate_document(document))

