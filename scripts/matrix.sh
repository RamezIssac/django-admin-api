#!/usr/bin/env bash
# Run the suite across supported Django versions and the lowest/latest allowed MCP SDK.
#   scripts/matrix.sh            # full matrix
#   scripts/matrix.sh -k mcp     # extra pytest args are passed through
set -euo pipefail
cd "$(dirname "$0")/.."

DJANGOS=("django>=4.2,<5" "django>=5.2,<6" "django>=6.1,<6.2")
MCPS=("mcp==2.3.0" "mcp>=2.3,<3")

status=0
for django in "${DJANGOS[@]}"; do
  for mcp in "${MCPS[@]}"; do
    printf '%-18s %-14s ' "$django" "$mcp"
    if out=$(uv run --quiet --with "$django" --with "$mcp" pytest -q "$@" 2>&1); then
      echo "$out" | tail -1
    else
      echo "FAILED"; echo "$out" | tail -30; status=1
    fi
  done
done
exit $status
