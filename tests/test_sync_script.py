from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_FROM_ROOT = Path("scripts/sync-library-index.sh")


def _working_bash() -> str | None:
    candidates: list[Path] = []
    if os.name == "nt":
        for parent in (
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramW6432"),
            os.environ.get("ProgramFiles(x86)"),
        ):
            if parent:
                candidates.append(Path(parent) / "Git" / "bin" / "bash.exe")
                candidates.append(Path(parent) / "Git" / "usr" / "bin" / "bash.exe")
    else:
        discovered = shutil.which("bash")
        if discovered:
            candidates.append(Path(discovered))

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen or not candidate.is_file():
            continue
        seen.add(candidate)
        try:
            probe = subprocess.run(
                [str(candidate), "-c", "exit 0"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return str(candidate)
    return None


BASH = _working_bash()


FAKE_PYTHON = r"""#!/usr/bin/env bash
set -eu

printf 'python:%s\n' "$*" >> "$COMMAND_LOG"

if [[ "$1" == "-m" && "$2" == "aily_coder_libraries.sync" ]]; then
  count=0
  if [[ -f "$SYNC_COUNT_FILE" ]]; then
    count="$(< "$SYNC_COUNT_FILE")"
  fi
  count=$((count + 1))
  printf '%s\n' "$count" > "$SYNC_COUNT_FILE"

  index=1
  for status in $SYNC_STATUSES; do
    if [[ "$index" -eq "$count" ]]; then
      exit "$status"
    fi
    index=$((index + 1))
  done
  exit 99
fi

exit 98
"""


FAKE_SLEEP = r"""#!/usr/bin/env bash
set -eu
printf 'sleep:%s\n' "$*" >> "$COMMAND_LOG"
"""


@unittest.skipUnless(BASH, "a working Bash installation is required")
class SyncScriptTests(unittest.TestCase):
    maxDiff = None

    def _write_executable(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8", newline="\n")
        path.chmod(0o755)

    def _run_script(
        self,
        statuses: tuple[int, ...],
        *,
        arguments: tuple[str, ...] = (),
        environment_overrides: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        with tempfile.TemporaryDirectory(
            prefix=".sync-script-test-", dir=REPOSITORY_ROOT
        ) as directory:
            temporary_root = Path(directory)
            fake_bin = temporary_root / "bin"
            fake_bin.mkdir()
            self._write_executable(fake_bin / "python", FAKE_PYTHON)
            self._write_executable(fake_bin / "sleep", FAKE_SLEEP)

            relative_temporary_root = temporary_root.relative_to(
                REPOSITORY_ROOT
            ).as_posix()
            environment = os.environ.copy()
            for name in (
                "FULL_BOOTSTRAP",
                "PYTHON_BIN",
                "RUSTFS_ENDPOINT",
                "RUSTFS_ACCESS_KEY_ID",
                "RUSTFS_ACCESS_KEY",
                "RUSTFS_SECRET_ACCESS_KEY",
                "RUSTFS_SECRET_KEY",
                "RUSTFS_PACKAGE_BUCKET",
                "R2_ACCOUNT_ID",
                "R2_ENDPOINT",
                "R2_ACCESS_KEY_ID",
                "R2_SECRET_ACCESS_KEY",
                "R2_PACKAGE_BUCKET",
            ):
                environment.pop(name, None)
            environment.update(
                {
                    "COMMAND_LOG": f"./{relative_temporary_root}/commands.log",
                    "PYTHON_BIN": "python",
                    "SYNC_COUNT_FILE": (
                        f"./{relative_temporary_root}/sync-count.txt"
                    ),
                    "SYNC_STATUSES": " ".join(str(status) for status in statuses),
                }
            )
            environment.update(environment_overrides or {})

            # Prefix PATH after Git Bash has converted the inherited Windows PATH.
            # The script is launched from a child directory to also verify that it
            # changes to the repository root before running project commands.
            wrapper = (
                'fake_bin="$1"; shift; '
                'PATH="$fake_bin:$PATH"; export PATH; exec bash "$@"'
            )
            result = subprocess.run(
                [
                    BASH,
                    "-c",
                    wrapper,
                    "sync-script-test",
                    f"./{relative_temporary_root}/bin",
                    f"../{SCRIPT_FROM_ROOT.as_posix()}",
                    *arguments,
                ],
                cwd=temporary_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )

            command_log = temporary_root / "commands.log"
            lines = (
                command_log.read_text(encoding="utf-8").splitlines()
                if command_log.exists()
                else []
            )
            return result, lines

    def assert_result(
        self,
        result: subprocess.CompletedProcess[str],
        expected_status: int,
    ) -> None:
        self.assertEqual(
            result.returncode,
            expected_status,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_single_batch_runs_sync_once_with_defaults(self) -> None:
        result, lines = self._run_script((0,))

        self.assert_result(result, 0)
        self.assertEqual(
            lines,
            [
                "python:-m aily_coder_libraries.sync "
                "--max-repositories 250 --workers 4",
            ],
        )

    def test_sync_retries_until_success(self) -> None:
        result, lines = self._run_script((9, 9, 0))

        self.assert_result(result, 0)
        self.assertEqual(
            lines,
            [
                "python:-m aily_coder_libraries.sync "
                "--max-repositories 250 --workers 4",
                "sleep:15",
                "python:-m aily_coder_libraries.sync "
                "--max-repositories 250 --workers 4",
                "sleep:30",
                "python:-m aily_coder_libraries.sync "
                "--max-repositories 250 --workers 4",
            ],
        )

    def test_command_line_batch_settings_override_environment(self) -> None:
        result, lines = self._run_script(
            (0,),
            arguments=("--max-repositories", "12", "--workers", "2"),
            environment_overrides={
                "MAX_REPOSITORIES_PER_RUN": "99",
                "SCAN_WORKERS": "3",
            },
        )

        self.assert_result(result, 0)
        self.assertEqual(
            lines[-1],
            "python:-m aily_coder_libraries.sync "
            "--max-repositories 12 --workers 2",
        )

    def test_sync_retry_exhaustion_returns_last_status(self) -> None:
        result, lines = self._run_script((9, 8, 7))

        self.assert_result(result, 7)
        self.assertEqual(
            lines,
            [
                "python:-m aily_coder_libraries.sync "
                "--max-repositories 250 --workers 4",
                "sleep:15",
                "python:-m aily_coder_libraries.sync "
                "--max-repositories 250 --workers 4",
                "sleep:30",
                "python:-m aily_coder_libraries.sync "
                "--max-repositories 250 --workers 4",
            ],
        )

    def test_full_bootstrap_loops_on_75_without_retry_delay(self) -> None:
        result, lines = self._run_script(
            (75, 75, 0), environment_overrides={"FULL_BOOTSTRAP": "true"}
        )

        self.assert_result(result, 0)
        sync_line = (
            "python:-m aily_coder_libraries.sync --max-repositories 250 "
            "--workers 4 --require-bootstrap-complete"
        )
        self.assertEqual(
            lines,
            [
                sync_line,
                sync_line,
                sync_line,
            ],
        )

    def test_full_bootstrap_retries_then_returns_non_75_failure(self) -> None:
        result, lines = self._run_script(
            (75, 9, 9, 7), arguments=("--full-bootstrap",)
        )

        self.assert_result(result, 7)
        sync_line = (
            "python:-m aily_coder_libraries.sync --max-repositories 250 "
            "--workers 4 --require-bootstrap-complete"
        )
        self.assertEqual(
            lines,
            [
                sync_line,
                sync_line,
                "sleep:15",
                sync_line,
                "sleep:30",
                sync_line,
            ],
        )


if __name__ == "__main__":
    unittest.main()
