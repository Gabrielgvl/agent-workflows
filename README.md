# agent-workflows

Public repository for reusable GitHub workflows and actions built around agent tools.

This repository is intentionally tool-generic. Codex is the first workflow
family, but the repository is structured so future workflow sets can target
other agent tools without changing the repository identity.

## What lives here

- `/.github/workflows/codex-pr-review.yml`
  Reusable workflow for pull-request Codex review with inline PR comments and a
  sticky summary.
- `/.github/workflows/codex-review-override.yml`
  Reusable workflow for the `/codex-override` issue-comment flow.
- `/.github/codex-review/`
  Internal helper scripts that build the review prompt, run Codex in structured
  mode, normalize findings, and publish managed GitHub review comments.

## Auth contract

The shared review workflow restores a Codex CLI auth bundle from
`codex_auth_json_b64`.

The helper scripts decode the secret into an isolated `~/.codex/auth.json`,
validate the restored bundle shape, and run the review without relying on
runner-local Codex state. This keeps the workflow aligned with the current
Orbio CI contract: restored Codex session credentials in a temporary isolated
home, not API-key login during the job.

OpenAI documents persisted `auth.json` as an advanced pattern for trusted
private runners:

- https://developers.openai.com/codex/auth/ci-cd-auth/
- https://developers.openai.com/codex/cli/reference/#codex-login

## Consumer pattern

Each consumer repository keeps a thin caller workflow with repo-local triggers,
permissions, and concurrency. The caller delegates the implementation to this
repository by semver tag or immutable ref.

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
    uses: Gabrielgvl/agent-workflows/.github/workflows/codex-pr-review.yml@v1
    with:
      runs_on_json: '["ubuntu-latest"]'
      install_codex_cli: true
      codex_version: "0.115.0"
      review_model: "gpt-5.4"
      review_reasoning_effort: low
      max_inline_comments: "10"
    secrets:
      codex_auth_json_b64: ${{ secrets.CODEX_AUTH_JSON_B64 }}
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
    uses: Gabrielgvl/agent-workflows/.github/workflows/codex-review-override.yml@v1
```

## Behavior

- Draft pull requests are skipped.
- Fork pull requests are skipped.
- Blocking findings are published as native inline PR review comments.
- The workflow always maintains one sticky PR summary comment with the current
  verdict, counts, override state, and workflow links.
- Managed inline comments are replaced on reruns instead of accumulating.
- P0 always blocks.
- P1 blocks unless an admin override is active.
- P2 and P3 are visible in the summary and artifacts, but do not block.

## Inputs

Primary review inputs:

- `review_model`
  Default: `gpt-5.4`
  Use `gpt-5.2-codex` if you want the current OpenAI code-review cookbook's
  review-specialized recommendation.
- `review_reasoning_effort`
  Allowed: `minimal`, `low`, `medium`, `high`, `xhigh`
  Default: `low`
- `review_reasoning_summary`
  Allowed: `auto`, `concise`, `detailed`, `none`
  Default: unset
- `review_verbosity`
  Allowed: `low`, `medium`, `high`
  Default: unset
- `max_inline_comments`
  Allowed: `1` through `20`
  Default: `10`

Runner and installation inputs:

- `runs_on_json`
  JSON array passed directly to `runs-on`
- `working_directory`
  Checkout path for the caller repository
- `install_codex_cli`
  Whether the workflow should install Codex itself
- `node_version`
  Node version used when installing Codex
- `codex_version`
  `@openai/codex` version used when `install_codex_cli` is true
- `review_timeout_seconds`
  Inner Codex execution timeout
- `override_label`
  Label used by the override workflow to mark an approved P1 exception

Secrets:

- `codex_auth_json_b64`
  Required. Base64-encoded Codex CLI `auth.json` restored into an isolated
  temporary home for the review run.

## Release guidance

- Use `@v1` for a stable moving major tag.
- Use an immutable SHA when you need strict pinning.
- Roll forward or back by changing the caller ref in consumer repositories.
