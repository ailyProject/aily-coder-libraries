from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "sync_library_index.py"
SPEC = importlib.util.spec_from_file_location("manual_sync_script", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load {SCRIPT_PATH}")
sync_script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync_script)


def _completed(status: int) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess([], status)


class GitProxyLookupTests(unittest.TestCase):
    def test_reads_global_http_proxy_without_logging_it(self) -> None:
        completed = subprocess.CompletedProcess(
            [], 0, stdout="http://proxy.example.invalid:7890\n"
        )
        with patch.object(
            sync_script.subprocess, "run", return_value=completed
        ) as run:
            proxy = sync_script._read_global_git_proxy()

        self.assertEqual(proxy, "http://proxy.example.invalid:7890")
        run.assert_called_once_with(
            ["git", "config", "--global", "--get", "http.proxy"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            timeout=5,
            check=False,
            text=True,
            encoding="utf-8",
            errors="strict",
        )

    def test_missing_or_unreadable_global_proxy_is_ignored(self) -> None:
        for result in (
            subprocess.CompletedProcess([], 1, stdout=""),
            OSError("git unavailable"),
        ):
            with self.subTest(result=type(result).__name__), patch.object(
                sync_script.subprocess,
                "run",
                side_effect=result if isinstance(result, OSError) else None,
                return_value=result if not isinstance(result, OSError) else None,
            ):
                self.assertIsNone(sync_script._read_global_git_proxy())


class SyncScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        proxy_reader = patch.object(
            sync_script, "_read_global_git_proxy", return_value=None
        )
        self.read_global_git_proxy = proxy_reader.start()
        self.addCleanup(proxy_reader.stop)

    def test_sync_environment_passes_global_proxy_only_to_git(self) -> None:
        self.read_global_git_proxy.return_value = (
            "http://proxy.example.invalid:7890"
        )
        with patch.dict(os.environ, {}, clear=True):
            environment = sync_script._sync_environment()

        self.assertEqual(environment["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(environment["GIT_CONFIG_KEY_0"], "http.proxy")
        self.assertEqual(
            environment["GIT_CONFIG_VALUE_0"],
            "http://proxy.example.invalid:7890",
        )
        self.assertNotIn("HTTP_PROXY", environment)
        self.assertNotIn("HTTPS_PROXY", environment)

    def test_explicit_proxy_environment_is_not_overridden(self) -> None:
        for name in sync_script.PROXY_ENVIRONMENT_NAMES:
            with self.subTest(name=name), patch.dict(
                os.environ,
                {name: "http://explicit.example.invalid:8080"},
                clear=True,
            ):
                self.read_global_git_proxy.reset_mock()
                environment = sync_script._sync_environment()

                inherited_value = next(
                    value
                    for key, value in environment.items()
                    if key.casefold() == name.casefold()
                )
                self.assertEqual(
                    inherited_value, "http://explicit.example.invalid:8080"
                )
                self.assertNotIn("GIT_CONFIG_COUNT", environment)
                self.read_global_git_proxy.assert_not_called()

    def test_existing_git_proxy_runtime_config_is_not_overridden(self) -> None:
        inherited = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.proxy",
            "GIT_CONFIG_VALUE_0": "http://explicit.example.invalid:8080",
        }
        with patch.dict(os.environ, inherited, clear=True):
            environment = sync_script._sync_environment()

        for name, value in inherited.items():
            self.assertEqual(environment[name], value)
        self.read_global_git_proxy.assert_not_called()

    def test_global_proxy_appends_to_existing_git_runtime_config(self) -> None:
        inherited = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "*",
        }
        self.read_global_git_proxy.return_value = (
            "http://proxy.example.invalid:7890"
        )
        with patch.dict(os.environ, inherited, clear=True):
            environment = sync_script._sync_environment()

        for name, value in inherited.items():
            if name != "GIT_CONFIG_COUNT":
                self.assertEqual(environment[name], value)
        self.assertEqual(environment["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(environment["GIT_CONFIG_KEY_1"], "http.proxy")
        self.assertEqual(
            environment["GIT_CONFIG_VALUE_1"],
            "http://proxy.example.invalid:7890",
        )

    def test_single_batch_runs_sync_once_with_defaults(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                sync_script, "run_sync_with_retries", return_value=0
            ) as run_sync,
        ):
            status = sync_script.main([])

        self.assertEqual(status, 0)
        run_sync.assert_called_once_with(
            [
                sys.executable,
                "-m",
                "aily_coder_libraries.sync",
                "--max-repositories",
                "250",
                "--workers",
                "4",
            ]
        )

    def test_command_line_batch_settings_override_environment(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "MAX_REPOSITORIES_PER_RUN": "99",
                    "SCAN_WORKERS": "3",
                },
                clear=True,
            ),
            patch.object(
                sync_script, "run_sync_with_retries", return_value=0
            ) as run_sync,
        ):
            status = sync_script.main(
                ["--max-repositories", "12", "--workers", "2"]
            )

        self.assertEqual(status, 0)
        command = run_sync.call_args.args[0]
        self.assertEqual(
            command[-4:],
            ["--max-repositories", "12", "--workers", "2"],
        )

    def test_environment_file_supplies_local_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment_file = Path(directory) / ".env.sync"
            environment_file.write_text(
                "FULL_BOOTSTRAP=true\n"
                "MAX_REPOSITORIES_PER_RUN=12\n"
                "SCAN_WORKERS=2\n"
                "RUSTFS_ENDPOINT=https://storage.example.invalid\n"
                "R2_ACCESS_KEY_ID=from-file\n",
                encoding="utf-8",
            )
            with (
                patch.dict(
                    os.environ,
                    {"R2_ACCESS_KEY_ID": "from-shell"},
                    clear=True,
                ),
                patch.object(
                    sync_script, "run_sync_with_retries", return_value=0
                ) as run_sync,
            ):
                status = sync_script.main(
                    ["--env-file", str(environment_file)]
                )
                self.assertEqual(
                    os.environ["RUSTFS_ENDPOINT"],
                    "https://storage.example.invalid",
                )
                self.assertEqual(os.environ["R2_ACCESS_KEY_ID"], "from-shell")

        self.assertEqual(status, 0)
        command = run_sync.call_args.args[0]
        self.assertEqual(
            command[-5:],
            [
                "--max-repositories",
                "12",
                "--workers",
                "2",
                "--require-bootstrap-complete",
            ],
        )

    def test_sync_retries_until_success(self) -> None:
        command = [sys.executable, "-m", "aily_coder_libraries.sync"]
        with (
            patch.object(
                sync_script.subprocess,
                "run",
                side_effect=(_completed(9), _completed(9), _completed(0)),
            ) as run,
            patch.object(sync_script.time, "sleep") as sleep,
        ):
            status = sync_script.run_sync_with_retries(command)

        self.assertEqual(status, 0)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(15), call(30)])
        for invocation in run.call_args_list:
            self.assertEqual(invocation.args[0], command)
            self.assertEqual(invocation.kwargs["cwd"], REPOSITORY_ROOT)
            python_path = invocation.kwargs["env"]["PYTHONPATH"]
            self.assertEqual(
                python_path.split(os.pathsep)[0],
                str(REPOSITORY_ROOT / "src"),
            )

    def test_sync_retry_exhaustion_returns_last_status(self) -> None:
        with (
            patch.object(
                sync_script.subprocess,
                "run",
                side_effect=(_completed(9), _completed(8), _completed(7)),
            ) as run,
            patch.object(sync_script.time, "sleep") as sleep,
        ):
            status = sync_script.run_sync_with_retries(["sync"])

        self.assertEqual(status, 7)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(15), call(30)])

    def test_bootstrap_incomplete_status_is_not_retried(self) -> None:
        with (
            patch.object(
                sync_script.subprocess,
                "run",
                return_value=_completed(
                    sync_script.BOOTSTRAP_INCOMPLETE_EXIT_CODE
                ),
            ) as run,
            patch.object(sync_script.time, "sleep") as sleep,
        ):
            status = sync_script.run_sync_with_retries(["sync"])

        self.assertEqual(
            status, sync_script.BOOTSTRAP_INCOMPLETE_EXIT_CODE
        )
        run.assert_called_once()
        sleep.assert_not_called()

    def test_full_bootstrap_loops_on_75_until_complete(self) -> None:
        with (
            patch.dict(os.environ, {"FULL_BOOTSTRAP": "true"}, clear=True),
            patch.object(
                sync_script,
                "run_sync_with_retries",
                side_effect=(75, 75, 0),
            ) as run_sync,
        ):
            status = sync_script.main([])

        self.assertEqual(status, 0)
        self.assertEqual(run_sync.call_count, 3)
        command = run_sync.call_args.args[0]
        self.assertEqual(command[-1], "--require-bootstrap-complete")

    def test_full_bootstrap_returns_non_75_failure(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                sync_script,
                "run_sync_with_retries",
                side_effect=(75, 7),
            ) as run_sync,
        ):
            status = sync_script.main(["--full-bootstrap"])

        self.assertEqual(status, 7)
        self.assertEqual(run_sync.call_count, 2)


if __name__ == "__main__":
    unittest.main()
