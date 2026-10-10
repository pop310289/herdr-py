"""scripts/oneshot.py: one bash script that writes every file of a commit, for a machine that cannot clone the
repository. Both kinds are run with bash and must write each file byte for byte, with its executable bit; a script
changed on the way must say so; no line may be long enough for a terminal to cut it."""
import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "scripts", "oneshot.py")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import oneshot  # noqa: E402

NEEDED = ("bash", "git", "base64", "tar", "gzip", "mktemp", "grep")
MISSING = [c for c in NEEDED if shutil.which(c) is None] + ([] if shutil.which("sha256sum") or shutil.which("shasum") else ["sha256sum"])

# what a repository can hold that a here-document cannot carry as it is
FILES = {
    "a.txt": "plain text\nsecond line\n",
    "run.sh": "#!/bin/sh\necho \"$HOME\" `whoami` \\$x '\\n'\n",           # nothing in it may be expanded
    "no_newline.txt": "the last line has no newline",
    "bin/data.bin": bytes(range(256)),
    "empty.txt": "",
    "dir with space/中文.md": "名字有空格與中文\n",
    "mark.txt": "before\n" + oneshot.MARK + "\nafter\n",                    # a line equal to the mark
    "long.txt": "x" * 1500 + "\n",                                           # a line a terminal might cut
    "crlf.txt": "a\r\nb\r\n",
    "deep/a/b/c/d.txt": "deep\n",
}


def run(args, cwd, **kw):
    return subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                          timeout=120, **kw)


def tree(folder):
    """{path: (bytes, executable)} of every file under folder."""
    out = {}
    for d, _, files in os.walk(folder):
        for name in files:
            path = os.path.join(d, name)
            with open(path, "rb") as handle:
                out[os.path.relpath(path, folder)] = (handle.read(), bool(os.stat(path).st_mode & stat.S_IXUSR))
    return out


def trusting(repo):
    """The environment with repo marked as a safe directory, for this test's own git calls only: on a CI runner the
    checkout belongs to another user than the container's, and git refuses to read it otherwise."""
    return dict(os.environ, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="safe.directory", GIT_CONFIG_VALUE_0=repo)


def has_commits(repo):
    if shutil.which("git") is None:
        return False
    return subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "HEAD"], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=trusting(repo)).returncode == 0


def committed(repo, rev="HEAD"):
    """{path: (bytes, executable)} of the files of a commit, read from git archive (the one-shot script left out)."""
    raw = subprocess.run(["git", "-C", repo, "archive", "--format=tar", rev], stdout=subprocess.PIPE, check=True,
                         env=trusting(repo)).stdout
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        return {m.name: (tar.extractfile(m).read(), bool(m.mode & 0o100)) for m in tar.getmembers()
                if m.isfile() and m.name != oneshot.ONESHOT}


def manifest(script):
    """The paths a script's sha256 list names."""
    with open(script, encoding="utf-8") as handle:
        text = handle.read()
    sums = text.split("cat > \"$sums\" <<'%s'\n" % oneshot.MARK, 1)[1].split("\n%s\n" % oneshot.MARK, 1)[0]
    return sorted(line.split("  ", 1)[1] for line in sums.split("\n"))


@unittest.skipIf(MISSING, "needs " + ", ".join(MISSING))
class OneShotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
                        GIT_COMMITTER_EMAIL="t@example.invalid", GIT_CONFIG_NOSYSTEM="1", HOME=self.tmp)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        for name, content in FILES.items():
            path = os.path.join(self.repo, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(content if isinstance(content, bytes) else content.encode("utf-8"))
        os.chmod(os.path.join(self.repo, "run.sh"), 0o755)
        for args in (["init", "-q"], ["config", "core.autocrlf", "false"], ["add", "-A"], ["commit", "-q", "-m", "files"]):
            proc = run(["git"] + args, self.repo, env=self.env)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def make(self, repo, kind="both"):
        out = os.path.join(self.tmp, "out")
        os.makedirs(out, exist_ok=True)
        proc = run([sys.executable, SCRIPT, "--repo", repo, "--out", out, "--kind", kind], self.tmp, env=trusting(repo))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return {name.rsplit("-", 1)[1][:-3]: os.path.join(out, name) for name in sorted(os.listdir(out))}

    def unpack(self, script, dest):
        return run(["bash", script, dest], self.tmp)

    def oneshot(self, *args):
        return run([sys.executable, SCRIPT, "--repo", self.repo] + list(args), self.tmp, env=trusting(self.repo))

    def test_update_and_check_keep_the_repositorys_own_script_with_its_files(self):
        with open(os.path.join(self.repo, ".gitignore"), "w") as handle:
            handle.write("*.log\n")
        self.assertEqual(self.oneshot("--check").returncode, 1)  # not written yet
        self.assertEqual(self.oneshot("--update").returncode, 0)
        self.assertEqual(self.oneshot("--check").returncode, 0)
        script = os.path.join(self.repo, oneshot.ONESHOT)
        self.assertTrue(os.stat(script).st_mode & stat.S_IXUSR)
        self.assertEqual(manifest(script), sorted(list(FILES) + [".gitignore"]))  # never itself
        with open(os.path.join(self.repo, "x.log"), "w") as handle:
            handle.write("ignored\n")
        self.assertEqual(self.oneshot("--check").returncode, 0)  # what git ignores is not a file of the commit
        with open(os.path.join(self.repo, "a.txt"), "a") as handle:
            handle.write("a change\n")
        proc = self.oneshot("--check")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("run python3 scripts/oneshot.py --update", proc.stderr)
        self.oneshot("--update")
        with open(os.path.join(self.repo, "new.txt"), "w") as handle:
            handle.write("a new file, not added yet\n")
        self.assertEqual(self.oneshot("--check").returncode, 1)  # git add -A would commit it
        self.oneshot("--update")
        self.assertEqual(self.oneshot("--check").returncode, 0)
        dest = os.path.join(self.tmp, "got")
        proc = self.unpack(script, dest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        want = {name: data for name, data in tree(self.repo).items()
                if not name.startswith(".git" + os.sep) and name not in (oneshot.ONESHOT, "x.log")}
        self.assertEqual(tree(dest), want)

    def test_a_commits_scripts_leave_the_repositorys_own_script_out(self):
        self.oneshot("--update")
        for args in (["add", "-A"], ["commit", "-q", "-m", "its own script"]):
            self.assertEqual(run(["git"] + args, self.repo, env=self.env).returncode, 0)
        for kind, script in self.make(self.repo).items():
            self.assertNotIn(oneshot.ONESHOT, manifest(script), kind)
            dest = os.path.join(self.tmp, "got-" + kind)
            self.assertEqual(self.unpack(script, dest).returncode, 0)
            self.assertNotIn(oneshot.ONESHOT, tree(dest), kind)

    def test_both_kinds_write_every_file_byte_for_byte_with_its_mode(self):
        scripts = self.make(self.repo)
        self.assertEqual(sorted(scripts), ["files", "packed"])
        want = committed(self.repo)
        self.assertTrue(want["run.sh"][1])
        for kind, script in scripts.items():
            dest = os.path.join(self.tmp, "got-" + kind)
            proc = self.unpack(script, dest)
            self.assertEqual(proc.returncode, 0, kind + ": " + proc.stderr)
            self.assertIn("%d files written" % len(FILES), proc.stdout)
            self.assertEqual(tree(dest), want, kind)
        with open(scripts["files"], encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("cat > a.txt <<'%s'\nplain text\nsecond line\n%s\n" % (oneshot.MARK, oneshot.MARK), text)  # readable
        for name in ("no_newline.txt", "bin/data.bin", "mark.txt", "long.txt", "crlf.txt"):
            self.assertIn("base64 -d > %s <<'" % name, text)  # what a here-document would change
        self.assertIn(": > empty.txt\n", text)
        self.assertIn("chmod 755 run.sh\n", text)
        self.assertNotIn("chmod 755 a.txt", text)

    def test_no_line_is_long_enough_for_a_terminal_to_cut(self):
        for kind, script in self.make(self.repo).items():
            with open(script, "rb") as handle:
                longest = max(len(line) for line in handle.read().split(b"\n"))
            self.assertLess(longest, oneshot.LONG, kind)

    def test_a_script_changed_on_the_way_says_which_files_differ(self):
        scripts = self.make(self.repo)
        with open(scripts["files"], encoding="utf-8") as handle:
            text = handle.read()
        bad = os.path.join(self.tmp, "bad-files.sh")
        with open(bad, "w", encoding="utf-8") as handle:
            handle.write(text.replace("second line\n", "secend line\n", 1))
        proc = self.unpack(bad, os.path.join(self.tmp, "got-bad"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("a.txt: FAILED", proc.stderr)
        self.assertIn("this script changed on the way", proc.stderr)
        last = os.path.join(self.tmp, "bad-last.sh")  # the last file of the list is checked too
        with open(last, "w", encoding="utf-8") as handle:
            handle.write(text.replace("`whoami`", "`whoamI`", 1))
        proc = self.unpack(last, os.path.join(self.tmp, "got-bad-last"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("run.sh: FAILED", proc.stderr)
        sums = text.split("cat > \"$sums\" <<'%s'\n" % oneshot.MARK, 1)[1].split("\n%s\n" % oneshot.MARK, 1)[0]
        self.assertEqual(sorted(line.split("  ", 1)[1] for line in sums.split("\n")), sorted(FILES))
        with open(scripts["packed"], encoding="utf-8") as handle:
            text = handle.read()
        i = text.index("| tar -xzf -\n") + 400  # a character inside the archive's base64
        bad = os.path.join(self.tmp, "bad-packed.sh")
        with open(bad, "w", encoding="utf-8") as handle:
            handle.write(text[:i] + ("A" if text[i] != "A" else "B") + text[i + 1:])
        self.assertNotEqual(self.unpack(bad, os.path.join(self.tmp, "got-bad-packed")).returncode, 0)

    def test_the_folder_must_not_exist_yet(self):
        script = self.make(self.repo, kind="packed")["packed"]
        dest = os.path.join(self.tmp, "taken")
        os.makedirs(dest)
        proc = self.unpack(script, dest)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("already exists", proc.stderr)
        self.assertEqual(os.listdir(dest), [])

    @unittest.skipUnless(has_commits(ROOT), "not a git checkout with a commit")
    def test_this_repository_keeps_its_own_script_up_to_date(self):
        proc = run([sys.executable, SCRIPT, "--check"], ROOT, env=trusting(ROOT))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        dest = os.path.join(self.tmp, "herdr-py-own")
        proc = self.unpack(os.path.join(ROOT, oneshot.ONESHOT), dest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(tree(dest), {name: (data, bool(mode & 0o100)) for name, data, mode in oneshot.checkout_members(ROOT)})

    @unittest.skipUnless(has_commits(ROOT), "not a git checkout with a commit")
    def test_this_repository(self):
        want = committed(ROOT)
        for kind, script in self.make(ROOT).items():
            dest = os.path.join(self.tmp, "herdr-py-" + kind)
            proc = self.unpack(script, dest)
            self.assertEqual(proc.returncode, 0, kind + ": " + proc.stderr)
            self.assertEqual(tree(dest), want, kind)
            with open(script, "rb") as handle:  # its many folders and long lines too
                self.assertLess(max(len(line) for line in handle.read().split(b"\n")), oneshot.LONG, kind)


if __name__ == "__main__":
    unittest.main()
