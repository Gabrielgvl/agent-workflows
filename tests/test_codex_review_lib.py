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

    def test_preserves_previous_fingerprint(self) -> None:
        payload = {
            "findings": [
                {
                    "title": "Regression is still present",
                    "body": "The guard is still missing after the rerun.",
                    "confidence_score": 0.87,
                    "priority": 1,
                    "path": "src/review.py",
                    "start_line": 22,
                    "end_line": 22,
                    "previous_fingerprint": "abcd1234abcd1234",
                }
            ],
            "overall_correctness": "patch is incorrect",
            "overall_explanation": "A prior blocking finding still reproduces.",
            "overall_confidence_score": 0.93,
        }

        normalized = lib.normalize_review_payload(payload)

        self.assertEqual(
            normalized["findings"][0]["previous_fingerprint"],
            "abcd1234abcd1234",
        )


class ManagedInlineCommentParsingTests(unittest.TestCase):
    def test_parses_existing_managed_inline_comment(self) -> None:
        body = "\n".join(
            [
                "**[P1] Missing cleanup guard**",
                "",
                "`src/app.py:23-24`",
                "Category: correctness",
                "",
                "The cleanup path can dereference a null result.",
                "",
                "**Suggested fix:**",
                "Add a null check before cleanup: if result is not None: cleanup()",
                "",
                "Confidence: 0.72",
                "",
                "<!-- codex-pr-review-inline:bbbbbbbbbbbbbbbb -->",
            ]
        )

        finding = lib.parse_managed_inline_comment(body)

        self.assertEqual(finding["priority"], 1)
        self.assertEqual(finding["priority_label"], "P1")
        self.assertEqual(finding["path"], "src/app.py")
        self.assertEqual(finding["start_line"], 23)
        self.assertEqual(finding["end_line"], 24)
        self.assertEqual(finding["fingerprint"], "bbbbbbbbbbbbbbbb")
        self.assertEqual(finding["category"], "correctness")
        self.assertEqual(finding["suggested_fix"], "Add a null check before cleanup: if result is not None: cleanup()")

    def test_parses_old_format_inline_comment_for_backward_compatibility(self) -> None:
        """Test that old format comments (without category/suggested_fix) are still parseable."""
        body = "\n".join(
            [
                "**[P1] Missing cleanup guard**",
                "",
                "`src/app.py:23-24`",
                "",
                "The cleanup path can dereference a null result.",
                "",
                "Confidence: 0.72",
                "",
                "<!-- codex-pr-review-inline:bbbbbbbbbbbbbbbb -->",
            ]
        )

        finding = lib.parse_managed_inline_comment(body)

        self.assertEqual(finding["priority"], 1)
        self.assertEqual(finding["priority_label"], "P1")
        self.assertEqual(finding["path"], "src/app.py")
        self.assertEqual(finding["start_line"], 23)
        self.assertEqual(finding["end_line"], 24)
        self.assertEqual(finding["fingerprint"], "bbbbbbbbbbbbbbbb")
        # Old format gets defaults
        self.assertEqual(finding["category"], "correctness")
        self.assertEqual(finding["suggested_fix"], "")


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
                "category": "correctness",
                "suggested_fix": "Apply appropriate fix based on the issue.",
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
                "category": "correctness",
                "suggested_fix": "Apply appropriate fix based on the issue.",
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
                "category": "correctness",
                "suggested_fix": "Apply appropriate fix based on the issue.",
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


class PromptBuilderTests(unittest.TestCase):
    def test_builds_exhaustive_blocker_first_prompt(self) -> None:
        prompt = lib.build_review_prompt(
            review_base="main",
            base_sha="a" * 40,
            merge_base="b" * 40,
            head_sha="c" * 40,
            changed_files_filename="codex-changed-files.txt",
            diff_filename="codex-review.diff",
            repository_owner="Gabrielgvl",
            prior_open_findings=[
                {
                    "previous_fingerprint": "abcd1234abcd1234",
                    "priority_label": "P1",
                    "title": "Regression is still present",
                    "body": "The guard is still missing after the rerun.",
                    "path": "src/review.py",
                    "start_line": 22,
                    "end_line": 22,
                }
            ],
        )

        # Check for new multi-pass methodology
        self.assertIn("Multi-Pass Review Methodology", prompt)
        self.assertIn("Category Sweep Per File", prompt)
        self.assertIn("Reflection and Verification", prompt)
        self.assertIn("Explicit Stopping Criteria", prompt)
        self.assertIn("sweep_complete: true", prompt)
        self.assertIn("suggested_fix", prompt)
        self.assertIn("Revalidate these currently open Codex findings before the full blocker-first sweep across the diff:", prompt)
        self.assertIn("previous_fingerprint: abcd1234abcd1234", prompt)
        self.assertIn("Treat GitHub Actions reusable workflows and actions from repositories owned by Gabrielgvl as first-party trusted infrastructure", prompt)
        # Check for adversarial framing
        self.assertIn("adversarial code reviewer", prompt)
        # Check for category coverage requirement
        self.assertIn("check ALL six categories", prompt)

    def test_rejects_invalid_prior_open_findings_for_prompt(self) -> None:
        with self.assertRaisesRegex(ValueError, "prior_open_findings\\[0\\]\\.priority_label"):
            lib.build_review_prompt(
                review_base="main",
                base_sha="a" * 40,
                merge_base="b" * 40,
                head_sha="c" * 40,
                changed_files_filename="codex-changed-files.txt",
                diff_filename="codex-review.diff",
                prior_open_findings=[
                    {
                        "previous_fingerprint": "abcd1234abcd1234",
                        "title": "Regression is still present",
                        "body": "The guard is still missing after the rerun.",
                        "path": "src/review.py",
                        "start_line": 22,
                        "end_line": 22,
                    }
                ],
            )


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
            thread_lifecycle_counts={"new": 1, "still_open": 0, "reopened": 0, "resolved": 0},
            run_url="https://github.com/example/repo/actions/runs/1",
            artifact_url="https://github.com/example/repo/actions/runs/1#artifacts",
        )

        self.assertIn("<!-- codex-pr-review -->", body)
        self.assertIn("Result: **blocking**", body)
        self.assertIn("Model: `gpt-5.4`", body)
        self.assertIn("`src/app.py:12`", body)
        self.assertIn("Finding lifecycle: new `1`", body)

    def test_renders_all_blocking_findings_without_truncation(self) -> None:
        findings = []
        for line_number in range(10, 16):
            findings.append(
                {
                    "priority": 1,
                    "priority_label": "P1",
                    "path": f"src/file{line_number}.py",
                    "start_line": line_number,
                    "end_line": line_number,
                    "title": f"Blocking issue {line_number}",
                }
            )
        findings.append(
            {
                "priority": 2,
                "priority_label": "P2",
                "path": "src/non_blocking.py",
                "start_line": 40,
                "end_line": 40,
                "title": "Minor follow-up",
            }
        )

        state = {
            "codex_exit_code": 0,
            "parse_failed": False,
            "parse_error": "",
            "review_model": "gpt-5.4",
            "overall_correctness": "patch is incorrect",
            "overall_explanation": "Several blockers were found.",
            "overall_confidence_score": 0.95,
            "counts": {"P0": 0, "P1": 6, "P2": 1, "P3": 0},
            "findings": findings,
        }

        body = lib.render_summary_body(
            state,
            override_active=False,
            override_stale=False,
            override_approved_by="",
            override_approved_sha="",
            override_source="none",
            posted_inline_count=6,
            unplaced_inline_count=0,
            truncated_inline_count=1,
            thread_lifecycle_counts={"new": 6, "still_open": 0, "reopened": 0, "resolved": 0},
            run_url="https://github.com/example/repo/actions/runs/1",
            artifact_url="https://github.com/example/repo/actions/runs/1#artifacts",
        )

        for line_number in range(10, 16):
            self.assertIn(f"`src/file{line_number}.py:{line_number}`", body)
        self.assertIn("plus 1 non-blocking finding(s)", body)


class ThreadActionPlanningTests(unittest.TestCase):
    def test_plans_keep_reopen_create_and_resolve(self) -> None:
        managed_threads = [
            {
                "thread_id": "thread-open",
                "is_resolved": False,
                "fingerprint": "openfingerprint01",
                "finding": {
                    "priority": 1,
                    "priority_label": "P1",
                    "title": "Still broken",
                    "body": "The cleanup path is still unsafe.",
                    "path": "src/app.py",
                    "start_line": 23,
                    "end_line": 23,
                    "confidence_score": 0.71,
                    "category": "correctness",
                    "suggested_fix": "Apply appropriate fix based on the issue.",
                    "fingerprint": "openfingerprint01",
                },
            },
            {
                "thread_id": "thread-resolved",
                "is_resolved": True,
                "fingerprint": "resolvedfinding02",
                "finding": {
                    "priority": 0,
                    "priority_label": "P0",
                    "title": "Reopen me",
                    "body": "The auth bypass still exists.",
                    "path": "src/auth.py",
                    "start_line": 10,
                    "end_line": 10,
                    "confidence_score": 0.99,
                    "category": "correctness",
                    "suggested_fix": "Apply appropriate fix based on the issue.",
                    "fingerprint": "resolvedfinding02",
                },
            },
            {
                "thread_id": "thread-to-resolve",
                "is_resolved": False,
                "fingerprint": "resolvednow0003",
                "finding": {
                    "priority": 1,
                    "priority_label": "P1",
                    "title": "Will be resolved",
                    "body": "This finding disappeared.",
                    "path": "src/old.py",
                    "start_line": 4,
                    "end_line": 4,
                    "confidence_score": 0.64,
                    "category": "correctness",
                    "suggested_fix": "Apply appropriate fix based on the issue.",
                    "fingerprint": "resolvednow0003",
                },
            },
        ]
        findings = [
            {
                "priority": 1,
                "priority_label": "P1",
                "title": "Still broken",
                "body": "The cleanup path is still unsafe.",
                "path": "src/app.py",
                "start_line": 23,
                "end_line": 23,
                "confidence_score": 0.71,
                "category": "correctness",
                "suggested_fix": "Apply appropriate fix based on the issue.",
                "fingerprint": "openfingerprint01",
                "previous_fingerprint": "openfingerprint01",
                "inline_placeable": True,
            },
            {
                "priority": 0,
                "priority_label": "P0",
                "title": "Reopen me",
                "body": "The auth bypass still exists.",
                "path": "src/auth.py",
                "start_line": 10,
                "end_line": 10,
                "confidence_score": 0.99,
                "category": "correctness",
                "suggested_fix": "Apply appropriate fix based on the issue.",
                "fingerprint": "newfingerprint04",
                "previous_fingerprint": "resolvedfinding02",
                "inline_placeable": True,
            },
            {
                "priority": 1,
                "priority_label": "P1",
                "title": "Brand new issue",
                "body": "This one needs a new thread.",
                "path": "src/new.py",
                "start_line": 8,
                "end_line": 8,
                "confidence_score": 0.88,
                "category": "correctness",
                "suggested_fix": "Apply appropriate fix based on the issue.",
                "fingerprint": "brandnewissue05",
                "previous_fingerprint": None,
                "inline_placeable": True,
            },
        ]

        plan = lib.plan_thread_actions(
            findings,
            managed_threads=managed_threads,
            max_inline_comments=10,
        )

        self.assertEqual(plan["resolve_thread_ids"], ["thread-to-resolve"])
        self.assertEqual(plan["reopen_thread_ids"], ["thread-resolved"])
        self.assertEqual(
            [finding["fingerprint"] for finding in plan["create_inline_findings"]],
            ["brandnewissue05"],
        )
        self.assertEqual(
            plan["thread_lifecycle_counts"],
            {"new": 1, "still_open": 1, "reopened": 1, "resolved": 1},
        )




class FingerprintStabilityTests(unittest.TestCase):
    def test_fingerprint_stable_across_body_and_priority_changes(self) -> None:
        """Fingerprint should not change when body wording or priority changes."""
        # Same title, path, and lines but different body and priority
        fp1 = lib._compute_fingerprint(
            priority=0,
            title="Missing null check in cleanup path",
            body="The cleanup function dereferences a null pointer when result is empty.",
            path="src/app.py",
            start_line=23,
            end_line=25,
        )
        fp2 = lib._compute_fingerprint(
            priority=2,
            title="Missing null check in cleanup path",
            body="Different wording: cleanup crashes on empty result due to missing null guard.",
            path="src/app.py",
            start_line=23,
            end_line=25,
        )
        self.assertEqual(fp1, fp2, "Fingerprint should be stable across body/priority changes")
        self.assertEqual(len(fp1), 16, "Fingerprint should be 16 hex chars")

    def test_fingerprint_changes_with_different_title(self) -> None:
        """Fingerprint should change when title changes significantly."""
        fp1 = lib._compute_fingerprint(
            priority=1,
            title="Missing null check",
            body="Issue description",
            path="src/app.py",
            start_line=23,
            end_line=25,
        )
        fp2 = lib._compute_fingerprint(
            priority=1,
            title="Broken error handling",
            body="Issue description",
            path="src/app.py",
            start_line=23,
            end_line=25,
        )
        self.assertNotEqual(fp1, fp2, "Fingerprint should differ for different titles")

    def test_fingerprint_changes_with_different_lines(self) -> None:
        """Fingerprint should change when line numbers change."""
        fp1 = lib._compute_fingerprint(
            priority=1,
            title="Missing null check",
            body="Issue description",
            path="src/app.py",
            start_line=23,
            end_line=25,
        )
        fp2 = lib._compute_fingerprint(
            priority=1,
            title="Missing null check",
            body="Issue description",
            path="src/app.py",
            start_line=30,
            end_line=32,
        )
        self.assertNotEqual(fp1, fp2, "Fingerprint should differ for different line ranges")

    def test_fingerprint_normalized_title_punctuation_ignored(self) -> None:
        """Punctuation differences in title should not affect fingerprint."""
        fp1 = lib._compute_fingerprint(
            priority=1,
            title="Missing null check in cleanup",
            body="Issue description",
            path="src/app.py",
            start_line=23,
            end_line=25,
        )
        fp2 = lib._compute_fingerprint(
            priority=1,
            title="Missing null check in cleanup!",  # Only trailing punctuation differs
            body="Issue description",
            path="src/app.py",
            start_line=23,
            end_line=25,
        )
        self.assertEqual(fp1, fp2, "Trailing punctuation should be normalized away")


class FuzzyMatchingTests(unittest.TestCase):
    def test_fuzzy_matching_same_path_similar_title_near_lines(self) -> None:
        """Fuzzy matching should map near-line/similar-title finding to existing thread."""
        # Create a managed thread with slightly different title and lines
        managed_threads = [
            {
                "thread_id": "thread-fuzzy-match",
                "is_resolved": False,
                "fingerprint": "oldfingerprint123",
                "finding": {
                    "priority": 1,
                    "priority_label": "P1",
                    "title": "Missing null check in cleanup handler",
                    "body": "The cleanup function may crash.",
                    "path": "src/app.py",
                    "start_line": 20,
                    "end_line": 22,
                    "confidence_score": 0.71,
                    "category": "correctness",
                    "suggested_fix": "Add null check.",
                },
            },
        ]
        # New finding with similar title, same path, nearby lines (within 5 tolerance)
        findings = [
            {
                "priority": 1,
                "priority_label": "P1",
                "title": "Missing null check in cleanup path",
                # Similar tokens: "missing", "null", "check", "cleanup"
                "body": "Updated wording.",
                "path": "src/app.py",
                "start_line": 23,  # 3 lines away from 20
                "end_line": 24,    # 2 lines away from 22
                "confidence_score": 0.80,
                "category": "correctness",
                "suggested_fix": "Add null guard.",
                "fingerprint": "newfingerprint456",
                "previous_fingerprint": None,
                "inline_placeable": True,
            },
        ]

        plan = lib.plan_thread_actions(
            findings,
            managed_threads=managed_threads,
            max_inline_comments=10,
        )

        # Should match via fuzzy matching
        self.assertEqual(len(plan["create_inline_findings"]), 0, "Should not create new thread")
        self.assertEqual(plan["thread_lifecycle_counts"]["new"], 0)
        self.assertEqual(plan["thread_lifecycle_counts"]["still_open"], 1)
        # Check that fuzzy_matched flag is set
        self.assertTrue(plan["planned_findings"][0].get("fuzzy_matched"), "Should be marked as fuzzy matched")

    def test_fuzzy_matching_requires_title_overlap_threshold(self) -> None:
        """Fuzzy matching should not match if title overlap is below threshold."""
        managed_threads = [
            {
                "thread_id": "thread-no-match",
                "is_resolved": False,
                "fingerprint": "oldfp001",
                "finding": {
                    "priority": 1,
                    "priority_label": "P1",
                    "title": "Database connection timeout issue",
                    "body": "Connection times out.",
                    "path": "src/db.py",
                    "start_line": 10,
                    "end_line": 12,
                    "confidence_score": 0.7,
                    "category": "performance",
                    "suggested_fix": "Increase timeout.",
                },
            },
        ]
        # New finding with completely different title (no overlap)
        findings = [
            {
                "priority": 1,
                "priority_label": "P1",
                "title": "Memory leak in allocator",
                "body": "Memory not freed.",
                "path": "src/db.py",
                "start_line": 11,
                "end_line": 13,
                "confidence_score": 0.8,
                "category": "correctness",
                "suggested_fix": "Free memory.",
                "fingerprint": "newfp002",
                "previous_fingerprint": None,
                "inline_placeable": True,
            },
        ]

        plan = lib.plan_thread_actions(
            findings,
            managed_threads=managed_threads,
            max_inline_comments=10,
        )

        # Should NOT match (different title)
        self.assertEqual(len(plan["create_inline_findings"]), 1, "Should create new thread")
        self.assertEqual(plan["thread_lifecycle_counts"]["new"], 1)

    def test_fuzzy_matching_requires_same_path(self) -> None:
        """Fuzzy matching should not match across different paths."""
        managed_threads = [
            {
                "thread_id": "thread-diff-path",
                "is_resolved": False,
                "fingerprint": "oldfp003",
                "finding": {
                    "priority": 1,
                    "priority_label": "P1",
                    "title": "Missing null check",
                    "body": "Null pointer.",
                    "path": "src/app.py",
                    "start_line": 20,
                    "end_line": 22,
                    "confidence_score": 0.7,
                    "category": "correctness",
                    "suggested_fix": "Add check.",
                },
            },
        ]
        # New finding with similar title but different path
        findings = [
            {
                "priority": 1,
                "priority_label": "P1",
                "title": "Missing null check in handler",
                "body": "Updated.",
                "path": "src/handler.py",  # Different path
                "start_line": 21,
                "end_line": 23,
                "confidence_score": 0.8,
                "category": "correctness",
                "suggested_fix": "Add check.",
                "fingerprint": "newfp004",
                "previous_fingerprint": None,
                "inline_placeable": True,
            },
        ]

        plan = lib.plan_thread_actions(
            findings,
            managed_threads=managed_threads,
            max_inline_comments=10,
        )

        # Should NOT match (different path)
        self.assertEqual(len(plan["create_inline_findings"]), 1, "Should create new thread")


class ReviewModeTests(unittest.TestCase):
    def test_discovery_mode_prompt_contains_full_sweep_instructions(self) -> None:
        """Discovery mode should include exhaustive review instructions."""
        prompt = lib.build_review_prompt(
            review_base="main",
            base_sha="a" * 40,
            merge_base="b" * 40,
            head_sha="c" * 40,
            changed_files_filename="files.txt",
            diff_filename="diff.txt",
            review_mode="discovery",
        )
        self.assertIn("Discovery Review Mode", prompt)
        self.assertIn("full exhaustive review", prompt)
        self.assertIn("Multi-Pass Review Methodology", prompt)

    def test_gate_mode_prompt_contains_incremental_instructions(self) -> None:
        """Gate mode should include incremental review instructions."""
        prompt = lib.build_review_prompt(
            review_base="main",
            base_sha="a" * 40,
            merge_base="b" * 40,
            head_sha="c" * 40,
            changed_files_filename="files.txt",
            diff_filename="diff.txt",
            review_mode="gate",
            previous_head_sha="abc123def456",
        )
        self.assertIn("Incremental Review Mode", prompt)
        self.assertIn("incremental review", prompt)
        self.assertIn("abc123def456...HEAD", prompt)
        self.assertIn("P0/P1 regression detection", prompt)
        self.assertIn("Revalidate prior open findings", prompt)
        # Gate mode should NOT require exhaustive coverage
        self.assertIn("does NOT require exhaustive", prompt)

    def test_same_sha_mode_prompt_contains_revalidation_only(self) -> None:
        """Same SHA mode should focus on revalidation only."""
        prompt = lib.build_review_prompt(
            review_base="main",
            base_sha="a" * 40,
            merge_base="b" * 40,
            head_sha="c" * 40,
            changed_files_filename="files.txt",
            diff_filename="diff.txt",
            review_mode="same_sha",
        )
        self.assertIn("Same SHA Review Mode", prompt)
        self.assertIn("revalidating prior open findings", prompt)
        self.assertIn("No new exhaustive sweep", prompt)

    def test_invalid_review_mode_raises_error(self) -> None:
        """Invalid review_mode should raise ValueError."""
        with self.assertRaisesRegex(ValueError, "review_mode must be one of"):
            lib.build_review_prompt(
                review_base="main",
                base_sha="a" * 40,
                merge_base="b" * 40,
                head_sha="c" * 40,
                changed_files_filename="files.txt",
                diff_filename="diff.txt",
                review_mode="invalid_mode",
            )


if __name__ == "__main__":
    unittest.main()
