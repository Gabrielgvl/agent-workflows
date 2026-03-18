#!/usr/bin/env bash

set -euo pipefail

output_path="${CODEX_REVIEW_OUTPUT_PATH:-codex-review.txt}"
review_base="${CODEX_REVIEW_BASE:-}"
review_model="${CODEX_REVIEW_MODEL:-gpt-5.4}"
review_timeout_seconds="${CODEX_REVIEW_TIMEOUT_SECONDS:-900}"
runner_temp_root="${RUNNER_TEMP:-$PWD/.tmp/codex-review}"
source_home="${HOME:-}"
source_auth_path="${source_home%/}/.codex/auth.json"
codex_bin=""
timeout_bin=""
stdbuf_bin=""

mkdir -p "$runner_temp_root"

require_command() {
  local name="$1"
  if ! command -v "$name" >/dev/null 2>&1; then
    printf 'ERROR: required command not found: %s\n' "$name" > "$output_path"
    cat "$output_path"
    exit 2
  fi
}

resolve_timeout_bin() {
  if command -v timeout >/dev/null 2>&1; then
    timeout_bin="$(command -v timeout)"
    return
  fi
  if command -v gtimeout >/dev/null 2>&1; then
    timeout_bin="$(command -v gtimeout)"
    return
  fi
  cat > "$output_path" <<'EOF'
ERROR: timeout command not found.
Install GNU coreutils timeout (or gtimeout) on this runner before running codex-pr-review.
EOF
  cat "$output_path"
  exit 2
}

resolve_stdbuf_bin() {
  if command -v stdbuf >/dev/null 2>&1; then
    stdbuf_bin="$(command -v stdbuf)"
    return
  fi
  if command -v gstdbuf >/dev/null 2>&1; then
    stdbuf_bin="$(command -v gstdbuf)"
    return
  fi
  stdbuf_bin=""
}

resolve_realpath() {
  python3 - "$1" <<'PY'
import os
import sys

print(os.path.realpath(sys.argv[1]))
PY
}

require_command codex
require_command mktemp
require_command tee
require_command sh
require_command python3
require_command git
resolve_timeout_bin
resolve_stdbuf_bin

codex_invoked_path="$(command -v codex)"
codex_realpath="$(resolve_realpath "$codex_invoked_path")"
codex_bin="$codex_invoked_path"
if [[ "$codex_invoked_path" == */.volta/bin/codex || "$codex_realpath" == */.volta/bin/volta-shim ]]; then
  volta_root="${VOLTA_HOME:-}"
  if [[ -z "$volta_root" ]]; then
    case "$codex_realpath" in
      */.volta/bin/volta-shim)
        volta_root="${codex_realpath%/bin/volta-shim}"
        ;;
      */.volta/bin/codex)
        volta_root="${codex_realpath%/bin/codex}"
        ;;
    esac
  fi
  if [[ -n "$volta_root" ]]; then
    volta_codex_bin="$volta_root/tools/image/packages/@openai/codex/bin/codex"
    if [[ -x "$volta_codex_bin" ]]; then
      codex_bin="$volta_codex_bin"
    fi
  fi
fi
if [[ ! -x "$codex_bin" ]]; then
  printf 'ERROR: resolved Codex binary is not executable: %s\n' "$codex_bin" > "$output_path"
  cat "$output_path"
  exit 2
fi

codex_home_parent="$(mktemp -d "${runner_temp_root%/}/codex-home.XXXXXX")"
codex_zdotdir="$(mktemp -d "${runner_temp_root%/}/codex-zdotdir.XXXXXX")"
codex_auth_path="$codex_home_parent/.codex/auth.json"

cleanup() {
  rm -rf "$codex_home_parent" "$codex_zdotdir"
}
trap cleanup EXIT

mkdir -p \
  "$codex_home_parent/.codex" \
  "$codex_home_parent/.config" \
  "$codex_home_parent/.cache" \
  "$codex_home_parent/.local/state"
chmod 700 \
  "$codex_home_parent" \
  "$codex_home_parent/.codex" \
  "$codex_zdotdir"
: > "$codex_home_parent/.codex/config.toml"
: > "$codex_zdotdir/.zshenv"

export HOME="$codex_home_parent"
export XDG_CONFIG_HOME="$codex_home_parent/.config"
export XDG_CACHE_HOME="$codex_home_parent/.cache"
export XDG_STATE_HOME="$codex_home_parent/.local/state"
export ZDOTDIR="$codex_zdotdir"

while IFS='=' read -r name _; do
  case "$name" in
    CODEX_*)
      unset "$name"
      ;;
  esac
done < <(env)

if [[ -z "$review_base" ]]; then
  cat > "$output_path" <<'EOF'
ERROR: CODEX_REVIEW_BASE is required.
EOF
  cat "$output_path"
  exit 2
fi

review_base_ref="refs/remotes/origin/${review_base}"
if ! git rev-parse --verify "$review_base_ref" >/dev/null 2>&1; then
  cat > "$output_path" <<EOF
ERROR: review base ref ${review_base_ref} is missing locally.
Ensure the workflow fetches the base branch into ${review_base_ref} before invoking codex review.
EOF
  cat "$output_path"
  exit 2
fi

head_sha="$(git rev-parse HEAD)"
base_sha="$(git rev-parse "$review_base_ref")"
if ! git merge-base "$head_sha" "$base_sha" >/dev/null 2>&1; then
  shallow_state="$(git rev-parse --is-shallow-repository 2>/dev/null || echo unknown)"
  cat > "$output_path" <<EOF
ERROR: unable to compute git merge-base between HEAD (${head_sha}) and ${review_base_ref} (${base_sha}).
Codex review requires a valid common ancestor between the checked out PR ref and the fetched base branch.
Repository shallow state: ${shallow_state}
Ensure the workflow fetches the full base branch history before invoking codex review.
EOF
  cat "$output_path"
  exit 2
fi

if [[ ! -f "$source_auth_path" ]]; then
  cat > "$output_path" <<EOF
ERROR: authenticated Codex session file not found at:
$source_auth_path
Run the workflow authentication step before invoking the isolated review wrapper.
EOF
  cat "$output_path"
  exit 2
fi

cp "$source_auth_path" "$codex_auth_path"
chmod 600 "$codex_auth_path"

review_cmd=(
  "$codex_bin"
  --sandbox read-only
  --ask-for-approval never
  --model "$review_model"
  --disable js_repl
  --disable multi_agent
  --disable shell_snapshot
  -c 'model_reasoning_effort="low"'
  review
  --base "origin/${review_base}"
)

if [[ -n "$stdbuf_bin" ]]; then
  review_cmd=("$stdbuf_bin" -oL -eL "${review_cmd[@]}")
fi

set +e
"$timeout_bin" --signal=TERM --kill-after=30s "${review_timeout_seconds}s" \
  "${review_cmd[@]}" 2>&1 | tee "$output_path"
rc=${PIPESTATUS[0]}
set -e

if [[ "$rc" -eq 124 || "$rc" -eq 137 ]]; then
  printf '\nERROR: codex review timed out after %ss.\n' "$review_timeout_seconds" | tee -a "$output_path"
fi

exit "$rc"
