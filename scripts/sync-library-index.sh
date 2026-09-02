#!/usr/bin/env bash

set -euo pipefail
set +x

readonly BOOTSTRAP_INCOMPLETE_EXIT_CODE=75
readonly REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

full_bootstrap="${FULL_BOOTSTRAP:-false}"
max_repositories="${MAX_REPOSITORIES_PER_RUN:-250}"
workers="${SCAN_WORKERS:-4}"
python_bin="${PYTHON_BIN:-python}"

usage() {
  cat <<'EOF'
Usage: bash scripts/sync-library-index.sh [options]

Options:
  --full-bootstrap          Run checkpoint batches until bootstrap completes.
  --max-repositories VALUE  Repositories per checkpoint batch (default: 250).
  --workers VALUE           Concurrent scans and uploads (default: 4).
  -h, --help                Show this help message.

The storage credentials and public download URLs are read from the same
environment variables as the GitHub Actions workflow.
EOF
}

while (($# > 0)); do
  case "$1" in
    --full-bootstrap)
      full_bootstrap=true
      ;;
    --max-repositories)
      if (($# < 2)); then
        echo "Missing value for --max-repositories" >&2
        exit 2
      fi
      max_repositories="$2"
      shift
      ;;
    --workers)
      if (($# < 2)); then
        echo "Missing value for --workers" >&2
        exit 2
      fi
      workers="$2"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [[ "$full_bootstrap" != "true" && "$full_bootstrap" != "false" ]]; then
  echo "FULL_BOOTSTRAP must be true or false" >&2
  exit 2
fi

cd "$REPOSITORY_ROOT"

run_sync_with_retries() {
  local attempt sync_status delay_seconds
  for attempt in 1 2 3; do
    if PYTHONPATH=src "$python_bin" -m aily_coder_libraries.sync \
      --max-repositories "$max_repositories" \
      --workers "$workers" \
      "$@"; then
      return 0
    else
      sync_status=$?
    fi

    if [[ "$sync_status" -eq "$BOOTSTRAP_INCOMPLETE_EXIT_CODE" ]]; then
      return "$BOOTSTRAP_INCOMPLETE_EXIT_CODE"
    fi
    if [[ "$attempt" -eq 3 ]]; then
      return "$sync_status"
    fi

    delay_seconds=$((attempt * 15))
    echo "Sync failed with exit code $sync_status; retrying in ${delay_seconds}s (attempt $((attempt + 1))/3)"
    sleep "$delay_seconds"
  done
}

if [[ "$full_bootstrap" == "true" ]]; then
  while true; do
    if run_sync_with_retries --require-bootstrap-complete; then
      break
    else
      sync_status=$?
    fi
    if [[ "$sync_status" -ne "$BOOTSTRAP_INCOMPLETE_EXIT_CODE" ]]; then
      exit "$sync_status"
    fi
  done
else
  run_sync_with_retries
fi
