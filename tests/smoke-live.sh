#!/usr/bin/env bash
# Live smoke test (needs network): run the public profile against the real feeds.
# Passes when every check reports a status and every validated check reports exactly
# one result. A "failed" result is printed but does not fail the test: a vendor can be
# degraded right now.
set -euo pipefail

# shellcheck source=tests/lib.sh
. "$(dirname "$0")/lib.sh"
cd "$ARGUS_ROOT"

NAME="argus-smoke-live"
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT

out="$(new_out_dir)"
metrics="$out/metrics.jsonl"
run_detached "$NAME" - "$out" "${PUBLIC_LAYERS[@]}"

i=0
while [ "$i" -lt 120 ] && ! has_all_validations "$metrics"; do
  sleep 1
  i=$((i + 1))
done

results="$(validation_results "$metrics")"
for id in "${VALIDATED_CHECKS[@]}" "$WEB_CHECK"; do
  class="$(status_class "$metrics" "$id")"
  if [ -z "$class" ]; then
    fail "$id" "no httpcheck.status point"
    continue
  fi
  if [ "$id" = "$WEB_CHECK" ]; then
    printf '%-36s %s\n' "$id" "$class"
    [ "$class" = 2xx ] || fail "$id" "status class $class"
    continue
  fi
  count="$(printf '%s\n' "$results" | awk -F '\t' -v id="$id" '$1 == id' | wc -l | tr -d ' ')"
  result="$(printf '%s\n' "$results" | awk -F '\t' -v id="$id" '$1 == id { print $2 }')"
  printf '%-36s %s %s\n' "$id" "$class" "${result:-none}"
  [ "$count" = 1 ] || fail "$id" "expected 1 validation result, got $count"
done

if [ "$FAILURES" -gt 0 ]; then
  exit 1
fi
printf 'smoke test passed\n'
