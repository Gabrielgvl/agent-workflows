from __future__ import annotations

import importlib.util
import pathlib
import unittest


def load_module():
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    module_path = repo_root / ".github" / "codex-review" / "codex_review_lib.py"
    spec = importlib.util.spec_from_file_location("codex_review_lib", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


lib = load_module()


class NormalizeReviewPayloadTests(unittest.TestCase):
    def test_normalizes_findings_and_deduplicates(self) -> None:
        payload = {
            "findings": [
                {
                    "title": "Missing error handling",
                    "body": "The new branch can raise and skip cleanup.",
                    "confidence_score": 0.92,
                    "priority": 1,
                    "path": "src/review.py",
                    "start_line": 14,
                    "end_line": 18,
                },
                {
                    "title": "Missing error handling",
                    "body": "The new branch can raise and skip cleanup.",
                    "confidence_score": 0.92,
                    "priority": 1,
                    "path": "src/review.py",
                    "start_line": 14,
                    "end_line": 18,
                },
            ],
            "overall_correctness": "patch is incorrect",
            "overall_explanation": "The failure path is broken.",
            "overall_confidence_score": 0.9,
        }

        normalized = lib.normalize_review_payload(payload)

        self.assertEqual(normalized["overall_correctness"], "patch is incorrect")
        self.assertEqual(len(normalized["findings"]), 1)
        self.assertEqual(normalized["findings"][0]["priority_label"], "P1")
        self.assertEqual(normalized["findings"][0]["path"], "src/review.py")

    def test_supports_cookbook_style_code_location(self) -> None:
        payload = {
            "findings": [
                {
                    "title": "Wrong branch",
                    "body": "The condition uses the wrong state.",
                    "confidence_score": 0.81,
                    "priority": 0,
                    "code_location": {
                        "absolute_file_path": "src/state.py",
                        "line_range": {"start": 8, "end": 9},
                    },
                }
            ],
            "overall_correctness": "patch is incorrect",
            "overall_explanation": "A blocking condition is inverted.",
            "overall_confidence_score": 0.88,
        }

        normalized = lib.normalize_review_payload(payload)

        finding = normalized["findings"][0]
        self.assertEqual(finding["path"], "src/state.py")
        self.assertEqual(finding["start_line"], 8)
        self.assertEqual(finding["end_line"], 9)


class InlinePlacementTests(unittest.TestCase):
    def test_classifies_placeable_unplaced_and_truncated_findings(self) -> None:
        diff_text = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -8,2 +8,5 @@
 existing = True
+new_value = build()
+return new_value
@@ -20,1 +23,2 @@
 old_line = False
+cleanup()
"""
        commentable = lib.parse_commentable_lines(diff_text)

        findings = [
            {
                "priority": 0,
                "priority_label": "P0",
                "title": "Broken initialization",
                "body": "The new value can be undefined.",
                "path": "src/app.py",
                "start_line": 9,
                "end_line": 10,
                "confidence_score": 0.98,
                "fingerprint": "aaaa",
            },
            {
                "priority": 1,
                "priority_label": "P1",
                "title": "Missing cleanup guard",
                "body": "The cleanup call runs without a null check.",
                "path": "src/app.py",
                "start_line": 23,
                "end_line": 23,
                "confidence_score": 0.72,
                "fingerprint": "bbbb",
            },
            {
                "priority": 1,
                "priority_label": "P1",
                "title": "Unplaceable finding",
                "body": "This points outside the diff.",
                "path": "src/app.py",
                "start_line": 40,
                "end_line": 40,
                "confidence_score": 0.7,
                "fingerprint": "cccc",
            },
        ]

        annotated, counts = lib.annotate_inline_candidates(
            findings,
            commentable_lines=commentable,
            max_inline_comments=1,
        )

        self.assertTrue(annotated[0]["selected_for_inline"])
        self.assertFalse(annotated[1]["selected_for_inline"])
        self.assertTrue(annotated[1]["inline_placeable"])
        self.assertFalse(annotated[2]["inline_placeable"])
        self.assertEqual(counts["selected_inline_count"], 1)
        self.assertEqual(counts["truncated_inline_count"], 1)
        self.assertEqual(counts["unplaced_inline_count"], 1)


class SummaryRenderingTests(unittest.TestCase):
    def test_renders_summary_body(self) -> None:
        state = {
            "codex_exit_code": 0,
            "parse_failed": False,
            "parse_error": "",
            "review_model": "gpt-5.4",
            "overall_correctness": "patch is incorrect",
            "overall_explanation": "A blocking issue was found.",
            "overall_confidence_score": 0.91,
            "counts": {"P0": 1, "P1": 0, "P2": 0, "P3": 0},
            "findings": [
                {
                    "priority_label": "P0",
                    "path": "src/app.py",
                    "start_line": 12,
                    "end_line": 12,
                    "title": "Broken auth check",
                }
            ],
        }

        body = lib.render_summary_body(
            state,
            override_active=False,
            override_stale=False,
            override_approved_by="",
            override_approved_sha="",
            override_source="none",
            posted_inline_count=1,
            unplaced_inline_count=0,
            truncated_inline_count=0,
            run_url="https://github.com/example/repo/actions/runs/1",
            artifact_url="https://github.com/example/repo/actions/runs/1#artifacts",
        )

        self.assertIn("<!-- codex-pr-review -->", body)
        self.assertIn("Result: **blocking**", body)
        self.assertIn("Model: `gpt-5.4`", body)
        self.assertIn("`src/app.py:12`", body)


if __name__ == "__main__":
    unittest.main()
