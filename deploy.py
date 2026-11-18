#!/usr/bin/env python3
"""deploy.py -- commit a released image tag straight to a deploy repo's main.

Called by action.yaml when `deploy-repo` is set; every input arrives as an
env var. homelab#2320 step 2: merge = release = deploy, with no bump PR.

    DEPLOY_REPO        owner/name of the deploy repo (kustomize)
    DEPLOY_IMAGES      image names, space/newline separated
                       (harbor.sirddail.net/ai/homelab-bin)
    DEPLOY_VERSION     the image tag to deploy, X.Y.Z (a leading v is dropped)
    GH_TOKEN           contents: write on DEPLOY_REPO
    DEPLOY_REMOTE_URL  test seam: a git URL used instead of github.com
    DEPLOY_RENDER_CMD  the renderer, default `kubectl kustomize`

Stdlib only: the ARC runner image has python3 but no pip.
"""
from __future__ import annotations

import base64
import difflib
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

BOT = ("homelab-bump-bot", "noreply@sirddail.net")


class DeployError(Exception):
    """Something we cannot do safely. Nothing has been pushed."""


def git(cwd, *args, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       timeout=180)
    if check and r.returncode != 0:
        raise DeployError(f"git {args[0]}: {(r.stderr or r.stdout).strip()[:400]}")
    return r


SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


SUFFIX = re.compile(r"^(-[0-9A-Za-z][0-9A-Za-z.-]*)?$")


def semver(tag: str, suffix: str = "") -> tuple[int, int, int] | None:
    """The X.Y.Z of `tag`, or None. With a declared `suffix` (tag-suffix,
    e.g. `-rocm-homelab`), the tag must be exactly X.Y.Z<suffix>: docker tags
    can't carry `+build` info, so it rides after a `-`, where it looks just
    like a prerelease. Undeclared, any suffix is refused."""
    if suffix:
        if not tag.endswith(suffix):
            return None
        tag = tag[: -len(suffix)]
    m = SEMVER.match(tag)
    return tuple(int(x) for x in m.groups()) if m else None


def bump_text(text: str, image: str, version: str,
              suffix: str = "") -> tuple[str, list[str]]:
    """Raise the newTag line under each `- name: <image>` to `version`.

    Returns the new text and the current tags it found. Never lowers a tag,
    and refuses one it cannot compare (`latest`, a digest, a prerelease):
    whoever pinned that made a decision a release must not overwrite.
    """
    want = semver(version, suffix)
    lines = text.splitlines(keepends=True)
    found = []
    for i, line in enumerate(lines):
        if re.match(rf"^\s*-?\s*name:\s*{re.escape(image)}\s*$", line):
            for j in range(i + 1, min(i + 4, len(lines))):
                m = re.match(r"^\s*newTag:\s*(\S+)\s*$", lines[j])
                if m:
                    cur = m.group(1).strip("'\"")
                    found.append(cur)
                    if semver(cur, suffix) is None:
                        raise DeployError(f"{image} is at {cur!r}, which is not X.Y.Z{suffix}: "
                                          f"refusing to overwrite a tag it cannot compare")
                    if semver(cur, suffix) < want:
                        lines[j] = re.sub(r"(newTag:\s*)\S+", rf"\g<1>{version}", lines[j])
                    break
    return "".join(lines), found


NEWTAG = re.compile(r"^[-+]\s*newTag:\s*\S+\s*$")


def check_diff(diff: str) -> None:
    """Refuse a diff that changes anything but `newTag:` values.

    Reads `git diff -U0` output. Every removed line must be a newTag line and
    be replaced by exactly one newTag line: a deploy changes a value, never
    the shape of a file.
    """
    minus = plus = 0
    in_hunk = False
    for line in diff.splitlines():
        # File headers come before a file's first @@; inside a hunk, a line
        # reading `--- x` is the removed YAML line `-- x`, not a header.
        if line.startswith("diff --git"):
            in_hunk = False
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith(("-", "+")):
            if not NEWTAG.match(line):
                raise DeployError(f"refusing a diff that changes more than newTag: {line!r}")
            if line.startswith("-"):
                minus += 1
            else:
                plus += 1
    if minus == 0 or minus != plus:
        raise DeployError(f"refusing an unpaired diff: {minus} newTag lines removed, "
                          f"{plus} added")


IMAGE_LINE = re.compile(r"^\s*(-\s*)?image:\s*\S+\s*$")


def roots(work: Path) -> list[Path]:
    """Kustomize build roots, as kustomize-validate-action picks them: every
    kustomization except bases (reached through their overlays) and
    Components (not buildable alone)."""
    out = []
    for k in sorted(work.rglob("kustomization.yaml")):
        rel = k.parent.relative_to(work)
        if ".git" in rel.parts or "base" in rel.parts:
            continue
        if re.search(r"^kind:\s*Component\s*$", k.read_text(), re.M):
            continue
        out.append(k.parent)
    return out


def render_all(work: Path, dirs: list[Path]) -> dict[str, str]:
    cmd = shlex.split(os.environ.get("DEPLOY_RENDER_CMD") or "kubectl kustomize")
    out = {}
    for d in dirs:
        r = subprocess.run([*cmd, str(d)], capture_output=True, text=True, timeout=300)
        rel = str(d.relative_to(work))
        if r.returncode != 0:
            raise DeployError(f"{rel} does not render: {(r.stderr or r.stdout).strip()[:400]}")
        out[rel] = r.stdout
    return out


TAG_CHARS = r"[0-9A-Za-z_.-]"


def ref_moved(old: str, new: str, images: list[str], version: str,
              suffix: str = "") -> bool:
    """True when the only difference between rendered lines `old` and `new`
    is one deployed image reference moving up to the release:
    `<image>:<tag>` -> `<image>:<version>`, `<tag>` below `version`.

    The field the reference sits in does not matter. gpu-batch copies each
    CronJob image into an env `value:` (replacements:, gpu-batch#16), and
    that copy moves with the tag. A second reference on the line, another
    image, another tag, or any other edit makes it False.
    """
    want = semver(version, suffix)
    hits = []
    for image in images:
        pat = rf"(?<![\w./-]){re.escape(image)}:({TAG_CHARS}+)(?!{TAG_CHARS}|[@:/])"
        hits += [(m, image) for m in re.finditer(pat, old)]
    if len(hits) != 1:
        return False
    m, image = hits[0]
    cur = semver(m.group(1), suffix)
    if want is None or cur is None or cur >= want:
        return False
    return new == f"{old[:m.start()]}{image}:{version}{old[m.end():]}"


def check_renders(before: dict[str, str], after: dict[str, str],
                  images: list[str] = (), version: str = "",
                  suffix: str = "") -> list[str]:
    """The bump must change rendered images and nothing else.

    main passed the deploy repo's required `manifests` check (kustomize
    build + kubeconform). A render that differs from main's only in image
    strings cannot change schema validity, so this proves as much as running
    the schema check again, without installing kubeconform on every runner.

    A changed line passes when it is an `image:` line, or when, paired
    one-to-one with its counterpart in the same hunk, the only change is one
    of `images` moving from its old tag to `version` (see ref_moved). A hunk
    whose removed and added counts differ gets no pairing: every line in it
    must be an `image:` line. Returns the roots whose render changed.
    """
    changed = []
    for root, old in before.items():
        new = after[root]
        if new == old:
            continue
        diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), n=0, lineterm=""))
        # Skip the two file headers by position: a removed `---` document
        # separator also starts with `---`.
        hunks, cur = [], None
        for line in diff[2:]:
            if line.startswith("@@"):
                cur = ([], [])
                hunks.append(cur)
                continue
            cur[0 if line.startswith("-") else 1].append(line[1:])

        def refuse(sign, line):
            raise DeployError(f"the bump changes {root}'s render beyond image "
                              f"lines: {sign + line!r}")

        for minus, plus in hunks:
            if len(minus) != len(plus):
                for sign, lines in (("-", minus), ("+", plus)):
                    for line in lines:
                        if not IMAGE_LINE.match(line):
                            refuse(sign, line)
                continue
            for a, b in zip(minus, plus):
                if IMAGE_LINE.match(a) and IMAGE_LINE.match(b):
                    continue
                if not ref_moved(a, b, images, version, suffix):
                    refuse("-", a)
        changed.append(root)
    if not changed:
        raise DeployError("no rendered manifest changes: the bumped newTag is not "
                          "built by any kustomize root, so nothing would deploy")
    return changed


def set_output(key: str, value: str) -> None:
    with open(os.environ.get("GITHUB_OUTPUT") or os.devnull, "a") as out:
        out.write(f"{key}={value}\n")


ATTEMPTS = 5


def plan(work: Path, repo: str, images: list[str], version: str,
         suffix: str = "") -> tuple[dict[Path, str], list[str]]:
    """New text for each kustomization the release moves, and the tags it
    replaces. Writes nothing."""
    seen = {image: [] for image in images}
    edits = {}
    replaced = []
    for f in sorted(work.rglob("kustomization.yaml")):
        if ".git" in f.relative_to(work).parts:
            continue
        text = new = f.read_text()
        for image in images:
            new, found = bump_text(new, image, version, suffix)
            seen[image] += found
            replaced += [t for t in found if semver(t, suffix) < semver(version, suffix)]
        if new != text:
            edits[f] = new
    missing = [image for image, found in seen.items() if not found]
    if missing:
        # Every image named must be deployed somewhere. Skipping one quietly
        # is how a consumer goes months without a bump.
        raise DeployError(f"no kustomization in {repo} has a newTag for {', '.join(missing)}")
    return edits, replaced


def open_pr(work: Path, auth: list[str], repo: str, images: list[str],
            version: str, title: str, reason: str) -> str:
    """The kill switch: the same validated commit, proposed instead of pushed."""
    branch = f"deploy/{images[0].rsplit('/', 1)[-1]}-{version}"
    git(work, *auth, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    body = (f"Release `{version}` of {', '.join(f'`{i}`' for i in images)}.\n\n"
            f"Opened as a PR because {reason}; otherwise release-action commits "
            f"this straight to main. The diff was "
            f"checked the same way: newTag lines only, no downgrade, every kustomize "
            f"root renders with image-reference-only changes.\n")
    r = subprocess.run(["gh", "pr", "create", "--repo", repo, "--base", "main",
                        "--head", branch, "--title", title, "--body", body],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise DeployError(f"gh pr create: {(r.stderr or r.stdout).strip()[:400]}")
    print(f"opened {r.stdout.strip().splitlines()[-1]}")
    return "pr"


def attempt(tmp: str, n: int, url: str, auth: list[str], repo: str,
            images: list[str], version: str, auto: bool = True,
            suffix: str = "") -> str:
    """One read-edit-validate-push against main as it is NOW.

    Returns "committed", "noop", or "retry" when main moved under us.
    """
    work = Path(tmp) / f"try{n}"
    git(tmp, *auth, "clone", "-q", "--depth", "1", "--branch", "main", url, str(work))
    edits, replaced = plan(work, repo, images, version, suffix)
    if not edits:
        print(f"{repo} main is already at or past {version}: nothing to deploy")
        return "noop"
    crossed = sorted({t for t in replaced if semver(t, suffix)[0] != semver(version, suffix)[0]})
    reason = "AUTO_DEPLOY is `false` for the source repo"
    if auto and crossed:
        # bump-image-tag.py, which this replaces, never crossed a major (its
        # constraint follows the deployed tag). A major is a breaking change:
        # it goes to review, not straight to main.
        print(f"{version} crosses the deployed major ({', '.join(crossed)}): "
              f"opening a PR instead of committing to main")
        auto = False
        reason = f"it crosses the deployed major ({', '.join(crossed)} -> {version})"
    build_roots = roots(work)
    before = render_all(work, build_roots)
    for f, text in edits.items():
        f.write_text(text)
    diff = git(work, "diff", "-U0").stdout
    check_diff(diff)
    print(diff)
    for root in check_renders(before, render_all(work, build_roots), images, version, suffix):
        print(f"renders: {root} (image references only)")
    title = f"deploy: {' '.join(i.rsplit('/', 1)[-1] for i in images)} {version}"
    git(work, "-c", f"user.name={BOT[0]}", "-c", f"user.email={BOT[1]}",
        "commit", "-qam", title)
    if not auto:
        return open_pr(work, auth, repo, images, version, title, reason)
    # A plain push: never --force. Rejected as non-fast-forward means someone
    # else landed first; start again from their main, never from ours.
    r = git(work, *auth, "push", "-q", "origin", "HEAD:main", check=False)
    if r.returncode == 0:
        print(f"deployed {version} to {repo}@{git(work, 'rev-parse', 'HEAD').stdout.strip()}")
        return "committed"
    err = (r.stderr or r.stdout).strip()
    if re.search(r"\((fetch first|non-fast-forward)\)", err):
        print(f"main moved during attempt {n}; re-reading it")
        return "retry"
    # Anything else (a ruleset refusal, auth) is not a race. Retrying would
    # hide it; falling back to a PR would too.
    raise DeployError(f"push to {repo} main refused: {err[:400]}")


def main() -> int:
    repo = os.environ["DEPLOY_REPO"]
    images = os.environ.get("DEPLOY_IMAGES", "").split()
    if not images:
        raise DeployError("deploy-images is empty")
    version = os.environ.get("DEPLOY_VERSION", "").removeprefix("v")
    suffix = os.environ.get("DEPLOY_TAG_SUFFIX", "").strip()
    if not SUFFIX.match(suffix):
        raise DeployError(f"tag-suffix {suffix!r} must start with '-' and use [0-9A-Za-z.-]")
    if semver(version, suffix) is None:
        raise DeployError(f"version {version!r} is not X.Y.Z{suffix}: only a release deploys")
    switch = (os.environ.get("DEPLOY_AUTO") or "true").strip().lower()
    if switch not in ("true", "false"):
        raise DeployError(f"auto-deploy must be true or false, got {switch!r}")
    auto = switch == "true"
    url = os.environ.get("DEPLOY_REMOTE_URL") or f"https://github.com/{repo}.git"
    auth = []
    if os.environ.get("GH_TOKEN") and not os.environ.get("DEPLOY_REMOTE_URL"):
        basic = base64.b64encode(f"x-access-token:{os.environ['GH_TOKEN']}".encode()).decode()
        auth = ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]
    with tempfile.TemporaryDirectory(prefix="deploy-") as tmp:
        for n in range(1, ATTEMPTS + 1):
            result = attempt(tmp, n, url, auth, repo, images, version, auto, suffix)
            if result != "retry":
                set_output("result", result)
                return 0
    raise DeployError(f"main kept moving: gave up after {ATTEMPTS} attempts")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DeployError as e:
        print(f"::error::{e}")
        sys.exit(1)
