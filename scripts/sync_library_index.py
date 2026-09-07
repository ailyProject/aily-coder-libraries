from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path


BOOTSTRAP_INCOMPLETE_EXIT_CODE = 75
MAX_SYNC_ATTEMPTS = 3
MAX_WORKERS = 4
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PROXY_ENVIRONMENT_NAMES = (
    "http_proxy",
    "HTTP_PROXY",
    "https_proxy",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
)


def _non_negative_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return parsed


def _worker_count(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= MAX_WORKERS:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_WORKERS}")
    return parsed


def _full_bootstrap_default() -> bool:
    value = os.environ.get("FULL_BOOTSTRAP", "false").strip().lower()
    if value not in {"true", "false"}:
        raise ValueError("FULL_BOOTSTRAP must be true or false")
    return value == "true"


def _load_environment_file(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise ValueError(f"cannot read environment file {path}: {exc}") from exc

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or ENVIRONMENT_NAME_PATTERN.fullmatch(name) is None:
            raise ValueError(
                f"{path}:{line_number}: expected an environment entry as NAME=VALUE"
            )
        os.environ.setdefault(name, value.strip())


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Synchronise the Aily Coder library index manually"
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="load NAME=VALUE settings from a local file",
    )
    parser.add_argument(
        "--full-bootstrap",
        action="store_true",
        default=None,
        help="run checkpoint batches until bootstrap completes",
    )
    parser.add_argument(
        "--max-repositories",
        type=_non_negative_integer,
        default=None,
        help="repositories per checkpoint batch (default: 250)",
    )
    parser.add_argument(
        "--workers",
        type=_worker_count,
        default=None,
        help=f"concurrent scans and uploads (default: 4, maximum: {MAX_WORKERS})",
    )
    return parser


def _read_global_git_proxy() -> str | None:
    try:
        result = subprocess.run(
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
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return None

    if result.returncode != 0:
        return None
    proxy = result.stdout.strip()
    return proxy or None


def _git_proxy_config_index(environment: dict[str, str]) -> int | None:
    raw_count = environment.get("GIT_CONFIG_COUNT", "0")
    try:
        count = int(raw_count or "0")
    except ValueError:
        return None
    if count < 0:
        return None

    for index in range(count):
        key_name = f"GIT_CONFIG_KEY_{index}"
        value_name = f"GIT_CONFIG_VALUE_{index}"
        if key_name not in environment or value_name not in environment:
            return None
        if environment[key_name].casefold() == "http.proxy":
            return None
    return count


def _sync_environment() -> dict[str, str]:
    environment = os.environ.copy()
    source_path = str(REPOSITORY_ROOT / "src")
    existing_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        os.pathsep.join((source_path, existing_python_path))
        if existing_python_path
        else source_path
    )
    if not any(name in environment for name in PROXY_ENVIRONMENT_NAMES):
        config_index = _git_proxy_config_index(environment)
        if config_index is not None:
            proxy = _read_global_git_proxy()
            if proxy is not None:
                # Pass only the proxy into Git's command scope. The scanner can
                # keep isolating credentials and URL rewrites, and storage SDKs
                # do not see a process-wide HTTP_PROXY/HTTPS_PROXY setting.
                environment["GIT_CONFIG_COUNT"] = str(config_index + 1)
                environment[f"GIT_CONFIG_KEY_{config_index}"] = "http.proxy"
                environment[f"GIT_CONFIG_VALUE_{config_index}"] = proxy
    return environment


def run_sync_with_retries(command: list[str]) -> int:
    environment = _sync_environment()
    for attempt in range(1, MAX_SYNC_ATTEMPTS + 1):
        status = subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=False,
        ).returncode
        if status in {0, BOOTSTRAP_INCOMPLETE_EXIT_CODE}:
            return status
        if attempt == MAX_SYNC_ATTEMPTS:
            return status

        delay_seconds = attempt * 15
        print(
            f"Sync failed with exit code {status}; retrying in "
            f"{delay_seconds}s (attempt {attempt + 1}/{MAX_SYNC_ATTEMPTS})",
            flush=True,
        )
        time.sleep(delay_seconds)

    raise AssertionError("retry loop did not return")


def main(argv: list[str] | None = None) -> int:
    parser = _build_argument_parser()
    args = parser.parse_args(argv)
    if args.env_file is not None:
        try:
            _load_environment_file(args.env_file)
        except ValueError as exc:
            parser.error(str(exc))

    try:
        full_bootstrap = (
            args.full_bootstrap
            if args.full_bootstrap is not None
            else _full_bootstrap_default()
        )
        max_repositories = (
            args.max_repositories
            if args.max_repositories is not None
            else _non_negative_integer(
                os.environ.get("MAX_REPOSITORIES_PER_RUN", "250")
            )
        )
        workers = (
            args.workers
            if args.workers is not None
            else _worker_count(os.environ.get("SCAN_WORKERS", "4"))
        )
    except (ValueError, argparse.ArgumentTypeError) as exc:
        parser.error(str(exc))

    if full_bootstrap and max_repositories == 0:
        parser.error("--max-repositories must be greater than 0 for full bootstrap")

    command = [
        sys.executable,
        "-m",
        "aily_coder_libraries.sync",
        "--max-repositories",
        str(max_repositories),
        "--workers",
        str(workers),
    ]
    if full_bootstrap:
        command.append("--require-bootstrap-complete")

    while True:
        status = run_sync_with_retries(command)
        if status != BOOTSTRAP_INCOMPLETE_EXIT_CODE:
            return status
        if not full_bootstrap:
            return status


if __name__ == "__main__":
    raise SystemExit(main())
