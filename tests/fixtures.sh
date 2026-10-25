#!/usr/bin/env bash
# Offline region-filter test: run the public profile against recorded feeds
# (tests/fixtures/) on an internal Docker network and check each result.
#   healthy   every validated check passes; the web check is 2xx
#   degraded  exactly the checks in DEGRADED_FAILS fail; the others pass; the web check is 4xx
set -euo pipefail

# shellcheck source=tests/lib.sh
. "$(dirname "$0")/lib.sh"
cd "$ARGUS_ROOT/tests"

DEGRADED_FAILS=(
  snowflake.overall.global
  snowflake.azure.eastus2
  databricks.overall.global
  databricks.compute.eastus
  hashicorp.overall.global
  hashicorp.vault-dedicated.eastus2
)

PROJECT=""
cleanup() {
  if [ -n "$PROJECT" ]; then
    docker compose -f compose.fixtures.yaml -p "$PROJECT" down -v >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

expected_result() {
  local variant="$1" id="$2" failing
  if [ "$variant" = degraded ]; then
    for failing in "${DEGRADED_FAILS[@]}"; do
      if [ "$failing" = "$id" ]; then
        printf 'failed\n'
        return
      fi
    done
  fi
  printf 'passed\n'
}

run_variant() {
  local variant="$1" expected_class="$2" out metrics results id want got class i=0 errors=0
  out="$(new_out_dir)"
  metrics="$out/metrics.jsonl"
  PROJECT="argus-fixtures-$variant"

  FIXTURE_VARIANT="$variant" ARGUS_OUT="$out" \
    docker compose -f compose.fixtures.yaml -p "$PROJECT" up -d --quiet-pull >/dev/null 2>&1

  while [ "$i" -lt 90 ] && ! has_all_validations "$metrics"; do
    sleep 1
    i=$((i + 1))
  done

  results="$(validation_results "$metrics")"
  for id in "${VALIDATED_CHECKS[@]}"; do
    want="$(expected_result "$variant" "$id")"
    got="$(printf '%s\n' "$results" | awk -F '\t' -v id="$id" '$1 == id { print $2 }')"
    if [ "$got" != "$want" ]; then
      printf '  %s: want %s, got %s\n' "$id" "$want" "${got:-nothing}"
      errors=$((errors + 1))
    fi
  done

  class="$(status_class "$metrics" "$WEB_CHECK")"
  if [ "$class" != "$expected_class" ]; then
    printf '  %s: want %s, got %s\n' "$WEB_CHECK" "$expected_class" "${class:-nothing}"
    errors=$((errors + 1))
  fi

  FIXTURE_VARIANT="$variant" ARGUS_OUT="$out" \
    docker compose -f compose.fixtures.yaml -p "$PROJECT" down -v >/dev/null 2>&1 || true
  PROJECT=""

  if [ "$errors" -eq 0 ]; then
    pass "$variant"
  else
    fail "$variant" "$errors check(s) did not match"
  fi
}

run_variant healthy 2xx
run_variant degraded 4xx

if [ "$FAILURES" -gt 0 ]; then
  exit 1
fi
printf 'all variants passed\n'
