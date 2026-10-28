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
  uses: dvystrcil/release-action@v0.1.0
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

## Outputs

| Name | Description |
| --- | --- |
| `tag` | The release created, e.g. `v1.2.3`. |
| `major-tag` | The floating tag moved, e.g. `v1`. Empty unless `move-major-tag`. |

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
      - uses: dvystrcil/release-action@v0.2.0
        with:
          token: ${{ github.token }}
          bump: ${{ inputs.bump }}
          move-major-tag: true
          initial-version: v1.0.0
```

## Releasing this action

`gh workflow run release.yaml --repo dvystrcil/release-action -f bump=minor`. It releases with `uses: ./`, this commit's own code, so each release is also the end-to-end test of the API calls. `tests/test-release.sh` covers the version math and refusals without mocking the API.

## Version policy

Pinned semver tags only — `@v0.1.0`, never `@main`. Each consumer bump is intentional.

The action's own release cadence follows the homelab convention: git release tags are `vX.Y.Z`. Patch = backwards-compatible change to the bump logic; minor = new input or behavior; major = breaking change to inputs or default behavior.

## Why it exists

The patch-bump release block was copy-pasted across 7+ `*-docker` repos during the homelab#236 migration. Extracting it cuts each consumer from ~20 lines to 3 and centralizes the maintenance.

## Related

- [dvystrcil/homelab#236](https://github.com/dvystrcil/homelab/issues/236) — the migration that surfaced the duplication
- [dvystrcil/homelab#237](https://github.com/dvystrcil/homelab/issues/237) — the extraction tracking issue
