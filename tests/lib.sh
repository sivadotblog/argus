# Shared helpers for the Argus test scripts. Source it; do not run it.
# Bash 3.2 compatible (macOS default shell).

ARGUS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="otel/opentelemetry-collector-contrib:0.162.0"

# Profiles (research R8). Layers merge in this order; full.yaml is always last
# because it holds the one service.extensions list.
PUBLIC_LAYERS=(base.yaml public.yaml)
FULL_LAYERS=(base.yaml public.yaml azure.yaml atlas.yaml snowflake-logs.yaml full.yaml)

# The 12 public checks that carry a validation, and the web check (data-model.md).
VALIDATED_CHECKS=(
  snowflake.overall.global
  snowflake.azure.eastus
  snowflake.azure.eastus2
  databricks.overall.global
  databricks.compute.eastus
  databricks.compute.eastus2
  databricks.sql.eastus
  databricks.sql.eastus2
  hashicorp.overall.global
  hashicorp.vault-dedicated.eastus
  hashicorp.vault-dedicated.eastus2
  hashicorp.terraform.global
)
WEB_CHECK=web.siva-blog.global

FAILURES=0

pass() { printf 'PASS %s\n' "$1"; }
fail() {
  printf 'FAIL %s: %s\n' "$1" "$2"
  FAILURES=$((FAILURES + 1))
}

# config_args <layer...> -> --config=/etc/argus/<layer> per layer
config_args() {
  local layer
  for layer in "$@"; do
    printf -- '--config=/etc/argus/%s\n' "$layer"
  done
}

# new_out_dir -> path of a fresh, world-writable output directory
new_out_dir() {
  local dir="$ARGUS_ROOT/tests/.out/$(date +%Y%m%d-%H%M%S)-$RANDOM"
  mkdir -p "$dir"
  chmod 0777 "$dir"
  printf '%s\n' "$dir"
}

# collector_validate <config-dir> <env-file|-> <layer...>
collector_validate() {
  local config_dir="$1" env_file="$2"
  shift 2
  local env_args=()
  if [ "$env_file" != "-" ]; then
    env_args=(--env-file "$env_file")
  fi
  # shellcheck disable=SC2046
  docker run --rm ${env_args[@]+"${env_args[@]}"} \
    -v "$config_dir:/etc/argus:ro" \
    "$IMAGE" validate $(config_args "$@")
}

# run_detached <name> <env-file|-> <out-dir> <layer...>
run_detached() {
  local name="$1" env_file="$2" out_dir="$3"
  shift 3
  local env_args=()
  if [ "$env_file" != "-" ]; then
    env_args=(--env-file "$env_file")
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true
  # shellcheck disable=SC2046
  docker run -d --name "$name" ${env_args[@]+"${env_args[@]}"} \
    -v "$ARGUS_ROOT/config:/etc/argus:ro" \
    -v "$out_dir:/var/lib/argus" \
    "$IMAGE" $(config_args "$@") >/dev/null
}

# wait_for_log <name> <regex> <seconds> -> 0 on match; 1 on timeout or "Error:"
wait_for_log() {
  local name="$1" pattern="$2" seconds="$3" i=0 logs
  while [ "$i" -lt "$seconds" ]; do
    logs="$(docker logs "$name" 2>&1 || true)"
    if printf '%s' "$logs" | grep -qE "$pattern"; then
      return 0
    fi
    if printf '%s' "$logs" | grep -q 'Error:'; then
      printf '%s\n' "$logs" | grep 'Error:' | head -3 >&2
      return 1
    fi
    sleep 1
    i=$((i + 1))
  done
  return 1
}

# validation_results <metrics.jsonl> -> "<check-id>\t<passed|failed>", latest per check
validation_results() {
  [ -s "$1" ] || return 0
  jq -r -s '
    [ .[] | .resourceMetrics[]?.scopeMetrics[]?.metrics[]?
      | select(.name == "httpcheck.validation.passed" or .name == "httpcheck.validation.failed")
      | .name as $n
      | (.sum // .gauge).dataPoints[]
      | select((.asInt // "0") != "0")
      | { id: (.attributes[] | select(.key == "http.url") | .value.stringValue | split("#")[1]),
          result: ($n | sub("httpcheck.validation."; "")),
          t: .timeUnixNano } ]
    | group_by(.id) | map(max_by(.t)) | .[] | "\(.id)\t\(.result)"' "$1"
}

# status_class <metrics.jsonl> <check-id> -> latest http.status_class with value 1
status_class() {
  [ -s "$1" ] || return 0
  jq -r -s --arg id "$2" '
    [ .[] | .resourceMetrics[]?.scopeMetrics[]?.metrics[]?
      | select(.name == "httpcheck.status")
      | (.sum // .gauge).dataPoints[]
      | select((.asInt // "0") != "0")
      | select(any(.attributes[]; .key == "http.url" and (.value.stringValue | endswith("#" + $id))))
      | { class: (.attributes[] | select(.key == "http.status_class") | .value.stringValue),
          t: .timeUnixNano } ]
    | if length == 0 then empty else (max_by(.t) | .class) end' "$1"
}

# has_all_validations <metrics.jsonl> -> 0 when every validated check has a result
has_all_validations() {
  local results id
  results="$(validation_results "$1")"
  for id in "${VALIDATED_CHECKS[@]}"; do
    printf '%s\n' "$results" | grep -q "^$id	" || return 1
  done
  return 0
}
