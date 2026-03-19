#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from codex_review_lib import (
    build_open_prior_findings,
    extract_inline_fingerprint,
    parse_managed_inline_comment,
)


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _graphql_request(
    *,
    graphql_url: str,
    token: str,
    query: str,
    variables: dict[str, Any],
) -> dict[str, Any]:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        graphql_url,
        data=payload,
        method="POST",
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
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub GraphQL request failed ({exc.code}): {body}") from exc

    parsed = json.loads(body)
    if parsed.get("errors"):
        raise RuntimeError(f"GitHub GraphQL request returned errors: {parsed['errors']}")
    data = parsed.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"GitHub GraphQL response did not include a data object: {parsed}")
    return data


def _is_managed_bot_comment(comment: dict[str, Any]) -> bool:
    author = comment.get("author") or {}
    login = str(author.get("login") or "")
    typename = str(author.get("__typename") or "")
    if not extract_inline_fingerprint(str(comment.get("body") or "")):
        return False
    if login.endswith("[bot]"):
        return True
    return typename == "Bot"


def _fetch_managed_threads(
    *,
    graphql_url: str,
    token: str,
    owner: str,
    repo: str,
    pull_number: int,
) -> list[dict[str, Any]]:
    query = """
    query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $number) {
          reviewThreads(first: 100, after: $cursor) {
            nodes {
              id
              isResolved
              isOutdated
              comments(first: 100) {
                nodes {
                  id
                  body
                  author {
                    __typename
                    login
                  }
                }
              }
            }
            pageInfo {
              hasNextPage
              endCursor
            }
          }
        }
      }
    }
    """

    cursor: str | None = None
    managed_threads: list[dict[str, Any]] = []
    while True:
        data = _graphql_request(
            graphql_url=graphql_url,
            token=token,
            query=query,
            variables={
                "owner": owner,
                "repo": repo,
                "number": pull_number,
                "cursor": cursor,
            },
        )
        review_threads = (
            data.get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
        )
        nodes = review_threads.get("nodes") or []
        for thread in nodes:
            comments = thread.get("comments", {}).get("nodes") or []
            managed_comments = [
                comment for comment in comments if isinstance(comment, dict) and _is_managed_bot_comment(comment)
            ]
            if not managed_comments:
                continue

            managed_comment = managed_comments[-1]
            finding = parse_managed_inline_comment(str(managed_comment.get("body") or ""))
            managed_threads.append(
                {
                    "thread_id": thread["id"],
                    "is_resolved": bool(thread.get("isResolved")),
                    "is_outdated": bool(thread.get("isOutdated")),
                    "fingerprint": finding["fingerprint"],
                    "comment_id": managed_comment.get("id"),
                    "author_login": (managed_comment.get("author") or {}).get("login", ""),
                    "finding": finding,
                }
            )

        page_info = review_threads.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            break

    managed_threads.sort(
        key=lambda thread: (
            thread["finding"]["priority"],
            thread["finding"]["path"],
            thread["finding"]["start_line"],
            thread["fingerprint"],
        )
    )
    return managed_threads


def main() -> int:
    token = _env("GITHUB_TOKEN")
    graphql_url = _env("GITHUB_GRAPHQL_URL", "https://api.github.com/graphql")
    repository = _env("GITHUB_REPOSITORY")
    pull_number = int(_env("GITHUB_PULL_NUMBER"))
    prior_threads_path = Path(
        _env("CODEX_REVIEW_PRIOR_THREADS_PATH", "codex-review-prior-threads.json")
    )
    open_findings_path = Path(
        _env(
            "CODEX_REVIEW_PRIOR_OPEN_FINDINGS_PATH",
            "codex-review-prior-open-findings.json",
        )
    )

    owner, repo = repository.split("/", 1)
    managed_threads = _fetch_managed_threads(
        graphql_url=graphql_url,
        token=token,
        owner=owner,
        repo=repo,
        pull_number=pull_number,
    )

    prior_threads_path.write_text(
        json.dumps({"managed_threads": managed_threads}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    open_findings_path.write_text(
        json.dumps(
            {"open_findings": build_open_prior_findings(managed_threads)},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
