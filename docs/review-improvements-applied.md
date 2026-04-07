# Review Completeness Improvements Applied

## Summary

Applied state-of-the-art research findings from OpenAI's CriticGPT paper and USENIX Security 2024 to improve review completeness and ensure issues are not missed.

## Changes Made

### 1. Schema Changes (`run_codex_review.sh`)

**New fields in findings:**
- `suggested_fix` (required) - Concrete, actionable fix recommendation with code snippet
- `category` (required) - One of: security, correctness, performance, maintainability, contract, integration

**New top-level fields:**
- `file_coverage` - Per-file checklist showing:
  - `categories_checked` - Which of the 6 categories were examined
  - `findings_count` - Number of findings for this file
  - `context_lines_read` - Lines of context read beyond diff
  - `confidence` - Confidence in coverage for this file

- `sweep_complete` - Boolean indicating all stopping criteria satisfied

- `sweep_reflection` - Object documenting reflection pass:
  - `zero_finding_files_reexamined` - Files re-checked during reflection
  - `additional_findings_from_reflection` - Issues found during reflection
  - `confidence_adjustment` - Adjustment to overall confidence
  - `notes` - Reflection notes

### 2. Prompt Engineering (`codex_review_lib.py`)

**Replaced single-pass approach with multi-pass methodology:**

#### Pass 1: File Inventory and Risk Assessment
- Identify security-sensitive files
- Identify correctness-critical files
- Map cross-file dependencies
- Prioritize by complexity

#### Pass 2: Category Sweep Per File
- Systematically check ALL 6 categories for EACH file:
  1. **security** - injection, auth bypasses, secrets, permissions
  2. **correctness** - logic errors, edge cases, null handling, race conditions
  3. **performance** - N+1 queries, memory leaks, blocking calls
  4. **maintainability** - names, duplication, complexity
  5. **contract** - breaking API changes, versioning
  6. **integration** - cross-file consistency, dependencies

- Require 50 lines of context per hunk
- Explicit `categories_checked` recording

#### Pass 3: Cross-Cutting Analysis
- Cross-file consistency
- Breaking changes across modules
- Integration impact

#### Pass 4: Reflection and Verification
- Re-examine zero-finding files
- Re-examine sparse categories
- Challenge own findings (remove hallucinations)
- Final sweep asking "What might a typical reviewer miss?"

**Explicit Stopping Criteria:**
- All 5 criteria must be satisfied for `sweep_complete: true`
- If any unsatisfied, must CONTINUE reviewing

**Adversarial Framing:**
- Changed from passive "reviewer" to "adversarial code reviewer"
- Goal: Find issues a typical reviewer would miss
- Explicit instruction to continue until "exhausted all reasonable search paths"

**Required `suggested_fix` for each finding:**
- Must provide specific code changes
- Must explain why fix resolves issue
- Must avoid vague suggestions

### 3. Output Format Changes

**Inline comments now include:**
- Category label
- Suggested fix section with concrete recommendations

**Summary now shows:**
- `Sweep complete: yes/no (may have missed issues)`
- `Coverage: X/Y files with full category sweep, N lines context read`
- Reflection pass findings

### 4. Normalization Updates

**`normalize_review_payload()` now:**
- Validates and extracts `file_coverage` array
- Validates `sweep_complete` boolean
- Validates `sweep_reflection` object
- Extracts `category` and `suggested_fix` from findings
- Provides defaults for backward compatibility

**`parse_managed_inline_comment()` now:**
- Parses category from inline comments
- Parses suggested_fix from inline comments

## Root Causes Addressed

| Root Cause | Solution Applied |
|------------|-----------------|
| Premature stopping | Explicit stopping criteria + multi-pass structure |
| Lack of systematic sweep | 6-category checklist per file |
| No reflection phase | Pass 4: Reflection and Verification |
| Domain knowledge gaps | Category-specific issue patterns in prompt |
| Insufficient context reading | Required 50 lines context per hunk |
| No explicit completion criteria | `sweep_complete` boolean + coverage metrics |
| Missing fix suggestions | Required `suggested_fix` field per finding |

## Files Changed

1. `.github/codex-review/run_codex_review.sh` - Updated JSON schema
2. `.github/codex-review/codex_review_lib.py` - Updated prompt, normalization, rendering
3. `tests/test_codex_review_lib.py` - Updated tests for new format

## Expected Improvements

Based on research findings:

- **20-30% more findings** from prompt engineering alone
- **Better coverage** via explicit category checklist
- **Fewer missed issues** via reflection phase
- **More actionable feedback** via required suggested_fix
- **Visible completeness** via sweep_complete and coverage metrics

## Backward Compatibility

The changes maintain backward compatibility:
- Missing `category` defaults to "correctness"
- Missing `suggested_fix` gets a placeholder message
- Missing `file_coverage` defaults to empty array
- Missing `sweep_complete` defaults to false
- Missing `sweep_reflection` defaults to empty object

## Testing

All 10 unit tests pass:
- `test_parses_existing_managed_inline_comment` - Updated for new format
- `test_normalizes_findings_and_deduplicates` - Passes
- `test_preserves_previous_fingerprint` - Passes
- `test_supports_cookbook_style_code_location` - Passes
- `test_builds_exhaustive_blocker_first_prompt` - Updated for new prompt
- `test_rejects_invalid_prior_open_findings_for_prompt` - Passes
- `test_renders_all_blocking_findings_without_truncation` - Passes
- `test_renders_summary_body` - Passes
- `test_plans_keep_reopen_create_and_resolve` - Passes
- `test_classifies_placeable_unplaced_and_truncated_findings` - Passes