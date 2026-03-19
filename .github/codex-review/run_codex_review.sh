#!/usr/bin/env bash

set -euo pipefail

raw_output_path="${CODEX_REVIEW_RAW_OUTPUT_PATH:-codex-review.json}"
review_log_path="${CODEX_REVIEW_LOG_PATH:-codex-review.log}"
schema_path="${CODEX_REVIEW_SCHEMA_PATH:-codex-review.schema.json}"
prompt_path="${CODEX_REVIEW_PROMPT_PATH:-codex-review.prompt.md}"
diff_path="${CODEX_REVIEW_DIFF_PATH:-codex-review.diff}"
changed_files_path="${CODEX_REVIEW_CHANGED_FILES_PATH:-codex-changed-files.txt}"
review_base="${CODEX_REVIEW_BASE:-}"
review_model="${CODEX_REVIEW_MODEL:-gpt-5.4}"
review_timeout_seconds="${CODEX_REVIEW_TIMEOUT_SECONDS:-900}"
review_reasoning_effort="${CODEX_REVIEW_REASONING_EFFORT:-low}"
review_reasoning_summary="${CODEX_REVIEW_REASONING_SUMMARY:-}"
review_verbosity="${CODEX_REVIEW_VERBOSITY:-}"
auth_json="${CODEX_AUTH_JSON:-}"
runner_temp_root="${RUNNER_TEMP:-$PWD/.tmp/codex-review}"
timeout_bin=""
stdbuf_bin=""

mkdir -p "$runner_temp_root"
rm -f \
  "$raw_output_path" \
  "$review_log_path" \
  "$schema_path" \
  "$prompt_path" \
  "$diff_path" \
  "$changed_files_path"

require_command() {
  local name="$1"
  if ! command -v "$name" >/dev/null 2>&1; then
    printf 'ERROR: required command not found: %s\n' "$name" > "$review_log_path"
    cat "$review_log_path"
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
  printf 'ERROR: timeout command not found.\n' > "$review_log_path"
  cat "$review_log_path"
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

require_command codex
require_command git
require_command mktemp
require_command python3
resolve_timeout_bin
resolve_stdbuf_bin

if [[ -z "$review_base" ]]; then
  printf 'ERROR: CODEX_REVIEW_BASE is required.\n' > "$review_log_path"
  cat "$review_log_path"
  exit 2
fi

if [[ -z "$auth_json" ]]; then
  printf 'ERROR: CODEX_AUTH_JSON is required.\n' > "$review_log_path"
  cat "$review_log_path"
  exit 2
fi

review_base_ref="refs/remotes/origin/${review_base}"
if ! git rev-parse --verify "$review_base_ref" >/dev/null 2>&1; then
  printf 'ERROR: review base ref %s is missing locally.\n' "$review_base_ref" > "$review_log_path"
  cat "$review_log_path"
  exit 2
fi

head_sha="$(git rev-parse HEAD)"
base_sha="$(git rev-parse "$review_base_ref")"
if ! merge_base="$(git merge-base "$head_sha" "$base_sha")"; then
  cat > "$review_log_path" <<EOF
ERROR: unable to compute git merge-base between HEAD (${head_sha}) and ${review_base_ref} (${base_sha}).
Ensure the caller workflow checks out the pull request head SHA and fetches the full base branch before invoking codex-pr-review.
EOF
  cat "$review_log_path"
  exit 2
fi

git diff --name-status --find-renames "${review_base_ref}...HEAD" > "$changed_files_path"
git diff --relative --find-renames --unified=5 "${review_base_ref}...HEAD" > "$diff_path"

cat > "$schema_path" <<'JSON'
{
  "type": "object",
  "properties": {
    "findings": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "title": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120
          },
          "body": {
            "type": "string",
            "minLength": 1
          },
          "confidence_score": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          },
          "priority": {
            "type": "integer",
            "minimum": 0,
            "maximum": 3
          },
          "path": {
            "type": "string",
            "minLength": 1
          },
          "start_line": {
            "type": "integer",
            "minimum": 1
          },
          "end_line": {
            "type": "integer",
            "minimum": 1
          }
        },
        "required": [
          "title",
          "body",
          "confidence_score",
          "priority",
          "path",
          "start_line",
          "end_line"
        ],
        "additionalProperties": false
      }
    },
    "overall_correctness": {
      "type": "string",
      "enum": [
        "patch is correct",
        "patch is incorrect"
      ]
    },
    "overall_explanation": {
      "type": "string",
      "minLength": 1
    },
    "overall_confidence_score": {
      "type": "number",
      "minimum": 0,
      "maximum": 1
    }
  },
  "required": [
    "findings",
    "overall_correctness",
    "overall_explanation",
    "overall_confidence_score"
  ],
  "additionalProperties": false
}
JSON

cat > "$prompt_path" <<EOF
You are acting as a reviewer for a proposed code change made by another engineer.

Review only the pull request changes introduced by comparing \`origin/${review_base}...HEAD\`.
The repository root is the current working directory.

Focus on actionable issues that affect correctness, performance, security, maintainability, or developer experience.
Flag only issues introduced by this pull request.
Do not report style nits, speculative concerns, or pre-existing issues.
Use the available tools to inspect the repository, changed files, and diff before deciding whether to raise a finding.

Return JSON that matches the provided schema and nothing else.

Output requirements:
- Use repo-relative POSIX paths in the \`path\` field.
- Use exact HEAD-side line numbers from the changed diff.
- Set \`priority\` to 0 for the most severe blocking issues and 3 for the least severe findings.
- Keep \`title\` short and direct.
- Make \`body\` concise, specific, and actionable.
- If there are no actionable findings, return an empty \`findings\` array.
- Use \`overall_correctness\` of \`patch is correct\` only when the patch is safe to merge as-is.

Context:
- Base ref: origin/${review_base}
- Base SHA: ${base_sha}
- Merge base SHA: ${merge_base}
- Head SHA: ${head_sha}
- Changed files list: $(basename "$changed_files_path")
- Unified diff: $(basename "$diff_path")
EOF

codex_home_parent="$(mktemp -d "${runner_temp_root%/}/codex-home.XXXXXX")"
codex_zdotdir="$(mktemp -d "${runner_temp_root%/}/codex-zdotdir.XXXXXX")"

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

codex_auth_path="$codex_home_parent/.codex/auth.json"

validate_auth_json() {
  python3 - "$1" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))

if not isinstance(data, dict):
    raise SystemExit("auth.json must be a JSON object.")

auth_mode = data.get("auth_mode")
if not isinstance(auth_mode, str) or not auth_mode.strip():
    raise SystemExit("auth.json is missing auth_mode.")

tokens = data.get("tokens")
if not isinstance(tokens, dict):
    raise SystemExit("auth.json is missing tokens.")

for key in ("access_token", "id_token", "refresh_token"):
    value = tokens.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"auth.json is missing tokens.{key}.")
PY
}

if ! AUTH_JSON="$auth_json" python3 - "$codex_auth_path" <<'PY'
import os
import pathlib
import sys

payload = os.environ.get("AUTH_JSON", "")
if not payload:
    raise SystemExit("CODEX_AUTH_JSON is empty.")

path = pathlib.Path(sys.argv[1])
path.write_text(payload, encoding="utf-8")
PY
then
  printf 'ERROR: failed to write CODEX_AUTH_JSON into auth.json.\n' > "$review_log_path"
  cat "$review_log_path"
  exit 2
fi

chmod 600 "$codex_auth_path"
if ! validate_auth_json "$codex_auth_path" > /dev/null 2>&1; then
  printf 'ERROR: restored auth.json failed validation.\n' > "$review_log_path"
  validate_auth_json "$codex_auth_path" >> "$review_log_path" 2>&1 || true
  cat "$review_log_path"
  exit 2
fi

# Keep the Codex subprocess isolated from runner-exported CODEX_* state.
while IFS='=' read -r name _; do
  case "$name" in
    CODEX_*)
      unset "$name"
      ;;
  esac
done < <(env)
unset OPENAI_API_KEY

review_cmd=(
  codex
  --ask-for-approval never
  exec
  --sandbox read-only
  --model "$review_model"
  --disable js_repl
  --disable multi_agent
  --disable shell_snapshot
  --output-schema "$schema_path"
  --output-last-message "$raw_output_path"
  --cd "$PWD"
  -c 'shell_environment_policy.inherit=core'
  -c "model_reasoning_effort=\"${review_reasoning_effort}\""
)

if [[ -n "$review_reasoning_summary" ]]; then
  review_cmd+=(-c "model_reasoning_summary=\"${review_reasoning_summary}\"")
fi

if [[ -n "$review_verbosity" ]]; then
  review_cmd+=(-c "model_verbosity=\"${review_verbosity}\"")
fi

review_cmd+=(-)

if [[ -n "$stdbuf_bin" ]]; then
  review_cmd=("$stdbuf_bin" -oL -eL "${review_cmd[@]}")
fi

set +e
"$timeout_bin" --signal=TERM --kill-after=30s "${review_timeout_seconds}s" \
  "${review_cmd[@]}" < "$prompt_path" 2>&1 | tee "$review_log_path"
rc=${PIPESTATUS[0]}
set -e

if [[ "$rc" -eq 124 || "$rc" -eq 137 ]]; then
  printf '\nERROR: codex exec timed out after %ss.\n' "$review_timeout_seconds" | tee -a "$review_log_path"
fi

exit "$rc"
