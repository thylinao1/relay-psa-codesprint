#!/usr/bin/env bash
# run_case.sh: run ONE scored eval case by task id (see evalx/tasks.json), e.g.
#   bash evalx/run_case.sh no_policy_auto_deny
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
[ $# -ge 1 ] || { echo "usage: bash evalx/run_case.sh <task_id> [extra args]" >&2; exit 2; }
exec "$ROOT/.venv/bin/python" "$HERE/harness.py" --task "$@"
