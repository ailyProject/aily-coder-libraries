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


class RepositoryAvailabilityTests(unittest.TestCase):
    def _failed_result(
        self,
        stderr: bytes,
        *,
        returncode: int = 128,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=["git", "ls-remote"],
            returncode=returncode,
            stdout=b"",
            stderr=stderr,
        )

    def test_github_repository_not_found_is_unavailable(self) -> None:
        result = self._failed_result(
            b"remote: Repository not found.\n"
            b"fatal: repository 'https://github.com/aily/missing/' not found\n"
        )

        with mock.patch.object(scanner, "_run_git", return_value=result):
            with self.assertRaisesRegex(
                scanner.RepositoryUnavailableError,
                "仓库不存在或无法匿名访问",
            ):
                scanner.discover_tags("https://github.com/aily/missing")

    def test_github_anonymous_login_prompt_is_unavailable(self) -> None:
        result = self._failed_result(
            b"fatal: could not read Username for 'https://github.com': "
            b"terminal prompts disabled\n"
        )

        with mock.patch.object(scanner, "_run_git", return_value=result):
            with self.assertRaises(scanner.RepositoryUnavailableError):
                scanner.discover_tags("https://github.com/aily/private")

    def test_transient_git_failures_are_not_marked_unavailable(self) -> None:
        transient_errors = (
            b"fatal: unable to access 'https://github.com/a/b': "
            b"Could not resolve host: github.com\n",
            b"fatal: unable to access 'https://github.com/a/b': "
            b"Received HTTP code 407 from proxy after CONNECT\n",
            b"fatal: unable to access 'https://github.com/a/b': "
            b"schannel: AcquireCredentialsHandle failed\n",
            b"fatal: unable to access 'https://github.com/a/b': "
            b"The requested URL returned error: 429\n",
        )
        for stderr in transient_errors:
            with self.subTest(stderr=stderr), mock.patch.object(
                scanner,
                "_run_git",
                return_value=self._failed_result(stderr),
            ):
                with self.assertRaises(scanner.IndexBuildError) as raised:
                    scanner.discover_tags("https://github.com/a/b")
                self.assertNotIsInstance(
                    raised.exception,
                    scanner.RepositoryUnavailableError,
                )

    def test_unavailable_signature_is_limited_to_github_exit_128(self) -> None:
        for repository_url, returncode, stderr in (
            (
                "https://gitlab.com/aily/missing",
                128,
                b"remote: Repository not found.\n",
            ),
            (
                "https://github.com/aily/missing",
                1,
                b"remote: Repository not found.\n",
            ),
            (
                "https://github.com/aily/missing",
                128,
                b"remote: Repository not found.\n",
            ),
        ):
            with self.subTest(
                repository_url=repository_url,
                returncode=returncode,
            ), mock.patch.object(
                scanner,
                "_run_git",
                return_value=self._failed_result(
                    stderr,
                    returncode=returncode,
                ),
            ):
                with self.assertRaises(scanner.IndexBuildError) as raised:
                    scanner.discover_tags(repository_url)
                self.assertNotIsInstance(
                    raised.exception,
                    scanner.RepositoryUnavailableError,
                )

    def test_missing_git_is_an_infrastructure_failure(self) -> None:
        with mock.patch.object(
            scanner.subprocess,
            "run",
            side_effect=FileNotFoundError("git unavailable"),
        ):
            with self.assertRaisesRegex(
                scanner.ScannerInfrastructureError,
                "找不到 git 可执行文件",
            ):
                scanner._run_git(["--version"], timeout_seconds=1)


class GitHubReleaseTests(unittest.TestCase):
    def _mock_release_response(self, final_url: str) -> mock.Mock:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = final_url
        opener = mock.Mock()
        opener.open.return_value = response
        return opener

    def test_latest_release_tag_is_read_from_github_redirect(self) -> None:
        opener = self._mock_release_response(
            "https://github.com/Aily/Library/releases/tag/stable%2Fv2"
        )

        with mock.patch.dict(
            os.environ,
            {"GIT_CONFIG_COUNT": "0"},
        ), mock.patch.object(scanner, "build_opener", return_value=opener):
            tag = scanner._latest_github_release_tag(
                "https://github.com/Aily/Library.git#variant",
                timeout_seconds=30,
            )

        self.assertEqual(tag, "stable/v2")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "HEAD")
        self.assertEqual(request.get_header("User-agent"), "aily-coder-libraries/0.1")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 15)
        self.assertEqual(
            request.full_url,
            "https://github.com/Aily/Library/releases/latest",
        )

    def test_release_lookup_reuses_scoped_git_proxy(self) -> None:
        opener = self._mock_release_response(
            "https://github.com/aily/library/releases/tag/v1"
        )
        environment = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.proxy",
            "GIT_CONFIG_VALUE_0": "http://proxy.example.invalid:8080",
        }

        with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(
            scanner,
            "build_opener",
            return_value=opener,
        ) as build:
            scanner._latest_github_release_tag(
                "https://github.com/aily/library",
                timeout_seconds=30,
            )

        proxy_handler = build.call_args.args[0]
        self.assertIsInstance(proxy_handler, scanner.ProxyHandler)
        self.assertEqual(
            proxy_handler.proxies,
            {
                "http": "http://proxy.example.invalid:8080",
                "https": "http://proxy.example.invalid:8080",
            },
        )

    def test_no_release_redirect_returns_none(self) -> None:
        no_release = self._mock_release_response(
            "https://github.com/aily/library/releases"
        )

        with mock.patch.dict(
            os.environ,
            {"GIT_CONFIG_COUNT": "0"},
        ), mock.patch.object(scanner, "build_opener", return_value=no_release):
            self.assertIsNone(
                scanner._latest_github_release_tag(
                    "https://github.com/aily/library",
                    timeout_seconds=30,
                )
            )

    def test_failed_release_lookup_is_not_treated_as_no_release(self) -> None:
        failed = mock.Mock()
        failed.open.side_effect = OSError("network unavailable")

        with mock.patch.dict(
            os.environ,
            {"GIT_CONFIG_COUNT": "0"},
        ), mock.patch.object(scanner, "build_opener", return_value=failed):
            with self.assertRaisesRegex(
                scanner.ScannerInfrastructureError,
                "无法确认 GitHub Latest Release",
            ):
                scanner._latest_github_release_tag(
                    "https://github.com/aily/library",
                    timeout_seconds=30,
                )

    def test_latest_release_limits_git_discovery_to_its_tag(self) -> None:
        release_tag = scanner.RemoteTag("a" * 40, "b" * 40)
        discover = mock.Mock(return_value={"v2": release_tag})

        with mock.patch.object(
            scanner,
            "_latest_github_release_tag",
            return_value="v2",
        ), mock.patch.object(scanner, "discover_tags", discover):
            tags = scanner._discover_preferred_tags(
                "https://github.com/aily/library",
                timeout_seconds=30,
            )

        self.assertEqual(tags, {"v2": release_tag})
        discover.assert_called_once_with(
            "https://github.com/aily/library",
            timeout_seconds=30,
            only_tag="v2",
        )

    def test_no_release_falls_back_to_all_tags(self) -> None:
        all_tags = {"v1": scanner.RemoteTag("a" * 40, None)}
        discover = mock.Mock(return_value=all_tags)

        with mock.patch.object(
            scanner,
            "_latest_github_release_tag",
            return_value=None,
        ), mock.patch.object(scanner, "discover_tags", discover):
            tags = scanner._discover_preferred_tags(
                "https://github.com/aily/library",
                timeout_seconds=30,
            )

        self.assertEqual(tags, all_tags)
        discover.assert_called_once_with(
            "https://github.com/aily/library",
            timeout_seconds=30,
        )

    def test_missing_release_tag_does_not_fall_back(self) -> None:
        discover = mock.Mock(return_value={})

        with mock.patch.object(
            scanner,
            "_latest_github_release_tag",
            return_value="deleted",
        ), mock.patch.object(scanner, "discover_tags", discover):
            with self.assertRaisesRegex(
                scanner.IndexBuildError,
                "Latest Release 指向的 tag 不存在",
            ):
                scanner._discover_preferred_tags(
                    "https://github.com/aily/library",
                    timeout_seconds=30,
                )

        discover.assert_called_once_with(
            "https://github.com/aily/library",
            timeout_seconds=30,
            only_tag="deleted",
        )


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
        selected = scanner.discover_tags(
            self.repository_url,
            timeout_seconds=30,
            only_tag="pretty-label",
        )

        self.assertEqual(tags["release-channel"].ref_oid, self.valid_commit_oid)
        self.assertIsNone(tags["release-channel"].commit_oid)
        self.assertEqual(tags["pretty-label"].ref_oid, self.annotated_ref_oid)
        self.assertEqual(tags["pretty-label"].commit_oid, self.valid_commit_oid)
        self.assertEqual(tags["bad-symlink"].ref_oid, self.symlink_commit_oid)
        self.assertEqual(selected, {"pretty-label": tags["pretty-label"]})

    def test_latest_release_scans_and_packages_only_its_tag(self) -> None:
        with mock.patch.object(
            scanner,
            "_latest_github_release_tag",
            return_value="pretty-label",
        ):
            result = self._scan()

        self.assertEqual(result.remote_tag_count, 1)
        self.assertEqual(
            [candidate.tag for candidate in result.candidates],
            ["pretty-label"],
        )
        package = result.candidates[0].materialize().package
        self.assertEqual(package.archive_file_name, "Local_Scanner_Test-2.4.0.zip")
        result.release_sources()

    def test_unpackageable_latest_release_does_not_open_tag_fallback(self) -> None:
        with mock.patch.object(
            scanner,
            "_latest_github_release_tag",
            return_value="bad-symlink",
        ):
            result = self._scan()

        self.assertEqual(result.remote_tag_count, 1)
        self.assertEqual(
            [candidate.tag for candidate in result.candidates],
            ["bad-symlink"],
        )
        with self.assertRaises(scanner.TerminalTagError):
            result.candidates[0].materialize()
        result.release_sources()

    def test_known_unpublished_release_does_not_open_tag_fallback(self) -> None:
        known_tags = {
            "pretty-label": scanner.TagUpdate(
                ref_oid=self.annotated_ref_oid,
                commit_oid=self.valid_commit_oid,
                archive_file_name=None,
            )
        }
        with mock.patch.object(
            scanner,
            "_latest_github_release_tag",
            return_value="pretty-label",
        ):
            result = self._scan(known_tags)

        self.assertEqual(result.remote_tag_count, 1)
        self.assertEqual(result.candidates, ())
        self.assertEqual(dict(result.tag_updates), {})

    def test_builds_deterministic_zip_and_rejects_symlink_tag(self) -> None:
        result = self._scan()
        candidates = {candidate.tag: candidate for candidate in result.candidates}

        self.assertEqual(result.remote_tag_count, 3)
        self.assertEqual(
            set(candidates),
            {"bad-symlink", "pretty-label", "release-channel"},
        )
        self.assertEqual(list(self.scan_root.rglob("*.zip")), [])
        self.assertEqual(candidates["release-channel"].metadata.version, "2.4.0")
        release_candidate = candidates["release-channel"].materialize()
        annotated_candidate = candidates["pretty-label"].materialize()
        self.assertEqual(
            release_candidate.package.archive_file_name,
            "Local_Scanner_Test-2.4.0.zip",
        )
        self.assertNotEqual("release-channel", "2.4.0")
        self.assertEqual(
            annotated_candidate.archive_path.read_bytes(),
            release_candidate.archive_path.read_bytes(),
        )

        root_name = "Local_Scanner_Test-2.4.0"
        with zipfile.ZipFile(release_candidate.archive_path) as archive:
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
            result.tag_updates["bad-symlink"].commit_oid, self.symlink_commit_oid
        )
        self.assertEqual(result.issues, ())
        with self.assertRaisesRegex(scanner.TerminalTagError, "symlink"):
            candidates["bad-symlink"].materialize()

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
        published = next(
            candidate
            for candidate in first.candidates
            if candidate.tag == "release-channel"
        ).materialize()
        known_tags["release-channel"] = scanner.TagUpdate(
            ref_oid=published.tag_ref_oid,
            commit_oid=published.tag_commit_oid,
            archive_file_name=published.package.archive_file_name,
        )
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
