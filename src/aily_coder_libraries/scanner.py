from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping
from urllib.parse import urlsplit

from .index import (
    IndexBuildError,
    LibraryMetadata,
    Package,
    ReleaseCandidate,
    archive_stem,
    parse_library_properties,
)

_OID_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SCCS_DIRECTORIES = frozenset({"CVS", "RCS", "SCCS"})
_LOCAL_TAG_REF = "refs/tags/aily-coder-library-scan"
_FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_MAX_PROPERTIES_BYTES = 4 * 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 200_000
_MAX_FETCHED_TAGS_PER_REPOSITORY = 1_000
_MAX_REPOSITORY_GIT_BYTES = 512 * 1024 * 1024
_MAX_REPOSITORY_PACKAGE_BYTES = 512 * 1024 * 1024
_TAR_OVERHEAD_ALLOWANCE = 256 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class RemoteTag:
    ref_oid: str
    commit_oid: str | None


@dataclass(frozen=True, slots=True)
class TagUpdate:
    ref_oid: str
    commit_oid: str | None
    archive_file_name: str | None


@dataclass(frozen=True, slots=True)
class ScanIssue:
    tag: str
    message: str


class TerminalTagError(Exception):
    """A content error that should only be retried after the tag changes."""


@dataclass(frozen=True, slots=True)
class ScannedRelease:
    repository_key: str
    repository_url: str
    tag: str
    tag_ref_oid: str
    tag_commit_oid: str
    metadata: LibraryMetadata
    _materializer: Callable[[], ReleaseCandidate] = field(
        repr=False,
        compare=False,
    )

    def materialize(self) -> ReleaseCandidate:
        return self._materializer()


@dataclass(frozen=True, slots=True)
class ScanResult:
    repository_key: str
    repository_url: str
    tag_updates: Mapping[str, TagUpdate]
    candidates: tuple[ScannedRelease, ...]
    issues: tuple[ScanIssue, ...]
    remote_tag_count: int
    _source_cleanup: Callable[[], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def release_sources(self) -> None:
        if self._source_cleanup is not None:
            self._source_cleanup()


_TerminalTagError = TerminalTagError


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "LC_ALL": "C",
        }
    )
    return environment


def _transport_repository_url(repository_url: str) -> str:
    parsed = urlsplit(repository_url)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or (
            parsed.fragment
            and not re.fullmatch(r"[0-9A-Za-z._-]+", parsed.fragment)
        )
    ):
        raise IndexBuildError("仓库 URL 必须是无凭据和查询参数的 HTTP(S) URL")
    return parsed._replace(fragment="").geturl()


def _run_git(
    arguments: list[str],
    *,
    timeout_seconds: int,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            ["git", *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            timeout=timeout_seconds,
            env=_git_environment(),
            check=False,
        )
    except FileNotFoundError as exc:
        raise IndexBuildError("找不到 git 可执行文件") from exc
    except subprocess.TimeoutExpired as exc:
        raise IndexBuildError("git 命令执行超时") from exc
    except OSError as exc:
        raise IndexBuildError("无法启动 git 命令") from exc

    if check and result.returncode != 0:
        raise IndexBuildError(f"git 命令失败，退出码 {result.returncode}")
    return result


def _normalize_oid(raw_oid: str) -> str:
    oid = raw_oid.strip().lower()
    if not _OID_PATTERN.fullmatch(oid):
        raise IndexBuildError("git 返回了无效的对象 OID")
    return oid


def _valid_tag_name(tag: str) -> bool:
    if not tag or tag.startswith("/") or tag.endswith("/"):
        return False
    if tag.startswith(".") or tag.endswith(".") or tag.endswith(".lock"):
        return False
    if ".." in tag or "@{" in tag or "//" in tag:
        return False
    return not any(
        character.isspace()
        or ord(character) < 0x20
        or ord(character) == 0x7F
        or character in "~^:?*[\\"
        for character in tag
    )


def discover_tags(
    repository_url: str,
    *,
    timeout_seconds: int = 120,
) -> dict[str, RemoteTag]:
    """Discover remote tag objects without cloning the repository."""
    transport_url = _transport_repository_url(repository_url)
    if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise IndexBuildError("timeout_seconds 必须大于 0")

    result = _run_git(
        ["ls-remote", "--tags", transport_url],
        timeout_seconds=timeout_seconds,
    )
    try:
        output = result.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise IndexBuildError("git tag 引用不是有效 UTF-8") from exc

    direct: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for raw_line in output.splitlines():
        if not raw_line:
            continue
        fields = raw_line.split(None, 1)
        if len(fields) != 2:
            raise IndexBuildError("git ls-remote 返回了无效数据")
        oid = _normalize_oid(fields[0])
        ref = fields[1]
        is_peeled = ref.endswith("^{}")
        if is_peeled:
            ref = ref[:-3]
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref.removeprefix("refs/tags/")
        if not _valid_tag_name(tag):
            raise IndexBuildError("远程仓库包含无效 tag 引用")

        destination = peeled if is_peeled else direct
        existing = destination.get(tag)
        if existing is not None and existing != oid:
            raise IndexBuildError("git ls-remote 返回了冲突的 tag OID")
        destination[tag] = oid

    dangling_peeled = peeled.keys() - direct.keys()
    if dangling_peeled:
        raise IndexBuildError("git ls-remote 返回了缺少 tag 对象的 peeled 引用")

    return {
        tag: RemoteTag(ref_oid=direct[tag], commit_oid=peeled.get(tag))
        for tag in sorted(direct)
    }


def _coerce_known_tag(value: TagUpdate | Mapping[str, object]) -> TagUpdate:
    if isinstance(value, TagUpdate):
        update = value
    elif isinstance(value, Mapping):
        ref_oid = value.get("refOid", value.get("ref_oid"))
        commit_oid = value.get("commitOid", value.get("commit_oid"))
        archive_file_name = value.get(
            "archiveFileName", value.get("archive_file_name")
        )
        if not isinstance(ref_oid, str):
            raise IndexBuildError("已知 tag 状态缺少 refOid")
        if commit_oid is not None and not isinstance(commit_oid, str):
            raise IndexBuildError("已知 tag 状态的 commitOid 无效")
        if archive_file_name is not None and not isinstance(archive_file_name, str):
            raise IndexBuildError("已知 tag 状态的 archiveFileName 无效")
        update = TagUpdate(
            ref_oid=ref_oid,
            commit_oid=commit_oid,
            archive_file_name=archive_file_name,
        )
    else:
        raise IndexBuildError("已知 tag 状态格式无效")

    ref_oid = _normalize_oid(update.ref_oid)
    commit_oid = (
        _normalize_oid(update.commit_oid) if update.commit_oid is not None else None
    )
    archive_file_name = update.archive_file_name
    if archive_file_name is not None:
        if (
            not archive_file_name
            or archive_file_name in {".", ".."}
            or "/" in archive_file_name
            or "\\" in archive_file_name
            or not archive_file_name.casefold().endswith(".zip")
        ):
            raise IndexBuildError("已知 tag 状态的 archiveFileName 无效")
        if commit_oid is None:
            raise IndexBuildError("已发布 tag 状态缺少 commitOid")
    return TagUpdate(ref_oid, commit_oid, archive_file_name)


def _initialize_bare_repository(path: Path, timeout_seconds: int) -> None:
    _run_git(
        ["init", "--bare", "--quiet", str(path)],
        timeout_seconds=timeout_seconds,
    )


def _directory_size_over_limit(path: Path, limit: int) -> bool:
    total = 0
    try:
        for root, _directories, files in os.walk(path):
            for file_name in files:
                file_path = os.path.join(root, file_name)
                try:
                    total += os.stat(file_path, follow_symlinks=False).st_size
                except FileNotFoundError:
                    continue
                if total > limit:
                    return True
    except OSError as exc:
        raise IndexBuildError("无法检查 Git 临时目录大小") from exc
    return False


def _run_bounded_git_fetch(
    arguments: list[str],
    bare_repository: Path,
    *,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[bytes]:
    command = ["git", *arguments]
    process: subprocess.Popen[bytes] | None = None
    try:
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                shell=False,
                env=_git_environment(),
            )
            deadline = time.monotonic() + timeout_seconds
            while process.poll() is None:
                if _directory_size_over_limit(
                    bare_repository, _MAX_REPOSITORY_GIT_BYTES
                ):
                    process.kill()
                    process.wait()
                    raise IndexBuildError("单仓库 Git 对象超过临时空间上限")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    process.wait()
                    raise IndexBuildError("git 命令执行超时")
                time.sleep(min(0.1, remaining))

            if _directory_size_over_limit(
                bare_repository, _MAX_REPOSITORY_GIT_BYTES
            ):
                raise IndexBuildError("单仓库 Git 对象超过临时空间上限")
            stdout.seek(0)
            stderr.seek(0)
            return subprocess.CompletedProcess(
                command,
                process.returncode,
                stdout.read(),
                stderr.read(),
            )
    except FileNotFoundError as exc:
        raise IndexBuildError("找不到 git 可执行文件") from exc
    except IndexBuildError:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        raise
    except OSError as exc:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        raise IndexBuildError("无法执行受限 git fetch") from exc


def _fetch_tag(
    bare_repository: Path,
    repository_url: str,
    tag: str,
    remote: RemoteTag,
    timeout_seconds: int,
) -> tuple[str, str]:
    remote_ref = f"refs/tags/{tag}"
    refspec = f"+{remote_ref}:{_LOCAL_TAG_REF}"
    fetch_arguments = [
        "--git-dir",
        str(bare_repository),
        "fetch",
        "--no-tags",
        "--depth=1",
        "--force",
        repository_url,
        refspec,
    ]
    fetch_result = _run_bounded_git_fetch(
        fetch_arguments,
        bare_repository,
        timeout_seconds=timeout_seconds,
    )
    if fetch_result.returncode != 0:
        stderr = fetch_result.stderr.lower()
        if b"shallow" not in stderr or b"support" not in stderr:
            raise IndexBuildError(
                f"git fetch tag 失败，退出码 {fetch_result.returncode}"
            )
        # A small number of legacy hosts only expose Git's dumb HTTP
        # transport. It cannot shallow-fetch, so retry the same advertised tag
        # without depth and keep the OID checks below.
        fallback_arguments = [
            argument for argument in fetch_arguments if argument != "--depth=1"
        ]
        fallback_result = _run_bounded_git_fetch(
            fallback_arguments,
            bare_repository,
            timeout_seconds=timeout_seconds,
        )
        if fallback_result.returncode != 0:
            raise IndexBuildError(
                f"git fetch tag 失败，退出码 {fallback_result.returncode}"
            )

    fetched_ref_result = _run_git(
        [
            "--git-dir",
            str(bare_repository),
            "rev-parse",
            "--verify",
            _LOCAL_TAG_REF,
        ],
        timeout_seconds=timeout_seconds,
    )
    try:
        fetched_ref_oid = _normalize_oid(fetched_ref_result.stdout.decode("ascii"))
    except UnicodeDecodeError as exc:
        raise IndexBuildError("fetch 后的 tag OID 无效") from exc
    if fetched_ref_oid != remote.ref_oid:
        raise IndexBuildError("tag 在发现与 fetch 之间发生变化，请重试")

    fetched_commit_result = _run_git(
        [
            "--git-dir",
            str(bare_repository),
            "rev-parse",
            "--verify",
            f"{_LOCAL_TAG_REF}^{{commit}}",
        ],
        timeout_seconds=timeout_seconds,
        check=False,
    )
    if fetched_commit_result.returncode != 0:
        raise _TerminalTagError("tag 不指向 commit 对象")
    try:
        fetched_commit_oid = _normalize_oid(
            fetched_commit_result.stdout.decode("ascii")
        )
    except (UnicodeDecodeError, IndexBuildError) as exc:
        raise _TerminalTagError("tag 的 commit OID 无效") from exc
    if remote.commit_oid is not None and fetched_commit_oid != remote.commit_oid:
        raise IndexBuildError("tag peeled OID 在发现与 fetch 之间发生变化，请重试")
    return fetched_ref_oid, fetched_commit_oid


def _show_library_properties(
    bare_repository: Path,
    commit_oid: str,
    *,
    timeout_seconds: int,
    max_source_bytes: int,
) -> bytes:
    size_limit = min(max_source_bytes, _MAX_PROPERTIES_BYTES)
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(
                [
                    "git",
                    "--git-dir",
                    str(bare_repository),
                    "show",
                    f"{commit_oid}:library.properties",
                ],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.PIPE,
                shell=False,
                timeout=timeout_seconds,
                env=_git_environment(),
                check=False,
            )
        except FileNotFoundError as exc:
            raise IndexBuildError("找不到 git 可执行文件") from exc
        except subprocess.TimeoutExpired as exc:
            raise IndexBuildError("读取 library.properties 超时") from exc
        except OSError as exc:
            raise IndexBuildError("无法读取 library.properties") from exc

        if result.returncode != 0:
            raise _TerminalTagError("tag 根目录缺少可读取的 library.properties")
        output.seek(0, os.SEEK_END)
        if output.tell() > size_limit:
            raise _TerminalTagError("library.properties 超过大小上限")
        output.seek(0)
        return output.read()


def _read_tree_modes(
    bare_repository: Path,
    commit_oid: str,
    *,
    timeout_seconds: int,
    max_source_bytes: int,
) -> dict[str, int]:
    result = _run_git(
        [
            "--git-dir",
            str(bare_repository),
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            commit_oid,
        ],
        timeout_seconds=timeout_seconds,
        check=False,
    )
    if result.returncode != 0:
        raise _TerminalTagError("无法读取 tag 的 Git tree")
    if len(result.stdout) > max(max_source_bytes, 16 * 1024 * 1024):
        raise _TerminalTagError("Git tree 条目数据超过大小上限")

    modes: dict[str, int] = {}
    records = result.stdout.split(b"\0")
    if records[-1] != b"":
        raise _TerminalTagError("Git tree 输出不完整")
    for record in records[:-1]:
        try:
            metadata, raw_path = record.split(b"\t", 1)
            raw_mode, object_type, _oid = metadata.split(b" ", 2)
            path = raw_path.decode("utf-8", errors="strict")
            mode = int(raw_mode, 8)
        except (ValueError, UnicodeDecodeError) as exc:
            raise _TerminalTagError("Git tree 包含无效条目") from exc

        if object_type == b"commit" or mode == 0o160000:
            raise _TerminalTagError("源码包含 submodule")
        if mode == 0o120000:
            raise _TerminalTagError("源码包含 symlink")
        if object_type != b"blob" or mode not in {0o100644, 0o100755}:
            raise _TerminalTagError("源码包含不支持的 Git tree 条目")
        _safe_archive_path(path)
        if path in modes:
            raise _TerminalTagError("Git tree 包含重复路径")
        modes[path] = mode
        if len(modes) > _MAX_ARCHIVE_ENTRIES:
            raise _TerminalTagError("源码归档条目数超过上限")
    if ".development" in modes:
        raise _TerminalTagError("源码包含 .development 标记")
    return modes


def _write_git_archive(
    bare_repository: Path,
    commit_oid: str,
    tar_path: Path,
    *,
    timeout_seconds: int,
    max_source_bytes: int,
) -> None:
    tar_limit = max_source_bytes + _TAR_OVERHEAD_ALLOWANCE
    process: subprocess.Popen[bytes] | None = None
    copy_error: list[Exception] = []
    limit_exceeded = threading.Event()

    try:
        with tar_path.open("xb") as output:
            process = subprocess.Popen(
                [
                    "git",
                    "--git-dir",
                    str(bare_repository),
                    "archive",
                    "--format=tar",
                    commit_oid,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=False,
                env=_git_environment(),
            )

            if process.stdout is None:
                raise IndexBuildError("无法读取 git archive 输出")

            def copy_archive() -> None:
                written = 0
                try:
                    while True:
                        chunk = process.stdout.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > tar_limit:
                            limit_exceeded.set()
                            process.kill()
                            break
                        output.write(chunk)
                except Exception as exc:
                    copy_error.append(exc)
                    process.kill()
                finally:
                    process.stdout.close()

            copy_thread = threading.Thread(target=copy_archive, daemon=True)
            copy_thread.start()
            try:
                return_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait()
                copy_thread.join()
                raise IndexBuildError("生成源码 tar 超时") from exc
            copy_thread.join()
    except FileExistsError as exc:
        raise IndexBuildError("临时 tar 文件已存在") from exc
    except FileNotFoundError as exc:
        raise IndexBuildError("找不到 git 可执行文件") from exc
    except OSError as exc:
        raise IndexBuildError("无法生成源码 tar") from exc

    if limit_exceeded.is_set():
        raise _TerminalTagError("源码 tar 超过大小上限")
    if copy_error:
        error = copy_error[0]
        raise IndexBuildError("无法写入源码 tar") from error
    if return_code != 0:
        raise _TerminalTagError("git archive 无法归档 tag")


def _safe_archive_path(name: str) -> tuple[str, ...]:
    if not name or name.startswith("/") or "\\" in name or "\x00" in name:
        raise _TerminalTagError("归档包含不安全路径")
    trimmed = name[:-1] if name.endswith("/") else name
    raw_parts = trimmed.split("/")
    if not trimmed or any(part in {"", ".", ".."} for part in raw_parts):
        raise _TerminalTagError("归档包含不安全路径")
    path = PurePosixPath(trimmed)
    if path.is_absolute() or tuple(path.parts) != tuple(raw_parts):
        raise _TerminalTagError("归档包含不安全路径")
    return tuple(raw_parts)


def _excluded_archive_path(parts: tuple[str, ...], *, is_directory: bool) -> bool:
    if any(part.startswith(".") for part in parts):
        return True
    directory_parts = parts if is_directory else parts[:-1]
    return any(part in _SCCS_DIRECTORIES for part in directory_parts)


def _zip_info(name: str, *, is_directory: bool, executable: bool = False) -> zipfile.ZipInfo:
    if is_directory and not name.endswith("/"):
        name = f"{name}/"
    info = zipfile.ZipInfo(filename=name, date_time=_FIXED_ZIP_TIMESTAMP)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_STORED if is_directory else zipfile.ZIP_DEFLATED
    permissions = 0o755 if is_directory or executable else 0o644
    file_type = stat.S_IFDIR if is_directory else stat.S_IFREG
    info.external_attr = ((file_type | permissions) << 16) | (0x10 if is_directory else 0)
    info.extra = b""
    info.comment = b""
    return info


def _create_deterministic_zip(
    tar_path: Path,
    archive_path: Path,
    root_name: str,
    tree_modes: Mapping[str, int],
    *,
    max_source_bytes: int,
) -> Package:
    if (
        not root_name
        or root_name in {".", ".."}
        or "/" in root_name
        or "\\" in root_name
    ):
        raise _TerminalTagError("archive_stem 生成了无效根目录名")

    try:
        with tarfile.open(tar_path, mode="r:") as source_tar:
            members = source_tar.getmembers()
            if len(members) > _MAX_ARCHIVE_ENTRIES:
                raise _TerminalTagError("源码归档条目数超过上限")

            source_size = 0
            seen_paths: set[str] = set()
            included: list[tuple[tarfile.TarInfo, tuple[str, ...]]] = []
            for member in members:
                parts = _safe_archive_path(member.name)
                normalized_name = "/".join(parts)
                if normalized_name in seen_paths:
                    raise _TerminalTagError("源码归档包含重复路径")
                seen_paths.add(normalized_name)

                if member.issym():
                    raise _TerminalTagError("源码归档包含 symlink")
                if member.islnk():
                    raise _TerminalTagError("源码归档包含 hardlink")
                if not member.isdir() and not member.isreg():
                    raise _TerminalTagError("源码归档包含特殊条目")

                if member.isreg():
                    source_size += member.size
                    if source_size > max_source_bytes:
                        raise _TerminalTagError("源码解压大小超过上限")
                    expected_mode = tree_modes.get(normalized_name)
                    if expected_mode is None:
                        raise _TerminalTagError("源码 tar 与 Git tree 不一致")
                if not _excluded_archive_path(parts, is_directory=member.isdir()):
                    included.append((member, parts))

            if not any(
                member.isreg() and parts == ("library.properties",)
                for member, parts in included
            ):
                raise _TerminalTagError("ZIP 中缺少 library.properties")
            included.sort(key=lambda item: "/".join(item[1]).encode("utf-8"))
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with zipfile.ZipFile(
                    archive_path,
                    mode="x",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                    allowZip64=True,
                    strict_timestamps=True,
                ) as output_zip:
                    output_zip.writestr(
                        _zip_info(root_name, is_directory=True),
                        b"",
                    )
                    for member, parts in included:
                        relative_name = "/".join(parts)
                        zip_name = f"{root_name}/{relative_name}"
                        if member.isdir():
                            output_zip.writestr(
                                _zip_info(zip_name, is_directory=True),
                                b"",
                            )
                            continue

                        source = source_tar.extractfile(member)
                        if source is None:
                            raise _TerminalTagError("无法读取源码归档条目")
                        executable = tree_modes[relative_name] == 0o100755
                        written = 0
                        with source, output_zip.open(
                            _zip_info(
                                zip_name,
                                is_directory=False,
                                executable=executable,
                            ),
                            mode="w",
                            force_zip64=member.size >= zipfile.ZIP64_LIMIT,
                        ) as destination:
                            while True:
                                chunk = source.read(1024 * 1024)
                                if not chunk:
                                    break
                                destination.write(chunk)
                                written += len(chunk)
                        if written != member.size:
                            raise _TerminalTagError("源码归档条目大小不一致")
            except BaseException:
                archive_path.unlink(missing_ok=True)
                raise
    except (tarfile.TarError, UnicodeError) as exc:
        raise _TerminalTagError("git archive 生成了无效 tar") from exc
    except OSError as exc:
        raise IndexBuildError("无法创建 ZIP 包") from exc

    digest = hashlib.sha256()
    try:
        with archive_path.open("rb") as archive:
            for chunk in iter(lambda: archive.read(1024 * 1024), b""):
                digest.update(chunk)
        archive_size = archive_path.stat().st_size
    except OSError as exc:
        raise IndexBuildError("无法校验 ZIP 包") from exc
    if archive_size <= 0:
        raise IndexBuildError("生成的 ZIP 包为空")
    return Package(
        archive_file_name=archive_path.name,
        size=archive_size,
        sha256=digest.hexdigest(),
    )


def _metadata_for_tag(
    bare_repository: Path,
    tag_commit_oid: str,
    *,
    timeout_seconds: int,
    max_source_bytes: int,
) -> LibraryMetadata:
    properties_data = _show_library_properties(
        bare_repository,
        tag_commit_oid,
        timeout_seconds=timeout_seconds,
        max_source_bytes=max_source_bytes,
    )
    try:
        metadata = parse_library_properties(properties_data)
        archive_stem(metadata)
        return metadata
    except (IndexBuildError, ValueError, UnicodeError) as exc:
        raise _TerminalTagError(f"library.properties 无效: {exc}") from exc


def _candidate_for_tag(
    *,
    repository_key: str,
    repository_url: str,
    tag: str,
    tag_ref_oid: str,
    tag_commit_oid: str,
    bare_repository: Path,
    package_directory: Path,
    scratch_directory: Path,
    metadata: LibraryMetadata,
    timeout_seconds: int,
    max_source_bytes: int,
) -> ReleaseCandidate:
    stem = archive_stem(metadata)
    archive_file_name = f"{stem}.zip"
    tag_directory = package_directory / hashlib.sha256(
        tag.encode("utf-8")
    ).hexdigest()
    archive_path = tag_directory / archive_file_name
    tar_path = scratch_directory / f"{hashlib.sha256(tag_ref_oid.encode('ascii')).hexdigest()}.tar"
    tree_modes = _read_tree_modes(
        bare_repository,
        tag_commit_oid,
        timeout_seconds=timeout_seconds,
        max_source_bytes=max_source_bytes,
    )
    try:
        _write_git_archive(
            bare_repository,
            tag_commit_oid,
            tar_path,
            timeout_seconds=timeout_seconds,
            max_source_bytes=max_source_bytes,
        )
        package = _create_deterministic_zip(
            tar_path,
            archive_path,
            stem,
            tree_modes,
            max_source_bytes=max_source_bytes,
        )
    finally:
        tar_path.unlink(missing_ok=True)

    return ReleaseCandidate(
        repository_key=repository_key,
        repository_url=repository_url,
        tag=tag,
        tag_ref_oid=tag_ref_oid,
        tag_commit_oid=tag_commit_oid,
        metadata=metadata,
        package=package,
        archive_path=archive_path,
    )


def _materialize_with_budget(
    *,
    package_bytes: list[int],
    repository_key: str,
    repository_url: str,
    tag: str,
    tag_ref_oid: str,
    tag_commit_oid: str,
    bare_repository: Path,
    package_directory: Path,
    scratch_directory: Path,
    metadata: LibraryMetadata,
    timeout_seconds: int,
    max_source_bytes: int,
) -> ReleaseCandidate:
    candidate = _candidate_for_tag(
        repository_key=repository_key,
        repository_url=repository_url,
        tag=tag,
        tag_ref_oid=tag_ref_oid,
        tag_commit_oid=tag_commit_oid,
        bare_repository=bare_repository,
        package_directory=package_directory,
        scratch_directory=scratch_directory,
        metadata=metadata,
        timeout_seconds=timeout_seconds,
        max_source_bytes=max_source_bytes,
    )
    package_bytes[0] += candidate.package.size
    if package_bytes[0] > _MAX_REPOSITORY_PACKAGE_BYTES:
        raise IndexBuildError("单仓库一次扫描生成的包总量超过上限")
    return candidate


def _remove_scan_sources(bare_repository: Path, scratch_directory: Path) -> None:
    shutil.rmtree(bare_repository, ignore_errors=True)
    shutil.rmtree(scratch_directory, ignore_errors=True)


def scan_repository(
    repository_key: str,
    repository_url: str,
    known_tags: Mapping[str, TagUpdate | Mapping[str, object]],
    temp_root: Path,
    *,
    timeout_seconds: int = 120,
    max_source_bytes: int = 1024 * 1024 * 1024,
) -> ScanResult:
    """Scan changed tags and build packages without depending on an upstream index."""
    if not repository_key:
        raise IndexBuildError("repository_key 不能为空")
    transport_url = _transport_repository_url(repository_url)
    if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise IndexBuildError("timeout_seconds 必须大于 0")
    if (
        isinstance(max_source_bytes, bool)
        or not isinstance(max_source_bytes, int)
        or max_source_bytes <= 0
    ):
        raise IndexBuildError("max_source_bytes 必须大于 0")

    normalized_known = {
        tag: _coerce_known_tag(value) for tag, value in known_tags.items()
    }
    remote_tags = discover_tags(
        repository_url,
        timeout_seconds=timeout_seconds,
    )
    temp_root.mkdir(parents=True, exist_ok=True)
    work_directory = Path(tempfile.mkdtemp(prefix="aily-library-scan-", dir=temp_root))
    bare_repository = work_directory / "repository.git"
    package_directory = work_directory / "packages"
    scratch_directory = work_directory / "scratch"
    scratch_directory.mkdir()

    updates: dict[str, TagUpdate] = {}
    candidates: list[ScannedRelease] = []
    issues: list[ScanIssue] = []
    fetched_tag_count = 0
    package_bytes = [0]
    keep_work_directory = False
    try:
        _initialize_bare_repository(bare_repository, timeout_seconds)
        for tag, remote in remote_tags.items():
            known = normalized_known.get(tag)
            if known is not None and known.ref_oid == remote.ref_oid:
                continue

            if known is not None and known.archive_file_name is not None:
                if (
                    remote.commit_oid is not None
                    and known.commit_oid == remote.commit_oid
                ):
                    updates[tag] = TagUpdate(
                        ref_oid=remote.ref_oid,
                        commit_oid=remote.commit_oid,
                        archive_file_name=known.archive_file_name,
                    )
                    continue
                if (
                    remote.commit_oid is not None
                    and known.commit_oid != remote.commit_oid
                ):
                    issues.append(
                        ScanIssue(
                            tag=tag,
                            message="已发布 tag 的 commit 发生改写，保留原版本",
                        )
                    )
                    continue
                try:
                    fetched_tag_count += 1
                    if fetched_tag_count > _MAX_FETCHED_TAGS_PER_REPOSITORY:
                        raise IndexBuildError("单仓库一次扫描需拉取的 tag 数超过上限")
                    _ref_oid, fetched_commit_oid = _fetch_tag(
                        bare_repository,
                        transport_url,
                        tag,
                        remote,
                        timeout_seconds,
                    )
                except _TerminalTagError as exc:
                    issues.append(
                        ScanIssue(
                            tag=tag,
                            message=f"已发布 tag 已不再有效，保留原版本: {exc}",
                        )
                    )
                    continue
                if fetched_commit_oid != known.commit_oid:
                    issues.append(
                        ScanIssue(
                            tag=tag,
                            message="已发布 tag 的 commit 发生改写，保留原版本",
                        )
                    )
                    continue
                updates[tag] = TagUpdate(
                    ref_oid=remote.ref_oid,
                    commit_oid=fetched_commit_oid,
                    archive_file_name=known.archive_file_name,
                )
                continue

            try:
                fetched_tag_count += 1
                if fetched_tag_count > _MAX_FETCHED_TAGS_PER_REPOSITORY:
                    raise IndexBuildError("单仓库一次扫描需拉取的 tag 数超过上限")
                fetched_ref_oid, fetched_commit_oid = _fetch_tag(
                    bare_repository,
                    transport_url,
                    tag,
                    remote,
                    timeout_seconds,
                )
            except _TerminalTagError as exc:
                updates[tag] = TagUpdate(
                    ref_oid=remote.ref_oid,
                    commit_oid=None,
                    archive_file_name=None,
                )
                issues.append(ScanIssue(tag=tag, message=str(exc)))
                continue

            try:
                metadata = _metadata_for_tag(
                    bare_repository,
                    fetched_commit_oid,
                    timeout_seconds=timeout_seconds,
                    max_source_bytes=max_source_bytes,
                )
            except _TerminalTagError as exc:
                updates[tag] = TagUpdate(
                    ref_oid=fetched_ref_oid,
                    commit_oid=fetched_commit_oid,
                    archive_file_name=None,
                )
                issues.append(ScanIssue(tag=tag, message=str(exc)))
                continue

            updates[tag] = TagUpdate(
                ref_oid=fetched_ref_oid,
                commit_oid=fetched_commit_oid,
                archive_file_name=None,
            )
            candidates.append(
                ScannedRelease(
                    repository_key=repository_key,
                    repository_url=repository_url,
                    tag=tag,
                    tag_ref_oid=fetched_ref_oid,
                    tag_commit_oid=fetched_commit_oid,
                    metadata=metadata,
                    _materializer=partial(
                        _materialize_with_budget,
                        package_bytes=package_bytes,
                        repository_key=repository_key,
                        repository_url=repository_url,
                        tag=tag,
                        tag_ref_oid=fetched_ref_oid,
                        tag_commit_oid=fetched_commit_oid,
                        bare_repository=bare_repository,
                        package_directory=package_directory,
                        scratch_directory=scratch_directory,
                        metadata=metadata,
                        timeout_seconds=timeout_seconds,
                        max_source_bytes=max_source_bytes,
                    ),
                )
            )

        keep_work_directory = bool(candidates)
        return ScanResult(
            repository_key=repository_key,
            repository_url=repository_url,
            tag_updates=updates,
            candidates=tuple(candidates),
            issues=tuple(issues),
            remote_tag_count=len(remote_tags),
            _source_cleanup=partial(
                _remove_scan_sources,
                bare_repository,
                scratch_directory,
            ),
        )
    finally:
        if not keep_work_directory:
            shutil.rmtree(work_directory, ignore_errors=True)
