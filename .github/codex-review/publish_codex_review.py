#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from codex_review_lib import (
    build_inline_comment_body,
    plan_thread_actions,
    render_summary_body,
)


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _request(
    method: str,
    url: str,
    *,
    token: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "agent-workflows/codex-pr-review",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    try:
        with urllib.request.urlopen(request) as response:  # noqa: S310
            payload = response.read().decode("utf-8")
            return response.status, json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            parsed = {"message": payload or exc.reason}
        return exc.code, parsed


def _graphql_request(
    *,
    graphql_url: str,
    token: str,
    query: str,
    variables: dict[str, Any],
) -> dict[str, Any]:
    status, payload = _request(
        "POST",
        graphql_url,
        token=token,
        body={"query": query, "variables": variables},
    )
    if status != 200:
        raise RuntimeError(f"GitHub GraphQL request failed ({status}): {payload}")
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected GitHub GraphQL response: {payload}")
    if payload.get("errors"):
        raise RuntimeError(f"GitHub GraphQL request returned errors: {payload['errors']}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"GitHub GraphQL response did not include a data object: {payload}")
    return data


def _paginate(url: str, *, token: str) -> list[dict[str, Any]]:
    page = 1
    items: list[dict[str, Any]] = []
    while True:
        separator = "&" if "?" in url else "?"
        page_url = f"{url}{separator}per_page=100&page={page}"
        status, payload = _request("GET", page_url, token=token)
        if status != 200:
            raise RuntimeError(f"GitHub API request failed ({status}) for {page_url}: {payload}")
        if not isinstance(payload, list):
            raise RuntimeError(f"Unexpected GitHub API response for {page_url}: {payload}")
        items.extend(payload)
        if len(payload) < 100:
            break
        page += 1
    return items


def _upsert_summary_comment(
    *,
    api_url: str,
    owner: str,
    repo: str,
    pull_number: str,
    token: str,
    body: str,
) -> None:
    comments = _paginate(
        f"{api_url}/repos/{owner}/{repo}/issues/{pull_number}/comments",
        token=token,
    )

    managed_comments = [
        comment
        for comment in comments
        if comment.get("user", {}).get("type") == "Bot" and "<!-- codex-pr-review -->" in (comment.get("body") or "")
    ]
    managed_comments.sort(key=lambda comment: comment.get("updated_at", ""), reverse=True)

    primary_comment = managed_comments[:1]
    duplicate_comments = managed_comments[1:]
    for comment in duplicate_comments:
        status, payload = _request(
            "DELETE",
            f"{api_url}/repos/{owner}/{repo}/issues/comments/{comment['id']}",
            token=token,
        )
        if status not in {204, 404}:
            raise RuntimeError(
                f"Failed to delete duplicate summary comment {comment['id']} ({status}): {payload}"
            )

    if primary_comment:
        comment = primary_comment[0]
        status, payload = _request(
            "PATCH",
            f"{api_url}/repos/{owner}/{repo}/issues/comments/{comment['id']}",
            token=token,
            body={"body": body},
        )
        if status != 200:
            raise RuntimeError(
                f"Failed to update summary comment {comment['id']} ({status}): {payload}"
            )
        return

    status, payload = _request(
        "POST",
        f"{api_url}/repos/{owner}/{repo}/issues/{pull_number}/comments",
        token=token,
        body={"body": body},
    )
    if status != 201:
        raise RuntimeError(f"Failed to create summary comment ({status}): {payload}")


def _is_unplaceable_review_error(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    message = str(payload.get("message", "")).lower()
    errors = payload.get("errors", [])

    combined: list[str] = [message]
    if isinstance(errors, list):
        for error in errors:
            if isinstance(error, dict):
                combined.extend(str(value).lower() for value in error.values())
            else:
                combined.append(str(error).lower())

    markers = ("diff", "line", "side", "start_line", "path", "pull_request_review_thread")
    return any(marker in text for marker in markers for text in combined)


def _post_inline_comments(
    *,
    api_url: str,
    owner: str,
    repo: str,
    pull_number: str,
    head_sha: str,
    token: str,
    findings: list[dict[str, Any]],
    initial_unplaced_count: int,
    initial_truncated_count: int,
) -> tuple[int, int, int]:
    posted = 0
    unplaced = initial_unplaced_count
    truncated = initial_truncated_count

    for finding in findings:
        body = {
            "body": build_inline_comment_body(finding),
            "commit_id": head_sha,
            "path": finding["path"],
            "line": finding["end_line"],
            "side": "RIGHT",
        }
        if finding["start_line"] != finding["end_line"]:
            body["start_line"] = finding["start_line"]
            body["start_side"] = "RIGHT"

        status, payload = _request(
            "POST",
            f"{api_url}/repos/{owner}/{repo}/pulls/{pull_number}/comments",
            token=token,
            body=body,
        )
        if status == 201:
            posted += 1
            continue
        if status == 422 and _is_unplaceable_review_error(payload):
            unplaced += 1
            continue
        raise RuntimeError(
            f"Failed to create inline review comment for {finding['path']}:{finding['start_line']} ({status}): {payload}"
        )

    return posted, unplaced, truncated


def _mutate_review_thread(
    *,
    graphql_url: str,
    token: str,
    thread_id: str,
    action: str,
) -> None:
    if action not in {"resolve", "unresolve"}:
        raise RuntimeError(f"Unsupported review thread action: {action}")

    mutation_name = "resolveReviewThread" if action == "resolve" else "unresolveReviewThread"
    query = f"""
    mutation($threadId: ID!) {{
      {mutation_name}(input: {{threadId: $threadId}}) {{
        thread {{
          id
          isResolved
        }}
      }}
    }}
    """
    _graphql_request(
        graphql_url=graphql_url,
        token=token,
        query=query,
        variables={"threadId": thread_id},
    )


def _load_managed_threads(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    managed_threads = payload.get("managed_threads")
    if not isinstance(managed_threads, list):
        raise RuntimeError(f"Invalid managed thread payload in {path}: {payload}")
    return managed_threads


def main() -> int:
    token = _env("GITHUB_TOKEN")
    api_url = _env("GITHUB_API_URL", "https://api.github.com")
    graphql_url = _env("GITHUB_GRAPHQL_URL", "https://api.github.com/graphql")
    repository = _env("GITHUB_REPOSITORY")
    pull_number = _env("GITHUB_PULL_NUMBER")
    head_sha = _env("GITHUB_HEAD_SHA")

    owner, repo = repository.split("/", 1)
    state_path = Path(_env("CODEX_REVIEW_STATE_PATH", "codex-review-state.json"))
    prior_threads_path = Path(
        _env("CODEX_REVIEW_PRIOR_THREADS_PATH", "codex-review-prior-threads.json")
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    managed_threads = _load_managed_threads(prior_threads_path)
    should_reconcile_threads = state.get("codex_exit_code") == 0 and not state.get("parse_failed")
    action_plan = {
        "create_inline_findings": [],
        "resolve_thread_ids": [],
        "reopen_thread_ids": [],
        "unplaced_inline_count": 0,
        "truncated_inline_count": 0,
        "thread_lifecycle_counts": {"new": 0, "still_open": 0, "reopened": 0, "resolved": 0},
    }

    posted_inline_count = 0
    unplaced_inline_count = 0
    truncated_inline_count = 0
    thread_lifecycle_counts = dict(action_plan["thread_lifecycle_counts"])

    if should_reconcile_threads:
        action_plan = plan_thread_actions(
            state.get("findings", []),
            managed_threads=managed_threads,
            max_inline_comments=int(state.get("max_inline_comments", 10)),
        )

        for thread_id in action_plan["resolve_thread_ids"]:
            _mutate_review_thread(
                graphql_url=graphql_url,
                token=token,
                thread_id=thread_id,
                action="resolve",
            )
        for thread_id in action_plan["reopen_thread_ids"]:
            _mutate_review_thread(
                graphql_url=graphql_url,
                token=token,
                thread_id=thread_id,
                action="unresolve",
            )

        posted_inline_count, unplaced_inline_count, truncated_inline_count = _post_inline_comments(
            api_url=api_url,
            owner=owner,
            repo=repo,
            pull_number=pull_number,
            head_sha=head_sha,
            token=token,
            findings=action_plan["create_inline_findings"],
            initial_unplaced_count=action_plan["unplaced_inline_count"],
            initial_truncated_count=action_plan["truncated_inline_count"],
        )

        thread_lifecycle_counts = dict(action_plan["thread_lifecycle_counts"])
        thread_lifecycle_counts["new"] = posted_inline_count

    summary_body = render_summary_body(
        state,
        override_active=_env("OVERRIDE_ACTIVE", "0") == "1",
        override_stale=_env("OVERRIDE_STALE", "0") == "1",
        override_approved_by=_env("OVERRIDE_APPROVED_BY", ""),
        override_approved_sha=_env("OVERRIDE_APPROVED_SHA", ""),
        override_source=_env("OVERRIDE_SOURCE", "none"),
        posted_inline_count=posted_inline_count,
        unplaced_inline_count=unplaced_inline_count,
        truncated_inline_count=truncated_inline_count,
        thread_lifecycle_counts=thread_lifecycle_counts,
        run_url=_env("GITHUB_RUN_URL"),
        artifact_url=_env("GITHUB_ARTIFACT_URL"),
    )

    _upsert_summary_comment(
        api_url=api_url,
        owner=owner,
        repo=repo,
        pull_number=pull_number,
        token=token,
        body=summary_body,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
