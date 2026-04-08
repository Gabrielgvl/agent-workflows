#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any

SUMMARY_MARKER = "<!-- codex-pr-review -->"
INLINE_MARKER_PREFIX = "<!-- codex-pr-review-inline:"
INLINE_MARKER_SUFFIX = " -->"
PRIORITY_LABELS = {0: "P0", 1: "P1", 2: "P2", 3: "P3"}
REVERSE_PRIORITY_LABELS = {label: priority for priority, label in PRIORITY_LABELS.items()}
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
INLINE_MARKER_RE = re.compile(r"<!-- codex-pr-review-inline:([0-9a-f]{16}) -->")

# New format with category and suggested_fix
INLINE_COMMENT_RE = re.compile(
    r"^\*\*\[(P[0-3])\] (?P<title>.+?)\*\*\n\n"
    r"`(?P<location>[^`]+)`\n"
    r"Category: (?P<category>[^\n]+)\n\n"
    r"(?P<body>.*?)\n\n"
    r"\*\*Suggested fix:\*\*\n(?P<suggested_fix>.*?)\n\n"
    r"Confidence: (?P<confidence>[^\n]+)\n\n"
    r"<!-- codex-pr-review-inline:(?P<fingerprint>[0-9a-f]{16}) -->\s*$",
    re.DOTALL,
)

# Old format without category and suggested_fix (for backward compatibility)
INLINE_COMMENT_RE_OLD = re.compile(
    r"^\*\*\[(P[0-3])\] (?P<title>.+?)\*\*\n\n"
    r"`(?P<location>[^`]+)`\n\n"
    r"(?P<body>.*?)\n\n"
    r"Confidence: (?P<confidence>[^\n]+)\n\n"
    r"<!-- codex-pr-review-inline:(?P<fingerprint>[0-9a-f]{16}) -->\s*$",
    re.DOTALL,
)


def _coerce_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer.")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise ValueError(f"{field_name} must be an integer.")


def _coerce_float(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number.")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError as exc:
            raise ValueError(f"{field_name} must be a number.") from exc
    raise ValueError(f"{field_name} must be a number.")


def _coerce_text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string.")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must not be empty.")
    return text


def _parse_priority_label(value: Any, *, field_name: str) -> tuple[int, str]:
    label = _coerce_text(value, field_name=field_name)
    if label not in REVERSE_PRIORITY_LABELS:
        raise ValueError(f"{field_name} must be one of: {', '.join(REVERSE_PRIORITY_LABELS)}.")
    return REVERSE_PRIORITY_LABELS[label], label


def _normalize_title_for_fingerprint(title: str) -> str:
    """Normalize title for stable fingerprint computation."""
    # Lowercase, strip whitespace, collapse multiple spaces to single
    normalized = title.strip().lower()
    # Remove punctuation that may vary between runs
    normalized = re.sub(r'[^\w\s]', '', normalized)
    normalized = re.sub(r'\s+', ' ', normalized)
    return normalized


def _compute_fingerprint(
    *,
    priority: int,
    title: str,
    body: str,
    path: str,
    start_line: int,
    end_line: int,
) -> str:
    """Compute a stable fingerprint from normalized title + path + lines."""
    # Drop body/priority dependence for stability
    fingerprint_source = json.dumps(
        {
            "title": _normalize_title_for_fingerprint(title),
            "path": path,
            "start_line": start_line,
            "end_line": end_line,
        },
        sort_keys=True,
    )
    return hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:16]


def _parse_location(location_text: str) -> tuple[str, int, int]:
    location = _coerce_text(location_text, field_name="finding.location")
    if ":" not in location:
        raise ValueError("finding.location must include a file path and line number.")
    path_text, line_text = location.rsplit(":", 1)
    path = normalize_repo_path(path_text)

    if "-" in line_text:
        start_text, end_text = line_text.split("-", 1)
        start_line = _coerce_int(start_text, field_name="finding.start_line")
        end_line = _coerce_int(end_text, field_name="finding.end_line")
    else:
        start_line = _coerce_int(line_text, field_name="finding.start_line")
        end_line = start_line

    if start_line < 1 or end_line < 1:
        raise ValueError("finding line numbers must be >= 1.")
    if end_line < start_line:
        raise ValueError("finding.end_line must be >= start_line.")
    return path, start_line, end_line


def _finding_signature(finding: dict[str, Any]) -> tuple[int, str, int, int, str]:
    return (
        int(finding["priority"]),
        str(finding["path"]),
        int(finding["start_line"]),
        int(finding["end_line"]),
        str(finding["title"]).strip().lower(),
    )


def _tokenize_title(title: str) -> set[str]:
    """Extract significant tokens from a title for fuzzy matching."""
    # Normalize similar to fingerprint normalization
    normalized = title.strip().lower()
    normalized = re.sub(r'[^\w\s]', '', normalized)
    tokens = set(normalized.split())
    # Filter out very short tokens (likely noise)
    return {t for t in tokens if len(t) >= 3}


def _title_overlap_score(title1: str, title2: str) -> float:
    """Compute Jaccard-like overlap score between two titles."""
    tokens1 = _tokenize_title(title1)
    tokens2 = _tokenize_title(title2)
    if not tokens1 or not tokens2:
        return 0.0
    intersection = tokens1 & tokens2
    union = tokens1 | tokens2
    return len(intersection) / len(union) if union else 0.0


def _line_distance_tolerance(
    finding: dict[str, Any],
    thread_finding: dict[str, Any],
    tolerance: int = 5,
) -> bool:
    """Check if finding lines are within tolerance of thread lines."""
    f_start = int(finding["start_line"])
    f_end = int(finding["end_line"])
    t_start = int(thread_finding["start_line"])
    t_end = int(thread_finding["end_line"])
    # Check if ranges overlap or are within tolerance
    return abs(f_start - t_start) <= tolerance and abs(f_end - t_end) <= tolerance


def _format_finding_location(path: str, start_line: int, end_line: int) -> str:
    if start_line == end_line:
        return f"{path}:{start_line}"
    return f"{path}:{start_line}-{end_line}"


def _finding_priority_label(finding: dict[str, Any]) -> str:
    priority_label = finding.get("priority_label")
    if isinstance(priority_label, str) and priority_label in REVERSE_PRIORITY_LABELS:
        return priority_label

    priority = finding.get("priority")
    if isinstance(priority, int) and priority in PRIORITY_LABELS:
        return PRIORITY_LABELS[priority]

    return ""


def _normalize_prior_open_finding_for_prompt(
    raw_finding: Any,
    *,
    index: int,
) -> dict[str, Any]:
    if not isinstance(raw_finding, dict):
        raise ValueError(f"prior_open_findings[{index}] must be an object.")

    previous_fingerprint = _coerce_text(
        raw_finding.get("previous_fingerprint"),
        field_name=f"prior_open_findings[{index}].previous_fingerprint",
    )
    title = _coerce_text(
        raw_finding.get("title"),
        field_name=f"prior_open_findings[{index}].title",
    )
    body = _coerce_text(
        raw_finding.get("body"),
        field_name=f"prior_open_findings[{index}].body",
    )
    path = normalize_repo_path(raw_finding.get("path"))
    start_line = _coerce_int(
        raw_finding.get("start_line"),
        field_name=f"prior_open_findings[{index}].start_line",
    )
    end_line = _coerce_int(
        raw_finding.get("end_line"),
        field_name=f"prior_open_findings[{index}].end_line",
    )
    if start_line < 1 or end_line < 1:
        raise ValueError(f"prior_open_findings[{index}] line numbers must be >= 1.")
    if end_line < start_line:
        raise ValueError(f"prior_open_findings[{index}].end_line must be >= start_line.")
    _, priority_label = _parse_priority_label(
        raw_finding.get("priority_label"),
        field_name=f"prior_open_findings[{index}].priority_label",
    )

    return {
        "previous_fingerprint": previous_fingerprint,
        "priority_label": priority_label,
        "title": title,
        "body": body,
        "path": path,
        "start_line": start_line,
        "end_line": end_line,
    }


def normalize_repo_path(raw_path: Any) -> str:
    if not isinstance(raw_path, str):
        raise ValueError("finding.path must be a string.")

    candidate = raw_path.strip().replace("\\", "/")
    if not candidate:
        raise ValueError("finding.path must not be empty.")
    if candidate.startswith("/") or candidate.startswith("../"):
        raise ValueError("finding.path must be repository-relative.")

    normalized = PurePosixPath(candidate)
    if normalized.is_absolute():
        raise ValueError("finding.path must be repository-relative.")

    parts = [part for part in normalized.parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("finding.path must not escape the repository root.")

    return PurePosixPath(*parts).as_posix()


def load_json_text(path_text: str) -> Any:
    text = path_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()
    if not text:
        raise ValueError("Structured review output was empty.")
    return json.loads(text)


def _extract_finding_fields(raw_finding: dict[str, Any]) -> tuple[str, int, int]:
    if "path" in raw_finding:
        path = normalize_repo_path(raw_finding["path"])
        start_line = _coerce_int(raw_finding.get("start_line"), field_name="finding.start_line")
        end_line = _coerce_int(raw_finding.get("end_line"), field_name="finding.end_line")
        return path, start_line, end_line

    code_location = raw_finding.get("code_location")
    if not isinstance(code_location, dict):
        raise ValueError("finding.path or finding.code_location is required.")

    path = normalize_repo_path(code_location.get("absolute_file_path"))
    line_range = code_location.get("line_range")
    if not isinstance(line_range, dict):
        raise ValueError("finding.code_location.line_range is required.")
    start_line = _coerce_int(line_range.get("start"), field_name="finding.code_location.line_range.start")
    end_line = _coerce_int(line_range.get("end"), field_name="finding.code_location.line_range.end")
    return path, start_line, end_line


def normalize_review_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Structured review output must be a JSON object.")

    findings = payload.get("findings")
    if not isinstance(findings, list):
        raise ValueError("Structured review output must include a findings array.")

    overall_correctness = _coerce_text(
        payload.get("overall_correctness"),
        field_name="overall_correctness",
    )
    if overall_correctness not in {"patch is correct", "patch is incorrect"}:
        raise ValueError(
            "overall_correctness must be 'patch is correct' or 'patch is incorrect'."
        )

    overall_explanation = _coerce_text(
        payload.get("overall_explanation"),
        field_name="overall_explanation",
    )
    overall_confidence_score = _coerce_float(
        payload.get("overall_confidence_score"),
        field_name="overall_confidence_score",
    )
    if not 0 <= overall_confidence_score <= 1:
        raise ValueError("overall_confidence_score must be between 0 and 1.")

    # Validate and normalize file_coverage
    file_coverage = payload.get("file_coverage", [])
    if not isinstance(file_coverage, list):
        file_coverage = []
    normalized_file_coverage: list[dict[str, Any]] = []
    for idx, fc in enumerate(file_coverage):
        if not isinstance(fc, dict):
            continue
        fc_path = _coerce_text(fc.get("path"), field_name=f"file_coverage[{idx}].path")
        categories_checked = fc.get("categories_checked", [])
        if not isinstance(categories_checked, list):
            categories_checked = []
        valid_categories = {"security", "correctness", "performance", "maintainability", "contract", "integration"}
        categories_checked = [c for c in categories_checked if c in valid_categories]
        findings_count = _coerce_int(fc.get("findings_count", 0), field_name=f"file_coverage[{idx}].findings_count")
        context_lines_read = _coerce_int(fc.get("context_lines_read", 0), field_name=f"file_coverage[{idx}].context_lines_read")
        fc_confidence = _coerce_float(fc.get("confidence", 0.5), field_name=f"file_coverage[{idx}].confidence")
        normalized_file_coverage.append({
            "path": fc_path,
            "categories_checked": categories_checked,
            "findings_count": findings_count,
            "context_lines_read": context_lines_read,
            "confidence": round(fc_confidence, 4),
        })

    # Validate sweep_complete
    sweep_complete = payload.get("sweep_complete", False)
    if not isinstance(sweep_complete, bool):
        sweep_complete = False

    # Validate and normalize sweep_reflection
    sweep_reflection = payload.get("sweep_reflection", {})
    if not isinstance(sweep_reflection, dict):
        sweep_reflection = {}
    zero_finding_files = sweep_reflection.get("zero_finding_files_reexamined", [])
    if not isinstance(zero_finding_files, list):
        zero_finding_files = []
    additional_findings = _coerce_int(
        sweep_reflection.get("additional_findings_from_reflection", 0),
        field_name="sweep_reflection.additional_findings_from_reflection"
    )
    confidence_adj = _coerce_float(
        sweep_reflection.get("confidence_adjustment", 0),
        field_name="sweep_reflection.confidence_adjustment"
    )
    reflection_notes = _coerce_text(
        sweep_reflection.get("notes", ""),
        field_name="sweep_reflection.notes"
    ) if sweep_reflection.get("notes") else ""
    normalized_sweep_reflection = {
        "zero_finding_files_reexamined": zero_finding_files,
        "additional_findings_from_reflection": additional_findings,
        "confidence_adjustment": round(confidence_adj, 4),
        "notes": reflection_notes,
    }

    valid_categories = {"security", "correctness", "performance", "maintainability", "contract", "integration"}
    normalized_findings: list[dict[str, Any]] = []
    seen_fingerprints: set[str] = set()
    seen_previous_fingerprints: set[str] = set()

    for index, raw_finding in enumerate(findings):
        if not isinstance(raw_finding, dict):
            raise ValueError(f"finding[{index}] must be an object.")

        title = _coerce_text(raw_finding.get("title"), field_name=f"finding[{index}].title")
        body = _coerce_text(raw_finding.get("body"), field_name=f"finding[{index}].body")
        priority = _coerce_int(raw_finding.get("priority"), field_name=f"finding[{index}].priority")
        if priority not in PRIORITY_LABELS:
            raise ValueError(f"finding[{index}].priority must be between 0 and 3.")

        # Handle suggested_fix (required field)
        suggested_fix = raw_finding.get("suggested_fix")
        if suggested_fix is not None:
            suggested_fix = _coerce_text(suggested_fix, field_name=f"finding[{index}].suggested_fix")
        else:
            # Provide a placeholder if missing (should not happen with new prompt)
            suggested_fix = "Review the code and apply appropriate fix based on the issue description."

        # Handle category (required field)
        category = raw_finding.get("category")
        if category is not None:
            category = _coerce_text(category, field_name=f"finding[{index}].category")
            if category not in valid_categories:
                category = "correctness"  # Default fallback
        else:
            category = "correctness"  # Default fallback

        confidence_score = _coerce_float(
            raw_finding.get("confidence_score"),
            field_name=f"finding[{index}].confidence_score",
        )
        if not 0 <= confidence_score <= 1:
            raise ValueError(f"finding[{index}].confidence_score must be between 0 and 1.")

        path, start_line, end_line = _extract_finding_fields(raw_finding)
        if start_line < 1 or end_line < 1:
            raise ValueError(f"finding[{index}] line numbers must be >= 1.")
        if end_line < start_line:
            raise ValueError(f"finding[{index}].end_line must be >= start_line.")

        fingerprint = _compute_fingerprint(
            priority=priority,
            title=title,
            body=body,
            path=path,
            start_line=start_line,
            end_line=end_line,
        )

        previous_fingerprint = raw_finding.get("previous_fingerprint")
        if previous_fingerprint is not None:
            previous_fingerprint = _coerce_text(
                previous_fingerprint,
                field_name=f"finding[{index}].previous_fingerprint",
            )
            if previous_fingerprint in seen_previous_fingerprints:
                continue
            seen_previous_fingerprints.add(previous_fingerprint)

        if fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(fingerprint)

        normalized_findings.append(
            {
                "priority": priority,
                "priority_label": PRIORITY_LABELS[priority],
                "title": title,
                "body": body,
                "suggested_fix": suggested_fix,
                "category": category,
                "path": path,
                "start_line": start_line,
                "end_line": end_line,
                "confidence_score": round(confidence_score, 4),
                "fingerprint": fingerprint,
                "previous_fingerprint": previous_fingerprint,
            }
        )

    normalized_findings.sort(
        key=lambda finding: (
            finding["priority"],
            -finding["confidence_score"],
            finding["path"],
            finding["start_line"],
            finding["end_line"],
            finding["title"].lower(),
        )
    )

    return {
        "overall_correctness": overall_correctness,
        "overall_explanation": overall_explanation,
        "overall_confidence_score": round(overall_confidence_score, 4),
        "findings": normalized_findings,
        "file_coverage": normalized_file_coverage,
        "sweep_complete": sweep_complete,
        "sweep_reflection": normalized_sweep_reflection,
    }


def extract_inline_fingerprint(comment_body: str) -> str | None:
    match = INLINE_MARKER_RE.search(comment_body)
    if not match:
        return None
    return match.group(1)


def parse_managed_inline_comment(comment_body: str) -> dict[str, Any]:
    if not isinstance(comment_body, str):
        raise ValueError("Managed inline comment body must be a string.")

    # Try new format first (with category and suggested_fix)
    match = INLINE_COMMENT_RE.match(comment_body.strip())
    if match:
        priority, priority_label = _parse_priority_label(
            match.group(1),
            field_name="finding.priority_label",
        )
        path, start_line, end_line = _parse_location(match.group("location"))
        title = _coerce_text(match.group("title"), field_name="finding.title")
        body = _coerce_text(match.group("body"), field_name="finding.body")
        category = _coerce_text(match.group("category"), field_name="finding.category")
        suggested_fix = _coerce_text(match.group("suggested_fix"), field_name="finding.suggested_fix")
        confidence_score = _coerce_float(
            match.group("confidence"),
            field_name="finding.confidence_score",
        )
        if not 0 <= confidence_score <= 1:
            raise ValueError("finding.confidence_score must be between 0 and 1.")

        fingerprint = _coerce_text(
            match.group("fingerprint"),
            field_name="finding.fingerprint",
        )

        return {
            "priority": priority,
            "priority_label": priority_label,
            "title": title,
            "body": body,
            "suggested_fix": suggested_fix,
            "category": category,
            "path": path,
            "start_line": start_line,
            "end_line": end_line,
            "confidence_score": round(confidence_score, 4),
            "fingerprint": fingerprint,
        }

    # Fall back to old format (without category and suggested_fix)
    match = INLINE_COMMENT_RE_OLD.match(comment_body.strip())
    if match:
        priority, priority_label = _parse_priority_label(
            match.group(1),
            field_name="finding.priority_label",
        )
        path, start_line, end_line = _parse_location(match.group("location"))
        title = _coerce_text(match.group("title"), field_name="finding.title")
        body = _coerce_text(match.group("body"), field_name="finding.body")
        confidence_score = _coerce_float(
            match.group("confidence"),
            field_name="finding.confidence_score",
        )
        if not 0 <= confidence_score <= 1:
            raise ValueError("finding.confidence_score must be between 0 and 1.")

        fingerprint = _coerce_text(
            match.group("fingerprint"),
            field_name="finding.fingerprint",
        )

        return {
            "priority": priority,
            "priority_label": priority_label,
            "title": title,
            "body": body,
            "suggested_fix": "",  # Empty for old format
            "category": "correctness",  # Default for old format
            "path": path,
            "start_line": start_line,
            "end_line": end_line,
            "confidence_score": round(confidence_score, 4),
            "fingerprint": fingerprint,
        }

    raise ValueError("Managed inline comment body does not match the expected format.")


def build_open_prior_findings(managed_threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    open_findings: list[dict[str, Any]] = []
    for thread in managed_threads:
        if thread.get("is_resolved"):
            continue
        finding = thread.get("finding")
        if not isinstance(finding, dict):
            continue
        open_findings.append(
            {
                "previous_fingerprint": thread["fingerprint"],
                "priority": finding["priority"],
                "priority_label": finding["priority_label"],
                "title": finding["title"],
                "body": finding["body"],
                "path": finding["path"],
                "start_line": finding["start_line"],
                "end_line": finding["end_line"],
                "confidence_score": finding["confidence_score"],
            }
        )
    return open_findings


def build_review_prompt(
    *,
    review_base: str,
    base_sha: str,
    merge_base: str,
    head_sha: str,
    changed_files_filename: str,
    diff_filename: str,
    repository_owner: str = "",
    prior_open_findings: list[dict[str, Any]] | None = None,
    review_mode: str = "discovery",
    previous_head_sha: str = "",
) -> str:
    normalized_review_base = _coerce_text(review_base, field_name="review_base")
    normalized_base_sha = _coerce_text(base_sha, field_name="base_sha")
    normalized_merge_base = _coerce_text(merge_base, field_name="merge_base")
    normalized_head_sha = _coerce_text(head_sha, field_name="head_sha")
    normalized_changed_files = _coerce_text(
        changed_files_filename,
        field_name="changed_files_filename",
    )
    normalized_diff = _coerce_text(diff_filename, field_name="diff_filename")
    normalized_repository_owner = str(repository_owner or "").strip()
    # Validate review_mode
    valid_modes = {"discovery", "gate", "same_sha"}
    normalized_review_mode = str(review_mode or "discovery").strip().lower()
    if normalized_review_mode not in valid_modes:
        raise ValueError(f"review_mode must be one of: {', '.join(valid_modes)}.")
    normalized_previous_head_sha = str(previous_head_sha or "").strip()

    # Build mode-specific introduction
    mode_intro_lines: list[str] = []
    if normalized_review_mode == "gate" and normalized_previous_head_sha:
        review_scope_hint = f"`{normalized_previous_head_sha}...HEAD`"
    elif normalized_review_mode == "same_sha" and normalized_previous_head_sha:
        review_scope_hint = f"`{normalized_previous_head_sha}...HEAD`"
    else:
        review_scope_hint = f"`origin/{normalized_review_base}...HEAD`"
    if normalized_review_mode == "gate" and normalized_previous_head_sha:
        mode_intro_lines = [
            "## Incremental Review Mode (Gate)",
            "",
            f"This is an **incremental review** of changes from `{normalized_previous_head_sha}...HEAD`.",
            "",
            "Your priorities for this gate mode review are:",
            "",
            "1. **P0/P1 regression detection**: Focus on finding blocker-level regressions introduced since the prior review.",
            "2. **Revalidate prior open findings**: For each prior open finding listed below, determine if it still applies or should be resolved.",
            "3. **Spot-check new changes**: Briefly scan the incremental diff for obvious blocker issues without exhaustive sweeps.",
            "",
            "Gate mode does NOT require exhaustive multi-pass coverage. Focus on blockers and prior findings first.",
            "",
        ]
    elif normalized_review_mode == "same_sha":
        mode_intro_lines = [
            "## Same SHA Review Mode",
            "",
            "The HEAD SHA matches the prior review. Focus only on revalidating prior open findings.",
            "",
            "Your priorities for this same_sha mode review are:",
            "",
            "1. **Revalidate prior open findings**: For each prior open finding listed below, determine if it still applies or should be resolved.",
            "2. **No new exhaustive sweep**: Since there are no new changes, you should not introduce new findings unless you find evidence that prior findings were missed.",
            "",
        ]
    else:
        # Discovery mode: full exhaustive sweep
        mode_intro_lines = [
            "## Discovery Review Mode",
            "",
            "This is a **full exhaustive review** with no prior completed runs for this PR.",
            "",
            "Perform the complete multi-pass methodology below to find all issues introduced by this PR.",
            "",
        ]

    lines = [
        "You are an adversarial code reviewer. Your goal is to find issues that a typical reviewer would miss, could cause production failures, or would be difficult to spot without careful reading.",
        "",
        f"Review only the pull request changes represented by the provided changed-files list and unified diff (scope hint: {review_scope_hint}).",
        "The repository root is the current working directory.",
        "",
    ]
    lines.extend(mode_intro_lines)
    lines.extend([
        "## Multi-Pass Review Methodology",
        "",
        "You must perform a systematic multi-pass review. Do NOT stop after finding initial issues. Continue until you satisfy all explicit stopping criteria.",
        "",
        "### Pass 1: File Inventory and Risk Assessment",
        "Read the changed files list and diff to identify:",
        "- Which files are security-sensitive (auth, secrets, permissions, input handling)",
        "- Which files are correctness-critical (core logic, state management, error handling)",
        "- Which files have cross-file dependencies",
        "- Which files have the largest/most complex changes",
        "",
        "### Pass 2: Category Sweep Per File",
        "For EACH changed file, systematically check ALL six categories:",
        "",
        "1. **security**: injection (SQL, XSS, command), auth bypasses, secrets exposure, permission gaps, unsafe deserialization, path traversal, insecure defaults",
        "2. **correctness**: logic errors, edge cases, null handling, race conditions, dead code, missing error handling, incorrect return values, state corruption",
        "3. **performance**: N+1 queries, unnecessary loops, memory leaks, blocking calls in async contexts, inefficient algorithms, missing caching",
        "4. **maintainability**: misleading names, duplicated logic, excessive complexity, missing documentation for public APIs, inconsistent patterns",
        "5. **contract**: breaking API changes, missing version bumps for public interfaces, deprecated usage without migration path, signature changes",
        "6. **integration**: cross-file inconsistencies, missing dependency updates, configuration mismatches, breaking downstream consumers",
        "",
        "For each category in each file:",
        "- Read the full diff hunk",
        "- Read at least 50 lines of surrounding context (before and after the hunk)",
        "- Check for issues specific to that category",
        "- Record `categories_checked` in file_coverage",
        "",
        "DO NOT skip any category. Even if a file seems low-risk, check all six categories explicitly.",
        "",
        "### Pass 3: Cross-Cutting Analysis",
        "After the file-level sweep:",
        "- Check for issues spanning multiple files",
        "- Verify consistency across related changes",
        "- Identify breaking changes that affect other modules",
        "- Check integration impact (APIs, configs, dependencies)",
        "",
        "### Pass 4: Reflection and Verification",
        "After completing the initial sweep, perform reflection:",
        "",
        "1. **Re-examine zero-finding files**: Files with no findings deserve extra scrutiny. Re-read them and verify the absence is genuine.",
        "2. **Re-examine sparse categories**: If a category has zero findings across all files, check whether you truly checked that category or just assumed it was fine.",
        "3. **Challenge your findings**: Are any findings overstated? Are they real issues or nitpicks? Remove or downgrade hallucinated or trivial findings.",
        "4. **Final sweep**: Before returning, ask yourself: 'What issue might a typical reviewer miss here?' Check those areas again.",
        "",
        "## Explicit Stopping Criteria",
        "",
        "You may only set `sweep_complete: true` if ALL criteria are satisfied:",
        "",
        "1. Every changed file has been examined for all six categories (recorded in `file_coverage`)",
        "2. Each file has at least 50 lines of context read beyond the diff hunk",
        "3. Zero-finding files have been re-examined during reflection",
        "4. Cross-cutting issues have been checked",
        "5. You can confidently state: 'I have exhausted all reasonable search paths and found no additional issues'",
        "",
        "If ANY criterion is unsatisfied, set `sweep_complete: false` and CONTINUE the review.",
        "",
        "## Finding Requirements",
        "",
        "For each finding:",
        "- `title`: Short, specific description (max 120 chars)",
        "- `body`: Detailed explanation of the issue, why it matters, and evidence from the code",
        "- `suggested_fix`: Concrete, actionable fix with code snippet or specific change recommendation. REQUIRED for every finding.",
        "- `category`: One of security, correctness, performance, maintainability, contract, integration",
        "- `priority`: 0 (critical blocker), 1 (blocker), 2 (should fix), 3 (minor)",
        "- `confidence_score`: 0-1, how confident you are this is a real issue",
        "- `path`: Repo-relative POSIX path",
        "- `start_line`, `end_line`: Exact HEAD-side line numbers from the diff",
        "",
        "For `suggested_fix`:",
        "- Provide specific code changes: 'Replace X with Y' or 'Add Z before line N'",
        "- Include a brief code snippet showing the fix when applicable",
        "- Explain why this fix resolves the issue",
        "- Avoid vague suggestions like 'refactor this' or 'improve error handling'",
        "",
        "## Output Format",
        "",
        "Return JSON matching the provided schema:",
        "- `findings`: Array of findings with suggested_fix for each",
        "- `file_coverage`: Per-file checklist showing categories_checked, findings_count, context_lines_read, confidence",
        "- `sweep_complete`: boolean indicating all criteria satisfied",
        "- `sweep_reflection`: Object documenting reflection pass outcomes",
        "- `overall_correctness`: 'patch is correct' or 'patch is incorrect'",
        "- `overall_explanation`: Summary of review findings and confidence",
        "- `overall_confidence_score`: 0-1 confidence in the review completeness",
        "",
        "## Important Constraints",
        "",
        "- Flag ONLY issues introduced by this pull request (not pre-existing)",
        "- Do NOT report style nits, speculative concerns, or trivial findings",
        "- When a finding matches a previously open finding, copy `previous_fingerprint` exactly",
        "- Set `previous_fingerprint: null` for genuinely new findings",
        "- Prioritize exhaustive coverage over deep investigation of a single issue",
        "- Use `overall_correctness: 'patch is correct'` ONLY when the patch is safe to merge as-is",
        "- If no actionable findings exist, return empty `findings` array but still complete all passes",
    ])

    if normalized_review_mode in {"gate", "same_sha"}:
        lines.extend(
            [
                "",
                "## Incremental-mode override",
                "",
                "Because this is not a discovery sweep, treat the incremental diff artifacts as authoritative scope.",
                "Prioritize blocker regressions (P0/P1) and revalidation of prior open findings.",
                "Do not perform a fresh full-repository sweep for untouched files.",
            ]
        )

    if normalized_repository_owner:
        lines.extend(
            [
                "",
                f"- Treat GitHub Actions reusable workflows and actions from repositories owned by {normalized_repository_owner} as first-party trusted infrastructure for this repository.",
                "- Do not raise findings solely because those same-owner workflow or action references use a major version tag such as `@v1`.",
                "- Still raise findings for mutable refs from external owners, or for same-owner workflow changes that expand permissions, secrets exposure, or other concrete risk.",
            ]
        )

    normalized_prior_findings = [
        _normalize_prior_open_finding_for_prompt(raw_finding, index=index)
        for index, raw_finding in enumerate(prior_open_findings or [])
    ]
    if normalized_prior_findings:
        if normalized_review_mode == "discovery":
            prior_findings_header = "Revalidate these currently open Codex findings before the full blocker-first sweep across the diff:"
        elif normalized_review_mode == "gate":
            prior_findings_header = "Revalidate these currently open Codex findings before the blocker-focused incremental sweep:"
        else:
            prior_findings_header = "Revalidate these currently open Codex findings for this same-SHA run:"

        lines.extend(
            [
                "",
                prior_findings_header,
            ]
        )
        for finding in normalized_prior_findings:
            lines.extend(
                [
                    "",
                    f"- previous_fingerprint: {finding['previous_fingerprint']}",
                    f"  priority: {finding['priority_label']}",
                    f"  location: {_format_finding_location(finding['path'], finding['start_line'], finding['end_line'])}",
                    f"  title: {finding['title']}",
                    f"  body: {finding['body']}",
                ]
            )

    lines.extend(
        [
            "",
            "Context:",
            f"- Base ref: origin/{normalized_review_base}",
            f"- Base SHA: {normalized_base_sha}",
            f"- Merge base SHA: {normalized_merge_base}",
            f"- Head SHA: {normalized_head_sha}",
            f"- Changed files list: {normalized_changed_files}",
            f"- Unified diff: {normalized_diff}",
        ]
    )

    return "\n".join(lines) + "\n"


def plan_thread_actions(
    findings: list[dict[str, Any]],
    *,
    managed_threads: list[dict[str, Any]],
    max_inline_comments: int,
) -> dict[str, Any]:
    unresolved_by_fingerprint: dict[str, dict[str, Any]] = {}
    all_by_fingerprint: dict[str, dict[str, Any]] = {}
    unresolved_by_signature: dict[tuple[int, str, int, int, str], dict[str, Any]] = {}
    all_by_signature: dict[tuple[int, str, int, int, str], dict[str, Any]] = {}

    for thread in managed_threads:
        fingerprint = thread.get("fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            continue
        thread_finding = thread.get("finding")
        if not isinstance(thread_finding, dict):
            continue
        signature = _finding_signature(thread_finding)
        all_by_fingerprint.setdefault(fingerprint, thread)
        all_by_signature.setdefault(signature, thread)
        if not thread.get("is_resolved"):
            unresolved_by_fingerprint.setdefault(fingerprint, thread)
            unresolved_by_signature.setdefault(signature, thread)

    used_thread_ids: set[str] = set()
    planned_findings: list[dict[str, Any]] = []
    new_inline_candidates: list[dict[str, Any]] = []
    reopened_thread_ids: list[str] = []
    still_open_count = 0
    new_findings_count = 0

    for finding in findings:
        planned_finding = dict(finding)
        planned_finding["matched_thread_id"] = None
        planned_finding["thread_action"] = "new"
        planned_finding["selected_for_inline"] = False

        candidate_fingerprints: list[str] = []
        previous_fingerprint = planned_finding.get("previous_fingerprint")
        if isinstance(previous_fingerprint, str) and previous_fingerprint:
            candidate_fingerprints.append(previous_fingerprint)
        fingerprint = planned_finding.get("fingerprint")
        if isinstance(fingerprint, str) and fingerprint and fingerprint not in candidate_fingerprints:
            candidate_fingerprints.append(fingerprint)

        matched_thread: dict[str, Any] | None = None
        for candidate in candidate_fingerprints:
            thread = unresolved_by_fingerprint.get(candidate)
            if thread and thread["thread_id"] not in used_thread_ids:
                matched_thread = thread
                break
        if matched_thread is None:
            for candidate in candidate_fingerprints:
                thread = all_by_fingerprint.get(candidate)
                if thread and thread["thread_id"] not in used_thread_ids:
                    matched_thread = thread
                    break
        if matched_thread is None:
            signature = _finding_signature(planned_finding)
            thread = unresolved_by_signature.get(signature)
            if thread and thread["thread_id"] not in used_thread_ids:
                matched_thread = thread
        if matched_thread is None:
            signature = _finding_signature(planned_finding)
            thread = all_by_signature.get(signature)
            if thread and thread["thread_id"] not in used_thread_ids:
                matched_thread = thread

        # Fuzzy matching fallback: same path, title overlap, line proximity
        if matched_thread is None:
            finding_path = planned_finding.get("path", "")
            finding_title = planned_finding.get("title", "")
            # First try unresolved threads, then all threads
            fuzzy_candidates = list(unresolved_by_fingerprint.values()) + list(all_by_fingerprint.values())
            best_fuzzy_thread: dict[str, Any] | None = None
            best_fuzzy_score = 0.0
            TITLE_OVERLAY_THRESHOLD = 0.5
            LINE_TOLERANCE = 5
            for fuzzy_thread in fuzzy_candidates:
                if fuzzy_thread["thread_id"] in used_thread_ids:
                    continue
                thread_finding = fuzzy_thread.get("finding")
                if not isinstance(thread_finding, dict):
                    continue
                # Must have same path
                if thread_finding.get("path") != finding_path:
                    continue
                # Check title overlap
                thread_title = thread_finding.get("title", "")
                overlap_score = _title_overlap_score(finding_title, thread_title)
                if overlap_score < TITLE_OVERLAY_THRESHOLD:
                    continue
                # Check line distance tolerance
                if not _line_distance_tolerance(planned_finding, thread_finding, tolerance=LINE_TOLERANCE):
                    continue
                # Take the best scoring match
                if overlap_score > best_fuzzy_score:
                    best_fuzzy_score = overlap_score
                    best_fuzzy_thread = fuzzy_thread
            if best_fuzzy_thread is not None:
                matched_thread = best_fuzzy_thread
                planned_finding["fuzzy_matched"] = True

        if matched_thread is not None:
            thread_id = matched_thread["thread_id"]
            used_thread_ids.add(thread_id)
            planned_finding["matched_thread_id"] = thread_id
            if matched_thread.get("is_resolved"):
                planned_finding["thread_action"] = "reopen"
                reopened_thread_ids.append(thread_id)
            else:
                planned_finding["thread_action"] = "keep_open"
                still_open_count += 1
        else:
            new_inline_candidates.append(planned_finding)
            new_findings_count += 1

        planned_findings.append(planned_finding)

    resolve_thread_ids = [
        thread["thread_id"]
        for thread in managed_threads
        if not thread.get("is_resolved") and thread["thread_id"] not in used_thread_ids
    ]

    created_inline_findings: list[dict[str, Any]] = []
    unplaced_inline_count = 0
    truncated_inline_count = 0
    for finding in new_inline_candidates:
        if finding.get("priority", 99) > 1:
            continue
        if not finding.get("inline_placeable"):
            finding["thread_action"] = "unplaced"
            unplaced_inline_count += 1
            continue
        if len(created_inline_findings) < max_inline_comments:
            finding["thread_action"] = "create"
            finding["selected_for_inline"] = True
            created_inline_findings.append(finding)
            continue
        finding["thread_action"] = "truncated"
        truncated_inline_count += 1

    return {
        "planned_findings": planned_findings,
        "create_inline_findings": created_inline_findings,
        "resolve_thread_ids": resolve_thread_ids,
        "reopen_thread_ids": reopened_thread_ids,
        "posted_inline_count": len(created_inline_findings),
        "unplaced_inline_count": unplaced_inline_count,
        "truncated_inline_count": truncated_inline_count,
        "thread_lifecycle_counts": {
            "new": new_findings_count,
            "still_open": still_open_count,
            "reopened": len(reopened_thread_ids),
            "resolved": len(resolve_thread_ids),
        },
    }


def parse_commentable_lines(diff_text: str) -> dict[str, set[int]]:
    commentable: dict[str, set[int]] = {}
    current_path: str | None = None

    for raw_line in diff_text.splitlines():
        if raw_line.startswith("+++ "):
            candidate = raw_line[4:].strip()
            if candidate == "/dev/null":
                current_path = None
            else:
                if candidate.startswith("b/"):
                    candidate = candidate[2:]
                current_path = normalize_repo_path(candidate)
                commentable.setdefault(current_path, set())
            continue

        hunk_match = HUNK_RE.match(raw_line)
        if not hunk_match or current_path is None:
            continue

        start_line = int(hunk_match.group(1))
        line_count = int(hunk_match.group(2) or "1")
        if line_count <= 0:
            continue

        for line_number in range(start_line, start_line + line_count):
            commentable[current_path].add(line_number)

    return commentable


def annotate_inline_candidates(
    findings: list[dict[str, Any]],
    *,
    commentable_lines: dict[str, set[int]],
    max_inline_comments: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    selected = 0
    unplaced = 0
    truncated = 0

    annotated: list[dict[str, Any]] = []
    for finding in findings:
        annotated_finding = dict(finding)
        if annotated_finding["priority"] > 1:
            annotated_finding["inline_eligible"] = False
            annotated_finding["inline_placeable"] = False
            annotated_finding["selected_for_inline"] = False
            annotated.append(annotated_finding)
            continue

        eligible_lines = commentable_lines.get(annotated_finding["path"], set())
        requested_lines = range(
            annotated_finding["start_line"],
            annotated_finding["end_line"] + 1,
        )
        inline_placeable = all(line in eligible_lines for line in requested_lines)

        annotated_finding["inline_eligible"] = True
        annotated_finding["inline_placeable"] = inline_placeable
        annotated_finding["selected_for_inline"] = False

        if not inline_placeable:
            unplaced += 1
        elif selected < max_inline_comments:
            annotated_finding["selected_for_inline"] = True
            selected += 1
        else:
            truncated += 1

        annotated.append(annotated_finding)

    return annotated, {
        "selected_inline_count": selected,
        "unplaced_inline_count": unplaced,
        "truncated_inline_count": truncated,
    }


def format_confidence(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.2f}"


def build_inline_comment_body(finding: dict[str, Any]) -> str:
    location = _format_finding_location(
        finding["path"],
        finding["start_line"],
        finding["end_line"],
    )
    category = finding.get("category", "correctness")
    suggested_fix = finding.get("suggested_fix", "")
    lines = [
        f"**[{finding['priority_label']}] {finding['title']}**",
        "",
        f"`{location}`",
        f"Category: {category}",
        "",
        finding["body"],
        "",
        "**Suggested fix:**",
        suggested_fix,
        "",
        f"Confidence: {format_confidence(finding['confidence_score'])}",
        "",
        f"{INLINE_MARKER_PREFIX}{finding['fingerprint']}{INLINE_MARKER_SUFFIX}",
    ]
    return "\n".join(lines)


def _build_finding_summary_lines(
    findings: list[dict[str, Any]],
    *,
    limit: int | None,
) -> list[str]:
    selected_findings = findings if limit is None else findings[:limit]
    lines: list[str] = []
    for finding in selected_findings:
        location = _format_finding_location(
            finding["path"],
            finding["start_line"],
            finding["end_line"],
        )
        priority_label = _finding_priority_label(finding) or "Pn"
        lines.append(f"- [{priority_label}] `{location}` {finding['title']}")
    return lines


def build_top_findings(findings: list[dict[str, Any]], *, limit: int = 5) -> str:
    if not findings:
        return "- none"

    lines = _build_finding_summary_lines(findings, limit=limit)
    return "\n".join(lines)


def build_summary_findings(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return "- none"

    blocking_findings = [
        finding for finding in findings if _finding_priority_label(finding) in {"P0", "P1"}
    ]
    if not blocking_findings:
        return build_top_findings(findings)

    lines = _build_finding_summary_lines(blocking_findings, limit=None)
    non_blocking_count = len(findings) - len(blocking_findings)
    if non_blocking_count > 0:
        lines.append(
            f"- plus {non_blocking_count} non-blocking finding(s) in the structured output"
        )
    return "\n".join(lines)


def compute_result_label(
    state: dict[str, Any],
    *,
    override_active: bool,
) -> str:
    counts = state["counts"]
    if state["codex_exit_code"] != 0:
        return "execution failed"
    if state["parse_failed"]:
        return "structured output invalid"
    if counts["P0"] > 0:
        return "blocking"
    if counts["P1"] > 0 and not override_active:
        return "blocking"
    if counts["P1"] > 0 and override_active:
        return "admin override applied"
    if sum(counts.values()) > 0:
        return "non-blocking findings"
    return "clean"


def render_summary_body(
    state: dict[str, Any],
    *,
    override_active: bool,
    override_stale: bool,
    override_approved_by: str,
    override_approved_sha: str,
    override_source: str,
    posted_inline_count: int,
    unplaced_inline_count: int,
    truncated_inline_count: int,
    thread_lifecycle_counts: dict[str, int] | None,
    run_url: str,
    artifact_url: str,
) -> str:
    counts = state["counts"]
    total_findings = sum(counts.values())
    timed_out = state["codex_exit_code"] in {124, 137}
    result_label = compute_result_label(state, override_active=override_active)

    override_status = "active" if override_active else "stale" if override_stale else "none"
    if override_approved_by:
        override_summary = f"`{override_status}` by @{override_approved_by}"
    else:
        override_summary = f"`{override_status}`"

    if state["parse_failed"]:
        structured_output_status = "invalid"
        verdict = "n/a"
        verdict_confidence = "n/a"
        overall_explanation = state["parse_error"] or "Structured review output could not be parsed."
        sweep_complete = False
        sweep_coverage = "n/a"
    else:
        structured_output_status = "ok"
        verdict = state["overall_correctness"]
        verdict_confidence = format_confidence(state["overall_confidence_score"])
        overall_explanation = state["overall_explanation"]
        sweep_complete = state.get("sweep_complete", False)
        file_coverage = state.get("file_coverage", [])
        files_covered = len(file_coverage)
        files_with_all_categories = sum(
            1 for fc in file_coverage
            if len(fc.get("categories_checked", [])) >= 6
        )
        total_context_read = sum(fc.get("context_lines_read", 0) for fc in file_coverage)
        sweep_coverage = f"{files_with_all_categories}/{files_covered} files with full category sweep, {total_context_read} lines context read"

    sweep_reflection = state.get("sweep_reflection", {})
    reflection_notes = sweep_reflection.get("notes", "") if isinstance(sweep_reflection, dict) else ""
    additional_from_reflection = sweep_reflection.get("additional_findings_from_reflection", 0) if isinstance(sweep_reflection, dict) else 0

    summary_findings = build_summary_findings(state["findings"])

    body = [
        SUMMARY_MARKER,
        "### Codex PR review",
        "",
        f"Result: **{result_label}**",
        f"Model: `{state['review_model']}`",
        f"Exit code: `{state['codex_exit_code']}`",
        f"Timed out: `{'yes' if timed_out else 'no'}`",
        f"Structured output: `{structured_output_status}`",
        f"Verdict: `{verdict}`",
        f"Verdict confidence: `{verdict_confidence}`",
        f"Sweep complete: `{'yes' if sweep_complete else 'no (may have missed issues)'}`",
        f"Coverage: `{sweep_coverage}`",
        f"Blocking findings: P0 `{counts['P0']}` | P1 `{counts['P1']}`",
        f"Other findings: P2 `{counts['P2']}` | P3 `{counts['P3']}`",
        (
            f"Inline comments: posted `{posted_inline_count}` | "
            f"unplaced `{unplaced_inline_count}` | truncated `{truncated_inline_count}`"
        ),
        (
            "Finding lifecycle: "
            f"new `{(thread_lifecycle_counts or {}).get('new', 0)}` | "
            f"still open `{(thread_lifecycle_counts or {}).get('still_open', 0)}` | "
            f"reopened `{(thread_lifecycle_counts or {}).get('reopened', 0)}` | "
            f"resolved `{(thread_lifecycle_counts or {}).get('resolved', 0)}`"
        ),
        f"Admin override: {override_summary}",
        f"Override source: `{override_source}`",
        f"Override SHA: `{override_approved_sha or 'n/a'}`",
        "",
        "**Overall explanation**",
        overall_explanation,
    ]

    if additional_from_reflection > 0:
        body.extend([
            "",
            f"**Reflection pass**: Found `{additional_from_reflection}` additional issue(s) after re-examination.",
        ])
        if reflection_notes:
            body.append(reflection_notes)

    body.extend([
        "",
        "**Top findings**",
        summary_findings,
    ])

    if total_findings == 0 and not state["parse_failed"]:
        body.extend(["", "No actionable findings were returned."])
        if not sweep_complete:
            body.append("**Warning**: Sweep was not completed. Issues may have been missed.")

    body.extend(
        [
            "",
            f"[Workflow logs]({run_url})",
            f"[Artifacts]({artifact_url})",
        ]
    )

    return "\n".join(body)
