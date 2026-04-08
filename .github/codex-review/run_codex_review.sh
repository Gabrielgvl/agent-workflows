#!/usr/bin/env bash

set -euo pipefail

raw_output_path="${CODEX_REVIEW_RAW_OUTPUT_PATH:-codex-review.json}"
review_log_path="${CODEX_REVIEW_LOG_PATH:-codex-review.log}"
schema_path="${CODEX_REVIEW_SCHEMA_PATH:-codex-review.schema.json}"
prompt_path="${CODEX_REVIEW_PROMPT_PATH:-codex-review.prompt.md}"
diff_path="${CODEX_REVIEW_DIFF_PATH:-codex-review.diff}"
changed_files_path="${CODEX_REVIEW_CHANGED_FILES_PATH:-codex-changed-files.txt}"
prior_open_findings_path="${CODEX_REVIEW_PRIOR_OPEN_FINDINGS_PATH:-codex-review-prior-open-findings.json}"
review_base="${CODEX_REVIEW_BASE:-}"
repository_owner="${CODEX_REVIEW_REPOSITORY_OWNER:-}"
review_model="${CODEX_REVIEW_MODEL:-gpt-5.4}"
review_timeout_seconds="${CODEX_REVIEW_TIMEOUT_SECONDS:-900}"
review_reasoning_effort="${CODEX_REVIEW_REASONING_EFFORT:-medium}"
review_reasoning_summary="${CODEX_REVIEW_REASONING_SUMMARY:-}"
review_verbosity="${CODEX_REVIEW_VERBOSITY:-}"
review_mode="${CODEX_REVIEW_MODE:-discovery}"
review_previous_head_sha="${CODEX_REVIEW_PREVIOUS_HEAD_SHA:-}"
auth_json="${CODEX_AUTH_JSON:-}"
runner_temp_root="${RUNNER_TEMP:-$PWD/.tmp/codex-review}"
timeout_bin=""
stdbuf_bin=""
heartbeat_seconds="${CODEX_REVIEW_HEARTBEAT_SECONDS:-30}"
heartbeat_pid=""
codex_bin=""
wrapped_codex_bin=""
toolchain_wrapper_dir=""
original_home="${HOME:-}"
original_volta_home="${VOLTA_HOME:-}"
workflow_helpers_dir="${WORKFLOW_HELPERS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

mkdir -p "$runner_temp_root"
rm -f \
  "$raw_output_path" \
  "$review_log_path" \
  "$schema_path" \
  "$prompt_path" \
  "$diff_path" \
  "$changed_files_path"

timestamp_utc() {
  date -u +%Y-%m-%dT%H:%M:%SZ
}

log_info() {
  printf '[%s] %s\n' "$(timestamp_utc)" "$*" | tee -a "$review_log_path"
}

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
codex_bin="$(command -v codex)"

if [[ -z "$original_volta_home" && -n "$original_home" && -d "$original_home/.volta" ]]; then
  original_volta_home="$original_home/.volta"
fi

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

# Validate review_mode
valid_modes="discovery gate same_sha"
if ! echo "$valid_modes" | grep -qw "$review_mode"; then
  printf 'ERROR: Invalid CODEX_REVIEW_MODE: %s. Allowed: %s\n' "$review_mode" "$valid_modes" > "$review_log_path"
  cat "$review_log_path"
  exit 2
fi

# Handle same_sha mode: skip codex exec, synthesize output from prior open findings
if [[ "$review_mode" == "same_sha" ]]; then
  log_info "Review mode: same_sha (skipping codex exec, synthesizing from prior open findings)"

  prior_open_findings_count="$(python3 - "$prior_open_findings_path" "$raw_output_path" <<'SYNTHESIZE_PY'
import json
import pathlib
import sys

prior_path = pathlib.Path(sys.argv[1])
output_path = pathlib.Path(sys.argv[2])


def to_text(value: object, default: str) -> str:
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return default


def to_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed


def to_float(value: object, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed


open_findings: list[dict[str, object]] = []
if prior_path.is_file():
    payload = json.loads(prior_path.read_text(encoding="utf-8"))
    raw_open_findings = payload.get("open_findings")
    if isinstance(raw_open_findings, list):
        open_findings = [item for item in raw_open_findings if isinstance(item, dict)]

synthesized_findings: list[dict[str, object]] = []
for finding in open_findings:
    start_line = max(1, to_int(finding.get("start_line"), 1))
    end_line = max(start_line, to_int(finding.get("end_line"), start_line))
    priority = to_int(finding.get("priority"), 2)
    if priority < 0 or priority > 3:
        priority = 2

    confidence_score = to_float(finding.get("confidence_score"), 0.5)
    if confidence_score < 0 or confidence_score > 1:
        confidence_score = 0.5

    previous_fingerprint = finding.get("previous_fingerprint")
    if isinstance(previous_fingerprint, str):
        previous_fingerprint = previous_fingerprint.strip() or None
    else:
        previous_fingerprint = None

    synthesized_findings.append(
        {
            "title": to_text(finding.get("title"), "Prior open finding"),
            "body": to_text(
                finding.get("body"),
                "A previously reported finding remains open and must be revalidated.",
            ),
            "suggested_fix": "Review and address the prior open finding.",
            "category": "correctness",
            "confidence_score": confidence_score,
            "priority": priority,
            "path": to_text(finding.get("path"), "unknown"),
            "start_line": start_line,
            "end_line": end_line,
            "previous_fingerprint": previous_fingerprint,
        }
    )

output = {
    "findings": synthesized_findings,
    "file_coverage": [],
    "sweep_complete": True,
    "sweep_reflection": {
        "zero_finding_files_reexamined": [],
        "additional_findings_from_reflection": 0,
        "confidence_adjustment": 0,
        "notes": "Synthesized from prior open findings in same_sha mode.",
    },
    "overall_correctness": "patch is incorrect" if synthesized_findings else "patch is correct",
    "overall_explanation": (
        "Prior open findings still require attention."
        if synthesized_findings
        else "No prior open findings remain."
    ),
    "overall_confidence_score": 0.8 if synthesized_findings else 1.0,
}

output_path.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
print(str(len(open_findings)))
SYNTHESIZE_PY
)"

  log_info "Prior open findings count: ${prior_open_findings_count}"
  log_info "Synthesized codex-review.json with ${prior_open_findings_count} findings from prior open findings"

  # Generate minimal diff for compatibility
  git diff --name-status --find-renames "${review_base_ref}...HEAD" > "$changed_files_path"
  git diff --relative --find-renames --unified=5 "${review_base_ref}...HEAD" > "$diff_path"
  changed_file_count="$(wc -l < "$changed_files_path" | tr -d '[:space:]')"
  log_info "Prepared review context for ${changed_file_count} changed files (same_sha mode)"

  exit 0
fi

# Compute diff base based on mode
diff_base_ref=""
diff_base_sha=""
if [[ "$review_mode" == "gate" && -n "$review_previous_head_sha" ]]; then
  # Gate mode: try to use previous_head_sha as diff base
  if git rev-parse --verify "$review_previous_head_sha" >/dev/null 2>&1; then
    diff_base_sha="$review_previous_head_sha"
    log_info "Review mode: gate (diff base: previous_head_sha=${review_previous_head_sha})"
  else
    log_info "Review mode: gate (previous_head_sha invalid, falling back to origin/${review_base})"
    diff_base_ref="$review_base_ref"
    diff_base_sha="$base_sha"
  fi
else
  # Discovery mode or gate mode without valid previous_head_sha
  diff_base_ref="$review_base_ref"
  diff_base_sha="$base_sha"
  log_info "Review mode: ${review_mode} (diff base: origin/${review_base})"
fi

if [[ -n "$diff_base_ref" ]]; then
  if ! merge_base="$(git merge-base "$head_sha" "$diff_base_sha")"; then
    cat > "$review_log_path" <<EOF
ERROR: unable to compute git merge-base between HEAD (${head_sha}) and ${diff_base_ref} (${diff_base_sha}).
Ensure the caller workflow checks out the pull request head SHA and fetches the full base branch before invoking codex-pr-review.
EOF
    cat "$review_log_path"
    exit 2
  fi
else
  if ! merge_base="$(git merge-base "$head_sha" "$diff_base_sha")"; then
    cat > "$review_log_path" <<EOF
ERROR: unable to compute git merge-base between HEAD (${head_sha}) and ${diff_base_sha}.
EOF
    cat "$review_log_path"
    exit 2
  fi
fi

if [[ -n "$diff_base_ref" ]]; then
  git diff --name-status --find-renames "${diff_base_ref}...HEAD" > "$changed_files_path"
  git diff --relative --find-renames --unified=5 "${diff_base_ref}...HEAD" > "$diff_path"
else
  git diff --name-status --find-renames "${diff_base_sha}...HEAD" > "$changed_files_path"
  git diff --relative --find-renames --unified=5 "${diff_base_sha}...HEAD" > "$diff_path"
fi
changed_file_count="$(wc -l < "$changed_files_path" | tr -d '[:space:]')"
log_info "Prepared review context for ${changed_file_count} changed files against ${diff_base_ref:-$diff_base_sha}."

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
          "suggested_fix": {
            "type": "string",
            "minLength": 1
          },
          "category": {
            "type": "string",
            "enum": [
              "security",
              "correctness",
              "performance",
              "maintainability",
              "contract",
              "integration"
            ]
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
          },
          "previous_fingerprint": {
            "type": [
              "string",
              "null"
            ]
          }
        },
        "required": [
          "title",
          "body",
          "suggested_fix",
          "category",
          "confidence_score",
          "priority",
          "path",
          "start_line",
          "end_line",
          "previous_fingerprint"
        ],
        "additionalProperties": false
      }
    },
    "file_coverage": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "path": {
            "type": "string",
            "minLength": 1
          },
          "categories_checked": {
            "type": "array",
            "items": {
              "type": "string",
              "enum": [
                "security",
                "correctness",
                "performance",
                "maintainability",
                "contract",
                "integration"
              ]
            }
          },
          "findings_count": {
            "type": "integer",
            "minimum": 0
          },
          "context_lines_read": {
            "type": "integer",
            "minimum": 0
          },
          "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          }
        },
        "required": [
          "path",
          "categories_checked",
          "findings_count",
          "context_lines_read",
          "confidence"
        ],
        "additionalProperties": false
      }
    },
    "sweep_complete": {
      "type": "boolean"
    },
    "sweep_reflection": {
      "type": "object",
      "properties": {
        "zero_finding_files_reexamined": {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        "additional_findings_from_reflection": {
          "type": "integer",
            "minimum": 0
        },
        "confidence_adjustment": {
          "type": "number",
            "minimum": -1,
            "maximum": 1
        },
        "notes": {
          "type": "string"
        }
      },
      "required": [
        "zero_finding_files_reexamined",
        "additional_findings_from_reflection",
        "confidence_adjustment",
        "notes"
      ],
      "additionalProperties": false
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
    "file_coverage",
    "sweep_complete",
    "sweep_reflection",
    "overall_correctness",
    "overall_explanation",
    "overall_confidence_score"
  ],
  "additionalProperties": false
}
JSON

prior_open_findings_count="$(python3 - "$workflow_helpers_dir/codex_review_lib.py" "$prompt_path" "$review_base" "$diff_base_sha" "$merge_base" "$head_sha" "$(basename "$changed_files_path")" "$(basename "$diff_path")" "$repository_owner" "$prior_open_findings_path" "$review_mode" "$review_previous_head_sha" <<'PY'
import importlib.util
import json
import pathlib
import sys

module_path = pathlib.Path(sys.argv[1])
prompt_path = pathlib.Path(sys.argv[2])
review_base = sys.argv[3]
base_sha = sys.argv[4]
merge_base = sys.argv[5]
head_sha = sys.argv[6]
changed_files_filename = sys.argv[7]
diff_filename = sys.argv[8]
repository_owner = sys.argv[9]
prior_path = pathlib.Path(sys.argv[10])
review_mode_arg = sys.argv[11] if len(sys.argv) > 11 else "discovery"
previous_head_sha_arg = sys.argv[12] if len(sys.argv) > 12 else ""

spec = importlib.util.spec_from_file_location("codex_review_lib", module_path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)

prior_open_findings = []
if prior_path.is_file():
    payload = json.loads(prior_path.read_text(encoding="utf-8"))
    prior_open_findings = payload.get("open_findings")
    if not isinstance(prior_open_findings, list):
        raise SystemExit("open_findings payload must include an open_findings array.")

prompt_path.write_text(
    module.build_review_prompt(
        review_base=review_base,
        base_sha=base_sha,
        merge_base=merge_base,
        head_sha=head_sha,
        changed_files_filename=changed_files_filename,
        diff_filename=diff_filename,
        repository_owner=repository_owner,
        prior_open_findings=prior_open_findings,
        review_mode=review_mode_arg,
        previous_head_sha=previous_head_sha_arg,
    ),
    encoding="utf-8",
)

print(str(len(prior_open_findings)))
PY
)"

codex_home_parent="$(mktemp -d "${runner_temp_root%/}/codex-home.XXXXXX")"
codex_zdotdir="$(mktemp -d "${runner_temp_root%/}/codex-zdotdir.XXXXXX")"
toolchain_wrapper_dir="$(mktemp -d "${runner_temp_root%/}/codex-bin.XXXXXX")"
wrapped_codex_bin="$toolchain_wrapper_dir/codex"

cleanup() {
  if [[ -n "${heartbeat_pid:-}" ]]; then
    kill "$heartbeat_pid" >/dev/null 2>&1 || true
    wait "$heartbeat_pid" 2>/dev/null || true
    heartbeat_pid=""
  fi

  local cleanup_path
  for cleanup_path in "$codex_home_parent" "$codex_zdotdir" "$toolchain_wrapper_dir"; do
    [[ -z "$cleanup_path" ]] && continue
    chmod -R u+w "$cleanup_path" >/dev/null 2>&1 || true
    rm -rf "$cleanup_path" >/dev/null 2>&1 || true
    if [[ -e "$cleanup_path" ]]; then
      printf '[%s] WARN: unable to fully remove temporary path %s\n' \
        "$(timestamp_utc)" "$cleanup_path" >> "$review_log_path" 2>/dev/null || true
    fi
  done
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
  "$codex_zdotdir" \
  "$toolchain_wrapper_dir"
: > "$codex_home_parent/.codex/config.toml"
: > "$codex_zdotdir/.zshenv"

{
  printf '%s\n' '#!/usr/bin/env bash'
  printf '%s\n' 'set -euo pipefail'
  if [[ -n "$original_volta_home" ]]; then
    printf 'export VOLTA_HOME=%q\n' "$original_volta_home"
    printf 'export PATH=%q:$PATH\n' "$original_volta_home/bin"
  fi
  printf 'exec %q "$@"\n' "$codex_bin"
} > "$wrapped_codex_bin"
chmod 755 "$wrapped_codex_bin"

export HOME="$codex_home_parent"
export XDG_CONFIG_HOME="$codex_home_parent/.config"
export XDG_CACHE_HOME="$codex_home_parent/.cache"
export XDG_STATE_HOME="$codex_home_parent/.local/state"
export ZDOTDIR="$codex_zdotdir"
export PATH="$toolchain_wrapper_dir:$PATH"
if [[ -n "$original_volta_home" ]]; then
  export VOLTA_HOME="$original_volta_home"
fi

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
log_info "Restored isolated Codex auth bundle."

if ! "$wrapped_codex_bin" --version >/dev/null 2>&1; then
  printf 'ERROR: unable to execute codex after HOME isolation.\n' >> "$review_log_path"
  "$wrapped_codex_bin" --version >> "$review_log_path" 2>&1 || true
  cat "$review_log_path"
  exit 2
fi
log_info "Pinned Codex executable for isolated HOME."

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
  "$wrapped_codex_bin"
  --ask-for-approval never
  exec
  # Trusted self-hosted CI runners can hit bubblewrap/Landlock limitations on
  # Linux. Run reviews with full access instead of relying on the OS sandbox.
  --sandbox danger-full-access
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

start_heartbeat() {
  if ! [[ "$heartbeat_seconds" =~ ^[0-9]+$ ]] || [[ "$heartbeat_seconds" -lt 1 ]]; then
    return
  fi

  local start_epoch="$1"
  (
    while true; do
      sleep "$heartbeat_seconds"
      now="$(date +%s)"
      elapsed="$((now - start_epoch))"
      printf '[%s] Codex review still running (%ss elapsed, %ss timeout).\n' \
        "$(timestamp_utc)" "$elapsed" "$review_timeout_seconds" | tee -a "$review_log_path"
    done
  ) &
  heartbeat_pid=$!
}

review_start_epoch="$(date +%s)"
log_info "Starting Codex review with model=${review_model}, reasoning_effort=${review_reasoning_effort}, timeout=${review_timeout_seconds}s, prior_open_findings=${prior_open_findings_count}."

set +e
start_heartbeat "$review_start_epoch"
"$timeout_bin" --signal=TERM --kill-after=30s "${review_timeout_seconds}s" \
  "${review_cmd[@]}" < "$prompt_path" > >(tee -a "$review_log_path") 2>&1
rc=$?
if [[ -n "${heartbeat_pid:-}" ]]; then
  kill "$heartbeat_pid" >/dev/null 2>&1 || true
  wait "$heartbeat_pid" 2>/dev/null || true
  heartbeat_pid=""
fi
set -e

review_elapsed_seconds="$(( $(date +%s) - review_start_epoch ))"
log_info "Codex review command finished in ${review_elapsed_seconds}s with exit code ${rc}."

if [[ "$rc" -eq 124 || "$rc" -eq 137 ]]; then
  printf '\n[%s] ERROR: codex exec timed out after %ss.\n' \
    "$(timestamp_utc)" "$review_timeout_seconds" | tee -a "$review_log_path"
fi

exit "$rc"
