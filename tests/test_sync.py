from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

from aily_coder_libraries import sync as sync_module
from aily_coder_libraries.index import (
    OUTPUT_FILENAME,
    IndexBuildError,
    LibraryMetadata,
    Package,
    ReleaseCandidate,
    archive_stem,
)
from aily_coder_libraries.scanner import (
    ScanResult,
    ScannedRelease,
    TagUpdate,
    TerminalTagError,
)
from aily_coder_libraries.state import (
    STATE_FILENAME,
    parse_state,
)
from aily_coder_libraries.sync import (
    MAX_ARCHIVE_SOURCE_BYTES,
    MAX_SCAN_WORKERS,
    R2_TARGET_NAME,
    RUSTFS_TARGET_NAME,
    SyncError,
    synchronise as _synchronise,
)


RUSTFS_PUBLIC_BASE_URL = "https://rustfs-packages.example.com"
R2_PUBLIC_BASE_URL = "https://r2-packages.example.com"
PUBLIC_BASE_URL = R2_PUBLIC_BASE_URL
PUBLIC_BASE_URLS = {
    RUSTFS_TARGET_NAME: RUSTFS_PUBLIC_BASE_URL,
    R2_TARGET_NAME: R2_PUBLIC_BASE_URL,
}


def index_output_paths(output_path: Path) -> dict[str, Path]:
    return {
        RUSTFS_TARGET_NAME: output_path,
        R2_TARGET_NAME: output_path.parent / "r2" / output_path.name,
    }


def synchronise(
    repository_urls: tuple[str, ...],
    targets: tuple[Any, ...],
    output_path: Path,
    public_download_base_url: str,
    **kwargs: Any,
):
    if public_download_base_url != R2_PUBLIC_BASE_URL:
        raise AssertionError("test state base must use the R2 public URL")
    return _synchronise(
        repository_urls,
        targets,
        index_output_paths(output_path),
        PUBLIC_BASE_URLS,
        **kwargs,
    )


@dataclass(frozen=True, slots=True)
class ReleaseSpec:
    name: str
    version: str
    tag: str
    payload: bytes
    ref_character: str
    commit_character: str


Event = tuple[str, str, str, str]


class FakeTarget:
    def __init__(
        self,
        name: str,
        events: list[Event],
        *,
        fail_package_once: bool = False,
        fail_index_once: bool = False,
        stores_state: bool = False,
    ) -> None:
        self.name = name
        self.index_key = OUTPUT_FILENAME
        self.state_key = f".state/{STATE_FILENAME}"
        self.stores_state = stores_state
        self.events = events
        self.fail_package_once = fail_package_once
        self.fail_index_once = fail_index_once
        self.packages: dict[str, bytes] = {}
        self.documents: dict[str, bytes] = {}

    def package_key(self, package: Package) -> str:
        return f"libraries/{package.archive_file_name}"

    def upload_package(self, package: Package, path: Path) -> bool:
        key = self.package_key(package)
        data = path.read_bytes()
        if len(data) != package.size:
            raise AssertionError("candidate package size mismatch")
        if hashlib.sha256(data).hexdigest() != package.sha256:
            raise AssertionError("candidate package digest mismatch")

        existing = self.packages.get(key)
        if existing is not None:
            if hashlib.sha256(existing).hexdigest() != package.sha256:
                raise RuntimeError(f"{self.name} flat package conflict")
            return False
        if self.fail_package_once:
            self.fail_package_once = False
            raise RuntimeError(f"{self.name} package upload failure")

        self.packages[key] = data
        self.events.append((self.name, "package", key, package.sha256))
        return True

    def document_matches(self, key: str, size: int, sha256: str) -> bool:
        if key == self.state_key and not self.stores_state:
            raise AssertionError(f"{self.name} must not access state")
        data = self.documents.get(key)
        return (
            data is not None
            and len(data) == size
            and hashlib.sha256(data).hexdigest() == sha256
        )

    def read_document_bytes(self, key: str, *, max_bytes: int) -> bytes | None:
        if key == self.state_key and not self.stores_state:
            raise AssertionError(f"{self.name} must not access state")
        data = self.documents.get(key)
        if data is not None and len(data) > max_bytes:
            raise RuntimeError("fake document exceeds max_bytes")
        return data

    def upload_document_bytes(
        self,
        key: str,
        data: bytes,
        *,
        sha256: str,
        content_type: str,
        cache_control: str,
    ) -> None:
        del content_type, cache_control
        if key == self.state_key and not self.stores_state:
            raise AssertionError(f"{self.name} must not access state")
        if hashlib.sha256(data).hexdigest() != sha256:
            raise AssertionError("document digest mismatch")
        if key == self.index_key and self.fail_index_once:
            self.fail_index_once = False
            raise RuntimeError(f"{self.name} index upload failure")
        self.documents[key] = data
        self.events.append((self.name, "document", key, sha256))


def make_targets(
    events: list[Event],
    *,
    second_fails_package_once: bool = False,
    second_fails_index_once: bool = False,
) -> tuple[FakeTarget, FakeTarget]:
    return (
        FakeTarget("rustfs", events),
        FakeTarget(
            "cloudflare-r2",
            events,
            fail_package_once=second_fails_package_once,
            fail_index_once=second_fails_index_once,
            stores_state=True,
        ),
    )


def make_scan_function(
    plans: Mapping[str, tuple[ReleaseSpec, ...] | Exception],
    calls: list[tuple[str, dict[str, Any]]] | None = None,
    materialized_tags: list[str] | None = None,
    terminal_tags: set[str] | None = None,
    materialization_errors: Mapping[str, Exception] | None = None,
):
    def scan(
        repository_key: str,
        repository_url: str,
        known_tags: Mapping[str, object],
        temporary_root: Path,
        *,
        timeout_seconds: int,
        max_source_bytes: int,
    ) -> ScanResult:
        if calls is not None:
            calls.append((repository_key, dict(known_tags)))
        if timeout_seconds <= 0 or max_source_bytes <= 0:
            raise AssertionError("invalid scanner limits")

        plan = plans[repository_url]
        if isinstance(plan, Exception):
            raise plan

        candidates: list[ScannedRelease] = []
        updates: dict[str, TagUpdate] = {}
        for spec in plan:
            metadata = LibraryMetadata(
                name=spec.name,
                version=spec.version,
                author="Aily",
                maintainer="Aily <info@example.com>",
                sentence=f"{spec.name} test library.",
                category="Other",
                architectures=("*",),
            )
            path_name = hashlib.sha256(
                f"{repository_key}\0{spec.tag}".encode("utf-8")
            ).hexdigest()
            archive_path = temporary_root / f"{path_name}.zip"
            ref_oid = spec.ref_character * 40
            commit_oid = spec.commit_character * 40
            known = known_tags.get(spec.tag)
            if isinstance(known, Mapping) and known.get("refOid") == ref_oid:
                continue

            def materialize(
                *,
                current_spec: ReleaseSpec = spec,
                current_metadata: LibraryMetadata = metadata,
                current_archive_path: Path = archive_path,
                current_ref_oid: str = ref_oid,
                current_commit_oid: str = commit_oid,
            ) -> ReleaseCandidate:
                if materialized_tags is not None:
                    materialized_tags.append(current_spec.tag)
                if terminal_tags is not None and current_spec.tag in terminal_tags:
                    raise TerminalTagError("test package content failure")
                if (
                    materialization_errors is not None
                    and current_spec.tag in materialization_errors
                ):
                    raise materialization_errors[current_spec.tag]
                current_archive_path.write_bytes(current_spec.payload)
                package = Package(
                    archive_file_name=f"{archive_stem(current_metadata)}.zip",
                    size=len(current_spec.payload),
                    sha256=hashlib.sha256(current_spec.payload).hexdigest(),
                )
                return ReleaseCandidate(
                    repository_key=repository_key,
                    repository_url=repository_url,
                    tag=current_spec.tag,
                    tag_ref_oid=current_ref_oid,
                    tag_commit_oid=current_commit_oid,
                    metadata=current_metadata,
                    package=package,
                    archive_path=current_archive_path,
                )

            candidates.append(
                ScannedRelease(
                    repository_key=repository_key,
                    repository_url=repository_url,
                    tag=spec.tag,
                    tag_ref_oid=ref_oid,
                    tag_commit_oid=commit_oid,
                    metadata=metadata,
                    _materializer=materialize,
                )
            )
            updates[spec.tag] = TagUpdate(
                ref_oid=ref_oid,
                commit_oid=commit_oid,
                archive_file_name=None,
            )

        return ScanResult(
            repository_key=repository_key,
            repository_url=repository_url,
            tag_updates=updates,
            candidates=tuple(candidates),
            issues=(),
            remote_tag_count=len(plan),
        )

    return scan


def release_spec(
    name: str,
    version: str,
    tag: str,
    payload: bytes,
    *,
    ref: str = "1",
    commit: str = "a",
) -> ReleaseSpec:
    return ReleaseSpec(name, version, tag, payload, ref, commit)


class CommandLineTests(unittest.TestCase):
    def test_bootstrap_completion_exit_statuses(self) -> None:
        base_arguments = [
            "--dry-run",
            "--rustfs-public-download-base-url",
            RUSTFS_PUBLIC_BASE_URL,
            "--r2-public-download-base-url",
            R2_PUBLIC_BASE_URL,
            "--max-repositories",
            "2",
        ]
        for require_complete, bootstrap_complete, expected_status in (
            (False, False, 0),
            (True, False, sync_module.BOOTSTRAP_INCOMPLETE_EXIT_CODE),
            (True, True, 0),
        ):
            arguments = list(base_arguments)
            if require_complete:
                arguments.append("--require-bootstrap-complete")
            summary = sync_module.SyncSummary(
                scanned_repository_count=2,
                failed_repository_count=0,
                discovered_tag_count=0,
                added_release_count=0,
                release_count=0,
                uploaded_package_object_count=0,
                uploaded_document_object_count=0,
                next_cursor=0 if bootstrap_complete else 2,
                bootstrap_complete=bootstrap_complete,
                index_published=False,
            )
            with self.subTest(
                require_complete=require_complete,
                bootstrap_complete=bootstrap_complete,
            ), mock.patch.object(
                sync_module,
                "read_repository_urls",
                return_value=("https://github.com/aily/example",),
            ), mock.patch.object(
                sync_module, "synchronise", return_value=summary
            ):
                self.assertEqual(sync_module.main(arguments), expected_status)

    def test_require_bootstrap_complete_rejects_unlimited_batch(self) -> None:
        with mock.patch.object(sync_module, "read_repository_urls") as read_urls:
            status = sync_module.main(
                [
                    "--dry-run",
                    "--rustfs-public-download-base-url",
                    RUSTFS_PUBLIC_BASE_URL,
                    "--r2-public-download-base-url",
                    R2_PUBLIC_BASE_URL,
                    "--max-repositories",
                    "0",
                    "--require-bootstrap-complete",
                ]
            )

        self.assertEqual(status, 1)
        read_urls.assert_not_called()


class BootstrapTests(unittest.TestCase):
    def test_unlimited_batch_completes_bootstrap_and_publishes_both_indexes(
        self,
    ) -> None:
        repository_urls = tuple(
            f"https://github.com/aily/full-{index}" for index in range(3)
        )
        events: list[Event] = []
        targets = make_targets(events)
        scan = make_scan_function(
            {
                repository_url: (
                    release_spec(
                        f"Full{index}",
                        "1.0.0",
                        "v1",
                        f"full-{index}".encode("ascii"),
                    ),
                )
                for index, repository_url in enumerate(repository_urls)
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            summary = synchronise(
                repository_urls,
                targets,
                Path(directory) / OUTPUT_FILENAME,
                PUBLIC_BASE_URL,
                workers=2,
                max_repositories=0,
                scan_function=scan,
            )

        self.assertEqual(summary.scanned_repository_count, 3)
        self.assertEqual(summary.next_cursor, 0)
        self.assertTrue(summary.bootstrap_complete)
        self.assertTrue(summary.index_published)
        for target in targets:
            index_document = json.loads(target.documents[target.index_key])
            self.assertCountEqual(
                [entry["name"] for entry in index_document["libraries"]],
                ["Full0", "Full1", "Full2"],
            )

    def test_two_repository_bootstrap_resumes_and_publishes_packages_first(
        self,
    ) -> None:
        first_url = "https://github.com/aily/first"
        second_url = "https://github.com/aily/second"
        repository_urls = (first_url, second_url)
        events: list[Event] = []
        targets = make_targets(events)
        scan = make_scan_function(
            {
                first_url: (
                    release_spec("First", "1.0.0", "v1.0.0", b"first"),
                ),
                second_url: (
                    release_spec(
                        "Second",
                        "2.0.0",
                        "v2.0.0",
                        b"second",
                        ref="2",
                        commit="b",
                    ),
                ),
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / OUTPUT_FILENAME
            first_summary = synchronise(
                repository_urls,
                targets,
                output,
                PUBLIC_BASE_URL,
                workers=1,
                max_repositories=1,
                scan_function=scan,
            )

            self.assertFalse(first_summary.bootstrap_complete)
            self.assertFalse(first_summary.index_published)
            self.assertEqual(first_summary.next_cursor, 1)
            self.assertNotIn(targets[0].state_key, targets[0].documents)
            self.assertIn(targets[1].state_key, targets[1].documents)
            self.assertNotIn(targets[0].index_key, targets[0].documents)
            self.assertNotIn(targets[1].index_key, targets[1].documents)

            first_state = parse_state(
                targets[1].documents[targets[1].state_key]
            )
            self.assertEqual(first_state.document["cursor"], 1)
            self.assertEqual(len(first_state.document["releases"]), 1)

            first_final_state_position = min(
                index
                for index, event in enumerate(events)
                if event[1] == "document"
                and event[0] == "cloudflare-r2"
                and event[2] == targets[1].state_key
            )
            self.assertLess(
                max(
                    index
                    for index, event in enumerate(events)
                    if event[1] == "package"
                ),
                first_final_state_position,
            )

            second_run_start = len(events)
            second_summary = synchronise(
                repository_urls,
                targets,
                output,
                PUBLIC_BASE_URL,
                workers=1,
                max_repositories=1,
                scan_function=scan,
            )

            self.assertTrue(second_summary.bootstrap_complete)
            self.assertTrue(second_summary.index_published)
            self.assertEqual(second_summary.next_cursor, 0)
            final_state = parse_state(
                targets[1].documents[targets[1].state_key]
            )
            self.assertEqual(final_state.document["generation"], 2)
            self.assertEqual(len(final_state.document["releases"]), 2)
            self.assertTrue(final_state.document["bootstrapComplete"])
            self.assertTrue(
                all(
                    release["entry"]["url"]
                    == (
                        f"{R2_PUBLIC_BASE_URL}/"
                        "libraries/"
                        f"{release['entry']['archiveFileName']}"
                    )
                    for release in final_state.document["releases"]
                )
            )

            rustfs_index = json.loads(
                targets[0].documents[targets[0].index_key]
            )
            r2_index = json.loads(
                targets[1].documents[targets[1].index_key]
            )
            self.assertEqual(len(rustfs_index["libraries"]), 2)
            self.assertEqual(
                [
                    {key: value for key, value in entry.items() if key != "url"}
                    for entry in rustfs_index["libraries"]
                ],
                [
                    {key: value for key, value in entry.items() if key != "url"}
                    for entry in r2_index["libraries"]
                ],
            )
            self.assertTrue(
                all(
                    entry["url"]
                    == (
                        f"{RUSTFS_PUBLIC_BASE_URL}/libraries/"
                        f"{entry['archiveFileName']}"
                    )
                    for entry in rustfs_index["libraries"]
                )
            )
            self.assertTrue(
                all(
                    entry["url"]
                    == (
                        f"{R2_PUBLIC_BASE_URL}/libraries/"
                        f"{entry['archiveFileName']}"
                    )
                    for entry in r2_index["libraries"]
                )
            )
            self.assertEqual(targets[0].index_key, OUTPUT_FILENAME)
            self.assertEqual(targets[1].index_key, OUTPUT_FILENAME)

            second_events = events[second_run_start:]
            package_positions = [
                index
                for index, event in enumerate(second_events)
                if event[1] == "package"
            ]
            state_final_positions = [
                index
                for index, event in enumerate(second_events)
                if event[1] == "document"
                and event[0] == "cloudflare-r2"
                and event[2] == targets[1].state_key
            ]
            index_final_positions = [
                index
                for index, event in enumerate(second_events)
                if event[1] == "document"
                and event[2] in {target.index_key for target in targets}
            ]
            self.assertEqual(len(index_final_positions), 2)
            self.assertTrue(
                all(
                    second_events[index][2] == OUTPUT_FILENAME
                    for index in index_final_positions
                )
            )
            self.assertLess(max(package_positions), min(state_final_positions))
            self.assertLess(
                max(state_final_positions),
                min(index_final_positions),
            )


class LatestVersionPolicyTests(unittest.TestCase):
    def test_bootstrap_keeps_one_zip_then_appends_only_higher_versions(
        self,
    ) -> None:
        repository_url = "https://github.com/aily/latest-only-bootstrap"
        plans: dict[str, tuple[ReleaseSpec, ...] | Exception] = {
            repository_url: (
                release_spec("LatestOnly", "1.0.0", "v1", b"v1"),
                release_spec(
                    "LatestOnly",
                    "2.0.0",
                    "v2",
                    b"v2",
                    ref="2",
                    commit="b",
                ),
            )
        }
        events: list[Event] = []
        targets = make_targets(events)
        materialized_tags: list[str] = []
        scan = make_scan_function(plans, materialized_tags=materialized_tags)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / OUTPUT_FILENAME

            def run_sync() -> sync_module.SyncSummary:
                return synchronise(
                    (repository_url,),
                    targets,
                    output,
                    PUBLIC_BASE_URL,
                    workers=1,
                    max_repositories=1,
                    scan_function=scan,
                )

            first = run_sync()

            first_state = parse_state(
                targets[1].documents[targets[1].state_key]
            )
            self.assertEqual(first.added_release_count, 1)
            self.assertEqual(first.release_count, 1)
            self.assertEqual(
                [
                    release["entry"]["version"]
                    for release in first_state.document["releases"]
                ],
                ["2.0.0"],
            )
            first_tags = first_state.document["repositories"][
                "github.com/aily/latest-only-bootstrap"
            ]["tags"]
            self.assertEqual(
                first_tags["v2"]["archiveFileName"],
                "LatestOnly-2.0.0.zip",
            )
            self.assertIsNone(first_tags["v1"]["archiveFileName"])

            plans[repository_url] = (
                release_spec(
                    "LatestOnly",
                    "3.0.0",
                    "v3",
                    b"v3",
                    ref="3",
                    commit="c",
                ),
            )
            second = run_sync()

            plans[repository_url] = (
                release_spec(
                    "LatestOnly",
                    "3.0.0+build.2",
                    "v3-build",
                    b"v3 build",
                    ref="4",
                    commit="d",
                ),
                release_spec(
                    "LatestOnly",
                    "2.5.0",
                    "v2.5",
                    b"v2.5",
                    ref="5",
                    commit="e",
                ),
            )
            third = run_sync()

        self.assertEqual(second.added_release_count, 1)
        self.assertEqual(second.release_count, 2)
        self.assertEqual(third.added_release_count, 0)
        self.assertEqual(third.release_count, 2)
        self.assertEqual(materialized_tags, ["v2", "v3"])
        final_state = parse_state(
            targets[1].documents[targets[1].state_key]
        )
        self.assertEqual(
            [
                release["entry"]["version"]
                for release in final_state.document["releases"]
            ],
            ["2.0.0", "3.0.0"],
        )
        final_tags = final_state.document["repositories"][
            "github.com/aily/latest-only-bootstrap"
        ]["tags"]
        self.assertIsNone(final_tags["v2.5"]["archiveFileName"])
        self.assertIsNone(final_tags["v3-build"]["archiveFileName"])
        for target in targets:
            self.assertEqual(
                set(target.packages),
                {
                    "libraries/LatestOnly-2.0.0.zip",
                    "libraries/LatestOnly-3.0.0.zip",
                },
            )
            index_document = json.loads(
                target.documents[target.index_key]
            )
            self.assertEqual(
                [entry["version"] for entry in index_document["libraries"]],
                ["3.0.0", "2.0.0"],
            )

    def test_highest_unpackageable_version_falls_back_without_building_older_tags(
        self,
    ) -> None:
        repository_url = "https://github.com/aily/lazy-package-fallback"
        materialized_tags: list[str] = []
        scan = make_scan_function(
            {
                repository_url: (
                    release_spec("LazyFallback", "1.0.0", "v1", b"v1"),
                    release_spec(
                        "LazyFallback",
                        "2.0.0",
                        "v2",
                        b"v2",
                        ref="2",
                        commit="b",
                    ),
                    release_spec(
                        "LazyFallback",
                        "3.0.0",
                        "v3",
                        b"v3",
                        ref="3",
                        commit="c",
                    ),
                )
            },
            materialized_tags=materialized_tags,
            terminal_tags={"v3"},
        )
        targets = make_targets([])

        with tempfile.TemporaryDirectory() as directory:
            summary = synchronise(
                (repository_url,),
                targets,
                Path(directory) / OUTPUT_FILENAME,
                PUBLIC_BASE_URL,
                workers=1,
                max_repositories=1,
                scan_function=scan,
            )

        self.assertEqual(materialized_tags, ["v3", "v2"])
        self.assertEqual(summary.added_release_count, 1)
        state = parse_state(targets[1].documents[targets[1].state_key])
        self.assertEqual(
            [release["entry"]["version"] for release in state.document["releases"]],
            ["2.0.0"],
        )
        tags = state.document["repositories"][
            "github.com/aily/lazy-package-fallback"
        ]["tags"]
        self.assertIsNone(tags["v3"]["archiveFileName"])
        self.assertEqual(tags["v2"]["archiveFileName"], "LazyFallback-2.0.0.zip")
        self.assertIsNone(tags["v1"]["archiveFileName"])

    def test_same_commit_tag_aliases_share_one_materialized_zip(self) -> None:
        repository_url = "https://github.com/aily/lazy-tag-aliases"
        materialized_tags: list[str] = []
        scan = make_scan_function(
            {
                repository_url: (
                    release_spec("LazyAliases", "1.0.0", "v1", b"v1"),
                    release_spec(
                        "LazyAliases",
                        "2.0.0",
                        "v2-z",
                        b"v2",
                        ref="2",
                        commit="b",
                    ),
                    release_spec(
                        "LazyAliases",
                        "2.0.0",
                        "v2-a",
                        b"v2",
                        ref="3",
                        commit="b",
                    ),
                )
            },
            materialized_tags=materialized_tags,
        )
        targets = make_targets([])

        with tempfile.TemporaryDirectory() as directory:
            synchronise(
                (repository_url,),
                targets,
                Path(directory) / OUTPUT_FILENAME,
                PUBLIC_BASE_URL,
                workers=1,
                max_repositories=1,
                scan_function=scan,
            )

        self.assertEqual(materialized_tags, ["v2-a"])
        state = parse_state(targets[1].documents[targets[1].state_key])
        repository = state.document["repositories"][
            "github.com/aily/lazy-tag-aliases"
        ]
        self.assertEqual(
            repository["tags"]["v2-a"]["archiveFileName"],
            "LazyAliases-2.0.0.zip",
        )
        self.assertEqual(
            repository["tags"]["v2-z"]["archiveFileName"],
            "LazyAliases-2.0.0.zip",
        )
        self.assertIsNone(repository["tags"]["v1"]["archiveFileName"])

    def test_same_version_uses_only_the_content_valid_commit(self) -> None:
        repository_url = "https://github.com/aily/lazy-valid-commit"
        materialized_tags: list[str] = []
        scan = make_scan_function(
            {
                repository_url: (
                    release_spec("LazyCommit", "1.0.0", "v1", b"v1"),
                    release_spec(
                        "LazyCommit",
                        "2.0.0",
                        "v2-a",
                        b"valid",
                        ref="2",
                        commit="b",
                    ),
                    release_spec(
                        "LazyCommit",
                        "2.0.0",
                        "v2-z",
                        b"invalid",
                        ref="3",
                        commit="c",
                    ),
                )
            },
            materialized_tags=materialized_tags,
            terminal_tags={"v2-z"},
        )
        targets = make_targets([])

        with tempfile.TemporaryDirectory() as directory:
            synchronise(
                (repository_url,),
                targets,
                Path(directory) / OUTPUT_FILENAME,
                PUBLIC_BASE_URL,
                workers=1,
                max_repositories=1,
                scan_function=scan,
            )

        self.assertEqual(materialized_tags, ["v2-z", "v2-a"])
        state = parse_state(targets[1].documents[targets[1].state_key])
        self.assertEqual(
            [release["entry"]["version"] for release in state.document["releases"]],
            ["2.0.0"],
        )
        repository = state.document["repositories"][
            "github.com/aily/lazy-valid-commit"
        ]
        self.assertEqual(
            repository["tags"]["v2-a"]["archiveFileName"],
            "LazyCommit-2.0.0.zip",
        )
        self.assertIsNone(repository["tags"]["v2-z"]["archiveFileName"])
        self.assertIsNone(repository["tags"]["v1"]["archiveFileName"])


class ResumeAfterFailureTests(unittest.TestCase):
    def test_single_target_index_failure_repairs_only_missing_copy(self) -> None:
        repository_url = "https://github.com/aily/index-retry"
        events: list[Event] = []
        targets = make_targets(events, second_fails_index_once=True)
        scan = make_scan_function(
            {
                repository_url: (
                    release_spec("IndexRetry", "1.0.0", "v1.0.0", b"retry"),
                )
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / OUTPUT_FILENAME
            with self.assertRaisesRegex(RuntimeError, "index upload failure"):
                synchronise(
                    (repository_url,),
                    targets,
                    output,
                    PUBLIC_BASE_URL,
                    workers=1,
                    max_repositories=1,
                    scan_function=scan,
                )

            self.assertIn(targets[0].index_key, targets[0].documents)
            self.assertNotIn(targets[1].index_key, targets[1].documents)
            self.assertIn(targets[1].state_key, targets[1].documents)
            retry_start = len(events)

            summary = synchronise(
                (repository_url,),
                targets,
                output,
                PUBLIC_BASE_URL,
                workers=1,
                max_repositories=1,
                scan_function=scan,
            )

        retry_index_events = [
            event
            for event in events[retry_start:]
            if event[1] == "document" and event[2] == OUTPUT_FILENAME
        ]
        self.assertEqual(summary.uploaded_document_object_count, 2)
        self.assertEqual(
            [event[0] for event in retry_index_events],
            [R2_TARGET_NAME],
        )
        rustfs_index = json.loads(targets[0].documents[OUTPUT_FILENAME])
        r2_index = json.loads(targets[1].documents[OUTPUT_FILENAME])
        self.assertEqual(
            rustfs_index["libraries"][0]["url"],
            (
                f"{RUSTFS_PUBLIC_BASE_URL}/libraries/"
                "IndexRetry-1.0.0.zip"
            ),
        )
        self.assertEqual(
            r2_index["libraries"][0]["url"],
            f"{R2_PUBLIC_BASE_URL}/libraries/IndexRetry-1.0.0.zip",
        )

    def test_single_target_package_failure_publishes_no_documents_then_resumes(
        self,
    ) -> None:
        repository_url = "https://github.com/aily/retry"
        repository_urls = (repository_url,)
        events: list[Event] = []
        targets = make_targets(events, second_fails_package_once=True)
        scan = make_scan_function(
            {
                repository_url: (
                    release_spec("Retry", "1.0.0", "v1.0.0", b"retry"),
                )
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / OUTPUT_FILENAME
            with self.assertRaisesRegex(RuntimeError, "package upload failure"):
                synchronise(
                    repository_urls,
                    targets,
                    output,
                    PUBLIC_BASE_URL,
                    workers=1,
                    max_repositories=1,
                    scan_function=scan,
                )

            self.assertEqual(len(targets[0].packages), 1)
            self.assertEqual(targets[1].packages, {})
            self.assertEqual(targets[0].documents, {})
            self.assertEqual(targets[1].documents, {})
            self.assertFalse(output.exists())

            summary = synchronise(
                repository_urls,
                targets,
                output,
                PUBLIC_BASE_URL,
                workers=1,
                max_repositories=1,
                scan_function=scan,
            )

        self.assertTrue(summary.index_published)
        self.assertEqual(summary.uploaded_package_object_count, 1)
        self.assertEqual(len(targets[0].packages), 1)
        self.assertEqual(len(targets[1].packages), 1)
        self.assertNotIn(targets[0].state_key, targets[0].documents)
        self.assertIn(targets[1].state_key, targets[1].documents)
        self.assertIn(targets[0].index_key, targets[0].documents)
        self.assertIn(targets[1].index_key, targets[1].documents)


class CollisionTests(unittest.TestCase):
    def test_flat_archive_collision_does_not_upload_or_overwrite(self) -> None:
        first_url = "https://github.com/aily/dash-name"
        second_url = "https://github.com/aily/underscore-name"
        repository_urls = (first_url, second_url)
        events: list[Event] = []
        targets = make_targets(events)
        first_payload = b"original package"
        scan = make_scan_function(
            {
                first_url: (
                    release_spec(
                        "Foo-Bar",
                        "1.0.0",
                        "v1.0.0",
                        first_payload,
                    ),
                ),
                second_url: (
                    release_spec(
                        "Foo_Bar",
                        "1.0.0",
                        "v1.0.0",
                        b"conflicting package",
                        ref="2",
                        commit="b",
                    ),
                ),
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / OUTPUT_FILENAME
            synchronise(
                repository_urls,
                targets,
                output,
                PUBLIC_BASE_URL,
                workers=1,
                max_repositories=1,
                scan_function=scan,
            )
            second_run_start = len(events)
            summary = synchronise(
                repository_urls,
                targets,
                output,
                PUBLIC_BASE_URL,
                workers=1,
                max_repositories=1,
                scan_function=scan,
            )

        self.assertTrue(summary.index_published)
        self.assertEqual(summary.added_release_count, 0)
        self.assertFalse(
            any(event[1] == "package" for event in events[second_run_start:])
        )
        package_key = "libraries/Foo_Bar-1.0.0.zip"
        for target in targets:
            self.assertEqual(target.packages[package_key], first_payload)

        state = parse_state(targets[1].documents[targets[1].state_key])
        self.assertEqual(len(state.document["releases"]), 1)
        second_key = "github.com/aily/underscore-name"
        self.assertIsNone(
            state.document["repositories"][second_key]["tags"]["v1.0.0"][
                "archiveFileName"
            ]
        )

    def test_same_name_version_from_different_commits_is_not_added(self) -> None:
        repository_url = "https://github.com/aily/duplicate-version"
        events: list[Event] = []
        targets = make_targets(events)
        materialized_tags: list[str] = []
        scan = make_scan_function(
            {
                repository_url: (
                    release_spec(
                        "Same",
                        "2.0.0",
                        "v2-a",
                        b"first v2",
                        ref="1",
                        commit="a",
                    ),
                    release_spec(
                        "Same",
                        "2.0.0",
                        "v2-b",
                        b"second v2",
                        ref="2",
                        commit="b",
                    ),
                    release_spec(
                        "Same",
                        "1.0.0",
                        "v1",
                        b"valid v1",
                        ref="3",
                        commit="c",
                    ),
                )
            },
            materialized_tags=materialized_tags,
        )

        with tempfile.TemporaryDirectory() as directory:
            summary = synchronise(
                (repository_url,),
                targets,
                Path(directory) / OUTPUT_FILENAME,
                PUBLIC_BASE_URL,
                workers=1,
                max_repositories=1,
                scan_function=scan,
            )

        self.assertTrue(summary.index_published)
        self.assertEqual(summary.added_release_count, 1)
        self.assertEqual(
            set(materialized_tags),
            {"v2-a", "v2-b", "v1"},
        )
        state = parse_state(targets[1].documents[targets[1].state_key])
        self.assertEqual(
            [release["entry"]["version"] for release in state.document["releases"]],
            ["1.0.0"],
        )
        tags = state.document["repositories"][
            "github.com/aily/duplicate-version"
        ]["tags"]
        self.assertIsNone(tags["v2-a"]["archiveFileName"])
        self.assertIsNone(tags["v2-b"]["archiveFileName"])
        self.assertEqual(tags["v1"]["archiveFileName"], "Same-1.0.0.zip")
        for target in targets:
            self.assertEqual(
                set(target.packages),
                {"libraries/Same-1.0.0.zip"},
            )


class StateRecoveryTests(unittest.TestCase):
    def test_materialization_failure_is_isolated_and_retried(self) -> None:
        failing_url = "https://github.com/aily/materialization-retry"
        available_url = "https://github.com/aily/materialization-available"
        materialization_errors: dict[str, Exception] = {
            "v1-failing": IndexBuildError("temporary ZIP failure")
        }
        scan = make_scan_function(
            {
                failing_url: (
                    release_spec(
                        "MaterializationRetry",
                        "1.0.0",
                        "v1-failing",
                        b"retry",
                    ),
                ),
                available_url: (
                    release_spec(
                        "MaterializationAvailable",
                        "1.0.0",
                        "v1-available",
                        b"available",
                        ref="2",
                        commit="b",
                    ),
                ),
            },
            materialization_errors=materialization_errors,
        )
        targets = make_targets([])

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / OUTPUT_FILENAME
            first = synchronise(
                (failing_url, available_url),
                targets,
                output,
                PUBLIC_BASE_URL,
                workers=2,
                max_repositories=2,
                scan_function=scan,
            )
            first_state = parse_state(
                targets[1].documents[targets[1].state_key]
            )
            self.assertFalse(first.bootstrap_complete)
            self.assertEqual(first.failed_repository_count, 1)
            self.assertEqual(
                set(first_state.document["retryRepositories"]),
                {"github.com/aily/materialization-retry"},
            )
            self.assertEqual(
                [
                    release["entry"]["name"]
                    for release in first_state.document["releases"]
                ],
                ["MaterializationAvailable"],
            )
            self.assertEqual(
                first_state.document["repositories"][
                    "github.com/aily/materialization-retry"
                ]["tags"],
                {},
            )

            materialization_errors.clear()
            second = synchronise(
                (failing_url, available_url),
                targets,
                output,
                PUBLIC_BASE_URL,
                workers=2,
                max_repositories=2,
                scan_function=scan,
            )

        self.assertTrue(second.bootstrap_complete)
        self.assertEqual(second.failed_repository_count, 0)
        final_state = parse_state(targets[1].documents[targets[1].state_key])
        self.assertEqual(final_state.document["retryRepositories"], {})
        self.assertEqual(
            {
                release["entry"]["name"]
                for release in final_state.document["releases"]
            },
            {"MaterializationAvailable", "MaterializationRetry"},
        )

    def test_annotated_tag_object_change_keeps_release_provenance_valid(self) -> None:
        repository_url = "https://github.com/aily/annotated"
        events: list[Event] = []
        targets = make_targets(events)
        initial_scan = make_scan_function(
            {
                repository_url: (
                    release_spec("Annotated", "1.0.0", "v1", b"zip"),
                )
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / OUTPUT_FILENAME
            synchronise(
                (repository_url,),
                targets,
                output,
                PUBLIC_BASE_URL,
                workers=1,
                max_repositories=1,
                scan_function=initial_scan,
            )

            def updated_annotation(
                repository_key: str,
                repository_url: str,
                known_tags: Mapping[str, object],
                temporary_root: Path,
                *,
                timeout_seconds: int,
                max_source_bytes: int,
            ) -> ScanResult:
                del temporary_root, timeout_seconds, max_source_bytes
                known = known_tags["v1"]
                self.assertIsInstance(known, dict)
                return ScanResult(
                    repository_key=repository_key,
                    repository_url=repository_url,
                    tag_updates={
                        "v1": TagUpdate(
                            ref_oid="2" * 40,
                            commit_oid="a" * 40,
                            archive_file_name="Annotated-1.0.0.zip",
                        )
                    },
                    candidates=(),
                    issues=(),
                    remote_tag_count=1,
                )

            synchronise(
                (repository_url,),
                targets,
                output,
                PUBLIC_BASE_URL,
                workers=1,
                max_repositories=1,
                scan_function=updated_annotation,
            )

        state = parse_state(targets[1].documents[targets[1].state_key])
        release = state.document["releases"][0]
        tag = state.document["repositories"][
            "github.com/aily/annotated"
        ]["tags"]["v1"]
        self.assertEqual(release["tagRefOid"], "2" * 40)
        self.assertEqual(tag["refOid"], "2" * 40)

    def test_requires_exactly_one_state_owner(self) -> None:
        for stores_state in ((False, False), (True, True)):
            with self.subTest(stores_state=stores_state):
                targets = make_targets([])
                targets[0].stores_state = stores_state[0]
                targets[1].stores_state = stores_state[1]
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaisesRegex(
                        SyncError, "恰好配置一个 state 存储目标"
                    ):
                        synchronise(
                            ("https://github.com/aily/invalid-state-owner",),
                            targets,
                            Path(directory) / OUTPUT_FILENAME,
                            PUBLIC_BASE_URL,
                        )

    def test_transient_failure_is_retried_before_bootstrap_publication(self) -> None:
        first_url = "https://github.com/aily/available"
        failing_url = "https://github.com/aily/failing"
        repository_urls = (first_url, failing_url)
        events: list[Event] = []
        targets = make_targets(events)
        successful_scan = make_scan_function(
            {
                first_url: (
                    release_spec("Available", "1.0.0", "v1", b"available"),
                ),
                failing_url: (
                    release_spec("Recovered", "1.0.0", "v1", b"recovered"),
                ),
            }
        )
        attempts: dict[str, int] = {}

        def scan(
            repository_key: str,
            repository_url: str,
            known_tags: Mapping[str, object],
            temporary_root: Path,
            *,
            timeout_seconds: int,
            max_source_bytes: int,
        ) -> ScanResult:
            attempts[repository_url] = attempts.get(repository_url, 0) + 1
            if repository_url == failing_url and attempts[repository_url] <= 2:
                raise RuntimeError("temporary scan failure")
            return successful_scan(
                repository_key,
                repository_url,
                known_tags,
                temporary_root,
                timeout_seconds=timeout_seconds,
                max_source_bytes=max_source_bytes,
            )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / OUTPUT_FILENAME
            first_summary = synchronise(
                repository_urls,
                targets,
                output,
                PUBLIC_BASE_URL,
                workers=1,
                max_repositories=2,
                scan_function=scan,
            )

            self.assertFalse(first_summary.bootstrap_complete)
            self.assertFalse(first_summary.index_published)
            first_state = parse_state(targets[1].documents[targets[1].state_key])
            self.assertEqual(first_state.document["cursor"], 2)
            self.assertEqual(
                first_state.document["retryRepositories"],
                {"github.com/aily/failing": 1},
            )
            for target in targets:
                self.assertNotIn(target.index_key, target.documents)

            second_summary = synchronise(
                repository_urls,
                targets,
                output,
                PUBLIC_BASE_URL,
                workers=1,
                max_repositories=2,
                scan_function=scan,
            )

        self.assertTrue(second_summary.bootstrap_complete)
        self.assertTrue(second_summary.index_published)
        self.assertEqual(second_summary.scanned_repository_count, 1)
        self.assertEqual(attempts[first_url], 1)
        self.assertEqual(attempts[failing_url], 3)
        final_state = parse_state(targets[1].documents[targets[1].state_key])
        self.assertEqual(final_state.document["retryRepositories"], {})
        self.assertEqual(final_state.document["cursor"], 0)
        index_document = json.loads(targets[0].documents[targets[0].index_key])
        self.assertEqual(
            {entry["name"] for entry in index_document["libraries"]},
            {"Available", "Recovered"},
        )

    def test_persistent_failure_waits_three_rounds_then_enters_steady_state(
        self,
    ) -> None:
        first_url = "https://github.com/aily/available"
        failing_url = "https://github.com/aily/persistently-failing"
        repository_urls = (first_url, failing_url)
        events: list[Event] = []
        targets = make_targets(events)
        calls: list[tuple[str, dict[str, Any]]] = []
        scan = make_scan_function(
            {
                first_url: (
                    release_spec("Available", "1.0.0", "v1", b"available"),
                ),
                failing_url: RuntimeError("persistent scan failure"),
            },
            calls,
        )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / OUTPUT_FILENAME
            summaries = []
            for _attempt in range(3):
                summaries.append(
                    synchronise(
                        repository_urls,
                        targets,
                        output,
                        PUBLIC_BASE_URL,
                        workers=1,
                        max_repositories=2,
                        scan_function=scan,
                    )
                )

                state = parse_state(targets[1].documents[targets[1].state_key])
                if len(summaries) < 3:
                    self.assertFalse(state.document["bootstrapComplete"])
                    self.assertEqual(
                        state.document["retryRepositories"],
                        {"github.com/aily/persistently-failing": len(summaries)},
                    )
                    for target in targets:
                        self.assertNotIn(target.index_key, target.documents)

            self.assertTrue(summaries[2].bootstrap_complete)
            self.assertTrue(summaries[2].index_published)
            final_bootstrap_state = parse_state(
                targets[1].documents[targets[1].state_key]
            )
            self.assertEqual(final_bootstrap_state.document["retryRepositories"], {})
            self.assertEqual(final_bootstrap_state.document["cursor"], 0)
            index_document = json.loads(
                targets[0].documents[targets[0].index_key]
            )
            self.assertEqual(
                [entry["name"] for entry in index_document["libraries"]],
                ["Available"],
            )

            steady_summary = synchronise(
                repository_urls,
                targets,
                output,
                PUBLIC_BASE_URL,
                workers=1,
                max_repositories=2,
                scan_function=scan,
            )

        self.assertTrue(steady_summary.bootstrap_complete)
        steady_state = parse_state(targets[1].documents[targets[1].state_key])
        self.assertEqual(
            steady_state.document["retryRepositories"],
            {"github.com/aily/persistently-failing": 1},
        )
        called_keys = [repository_key for repository_key, _known_tags in calls]
        self.assertEqual(called_keys.count("github.com/aily/available"), 2)
        self.assertEqual(
            called_keys.count("github.com/aily/persistently-failing"),
            8,
        )


class DryRunTests(unittest.TestCase):
    def test_rejects_identical_public_download_bases(self) -> None:
        repository_url = "https://github.com/aily/dry-run"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / OUTPUT_FILENAME
            with self.assertRaisesRegex(SyncError, "公开下载基址不能相同"):
                _synchronise(
                    (repository_url,),
                    (),
                    index_output_paths(output),
                    {
                        RUSTFS_TARGET_NAME: RUSTFS_PUBLIC_BASE_URL,
                        R2_TARGET_NAME: RUSTFS_PUBLIC_BASE_URL,
                    },
                    dry_run=True,
                )

    def test_dry_run_writes_local_candidate_without_targets(self) -> None:
        repository_url = "https://github.com/aily/dry-run"
        calls: list[tuple[str, dict[str, Any]]] = []
        scan = make_scan_function(
            {
                repository_url: (
                    release_spec(
                        "DryRun",
                        "1.0.0",
                        "v1.0.0",
                        b"dry run",
                    ),
                )
            },
            calls,
        )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / OUTPUT_FILENAME
            summary = synchronise(
                (repository_url,),
                (),
                output,
                PUBLIC_BASE_URL,
                workers=1,
                max_repositories=1,
                dry_run=True,
                scan_function=scan,
            )
            rustfs_document = json.loads(output.read_text(encoding="utf-8"))
            r2_output = index_output_paths(output)[R2_TARGET_NAME]
            r2_document = json.loads(r2_output.read_text(encoding="utf-8"))

        self.assertTrue(summary.bootstrap_complete)
        self.assertFalse(summary.index_published)
        self.assertEqual(summary.uploaded_package_object_count, 0)
        self.assertEqual(summary.uploaded_document_object_count, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], {})
        self.assertEqual(len(rustfs_document["libraries"]), 1)
        self.assertEqual(len(r2_document["libraries"]), 1)
        self.assertEqual(
            rustfs_document["libraries"][0]["url"],
            f"{RUSTFS_PUBLIC_BASE_URL}/libraries/DryRun-1.0.0.zip",
        )
        self.assertEqual(
            r2_document["libraries"][0]["url"],
            f"{R2_PUBLIC_BASE_URL}/libraries/DryRun-1.0.0.zip",
        )
        self.assertEqual(output.name, r2_output.name)
        self.assertEqual(output.name, OUTPUT_FILENAME)


class ResourceBudgetTests(unittest.TestCase):
    def test_rejects_concurrency_or_source_size_above_runner_budget(self) -> None:
        cases = (
            ({"workers": MAX_SCAN_WORKERS + 1}, "workers 不能大于"),
            (
                {"max_source_bytes": MAX_ARCHIVE_SOURCE_BYTES + 1},
                "MAX_ARCHIVE_SOURCE_BYTES 不能大于",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / OUTPUT_FILENAME
            for arguments, message in cases:
                with self.subTest(arguments=arguments), self.assertRaisesRegex(
                    SyncError, message
                ):
                    synchronise(
                        ("https://github.com/aily/resource-budget",),
                        (),
                        output,
                        PUBLIC_BASE_URL,
                        dry_run=True,
                        **arguments,
                    )

    def test_large_batch_releases_each_worker_window_before_next_scan(self) -> None:
        repository_urls = tuple(
            f"https://github.com/aily/window-{index}" for index in range(3)
        )
        plans = {
            repository_url: (
                release_spec(
                    f"Window{index}",
                    "1.0.0",
                    "v1",
                    f"window-{index}".encode("ascii"),
                ),
            )
            for index, repository_url in enumerate(repository_urls)
        }
        base_scan = make_scan_function(plans)
        first_window_roots: list[Path] = []

        def scan(
            repository_key: str,
            repository_url: str,
            known_tags: Mapping[str, object],
            temporary_root: Path,
            *args: object,
            **kwargs: object,
        ) -> ScanResult:
            if repository_url in repository_urls[:2]:
                first_window_roots.append(temporary_root)
            else:
                self.assertTrue(first_window_roots)
                self.assertTrue(
                    all(not path.exists() for path in first_window_roots)
                )
            return base_scan(
                repository_key,
                repository_url,
                known_tags,
                temporary_root,
                *args,
                **kwargs,
            )

        with tempfile.TemporaryDirectory() as directory:
            summary = synchronise(
                repository_urls,
                (),
                Path(directory) / OUTPUT_FILENAME,
                PUBLIC_BASE_URL,
                workers=2,
                max_repositories=3,
                dry_run=True,
                scan_function=scan,
            )

        self.assertEqual(summary.scanned_repository_count, 3)
        self.assertEqual(summary.added_release_count, 3)


if __name__ == "__main__":
    unittest.main()
