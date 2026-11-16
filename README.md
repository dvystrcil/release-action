# release-action

Composite GitHub Action that creates the next `vX.Y.Z` GitHub release with auto-generated release notes, and can move a floating major tag (`v1`) for action repos. Extracted from the duplicated `Create GitHub release` block in the homelab's `*-docker` build workflows.

## What it does

1. Reads the repo's latest release (`initial-version` only if there is **none**, i.e. HTTP 404; any other error fails the step).
2. Bumps it: `patch` (default), `minor` (resets patch) or `major` (resets both). Refuses a latest tag that isn't a clean `vX.Y.Z`.
3. Refuses if that tag already exists, then creates the release at the workflow's commit with auto-generated release notes.
4. With `move-major-tag: true`, points `v<MAJOR>` at the same commit, creating it if absent. A `major` bump therefore creates `v2` and leaves `v1` where it is.

Pairs with the homelab's image-tagging convention: git tags use `vX.Y.Z`, Docker image tags use `X.Y.Z` (the `docker/metadata-action` `type=semver,pattern={{version}}` default strips the `v`).

## Usage

```yaml
- name: Generate app token
  id: app-token
  uses: actions/create-github-app-token@v3
  with:
    app-id: ${{ vars.CICD_APP_ID }}
    private-key: ${{ secrets.CICD_APP_PRIVATE_KEY }}

- name: Auto-bump release
  uses: dvystrcil/release-action@v1
  with:
    token: ${{ steps.app-token.outputs.token }}
```

## Inputs

| Name | Required | Default | Description |
| --- | --- | --- | --- |
| `token` | yes | — | GitHub token with `contents: write`. Typically minted via `actions/create-github-app-token@v3` from the homelab CICD App; a `secrets.GITHUB_TOKEN` also works for same-repo releases. |
| `initial-version` | no | `v0.0.0` | The starting tag when the repo has no prior releases. A `patch` bump makes the first release `v0.0.1`. |
| `bump` | no | `patch` | `patch`, `minor` or `major`. |
| `move-major-tag` | no | `false` | `true` for action repos whose consumers pin `@v1`: moves (or creates) `v<MAJOR>` to the release commit. |
| `target` | no | workflow commit | Commit SHA to release. |
| `create-release` | no | `true` | `false`: create no release, only deploy `version`. |
| `version` | no | the release just created | Tag to deploy (`vX.Y.Z` or `X.Y.Z`). |
| `deploy-repo` | no | — | `owner/name` of the kustomize deploy repo. Empty: no deploy. |
| `deploy-images` | no | — | Image names whose `newTag` moves. Each must appear in the deploy repo. |
| `deploy-token` | no | `token` | `contents: write` (+ `pull-requests: write` for the kill switch) on `deploy-repo`. |
| `auto-deploy` | no | `true` | Kill switch: `false` opens a bump PR instead. Pass `${{ vars.AUTO_DEPLOY }}`. |

## Outputs

| Name | Description |
| --- | --- |
| `tag` | The release created, e.g. `v1.2.3`. |
| `major-tag` | The floating tag moved, e.g. `v1`. Empty unless `move-major-tag`. |
| `deploy-result` | `committed`, `noop` (deploy repo already at or past it), `pr` (kill switch), or empty. |

## Deploy: merge = release = deploy

With `deploy-repo` set, the released tag is committed **straight to the deploy repo's `main`** (no bump PR). See [homelab#2320](https://github.com/dvystrcil/homelab/issues/2320). `deploy.py` does it, and each guard below has a test in `tests/test_deploy.py`:

- **newTag lines only.** It edits the `newTag:` under `- name: <image>` in every `kustomization.yaml` that lists the image, as a text edit, never a YAML round-trip. `git diff -U0` must then show only paired `newTag:` lines.
- **Validation.** It renders every kustomize root (bases and Components excluded, as kustomize-validate-action does) before and after the edit. Every render must succeed, the renders may differ only in `image:` lines, and at least one must change. main already passed the deploy repo's required `manifests` check, so an image-only render diff cannot break the schema.
- **Never downgrade.** A tag already at or past the release is left alone (`noop`). A tag it cannot compare (`latest`, a digest, a prerelease) is refused, not overwritten. An image the deploy repo doesn't list fails the step.
- **Re-read main, retry on non-fast-forward.** Each attempt starts from a fresh clone of `main`. A push rejected as non-fast-forward starts over from the new `main`, up to 5 times. Any other refusal (ruleset, auth) fails at once, without retrying and without falling back to a PR.
- **Concurrency.** A composite action cannot declare `concurrency:`, so the calling job must (example below). The guards above keep correctness without it: an older release that loses a race finds main already past it and does nothing. GitHub concurrency groups are scoped to one repo, so two *different* source repos writing one deploy repo are serialized only by the retry.
- **Kill switch.** `auto-deploy: false` makes the same validated commit, pushes it to `deploy/<image>-<version>` and opens a PR. Anything other than `true`, `false` or empty is refused.

Commits are authored `homelab-bump-bot` (homelab's auto-merge gate keys on it for the PR path). The push needs a **ruleset bypass for the token's App** on the deploy repo's `main`.

Call it **after the image tag exists**. In a `*-docker` repo that is the `release: published` workflow, after promote:

```yaml
jobs:
  deploy:
    needs: promote
    runs-on: tor-dice-docker-runner
    concurrency:
      group: deploy-dvystrcil/tor-dice
      cancel-in-progress: false   # one pending run: the latest release wins
    steps:
      - uses: actions/create-github-app-token@v3
        id: app-token
        with:
          client-id: ${{ env.CICD_APP_CLIENT_ID }}
          private-key: ${{ env.CICD_APP_PRIVATE_KEY }}
          owner: ${{ github.repository_owner }}
          repositories: tor-dice
      - uses: dvystrcil/release-action@v1
        with:
          token: ${{ steps.app-token.outputs.token }}
          create-release: false
          version: ${{ github.event.release.tag_name }}
          deploy-repo: dvystrcil/tor-dice
          deploy-images: harbor.sirddail.net/ai/tor-dice
          auto-deploy: ${{ vars.AUTO_DEPLOY }}
```

The runner needs `git` and `python3` (stdlib only). `kubectl` is installed if absent.

## Action repos: release on demand, move `v1`

For a repo consumed as `@v1`, moving `v1` IS the release to every consumer, so trigger it on purpose instead of on merge:

```yaml
on:
  workflow_dispatch:
    inputs:
      bump: {type: choice, options: [patch, minor, major], default: patch}
permissions:
  contents: write
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: dvystrcil/release-action@v1
        with:
          token: ${{ github.token }}
          bump: ${{ inputs.bump }}
          move-major-tag: true
          initial-version: v1.0.0
```

## Releasing this action

`gh workflow run release.yaml --repo dvystrcil/release-action -f bump=minor`. It releases with `uses: ./`, this commit's own code, so each release is also the end-to-end test of the API calls. `tests/test-release.sh` covers the version math and refusals without mocking the API. `python3 -m unittest discover -s tests` runs the deploy guards against a real bare git repo.

## Version policy

Consumers pin the floating major tag, **`@v1`**, never `@main` or a SHA (homelab's own-actions policy: a fix ships fleet-wide by moving the tag, with no Renovate PR per repo). Every release of this action moves `v1` (`move-major-tag: true` in `release.yaml`), so a release *is* the rollout. That is why releasing is manual.

- A breaking change is a **major** bump: it creates `v2` and leaves `v1` where it was, so no `@v1` consumer gets it by accident.
- Rollback: point `v1` back at the previous release, `git push -f origin <previous release sha>:refs/tags/v1`.
- An exact tag (`@v1.0.0`) still works for a consumer that must not move.

The action's own release cadence follows the homelab convention: git release tags are `vX.Y.Z`. Patch = backwards-compatible change to the bump logic; minor = new input or behavior; major = breaking change to inputs or default behavior.

## Why it exists

The patch-bump release block was copy-pasted across 7+ `*-docker` repos during the homelab#236 migration. Extracting it cuts each consumer from ~20 lines to 3 and centralizes the maintenance.

## Related

- [dvystrcil/homelab#236](https://github.com/dvystrcil/homelab/issues/236) — the migration that surfaced the duplication
- [dvystrcil/homelab#237](https://github.com/dvystrcil/homelab/issues/237) — the extraction tracking issue
