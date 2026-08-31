from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

OUTPUT_FILENAME = "libraries-coder-index.json"

_VALID_CATEGORIES = frozenset(
    {
        "Display",
        "Communication",
        "Signal Input/Output",
        "Sensors",
        "Device Control",
        "Timing",
        "Data Storage",
        "Data Processing",
        "Other",
    }
)
_VERSION_PATTERN = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)"
    r"(?:\.(?P<minor>0|[1-9][0-9]*))?"
    r"(?:\.(?P<patch>0|[1-9][0-9]*))?"
    r"(?P<prerelease>-(?:[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?P<build>\+(?:[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_CHECKSUM_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_ARCHIVE_NAME_PATTERN = re.compile(r"^[^/\\]+\.zip$", re.IGNORECASE)
_REPOSITORY_FRAGMENT_PATTERN = re.compile(r"^[0-9A-Za-z._-]+$")


class IndexBuildError(ValueError):
    """Raised when repository metadata cannot produce a safe index."""


@dataclass(frozen=True, slots=True)
class Package:
    archive_file_name: str
    size: int
    sha256: str

    @property
    def relative_key(self) -> str:
        return self.archive_file_name


@dataclass(frozen=True, slots=True)
class Dependency:
    name: str
    version: str = ""


@dataclass(frozen=True, slots=True)
class LibraryMetadata:
    name: str
    version: str
    author: str
    maintainer: str
    sentence: str
    paragraph: str = ""
    website: str = ""
    category: str = "Uncategorized"
    architectures: tuple[str, ...] = ("*",)
    license: str = ""
    provides_includes: tuple[str, ...] = ()
    dependencies: tuple[Dependency, ...] = ()


@dataclass(frozen=True, slots=True)
class ReleaseCandidate:
    repository_key: str
    repository_url: str
    tag: str
    tag_ref_oid: str
    tag_commit_oid: str
    metadata: LibraryMetadata
    package: Package
    archive_path: Path


def read_repository_urls(path: Path) -> tuple[str, ...]:
    """Read and validate the non-empty URLs in a repository list."""
    urls: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise IndexBuildError(f"无法读取仓库列表: {path}") from exc

    for line in lines:
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        canonical_repository_url(value)
        urls.append(value)
    if not urls:
        raise IndexBuildError("仓库列表中没有有效 URL")
    return tuple(urls)


def canonical_repository_url(url: str) -> str:
    """Return a stable repository identity without changing its public URL."""
    value = url.strip()
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if (
        scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or (
            parsed.fragment
            and not _REPOSITORY_FRAGMENT_PATTERN.fullmatch(parsed.fragment)
        )
    ):
        raise IndexBuildError("无效的仓库 URL")

    host = parsed.hostname.casefold()
    try:
        port = parsed.port
    except ValueError as exc:
        raise IndexBuildError("仓库 URL 端口无效") from exc
    if port and not (
        (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"

    repository_path = parsed.path.rstrip("/")
    if repository_path.casefold().endswith(".git"):
        repository_path = repository_path[:-4].rstrip("/")
    if not repository_path or repository_path == "/":
        raise IndexBuildError("仓库 URL 缺少路径")
    if host == "github.com":
        repository_path = repository_path.casefold()
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""
    return f"{host}{repository_path}{fragment}"


def repository_list_digest(repository_urls: Iterable[str]) -> str:
    """Hash canonical repositories in scan order so the cursor stays valid."""
    keys = [canonical_repository_url(url) for url in repository_urls]
    if not keys:
        raise IndexBuildError("仓库列表中没有有效 URL")
    if len(keys) != len(set(keys)):
        raise IndexBuildError("仓库列表包含规范化后重复的 URL")
    payload = ("\n".join(keys) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def normalise_version(value: str) -> str:
    """Normalize common relaxed-semver forms to three numeric components."""
    version = value.strip()
    match = _VERSION_PATTERN.fullmatch(version)
    if not match:
        raise IndexBuildError(f"无效的库版本: {value!r}")

    prerelease = match.group("prerelease") or ""
    if prerelease:
        for identifier in prerelease[1:].split("."):
            if (
                identifier.isdigit()
                and len(identifier) > 1
                and identifier.startswith("0")
            ):
                raise IndexBuildError(f"预发布版本包含前导零: {value!r}")

    major = match.group("major")
    minor = match.group("minor") or "0"
    patch = match.group("patch") or "0"
    build = match.group("build") or ""
    return f"{major}.{minor}.{patch}{prerelease}{build}"


PrereleaseIdentifierKey = tuple[int, int, str]
VersionSortKey = tuple[
    int,
    int,
    int,
    tuple[int, tuple[PrereleaseIdentifierKey, ...]],
]


def version_sort_key(value: str) -> VersionSortKey:
    """Return a SemVer precedence key; build metadata does not affect precedence."""
    version = normalise_version(value)
    precedence = version.split("+", 1)[0]
    core, separator, prerelease = precedence.partition("-")
    major, minor, patch = (int(component) for component in core.split("."))
    if not separator:
        prerelease_key: tuple[int, tuple[PrereleaseIdentifierKey, ...]] = (1, ())
    else:
        identifiers: list[PrereleaseIdentifierKey] = []
        for identifier in prerelease.split("."):
            if identifier.isdigit():
                identifiers.append((0, int(identifier), ""))
            else:
                identifiers.append((1, 0, identifier))
        prerelease_key = (0, tuple(identifiers))
    return major, minor, patch, prerelease_key


def _split_csv(value: str) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for item in value.split(","):
        item = item.strip()
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return tuple(result)


def _split_dependencies(value: str) -> tuple[str, ...]:
    parts: list[str] = []
    start = 0
    depth = 0
    for index, character in enumerate(value):
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth < 0:
                raise IndexBuildError("depends 字段括号不匹配")
        elif character == "," and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
    if depth:
        raise IndexBuildError("depends 字段括号不匹配")
    parts.append(value[start:].strip())
    if any(not part for part in parts):
        raise IndexBuildError("depends 字段包含空依赖")
    return tuple(parts)


def _parse_dependency(value: str) -> Dependency:
    opening = value.find("(")
    if opening < 0:
        name = value.strip()
        version = ""
    else:
        if (
            opening == 0
            or not value[opening - 1].isspace()
            or not value.endswith(")")
        ):
            raise IndexBuildError(f"无效的依赖定义: {value!r}")
        name = value[:opening].strip()
        version = value[opening + 1 : -1].strip()
        if not version:
            raise IndexBuildError(f"依赖版本约束为空: {value!r}")

        depth = 0
        for index, character in enumerate(value[opening:], start=opening):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and index != len(value) - 1:
                    raise IndexBuildError(f"无效的依赖定义: {value!r}")
                if depth < 0:
                    raise IndexBuildError(f"无效的依赖定义: {value!r}")
        if depth:
            raise IndexBuildError(f"无效的依赖定义: {value!r}")

    if not name or any(character in name for character in "(),"):
        raise IndexBuildError(f"无效的依赖名称: {name!r}")
    return Dependency(name=name, version=version)


def parse_library_properties(data: bytes) -> LibraryMetadata:
    """Parse one root-level library.properties document."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise IndexBuildError("library.properties 必须使用 UTF-8 编码") from exc

    properties: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if "=" not in line:
            raise IndexBuildError(
                f"library.properties 第 {line_number} 行缺少 '='"
            )
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise IndexBuildError(
                f"library.properties 第 {line_number} 行缺少字段名"
            )
        properties[key] = value.strip()

    def required(key: str) -> str:
        value = properties.get(key, "").strip()
        if not value:
            raise IndexBuildError(f"library.properties 缺少必填字段 {key}")
        return value

    version = normalise_version(required("version"))

    if "architectures" not in properties:
        architectures = ("*",)
    else:
        raw_architectures = properties["architectures"]
        if not raw_architectures.strip():
            raise IndexBuildError("architectures 字段不能为空")
        architecture_parts = tuple(
            part.strip() for part in raw_architectures.split(",")
        )
        if any(not part for part in architecture_parts):
            raise IndexBuildError("architectures 字段包含空值")
        architectures = tuple(dict.fromkeys(architecture_parts))

    category = properties.get("category", "").strip()
    if category not in _VALID_CATEGORIES:
        category = "Uncategorized"

    depends = properties.get("depends", "").strip()
    dependencies = (
        tuple(
            _parse_dependency(item)
            for item in _split_dependencies(depends)
        )
        if depends
        else ()
    )

    return LibraryMetadata(
        name=required("name"),
        version=version,
        author=required("author"),
        maintainer=required("maintainer"),
        sentence=required("sentence"),
        paragraph=properties.get("paragraph", "").strip(),
        website=properties.get("url", "").strip(),
        category=category,
        architectures=architectures,
        license=properties.get("license", "").strip(),
        provides_includes=_split_csv(properties.get("includes", "")),
        dependencies=dependencies,
    )


def archive_stem(metadata: LibraryMetadata) -> str:
    """Return the Arduino-compatible root folder and archive stem."""
    if any(ord(character) < 32 or ord(character) == 127 for character in metadata.name):
        raise IndexBuildError("库名称包含控制字符")
    safe_name = re.sub(r"[^A-Za-z0-9]", "_", metadata.name)
    if not safe_name:
        raise IndexBuildError("库名称无法生成归档文件名")
    stem = f"{safe_name}-{normalise_version(metadata.version)}"
    if len(stem) > 200:
        raise IndexBuildError("库名称和版本生成的归档文件名过长")
    return stem


def _public_download_base_url(value: str) -> str:
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
            "公开下载基址必须是无凭据、查询参数和片段的 HTTP(S) URL"
        )
    return base


def build_release_entry(
    metadata: LibraryMetadata,
    repository_url: str,
    package: Package,
    public_download_base_url: str,
) -> dict[str, Any]:
    """Build one Arduino Library Manager compatible release entry."""
    expected_archive_name = f"{archive_stem(metadata)}.zip"
    if (
        not _ARCHIVE_NAME_PATTERN.fullmatch(package.archive_file_name)
        or package.archive_file_name != expected_archive_name
    ):
        raise IndexBuildError(
            f"归档文件名应为 {expected_archive_name!r}，"
            f"实际为 {package.archive_file_name!r}"
        )
    if (
        isinstance(package.size, bool)
        or not isinstance(package.size, int)
        or package.size <= 0
    ):
        raise IndexBuildError("归档大小必须是正整数")
    if not _CHECKSUM_PATTERN.fullmatch(package.sha256):
        raise IndexBuildError("归档 SHA-256 必须是 64 位十六进制字符串")

    canonical_repository_url(repository_url)
    public_base = _public_download_base_url(public_download_base_url)
    entry: dict[str, Any] = {
        "name": metadata.name,
        "version": normalise_version(metadata.version),
        "author": metadata.author,
        "maintainer": metadata.maintainer,
    }
    if metadata.license:
        entry["license"] = metadata.license
    entry["sentence"] = metadata.sentence
    if metadata.paragraph:
        entry["paragraph"] = metadata.paragraph
    if metadata.website:
        entry["website"] = metadata.website
    entry.update(
        {
            "category": metadata.category
            if metadata.category in _VALID_CATEGORIES
            else "Uncategorized",
            "architectures": list(metadata.architectures),
            "types": ["Arduino"],
            "repository": repository_url.strip(),
        }
    )
    if metadata.provides_includes:
        entry["providesIncludes"] = list(metadata.provides_includes)
    if metadata.dependencies:
        dependencies: list[dict[str, str]] = []
        for dependency in metadata.dependencies:
            item = {"name": dependency.name}
            if dependency.version:
                item["version"] = dependency.version
            dependencies.append(item)
        entry["dependencies"] = dependencies
    entry.update(
        {
            "url": (
                f"{public_base}/"
                f"{quote(package.archive_file_name, safe='-._~')}"
            ),
            "archiveFileName": package.archive_file_name,
            "size": package.size,
            "checksum": f"SHA-256:{package.sha256.lower()}",
        }
    )
    return entry


def build_index(
    release_records: Iterable[Mapping[str, Any]],
    active_repository_keys: Collection[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Build a deterministic index from persisted release state records."""
    active = (
        set(active_repository_keys)
        if active_repository_keys is not None
        else None
    )
    releases_by_identity: dict[tuple[str, str, str], dict[str, Any]] = {}
    repository_by_library: dict[str, str] = {}
    archive_owners: dict[str, tuple[str, str, str, int, str]] = {}

    for record in release_records:
        repository_key = record.get("repositoryKey")
        if not isinstance(repository_key, str) or not repository_key:
            raise IndexBuildError("release state 缺少 repositoryKey")
        if active is not None and repository_key not in active:
            continue

        raw_entry = record.get("entry")
        if raw_entry is None:
            continue
        if not isinstance(raw_entry, Mapping):
            raise IndexBuildError("release state 的 entry 必须是对象")
        entry = dict(raw_entry)

        name = entry.get("name")
        raw_version = entry.get("version")
        if not isinstance(name, str) or not name.strip():
            raise IndexBuildError("索引条目缺少 name")
        if not isinstance(raw_version, str):
            raise IndexBuildError(f"库 {name!r} 的索引条目缺少 version")
        name = name.strip()
        version = normalise_version(raw_version)
        entry["name"] = name
        entry["version"] = version
        entry["types"] = ["Arduino"]
        category = entry.get("category")
        entry["category"] = (
            category if category in _VALID_CATEGORIES else "Uncategorized"
        )

        library_key = name.casefold()
        owner = repository_by_library.setdefault(library_key, repository_key)
        if owner != repository_key:
            raise IndexBuildError(
                f"多个仓库使用了相同库名（忽略大小写）: {name}"
            )

        archive_name = entry.get("archiveFileName")
        size = entry.get("size")
        checksum = entry.get("checksum")
        if (
            not isinstance(archive_name, str)
            or not _ARCHIVE_NAME_PATTERN.fullmatch(archive_name)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
            or not isinstance(checksum, str)
            or not checksum.startswith("SHA-256:")
            or not _CHECKSUM_PATTERN.fullmatch(
                checksum.removeprefix("SHA-256:")
            )
        ):
            raise IndexBuildError(f"库 {name!r} {version} 的归档字段无效")

        identity = (repository_key, library_key, version)
        previous_entry = releases_by_identity.get(identity)
        if previous_entry is not None:
            if previous_entry != entry:
                raise IndexBuildError(
                    f"库 {name!r} {version} 存在冲突的 release state"
                )
            continue
        releases_by_identity[identity] = entry

        archive_owner = (
            repository_key,
            library_key,
            version,
            size,
            checksum.lower(),
        )
        previous_owner = archive_owners.setdefault(
            archive_name,
            archive_owner,
        )
        if previous_owner != archive_owner:
            raise IndexBuildError(f"归档文件名冲突: {archive_name}")

    releases_by_library: dict[str, list[dict[str, Any]]] = {}
    for entry in releases_by_identity.values():
        releases_by_library.setdefault(entry["name"].casefold(), []).append(entry)

    libraries: list[dict[str, Any]] = []
    for library_key in sorted(releases_by_library):
        releases = releases_by_library[library_key]
        latest = max(
            releases,
            key=lambda item: (
                version_sort_key(item["version"]),
                item["version"],
            ),
        )
        latest_category = latest["category"]
        for entry in sorted(
            releases,
            key=lambda item: (
                version_sort_key(item["version"]),
                item["version"],
                item["archiveFileName"],
            ),
            reverse=True,
        ):
            entry["category"] = latest_category
            libraries.append(entry)

    return {"libraries": libraries}
