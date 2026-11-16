"""deploy.py, driven as the action runs it: the real script, real git, and a
bare repository standing in for the deploy repo's GitHub remote.

Only two things are stubbed, both external binaries: the renderer
(`kubectl kustomize`, via DEPLOY_RENDER_CMD) and `gh` (for the kill-switch
PR). Everything that decides what lands on main runs for real.

    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy.py"
IMG = "harbor.sirddail.net/ai/homelab-bin"

KUST = textwrap.dedent(f"""\
    apiVersion: kustomize.config.k8s.io/v1beta1
    kind: Kustomization
    # a comment the bump must keep
    resources:
      - cronjob.yaml
    images:
      - name: {IMG}
        newTag: 0.0.5
""")

# Stand-in for `kubectl kustomize <dir>`: prints an image line per
# name/newTag pair, so a bump changes the render exactly the way a real one
# does. A directory holding a file named BROKEN fails to render.
RENDER = textwrap.dedent("""\
    #!/usr/bin/env python3
    import re, sys, pathlib
    import os, subprocess
    d = pathlib.Path(sys.argv[1])
    racer = os.environ.get("RACER")   # another writer lands while we work, once
    if racer and not os.path.exists(racer + ".done"):
        open(racer + ".done", "w").close()
        subprocess.run(["git", "-C", racer, "push", "-q", "origin", "HEAD:main"], check=True)
    if (d / "BROKEN").exists():
        sys.exit("render failed")
    if (d / "STAMP").exists():   # a generator whose output moves with the tag
        print("stamp: " + (d / "kustomization.yaml").read_text().split("newTag:")[1].split()[0])
    name = None
    for line in (d / "kustomization.yaml").read_text().splitlines():
        m = re.match(r"^\\s*-\\s*name:\\s*(\\S+)", line)
        if m: name = m.group(1)
        m = re.match(r"^\\s*newTag:\\s*(\\S+)", line)
        if m and name: print(f"        image: {name}:{m.group(1)}"); name = None
    print("kind: CronJob")
""")


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=check)


class DeployRepo(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="deploy-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.remote = self.tmp / "remote.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        self.work = self.tmp / "seed"
        git(self.tmp, "clone", "-q", str(self.remote), str(self.work))
        for k, v in {"user.name": "seed", "user.email": "s@x"}.items():
            git(self.work, "config", k, v)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.stub("render", RENDER)
        # Hermetic: no test may reach the real gh / GitHub, even when the
        # code under test is broken (a mutant once sent a real
        # `gh pr create --repo dvystrcil/cluster-ops`; it failed only
        # because the branch was local). Tests that expect a PR replace it.
        self.gh_log = self.gh_stub()
        self.files({"mcp-trust-audit/kustomization.yaml": KUST,
                    "README.md": "deploy repo\n"})

    # --- fixture helpers -------------------------------------------------
    def stub(self, name, body):
        p = self.bin / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
        return p

    def files(self, mapping, msg="seed"):
        """Commit `mapping` onto the remote's main, as another writer would."""
        git(self.work, "pull", "-q", "origin", "main", check=False)
        for rel, text in mapping.items():
            f = self.work / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
            git(self.work, "add", rel)
        git(self.work, "commit", "-q", "-m", msg)
        git(self.work, "push", "-q", "origin", "HEAD:main")

    def racer(self, mapping):
        """A clone holding one unpushed commit, pushed mid-deploy."""
        r = self.tmp / "racer"
        git(self.tmp, "clone", "-q", str(self.remote), str(r))
        for k, v in {"user.name": "racer", "user.email": "r@x"}.items():
            git(r, "config", k, v)
        for rel, text in mapping.items():
            (r / rel).parent.mkdir(parents=True, exist_ok=True)
            (r / rel).write_text(text)
            git(r, "add", rel)
        git(r, "commit", "-q", "-m", "racer")
        return str(r)

    def main_file(self, rel):
        return git(self.remote, "show", f"main:{rel}").stdout

    def main_sha(self):
        return git(self.remote, "rev-parse", "main").stdout.strip()

    def deploy(self, version="0.0.6", images=IMG, **env):
        e = {**os.environ,
             "PATH": f"{self.bin}:{os.environ['PATH']}",
             "PYTHONDONTWRITEBYTECODE": "1",
             "DEPLOY_REPO": "dvystrcil/cluster-ops",
             "DEPLOY_REMOTE_URL": str(self.remote),
             "DEPLOY_IMAGES": images,
             "DEPLOY_VERSION": version,
             "DEPLOY_RENDER_CMD": str(self.bin / "render"),
             "GITHUB_OUTPUT": str(self.tmp / "out"),
             **env}
        e.pop("GH_TOKEN", None)
        e["GH_CONFIG_DIR"] = str(self.tmp / "no-gh-auth")
        return subprocess.run([sys.executable, str(DEPLOY)], env=e,
                              capture_output=True, text=True, timeout=120)

    def output(self):
        p = self.tmp / "out"
        return dict(l.split("=", 1) for l in p.read_text().splitlines()) if p.exists() else {}

    # --- behaviours ------------------------------------------------------
    def test_release_lands_on_main_as_a_one_line_newtag_commit(self):
        r = self.deploy()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.main_file("mcp-trust-audit/kustomization.yaml"),
                         KUST.replace("newTag: 0.0.5", "newTag: 0.0.6"))
        stat_ = git(self.remote, "show", "--numstat", "--format=", "main").stdout.split()
        self.assertEqual(stat_, ["1", "1", "mcp-trust-audit/kustomization.yaml"])
        self.assertEqual(self.output().get("result"), "committed")

    def test_an_older_release_never_downgrades_main(self):
        # A slow run for 0.0.6 finishing after 0.0.7 already landed.
        self.files({"mcp-trust-audit/kustomization.yaml":
                    KUST.replace("0.0.5", "0.0.7")}, "0.0.7 landed first")
        before = self.main_sha()
        r = self.deploy("0.0.6")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.main_sha(), before)
        self.assertEqual(self.output().get("result"), "noop")

    def test_the_same_version_again_is_a_noop(self):
        before = self.main_sha()
        r = self.deploy("0.0.5")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.main_sha(), before)
        self.assertEqual(self.output().get("result"), "noop")

    def test_a_tag_it_cannot_compare_is_refused_not_overwritten(self):
        # `latest`, a digest pin or a prerelease on main is a human decision.
        self.files({"mcp-trust-audit/kustomization.yaml":
                    KUST.replace("0.0.5", "latest")}, "pinned by hand")
        before = self.main_sha()
        r = self.deploy("0.0.6")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.main_sha(), before)
        self.assertIn("latest", r.stdout + r.stderr)

    def test_an_image_the_deploy_repo_does_not_list_fails_loudly(self):
        # "Found nothing to bump" must never read as "already current".
        before = self.main_sha()
        r = self.deploy(images="harbor.sirddail.net/ai/typo-bin")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("typo-bin", r.stdout + r.stderr)
        self.assertEqual(self.main_sha(), before)

    def test_every_consumer_of_the_image_moves_in_one_commit(self):
        self.files({"health-watcher/kustomization.yaml": KUST,
                    "other-app/kustomization.yaml":
                        KUST.replace(IMG, "harbor.sirddail.net/ai/other")})
        before = self.main_sha()
        self.assertEqual(self.deploy().returncode, 0)
        self.assertEqual(git(self.remote, "rev-parse", "main~1").stdout.strip(), before)
        changed = git(self.remote, "show", "--name-only", "--format=", "main").stdout.split()
        self.assertEqual(sorted(changed), ["health-watcher/kustomization.yaml",
                                           "mcp-trust-audit/kustomization.yaml"])

    def test_a_kustomization_that_does_not_render_blocks_the_deploy(self):
        self.files({"mcp-trust-audit/BROKEN": ""})
        before = self.main_sha()
        r = self.deploy()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("mcp-trust-audit", r.stdout + r.stderr)
        self.assertEqual(self.main_sha(), before)

    def test_a_render_that_changes_more_than_the_image_blocks_the_deploy(self):
        self.files({"mcp-trust-audit/STAMP": ""})
        before = self.main_sha()
        r = self.deploy()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("stamp", r.stdout + r.stderr)
        self.assertEqual(self.main_sha(), before)

    def test_a_bump_no_render_picks_up_is_refused(self):
        # The tag moves in a base/ that no overlay renders: nothing deploys,
        # so the commit would only look like a deploy.
        self.files({"mcp-trust-audit/kustomization.yaml": "kind: Kustomization\n",
                    "orphan/base/kustomization.yaml": KUST})
        before = self.main_sha()
        r = self.deploy()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no rendered", r.stdout + r.stderr)
        self.assertEqual(self.main_sha(), before)

    def test_main_moving_under_the_deploy_is_retried_on_the_new_main(self):
        racer = self.racer({"other-app/kustomization.yaml": "kind: Kustomization\n"})
        r = self.deploy(RACER=racer)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(git(self.remote, "log", "-1", "--format=%s", "main~1").stdout.strip(), "racer")
        self.assertIn("newTag: 0.0.6", self.main_file("mcp-trust-audit/kustomization.yaml"))
        self.assertEqual(self.output().get("result"), "committed")

    def test_a_newer_release_landing_first_wins_the_race(self):
        racer = self.racer({"mcp-trust-audit/kustomization.yaml": KUST.replace("0.0.5", "0.0.7")})
        r = self.deploy(RACER=racer)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(git(self.remote, "log", "-1", "--format=%s", "main").stdout.strip(), "racer")
        self.assertEqual(self.output().get("result"), "noop")

    def gh_stub(self):
        log = self.tmp / "gh.log"
        self.stub("gh", f"""#!/bin/sh
printf '%s\\n' "$@" >> {log}
echo https://github.com/dvystrcil/cluster-ops/pull/9
""")
        return log

    def test_kill_switch_opens_a_bump_pr_and_leaves_main_alone(self):
        log = self.gh_log
        before = self.main_sha()
        r = self.deploy(DEPLOY_AUTO="false")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.main_sha(), before)
        branch = "deploy/homelab-bin-0.0.6"
        self.assertIn("newTag: 0.0.6", git(self.remote, "show",
                      f"{branch}:mcp-trust-audit/kustomization.yaml").stdout)
        args = log.read_text().splitlines()
        self.assertEqual(args[:2], ["pr", "create"])
        self.assertEqual(args[args.index("--head") + 1], branch)
        self.assertEqual(args[args.index("--repo") + 1], "dvystrcil/cluster-ops")
        self.assertEqual(self.output().get("result"), "pr")

    def test_kill_switch_still_never_downgrades(self):
        log = self.gh_log
        r = self.deploy("0.0.4", DEPLOY_AUTO="false")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertFalse(log.exists())
        self.assertEqual(self.output().get("result"), "noop")

    def test_an_unrecognised_kill_switch_value_is_refused(self):
        # A typo must not pick a side: neither deploy nor fall back.
        before = self.main_sha()
        r = self.deploy(DEPLOY_AUTO="flase")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.main_sha(), before)
        self.assertFalse(self.gh_log.exists(), "a typo fell back to a PR")
        self.assertEqual(git(self.remote, "branch", "--list", "deploy/*").stdout, "")

    def test_a_release_crossing_the_deployed_major_becomes_a_pr(self):
        # bump-image-tag.py never crossed a major (its constraint follows the
        # deployed tag). A major is a breaking change: review it, don't ship it.
        before = self.main_sha()
        r = self.deploy("1.0.0")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.main_sha(), before)
        self.assertEqual(self.output().get("result"), "pr")
        self.assertIn("newTag: 1.0.0", git(self.remote, "show",
                      "deploy/homelab-bin-1.0.0:mcp-trust-audit/kustomization.yaml").stdout)
        self.assertIn("major", r.stdout)

    def test_a_release_within_the_major_still_commits(self):
        self.files({"mcp-trust-audit/kustomization.yaml": KUST.replace("0.0.5", "1.2.3")})
        r = self.deploy("1.3.0")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.output().get("result"), "committed")

    def test_a_version_that_is_not_x_y_z_is_refused(self):
        before = self.main_sha()
        for bad in ("dev", "0.0.6-rc1", "0.6", ""):
            r = self.deploy(bad)
            self.assertNotEqual(r.returncode, 0, bad)
        self.assertEqual(self.main_sha(), before)


class DiffGuard(unittest.TestCase):
    """check_diff() is the last thing between an editing bug and production:
    whatever bump_text() did, only `newTag:` lines may change."""

    def setUp(self):
        sys.path.insert(0, str(ROOT))
        self.addCleanup(sys.path.remove, str(ROOT))
        import deploy
        self.check = deploy.check_diff
        self.Error = deploy.DeployError

    def test_newtag_only_diff_passes(self):
        self.check(textwrap.dedent("""\
            diff --git a/a/kustomization.yaml b/a/kustomization.yaml
            --- a/a/kustomization.yaml
            +++ b/a/kustomization.yaml
            @@ -8 +8 @@ images:
            -    newTag: 0.0.5
            +    newTag: 0.0.6
            """))

    def test_any_other_changed_line_is_refused(self):
        for line in ("-  - name: x", "+  - name: y", "+replicas: 3", "-", "+"):
            with self.subTest(line=line), self.assertRaises(self.Error):
                self.check(textwrap.dedent("""\
                    --- a/k.yaml
                    +++ b/k.yaml
                    @@ -8 +8 @@
                    -    newTag: 0.0.5
                    +    newTag: 0.0.6
                    """) + line + "\n")

    def test_a_hunk_line_that_looks_like_a_file_header_is_still_checked(self):
        # Removing the YAML line `-- x` shows as `--- x` inside the hunk.
        with self.assertRaises(self.Error):
            self.check("--- a/k\n+++ b/k\n@@ -8,2 +8 @@\n"
                       "-    newTag: 0.0.5\n--- x\n+    newTag: 0.0.6\n")

    def test_an_added_or_removed_newtag_line_is_refused(self):
        # Paired -/+ only: an extra `+ newTag:` would add a second tag.
        with self.assertRaises(self.Error):
            self.check("--- a/k\n+++ b/k\n@@ -8,0 +9 @@\n+    newTag: 0.0.6\n")

    def test_a_render_losing_a_document_separator_is_refused(self):
        import deploy
        with self.assertRaises(self.Error):
            deploy.check_renders({"r": "a: 1\n---\n  image: x:1\n"},
                                 {"r": "a: 1\n  image: x:2\n"})

    def test_an_empty_diff_is_refused(self):
        with self.assertRaises(self.Error):
            self.check("")


if __name__ == "__main__":
    unittest.main()
