#!/usr/bin/env python3

from __future__ import annotations

import json
import os
from pathlib import Path

from codex_review_lib import (
    annotate_inline_candidates,
    load_json_text,
    normalize_review_payload,
    parse_commentable_lines,
)


def _read_required_path(env_name: str, default: str) -> Path:
    return Path(os.environ.get(env_name, default))


def _to_int(env_name: str, default: str) -> int:
    value = os.environ.get(env_name, default)
    return int(value)


def main() -> int:
    raw_output_path = _read_required_path("CODEX_REVIEW_RAW_OUTPUT_PATH", "codex-review.json")
    diff_path = _read_required_path("CODEX_REVIEW_DIFF_PATH", "codex-review.diff")
    state_path = _read_required_path("CODEX_REVIEW_STATE_PATH", "codex-review-state.json")
    state_env_path = _read_required_path("CODEX_REVIEW_STATE_ENV_PATH", "codex-review-state.env")

    review_model = os.environ.get("CODEX_REVIEW_MODEL", "gpt-5.4")
    review_base = os.environ.get("CODEX_REVIEW_BASE", "")
    head_sha = os.environ.get("CODEX_REVIEW_HEAD_SHA", "")
    codex_exit_code = _to_int("CODEX_REVIEW_EXIT_CODE", "1")
    max_inline_comments = _to_int("CODEX_REVIEW_MAX_INLINE_COMMENTS", "10")

    state: dict[str, object] = {
        "codex_exit_code": codex_exit_code,
        "parse_failed": False,
        "parse_error": "",
        "review_model": review_model,
        "review_base": review_base,
        "head_sha": head_sha,
        "max_inline_comments": max_inline_comments,
        "overall_correctness": "",
        "overall_explanation": "",
        "overall_confidence_score": None,
        "counts": {"P0": 0, "P1": 0, "P2": 0, "P3": 0},
        "findings": [],
        "selected_inline_count": 0,
        "unplaced_inline_count": 0,
        "truncated_inline_count": 0,
    }

    try:
        if not raw_output_path.exists():
            raise ValueError(f"Structured review output was not generated at {raw_output_path}.")
        payload = load_json_text(raw_output_path.read_text(encoding="utf-8", errors="replace"))
        normalized = normalize_review_payload(payload)
        if not diff_path.exists():
            raise ValueError(f"Review diff was not generated at {diff_path}.")
        commentable_lines = parse_commentable_lines(
            diff_path.read_text(encoding="utf-8", errors="replace")
        )
        findings, inline_counts = annotate_inline_candidates(
            normalized["findings"],
            commentable_lines=commentable_lines,
            max_inline_comments=max_inline_comments,
        )

        counts = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
        for finding in findings:
            counts[finding["priority_label"]] += 1

        state.update(
            {
                "overall_correctness": normalized["overall_correctness"],
                "overall_explanation": normalized["overall_explanation"],
                "overall_confidence_score": normalized["overall_confidence_score"],
                "counts": counts,
                "findings": findings,
                **inline_counts,
            }
        )
    except Exception as exc:  # noqa: BLE001
        state["parse_failed"] = True
        state["parse_error"] = str(exc)

    state_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    counts = state["counts"]
    findings = state["findings"]
    state_env_path.write_text(
        "\n".join(
            [
                f"p0_count={counts['P0']}",
                f"p1_count={counts['P1']}",
                f"p2_count={counts['P2']}",
                f"p3_count={counts['P3']}",
                f"parse_failed={1 if state['parse_failed'] else 0}",
                f"findings_total={len(findings)}",
                f"selected_inline_count={state['selected_inline_count']}",
                f"unplaced_inline_count={state['unplaced_inline_count']}",
                f"truncated_inline_count={state['truncated_inline_count']}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
