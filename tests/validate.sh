#!/usr/bin/env bash
# Validate every Argus profile without real credentials.
#   public          validate with no environment at all
#   full            validate with tests/placeholder.env
#   splunk-example  uncomment the splunk_hec block in a copy of base.yaml, validate
#   full-startup    start the full profile with placeholders; it must reach "Everything is ready"
#                   (validate alone misses an extension that is defined but not listed)
#   compose         compose.yaml renders, including the full profile
#   layer-lists     lib.sh, both Compose services, and the Deployment list the same layers
#   kustomize       the Kubernetes manifests render with a hashed ConfigMap
#   naming          no file still uses the project's earlier, discarded name
set -euo pipefail

# shellcheck source=tests/lib.sh
. "$(dirname "$0")/lib.sh"
cd "$ARGUS_ROOT"

PLACEHOLDERS="$ARGUS_ROOT/tests/placeholder.env"

step_validate() {
  local name="$1" config_dir="$2" env_file="$3"
  shift 3
  local output
  if output="$(collector_validate "$config_dir" "$env_file" "$@" 2>&1)"; then
    pass "$name"
  else
    fail "$name" "$(printf '%s' "$output" | tail -3 | tr '\n' ' ')"
  fi
}

step_validate public "$ARGUS_ROOT/config" - "${PUBLIC_LAYERS[@]}"
step_validate full "$ARGUS_ROOT/config" "$PLACEHOLDERS" "${FULL_LAYERS[@]}"

splunk_dir="$(new_out_dir)"
cp "$ARGUS_ROOT"/config/*.yaml "$splunk_dir"/ 2>/dev/null || true
if [ -f "$splunk_dir/base.yaml" ] && grep -q '^# BEGIN splunk_hec' "$splunk_dir/base.yaml"; then
  awk '/^# BEGIN splunk_hec/ { inside = 1; print; next }
       /^# END splunk_hec/   { inside = 0; print; next }
       inside                { sub(/^# /, ""); print; next }
                             { print }' "$ARGUS_ROOT/config/base.yaml" > "$splunk_dir/base.yaml"
  step_validate splunk-example "$splunk_dir" "$PLACEHOLDERS" "${FULL_LAYERS[@]}"
else
  fail splunk-example "no '# BEGIN splunk_hec' block in config/base.yaml"
fi

startup_name="argus-validate-startup"
startup_out="$(new_out_dir)"
if run_detached "$startup_name" "$PLACEHOLDERS" "$startup_out" "${FULL_LAYERS[@]}" &&
  wait_for_log "$startup_name" 'Everything is ready' 60; then
  pass full-startup
else
  fail full-startup "collector did not reach 'Everything is ready' within 60 s"
fi
docker rm -f "$startup_name" >/dev/null 2>&1 || true

# The layer order lives in four places: lib.sh, both Compose services, and the
# Kubernetes Deployment. They must not drift.
expect_layers() {
  local name="$1" actual="$2"
  shift 2
  local expected
  expected="$(printf '%s\n' "$@")"
  if [ "$actual" = "$expected" ]; then
    pass "$name"
  else
    fail "$name" "layers '$(printf '%s' "$actual" | tr '\n' ' ')' differ from '$(printf '%s' "$expected" | tr '\n' ' ')'"
  fi
}

if compose_json="$(docker compose -f compose.yaml --profile full config --format json 2>&1)"; then
  pass compose
  layers_of() {
    printf '%s' "$compose_json" | jq -r --arg s "$1" '.services[$s].command[]' |
      sed -n 's#^--config=/etc/argus/##p'
  }
  expect_layers layer-lists:compose-argus "$(layers_of argus)" "${PUBLIC_LAYERS[@]}"
  expect_layers layer-lists:compose-argus-full "$(layers_of argus-full)" "${FULL_LAYERS[@]}"
else
  fail compose "$(printf '%s' "$compose_json" | tail -2 | tr '\n' ' ')"
fi

expect_layers layer-lists:k8s-deployment \
  "$(sed -n 's#^ *- --config=/etc/argus/##p' deploy/k8s/deployment.yaml)" "${FULL_LAYERS[@]}"

if rendered="$(kubectl kustomize . 2>&1)" && printf '%s' "$rendered" | grep -q 'name: argus-config-'; then
  pass kustomize
else
  fail kustomize "$(printf '%s' "$rendered" | tail -2 | tr '\n' ' ')"
fi

# The project was briefly named after the shield; no trace of that name may remain.
# The bracket keeps this pattern from matching itself.
if old_name="$(grep -rniI 'ae[g]is' . --exclude-dir=.git --exclude-dir=.claude --exclude-dir=.out)"; then
  fail naming "old project name found: $(printf '%s\n' "$old_name" | head -3 | tr '\n' ' ')"
else
  pass naming
fi

if [ "$FAILURES" -gt 0 ]; then
  printf '%d step(s) failed\n' "$FAILURES"
  exit 1
fi
printf 'all steps passed\n'
