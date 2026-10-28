#!/usr/bin/env bash
# release.sh -- create the next vX.Y.Z GitHub release (auto-generated notes)
# and, optionally, move the floating major tag (v1) to the same commit.
# Called by action.yaml; every input arrives as an env var.
#
#   BUMP            patch | minor | major          (default patch)
#   MOVE_MAJOR_TAG  true: point v<MAJOR> at the release commit, creating it
#                   if absent. A `major` bump therefore creates v2 and leaves
#                   v1 where it is -- v1 consumers never get a breaking change.
#   INITIAL_VERSION the "latest" when the repo has no release yet (default v0.0.0)
#   TARGET_SHA      commit to release (default: the workflow's GITHUB_SHA)
#   GH_TOKEN, REPO  contents: write on REPO
#
# tests/test-release.sh sources this with RELEASE_SH_LIB=1 for the pure parts.
set -euo pipefail

SEMVER='^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'

next_version() {  # next_version <vX.Y.Z> <patch|minor|major>
  [[ $1 =~ $SEMVER ]] || { echo "not a vX.Y.Z release tag: '$1'" >&2; return 1; }
  local ma=${BASH_REMATCH[1]} mi=${BASH_REMATCH[2]} pa=${BASH_REMATCH[3]}
  case $2 in
    patch) echo "v$ma.$mi.$((pa + 1))" ;;
    minor) echo "v$ma.$((mi + 1)).0" ;;
    major) echo "v$((ma + 1)).0.0" ;;
    *) echo "bump must be patch, minor or major, got '$2'" >&2; return 1 ;;
  esac
}

major_tag() {  # major_tag <vX.Y.Z> -> vX
  [[ $1 =~ $SEMVER ]] || { echo "not a vX.Y.Z release tag: '$1'" >&2; return 1; }
  echo "v${BASH_REMATCH[1]}"
}

api() {  # api <METHOD> <path> [json] -> sets $STATUS and $BODY
  # Call it plainly, never as $(api ...): a command substitution is a
  # subshell, and the STATUS it set would vanish with it.
  local f; f=$(mktemp)
  STATUS=$(curl -sS -o "$f" -w '%{http_code}' -X "$1" \
    -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json" \
    ${3:+-H "Content-Type: application/json" -d "$3"} \
    "https://api.github.com/repos/$REPO$2") || STATUS=000
  BODY=$(cat "$f"); rm -f "$f"
}

main() {
  local bump=${BUMP:-patch} sha=${TARGET_SHA:-${GITHUB_SHA:?}} latest new want

  # Only a 404 means "no release yet". Anything else (401, 403, 5xx) used to
  # fall back to INITIAL_VERSION too, which would quietly cut v0.0.1 into a
  # repo already at v0.1.1.
  api GET /releases/latest
  case $STATUS in
    200) latest=$(jq -r .tag_name <<<"$BODY") ;;
    404) latest=${INITIAL_VERSION:-v0.0.0}; echo "no release yet; starting from $latest" ;;
    *) echo "::error::reading the latest release: HTTP $STATUS $BODY"; return 1 ;;
  esac
  new=$(next_version "$latest" "$bump")

  api GET "/git/ref/tags/$new"
  [ "$STATUS" = 404 ] || { echo "::error::tag $new already exists (HTTP $STATUS); not overwriting a release"; return 1; }

  echo "Releasing $new ($bump bump from $latest) at $sha"
  api POST /releases "$(jq -n --arg t "$new" --arg s "$sha" \
    '{tag_name: $t, target_commitish: $s, name: $t, generate_release_notes: true}')"
  [ "$STATUS" = 201 ] || { echo "::error::creating release $new: HTTP $STATUS $BODY"; return 1; }
  echo "release: $(jq -r .html_url <<<"$BODY")"
  echo "tag=$new" >> "${GITHUB_OUTPUT:-/dev/null}"

  [ "${MOVE_MAJOR_TAG:-false}" = true ] || return 0
  local mt; mt=$(major_tag "$new")
  api GET "/git/ref/tags/$mt"
  case $STATUS in
    200) api PATCH "/git/refs/tags/$mt" "$(jq -n --arg s "$sha" '{sha: $s, force: true}')"; want=200 ;;
    404) api POST /git/refs "$(jq -n --arg r "refs/tags/$mt" --arg s "$sha" '{ref: $r, sha: $s}')"; want=201 ;;
    *) BODY="(lookup of $mt failed)"; want=x ;;
  esac
  if [ "$STATUS" != "$want" ]; then
    # The release exists; only the move failed. Re-running would cut ANOTHER
    # release, so say exactly how to finish this one by hand.
    echo "::error::$new released, but moving $mt failed: HTTP $STATUS $BODY"
    echo "::error::finish by hand: git tag -f $mt $sha && git push --force origin refs/tags/$mt"
    return 1
  fi
  echo "$mt -> $sha"
  echo "major-tag=$mt" >> "${GITHUB_OUTPUT:-/dev/null}"
}

[ -n "${RELEASE_SH_LIB:-}" ] || main "$@"
