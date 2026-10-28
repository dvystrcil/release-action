#!/usr/bin/env bash
# Unit tests for release.sh's pure parts: the next version, the floating
# major tag, and what it refuses. The API calls are not mocked here -- a mock
# encodes what we THINK the API does; those paths are proven by the dogfood
# release in .github/workflows/release.yaml instead.
#
#   tests/test-release.sh
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
# shellcheck source=release.sh
RELEASE_SH_LIB=1 . ./release.sh
fail=0

expect() {  # expect <want> <cmd...>: stdout of a command that must succeed
  local want=$1; shift
  local got; got=$("$@" 2>/dev/null) || { echo "FAIL: $* exited non-zero, want $want"; fail=1; return; }
  if [ "$got" = "$want" ]; then echo "ok: $* -> $got"; else echo "FAIL: $* -> $got, want $want"; fail=1; fi
}
refuse() {  # refuse <cmd...>: must fail
  if "$@" >/dev/null 2>&1; then echo "FAIL: $* succeeded, want refusal"; fail=1; else echo "ok: refused $*"; fi
}

expect v0.1.2 next_version v0.1.1 patch
expect v0.2.0 next_version v0.1.1 minor
expect v1.0.0 next_version v0.1.1 major
expect v0.0.1 next_version v0.0.0 patch   # initial-version default
expect v1.1.0 next_version v1.0.9 minor   # minor resets patch
expect v2.0.0 next_version v1.4.7 major   # major resets both
expect v1.0.10 next_version v1.0.9 patch  # numeric, not lexical

expect v1 major_tag v1.0.1
expect v0 major_tag v0.2.0
expect v12 major_tag v12.3.4

refuse next_version v0.1.1 ""             # empty bump
refuse next_version v0.1.1 Patch          # only the three exact words
refuse next_version v0.1.1 hotfix
refuse next_version v1 patch              # a floating tag is not a release version
refuse next_version 1.2.3 patch           # v prefix required (git-tag convention)
refuse next_version v1.2.3-rc1 patch      # no prereleases
refuse next_version v1.2 patch
refuse next_version v01.2.3 patch         # leading zero
refuse major_tag v1

# The notes must be generated against the PREVIOUS RELEASE, named explicitly.
# Left to itself GitHub picks a base -- on kustomize-validate-action's first
# release it picked the floating v1 tag, and v1.0.1's notes listed 1 PR of 17.
p=$(release_payload v1.2.4 abc123 v1.2.3)
expect v1.2.3 jq -r .previous_tag_name <<<"$p"
expect true jq -r .generate_release_notes <<<"$p"
expect abc123 jq -r .target_commitish <<<"$p"
expect v1.2.4 jq -r .tag_name <<<"$p"
# First release: no previous release exists, so none may be named.
expect false jq 'has("previous_tag_name")' <<<"$(release_payload v0.0.1 abc123 "")"

# api() sets $STATUS in the calling shell. Called as $(api ...), it runs in a
# subshell and STATUS is lost: main() died "STATUS: unbound variable" on every
# path when this was first written (caught by a real-API probe, not by these
# unit tests, which is why this static check exists).
if grep -nE '^[^#]*\$\(api ' release.sh; then
  echo "FAIL: api called inside \$( ) above -- STATUS would be lost"; fail=1
else
  echo "ok: api is never called in a subshell"
fi

# --- main(), composed. api() is the only I/O, so it is replaced by a stub
# that serves scripted responses and records every write. The status codes
# are the ones the REAL API returned in the 2026-10-28 probe (200 latest,
# 404 no release / unknown tag, 401 bad token, 200 existing v1 ref); this
# proves the wiring between them, not GitHub's behavior.
declare -A RESP
api() {
  local key="$1 $2"; echo "$key $( [ -n "${3:-}" ] && jq -c . <<<"$3")" >> "$CALLS"   # one line per call
  STATUS=${RESP[$key]%%|*}; BODY=${RESP[$key]#*|}
  [ -n "${RESP[$key]:-}" ] || { STATUS=599; BODY="unscripted: $key"; }
}
run_main() {  # run_main <MOVE_MAJOR_TAG> -> exit code; writes in $CALLS
  CALLS=$(mktemp); GITHUB_OUTPUT=$(mktemp)
  ( GH_TOKEN=x REPO=o/r GITHUB_SHA=sha1 MOVE_MAJOR_TAG=$1 BUMP=patch INITIAL_VERSION=v1.0.0 main ) >/dev/null 2>&1
}
posted() { grep '^POST /releases ' "$CALLS" | sed 's#^POST /releases ##'; }

RESP=(["GET /releases/latest"]='200|{"tag_name":"v1.2.3"}' ["GET /git/ref/tags/v1.2.4"]='404|{}'
      ["POST /releases"]='201|{"html_url":"u"}' ["GET /git/ref/tags/v1"]='200|{}' ["PATCH /git/refs/tags/v1"]='200|{}')
if run_main true; then
  expect v1.2.3 jq -r .previous_tag_name <<<"$(posted)"
  expect sha1 jq -r .target_commitish <<<"$(posted)"
  expect sha1 jq -r .sha <<<"$(grep '^PATCH /git/refs/tags/v1 ' "$CALLS" | sed 's#^PATCH /git/refs/tags/v1 ##')"
  expect "tag=v1.2.4" grep -x tag=v1.2.4 "$GITHUB_OUTPUT"
else echo "FAIL: main failed on the happy path: $(cat "$CALLS")"; fail=1; fi

RESP=(["GET /releases/latest"]='404|{}' ["GET /tags?per_page=1"]='200|[{"name":"v1"}]'
      ["GET /git/ref/tags/v1.0.1"]='404|{}' ["POST /releases"]='201|{"html_url":"u"}')
if run_main false; then
  expect false jq 'has("previous_tag_name")' <<<"$(posted)"
  expect v1.0.1 jq -r .tag_name <<<"$(posted)"
else echo "FAIL: main failed on a first release: $(cat "$CALLS")"; fail=1; fi

# Everything a fallback would need is scripted, so only main() rejecting the
# 401 itself can stop a release (the old code fell back to initial-version).
RESP=(["GET /releases/latest"]='401|{"message":"Bad credentials"}' ["GET /tags?per_page=1"]='200|[]'
      ["GET /git/ref/tags/v1.0.1"]='404|{}' ["POST /releases"]='201|{"html_url":"u"}')
if run_main false; then echo "FAIL: main succeeded on a 401"; fail=1
elif [ -n "$(posted)" ]; then echo "FAIL: a 401 still created a release"; fail=1
else echo "ok: a 401 on the latest-release read fails without releasing"; fi

RESP=(["GET /releases/latest"]='200|{"tag_name":"v1.2.3"}' ["GET /git/ref/tags/v1.2.4"]='200|{}')
if run_main false || [ -n "$(posted)" ]; then echo "FAIL: an existing tag was released over"; fail=1
else echo "ok: an existing tag is refused without releasing"; fi

[ $fail -eq 0 ] && echo "TEST-RELEASE-COMPLETE"
exit $fail
