from __future__ import annotations

import functools
import os
import subprocess
import tempfile
import threading
import unittest
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from aily_coder_libraries import scanner


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, message_format: str, *args: object) -> None:
        del message_format, args


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        del request, client_address


class ScannerEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.source_repository = self.root / "source"
        self.http_root = self.root / "http"
        self.remote_repository = self.http_root / "remote.git"
        self.scan_root = self.root / "scans"

        self._create_source_repository()
        self.http_root.mkdir()
        self._git(
            "clone",
            "--bare",
            "--quiet",
            str(self.source_repository),
            str(self.remote_repository),
        )
        self._update_server_info()
        self._start_http_server()

    def _git(
        self,
        *arguments: str,
        cwd: Path | None = None,
        input_data: bytes | None = None,
    ) -> str:
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
                "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "never",
                "LC_ALL": "C",
            }
        )
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
            env=environment,
        )
        if result.returncode != 0:
            self.fail(
                "git command failed: "
                + result.stderr.decode("utf-8", errors="replace")
            )
        return result.stdout.decode("ascii").strip()

    def _create_source_repository(self) -> None:
        self.source_repository.mkdir()
        self._git("init", "--quiet", cwd=self.source_repository)
        self._git(
            "config",
            "user.name",
            "Scanner Test",
            cwd=self.source_repository,
        )
        self._git(
            "config",
            "user.email",
            "scanner@example.invalid",
            cwd=self.source_repository,
        )
        self._git(
            "config",
            "core.autocrlf",
            "false",
            cwd=self.source_repository,
        )

        (self.source_repository / "src").mkdir()
        (self.source_repository / "nested" / ".private").mkdir(parents=True)
        (self.source_repository / "CVS").mkdir()
        (self.source_repository / "library.properties").write_bytes(
            b"\n".join(
                (
                    b"name=Local Scanner Test",
                    b"version=2.4",
                    b"author=Aily",
                    b"maintainer=Aily <dev@example.invalid>",
                    b"sentence=Local scanner fixture.",
                    b"paragraph=The tag name intentionally differs from the version.",
                    b"category=Other",
                    b"url=https://example.invalid/library",
                    b"architectures=samd",
                    b"",
                )
            )
        )
        (self.source_repository / "src" / "Fixture.h").write_bytes(
            b"#pragma once\n"
        )
        (self.source_repository / ".hidden.txt").write_bytes(b"hidden\n")
        (
            self.source_repository / "nested" / ".private" / "secret.txt"
        ).write_bytes(b"secret\n")
        (self.source_repository / "CVS" / "ignored.txt").write_bytes(
            b"ignored\n"
        )

        self._git("add", "--all", cwd=self.source_repository)
        self._git(
            "commit",
            "--quiet",
            "--no-gpg-sign",
            "-m",
            "valid library",
            cwd=self.source_repository,
        )
        self.valid_commit_oid = self._git(
            "rev-parse", "HEAD", cwd=self.source_repository
        )
        self._git("tag", "release-channel", cwd=self.source_repository)
        self._git(
            "tag",
            "--annotate",
            "--no-sign",
            "pretty-label",
            "--message",
            "annotated tag",
            cwd=self.source_repository,
        )
        self.annotated_ref_oid = self._git(
            "rev-parse",
            "refs/tags/pretty-label",
            cwd=self.source_repository,
        )

        symlink_blob_oid = self._git(
            "hash-object",
            "-w",
            "--stdin",
            cwd=self.source_repository,
            input_data=b"src/Fixture.h",
        )
        self._git(
            "update-index",
            "--add",
            "--cacheinfo",
            "120000",
            symlink_blob_oid,
            "linked-header",
            cwd=self.source_repository,
        )
        self._git(
            "commit",
            "--quiet",
            "--no-gpg-sign",
            "-m",
            "add symlink",
            cwd=self.source_repository,
        )
        self.symlink_commit_oid = self._git(
            "rev-parse", "HEAD", cwd=self.source_repository
        )
        self._git("tag", "bad-symlink", cwd=self.source_repository)

    def _update_server_info(self) -> None:
        self._git(
            "--git-dir",
            str(self.remote_repository),
            "update-server-info",
        )

    def _start_http_server(self) -> None:
        handler = functools.partial(
            _QuietHandler,
            directory=str(self.http_root),
        )
        self.http_server = _QuietServer(("127.0.0.1", 0), handler)
        port = self.http_server.server_address[1]
        self.repository_url = f"http://127.0.0.1:{port}/remote.git"
        self.http_thread = threading.Thread(
            target=self.http_server.serve_forever,
            kwargs={"poll_interval": 0.01},
            name="scanner-test-http-server",
            daemon=True,
        )
        self.http_thread.start()
        self.addCleanup(self._stop_http_server)

    def _stop_http_server(self) -> None:
        self.http_server.shutdown()
        self.http_server.server_close()
        self.http_thread.join(timeout=5)
        if self.http_thread.is_alive():
            raise RuntimeError("scanner test HTTP server did not stop")

    def _scan(
        self,
        known_tags: dict[str, scanner.TagUpdate] | None = None,
    ) -> scanner.ScanResult:
        return scanner.scan_repository(
            "localhost/local-scanner-test",
            self.repository_url,
            known_tags or {},
            self.scan_root,
            timeout_seconds=30,
            max_source_bytes=16 * 1024 * 1024,
        )

    def test_discovers_lightweight_and_annotated_tags(self) -> None:
        tags = scanner.discover_tags(self.repository_url, timeout_seconds=30)

        self.assertEqual(tags["release-channel"].ref_oid, self.valid_commit_oid)
        self.assertIsNone(tags["release-channel"].commit_oid)
        self.assertEqual(tags["pretty-label"].ref_oid, self.annotated_ref_oid)
        self.assertEqual(tags["pretty-label"].commit_oid, self.valid_commit_oid)
        self.assertEqual(tags["bad-symlink"].ref_oid, self.symlink_commit_oid)

    def test_builds_deterministic_zip_and_rejects_symlink_tag(self) -> None:
        result = self._scan()
        candidates = {candidate.tag: candidate for candidate in result.candidates}

        self.assertEqual(result.remote_tag_count, 3)
        self.assertEqual(set(candidates), {"pretty-label", "release-channel"})
        self.assertEqual(candidates["release-channel"].metadata.version, "2.4.0")
        self.assertEqual(
            candidates["release-channel"].package.archive_file_name,
            "Local_Scanner_Test-2.4.0.zip",
        )
        self.assertNotEqual("release-channel", "2.4.0")
        self.assertEqual(
            candidates["pretty-label"].archive_path.read_bytes(),
            candidates["release-channel"].archive_path.read_bytes(),
        )

        root_name = "Local_Scanner_Test-2.4.0"
        with zipfile.ZipFile(candidates["release-channel"].archive_path) as archive:
            entries = archive.infolist()
        names = [entry.filename for entry in entries]
        self.assertEqual(names[0], f"{root_name}/")
        self.assertTrue(all(name.startswith(f"{root_name}/") for name in names))
        self.assertIn(f"{root_name}/library.properties", names)
        self.assertFalse(any(".hidden" in name for name in names))
        self.assertFalse(any("/.private/" in name for name in names))
        self.assertFalse(any("/CVS/" in name for name in names))
        self.assertTrue(
            all(entry.date_time == (1980, 1, 1, 0, 0, 0) for entry in entries)
        )

        self.assertIsNone(
            result.tag_updates["bad-symlink"].archive_file_name
        )
        self.assertEqual(
            result.tag_updates["bad-symlink"].commit_oid,
            self.symlink_commit_oid,
        )
        self.assertEqual([issue.tag for issue in result.issues], ["bad-symlink"])
        self.assertIn("symlink", result.issues[0].message)

    def test_known_published_and_invalid_tags_are_not_reprocessed(self) -> None:
        first = self._scan()
        second = self._scan(dict(first.tag_updates))

        self.assertEqual(second.remote_tag_count, 3)
        self.assertEqual(second.candidates, ())
        self.assertEqual(dict(second.tag_updates), {})
        self.assertEqual(second.issues, ())

    def test_published_tag_commit_mutation_is_preserved(self) -> None:
        first = self._scan()
        known_tags = dict(first.tag_updates)
        original = known_tags["release-channel"]
        self._git(
            "--git-dir",
            str(self.remote_repository),
            "update-ref",
            "refs/tags/release-channel",
            self.symlink_commit_oid,
        )
        self._update_server_info()

        result = self._scan(known_tags)

        self.assertEqual(result.candidates, ())
        self.assertNotIn("release-channel", result.tag_updates)
        self.assertEqual(known_tags["release-channel"], original)
        mutation_issues = [
            issue for issue in result.issues if issue.tag == "release-channel"
        ]
        self.assertEqual(len(mutation_issues), 1)
        self.assertIn("改写", mutation_issues[0].message)

    def test_git_archive_is_stopped_while_crossing_size_limit(self) -> None:
        tar_path = self.root / "bounded.tar"
        with mock.patch.object(scanner, "_TAR_OVERHEAD_ALLOWANCE", 1024):
            with self.assertRaisesRegex(
                scanner._TerminalTagError, "源码 tar 超过大小上限"
            ):
                scanner._write_git_archive(
                    self.remote_repository,
                    self.valid_commit_oid,
                    tar_path,
                    timeout_seconds=30,
                    max_source_bytes=1,
                )

        self.assertLessEqual(tar_path.stat().st_size, 1025)

    def test_git_fetch_is_stopped_when_bare_repository_crosses_limit(self) -> None:
        with mock.patch.object(scanner, "_MAX_REPOSITORY_GIT_BYTES", 1):
            with self.assertRaisesRegex(
                scanner.IndexBuildError, "Git 对象超过临时空间上限"
            ):
                self._scan()


if __name__ == "__main__":
    unittest.main()
