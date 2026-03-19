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
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


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

    normalized_findings: list[dict[str, Any]] = []
    seen_fingerprints: set[str] = set()

    for index, raw_finding in enumerate(findings):
        if not isinstance(raw_finding, dict):
            raise ValueError(f"finding[{index}] must be an object.")

        title = _coerce_text(raw_finding.get("title"), field_name=f"finding[{index}].title")
        body = _coerce_text(raw_finding.get("body"), field_name=f"finding[{index}].body")
        priority = _coerce_int(raw_finding.get("priority"), field_name=f"finding[{index}].priority")
        if priority not in PRIORITY_LABELS:
            raise ValueError(f"finding[{index}].priority must be between 0 and 3.")

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

        fingerprint_source = json.dumps(
            {
                "priority": priority,
                "title": title,
                "body": body,
                "path": path,
                "start_line": start_line,
                "end_line": end_line,
            },
            sort_keys=True,
        )
        fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:16]
        if fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(fingerprint)

        normalized_findings.append(
            {
                "priority": priority,
                "priority_label": PRIORITY_LABELS[priority],
                "title": title,
                "body": body,
                "path": path,
                "start_line": start_line,
                "end_line": end_line,
                "confidence_score": round(confidence_score, 4),
                "fingerprint": fingerprint,
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
    location = (
        f"{finding['path']}:{finding['start_line']}"
        if finding["start_line"] == finding["end_line"]
        else f"{finding['path']}:{finding['start_line']}-{finding['end_line']}"
    )
    lines = [
        f"**[{finding['priority_label']}] {finding['title']}**",
        "",
        f"`{location}`",
        "",
        finding["body"],
        "",
        f"Confidence: {format_confidence(finding['confidence_score'])}",
        "",
        f"{INLINE_MARKER_PREFIX}{finding['fingerprint']}{INLINE_MARKER_SUFFIX}",
    ]
    return "\n".join(lines)


def build_top_findings(findings: list[dict[str, Any]], *, limit: int = 5) -> str:
    if not findings:
        return "- none"

    lines: list[str] = []
    for finding in findings[:limit]:
        location = (
            f"{finding['path']}:{finding['start_line']}"
            if finding["start_line"] == finding["end_line"]
            else f"{finding['path']}:{finding['start_line']}-{finding['end_line']}"
        )
        lines.append(f"- [{finding['priority_label']}] `{location}` {finding['title']}")
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
    else:
        structured_output_status = "ok"
        verdict = state["overall_correctness"]
        verdict_confidence = format_confidence(state["overall_confidence_score"])
        overall_explanation = state["overall_explanation"]

    top_findings = build_top_findings(state["findings"])

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
        f"Blocking findings: P0 `{counts['P0']}` | P1 `{counts['P1']}`",
        f"Other findings: P2 `{counts['P2']}` | P3 `{counts['P3']}`",
        (
            f"Inline comments: posted `{posted_inline_count}` | "
            f"unplaced `{unplaced_inline_count}` | truncated `{truncated_inline_count}`"
        ),
        f"Admin override: {override_summary}",
        f"Override source: `{override_source}`",
        f"Override SHA: `{override_approved_sha or 'n/a'}`",
        "",
        "**Overall explanation**",
        overall_explanation,
        "",
        "**Top findings**",
        top_findings,
    ]

    if total_findings == 0 and not state["parse_failed"]:
        body.extend(["", "No actionable findings were returned."])

    body.extend(
        [
            "",
            f"[Workflow logs]({run_url})",
            f"[Artifacts]({artifact_url})",
        ]
    )

    return "\n".join(body)
