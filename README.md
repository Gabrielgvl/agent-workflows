# agent-workflows

Public repository for reusable GitHub workflows and actions built around agent tools.

This repository is intentionally tool-generic. Codex is the first workflow
family, but the repository is structured so future workflow sets can target
other agent tools without changing the repository identity.

## What lives here

- `/.github/workflows/codex-pr-review.yml`
  Reusable workflow for pull-request Codex review.
- `/.github/workflows/codex-review-override.yml`
  Reusable workflow for the `/codex-override` issue-comment flow.
- `/.github/actions/run-codex-review/`
  Composite action that runs the hardened Codex review wrapper and parses severity
  counts.

## Auth contract

The first shared review workflow supports explicit API-key login in v1.

The workflow requires the caller to pass an API key secret, which is used to run:

```bash
printenv OPENAI_API_KEY | codex login --with-api-key
```

OpenAI's current guidance recommends API keys as the default auth path for CI
automation, and treats persisted `auth.json` as an advanced pattern for trusted
private runners only:

- https://developers.openai.com/codex/noninteractive/#use-api-key-auth-recommended
- https://developers.openai.com/codex/auth/ci-cd-auth/
- https://developers.openai.com/codex/cli/reference/#codex-login

## Consumer pattern

Each consumer repository keeps a thin caller workflow with repo-local triggers,
permissions, and concurrency. The caller delegates the implementation to this
repository by immutable ref.

Example PR review caller:

```yaml
name: codex-pr-review

on:
  pull_request:
    types:
      - opened
      - synchronize
      - reopened
      - ready_for_review
      - labeled
      - unlabeled

concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: true

permissions:
  contents: read
  issues: write
  pull-requests: write

jobs:
  review:
    uses: <owner>/agent-workflows/.github/workflows/codex-pr-review.yml@<immutable-ref>
    with:
      shared_repository: <owner>/agent-workflows
      shared_ref: <immutable-ref>
      runs_on_json: '["self-hosted","linux"]'
      install_codex_cli: true
      codex_version: "0.114.0"
    secrets:
      openai_api_key: ${{ secrets.OPENAI_API_KEY }}
```

Example override caller:

```yaml
name: codex-review-override

on:
  issue_comment:
    types:
      - created
      - edited

permissions:
  actions: write
  contents: read
  issues: write
  pull-requests: write

jobs:
  override:
    uses: <owner>/agent-workflows/.github/workflows/codex-review-override.yml@<immutable-ref>
```

## Release guidance

- Publish immutable refs for every consumer update.
- Keep caller workflows pinned to immutable SHAs.
- Roll forward or back by changing the caller ref in consumer repositories.
