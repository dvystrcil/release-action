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

# api() sets $STATUS in the calling shell. Called as $(api ...), it runs in a
# subshell and STATUS is lost: main() died "STATUS: unbound variable" on every
# path when this was first written (caught by a real-API probe, not by these
# unit tests, which is why this static check exists).
if grep -nE '^[^#]*\$\(api ' release.sh; then
  echo "FAIL: api called inside \$( ) above -- STATUS would be lost"; fail=1
else
  echo "ok: api is never called in a subshell"
fi

[ $fail -eq 0 ] && echo "TEST-RELEASE-COMPLETE"
exit $fail
