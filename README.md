# release-action

Composite GitHub Action that auto-creates a patch-bump GitHub release on push to `main`. Extracted from the duplicated `Create GitHub release` block in the homelab's `*-docker` build workflows.

## What it does

1. Reads the repo's latest release tag (or `initial-version` if none).
2. Strips the `v` prefix, increments the patch component, prepends `v` again.
3. Calls the GitHub API to create the new release with auto-generated release notes.

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
| `initial-version` | no | `v0.0.0` | The starting tag when the repo has no prior releases. The action will bump this to `v0.0.1` on first run. |

## Version policy

Pinned semver tags only — `@v0.1.0`, never `@main`. Each consumer bump is intentional.

The action's own release cadence follows the homelab convention: git release tags are `vX.Y.Z`. Patch = backwards-compatible change to the bump logic; minor = new input or behavior; major = breaking change to inputs or default behavior.

## Why it exists

The patch-bump release block was copy-pasted across 7+ `*-docker` repos during the homelab#236 migration. Extracting it cuts each consumer from ~20 lines to 3 and centralizes the maintenance.

## Related

- [dvystrcil/homelab#236](https://github.com/dvystrcil/homelab/issues/236) — the migration that surfaced the duplication
- [dvystrcil/homelab#237](https://github.com/dvystrcil/homelab/issues/237) — the extraction tracking issue
