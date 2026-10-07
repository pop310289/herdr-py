"""Tests for scripts/ci_privacy.py on small git repositories made in a temporary folder.

Needs the git command; no network. Values that must fail are put together at run time
(for example "/" + "home" + "/bob"), so this file itself passes the check it tests.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "ci_privacy.py")
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "tests.yml")


def load_script():
    spec = importlib.util.spec_from_file_location("ci_privacy", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ci_privacy = load_script()

NOREPLY = "12345+octo@users.noreply.github.com"
MAC_HOME = "/" + "Users" + "/alice"
LINUX_HOME = "/" + "home" + "/bob"
PERSONAL = "alice.smith" + "@" + "gmail.com"
ZERO = "0" * 40


class LineRules(unittest.TestCase):
    def test_home_paths_on_both_sides_of_the_rule(self):
        for text in (MAC_HOME + "/notes.txt", "cd " + LINUX_HOME, "PATH=/usr/bin:" + LINUX_HOME + "/bin",
                     "/" + "home" + "/a/x", "/" + "home" + "/_svc/x", "file://" + MAC_HOME + "/x"):
            self.assertTrue(ci_privacy.line_findings(text), text)
        for text in ("/home/<name>/x", "/Users/<name>/x", "$HOME/x", "${HOME}/x", "~/x", "/home/ and /Users/",
                     "/" + "home" + "/../x", "/" + "home" + "/.cache", "/homework/x", "/api/users/42/", "/tmp/x"):
            self.assertEqual(ci_privacy.line_findings(text), [], text)

    def test_findings_are_masked(self):
        self.assertEqual(ci_privacy.line_findings("see " + MAC_HOME + "/notes"), ["home-directory path /Users/***"])
        self.assertEqual(ci_privacy.line_findings("mail " + PERSONAL), ["e-mail address ***@gmail.com"])

    def test_allowed_addresses_on_both_sides_of_the_rule(self):
        for address in (NOREPLY, "octo@users.noreply.github.com", "X@Users.NoReply.GitHub.com", "noreply@github.com",
                        "NoReply@GitHub.com", "dev@example.com", "dev@EXAMPLE.com", "dev@mail.example.org",
                        "dev@example.net", "dev@host.example", "dev@box.test", "dev@Box.TEST", "dev@x.invalid",
                        "dev@app.localhost", "git@github.com"):
            self.assertTrue(ci_privacy.email_allowed(address), address)
        for local, domain in (("alice.smith", "gmail.com"), ("x", "users.noreply.github.com.evil.io"),
                              ("x", "noreply.github.com"), ("support", "github.com"), ("dev", "example.co"),
                              ("dev", "notexample.com"), ("dev", "box.testing"), ("gitx", "github.com"),
                              ("alice", "MacBook-Pro.local"), ("noreply", "anthropic.com")):
            self.assertFalse(ci_privacy.email_allowed(local + "@" + domain), domain)

    def test_things_that_only_look_like_addresses(self):
        for text in ("git clone git@github.com:owner/repo.git", "opencode-ai@1.18.32", "uses: actions/checkout@v7",
                     "@property", "a @ b.com", "<id>+<login>@users.noreply.github.com", "y = x@w.T"):
            self.assertEqual(ci_privacy.line_findings(text), [], text)

    def test_message_rules(self):
        message = "Fix it\n\nCo-authored-by: x <" + NOREPLY + ">\nCLAUDE-SESSION: s\nCoauthored by x\nlog " + LINUX_HOME + "/y\n"
        self.assertEqual(ci_privacy.message_findings(message), [
            'message line 3: contains "Co-authored-by"', 'message line 4: contains "CLAUDE-SESSION"',
            "message line 6: home-directory path /home/***"])


class AddedLinesParser(unittest.TestCase):
    def test_counts_hunks_so_added_lines_that_look_like_headers_are_kept(self):
        diff = (b"diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n@@ -1,2 +1,3 @@\n-old\n-gone\n"
                b"+++ looks like a header\n+--- also not a header\n+third\n\\ No newline at end of file\n"
                b"@@ -10 +11,0 @@\n-removed only\n"
                b"diff --git a/g b/g\nnew file mode 100644\n--- /dev/null\n+++ b/g\n@@ -0,0 +1 @@\n+single\n")
        self.assertEqual(list(ci_privacy.added_lines(diff)), [
            ("f.txt", 1, "++ looks like a header"), ("f.txt", 2, "--- also not a header"), ("f.txt", 3, "third"),
            ("g", 1, "single")])

    def test_context_lines_advance_the_line_number(self):
        diff = b"--- a/f\n+++ b/f\n@@ -5,3 +5,4 @@\n ctx\n-a\n+b\n+c\n ctx2\n"
        self.assertEqual(list(ci_privacy.added_lines(diff)), [("f", 6, "b"), ("f", 7, "c")])


class GitRepo:
    def __init__(self, path, env):
        self.path, self.env = path, env
        os.makedirs(path)
        self.git("init", "-q")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")

    def git(self, *args, **extra_env):
        env = dict(self.env)
        env.update(extra_env)
        proc = subprocess.run(["git"] + list(args), cwd=self.path, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=60)
        if proc.returncode != 0:
            raise AssertionError("git {} failed: {}".format(" ".join(args), proc.stderr.decode("utf-8", "replace")))
        return proc.stdout.decode("utf-8", "replace").strip()

    def write(self, files):
        for name, content in files.items():
            full = os.path.join(self.path.encode("utf-8"), name.encode("utf-8"))  # bytes: no locale needed
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as handle:
                handle.write(content if isinstance(content, bytes) else content.encode("utf-8"))

    def commit(self, files=None, message="change", remove=(), **extra_env):
        self.write(files or {})
        for name in remove:
            os.remove(os.path.join(self.path, name))
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message, **extra_env)
        return self.git("rev-parse", "HEAD")


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GITHUB_"))}
        self.env.update({"HOME": home, "XDG_CONFIG_HOME": home, "GIT_CONFIG_NOSYSTEM": "1",
                         "GIT_AUTHOR_NAME": "octo", "GIT_AUTHOR_EMAIL": NOREPLY,
                         "GIT_COMMITTER_NAME": "octo", "GIT_COMMITTER_EMAIL": NOREPLY})
        self.repo = GitRepo(os.path.join(self.tmp, "repo"), self.env)

    def check(self, *args, **extra_env):
        env = dict(self.env)
        env.update(extra_env)
        proc = subprocess.run([sys.executable, SCRIPT, "--repo", self.repo.path] + list(args), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        return proc.returncode, proc.stdout.decode("utf-8") + proc.stderr.decode("utf-8")

    def github(self, name, payload):
        path = os.path.join(self.tmp, "event.json")
        with open(path, "w") as handle:
            json.dump(payload, handle)
        return self.check("--github", GITHUB_EVENT_NAME=name, GITHUB_EVENT_PATH=path)


@unittest.skipUnless(shutil.which("git"), "needs git (RHEL 8 images may not have it; CI installs it)")
class RangeChecks(RepoCase):
    def test_clean_range_passes(self):
        base = self.repo.commit({"a.txt": "hello\n"}, "start")
        self.repo.commit({"b.txt": "mail " + NOREPLY + " or dev@example.com\n"}, "more")
        code, out = self.check("--base", base)
        self.assertEqual(code, 0, out)
        self.assertIn("1 commit(s) to check", out)
        self.assertIn("ci_privacy: ok", out)

    def test_trailers_fail_in_any_case_and_name_the_line(self):
        base = self.repo.commit({"a.txt": "a\n"}, "start")
        first = self.repo.commit({"a.txt": "b\n"}, "Work\n\nCo-authored-by: Someone <" + NOREPLY + ">")
        self.repo.commit({"a.txt": "c\n"}, "clean message")
        third = self.repo.commit({"a.txt": "d\n"}, "More\n\nclaude-session: https://example.com/s")
        code, out = self.check("--base", base)
        self.assertEqual(code, 1, out)
        self.assertIn(first[:7] + ' message line 3: contains "Co-authored-by"', out)
        self.assertIn(third[:7] + ' message line 3: contains "claude-session"', out)
        self.assertIn("2 problem(s) in 2 of 3 commit(s)", out)

    def test_home_paths_and_personal_addresses_fail_masked(self):
        base = self.repo.commit({"a.txt": "a\n"}, "start")
        sha = self.repo.commit({"notes.md": "ok line\nlog at " + MAC_HOME + "/x.log\nmail " + NOREPLY + "\n"
                                "ask " + PERSONAL + "\nremote git@github.com:o/r.git\nrun in " + LINUX_HOME + "/w\n"})
        code, out = self.check("--base", base)
        self.assertEqual(code, 1, out)
        self.assertIn(sha[:7] + " notes.md:2: home-directory path /Users/***", out)
        self.assertIn(sha[:7] + " notes.md:4: e-mail address ***@gmail.com", out)
        self.assertIn(sha[:7] + " notes.md:6: home-directory path /home/***", out)
        self.assertIn("3 problem(s) in 1 of 1 commit(s)", out)
        for leaked in ("alice", "bob", "smith"):
            self.assertNotIn(leaked, out)

    def test_author_and_committer_addresses(self):
        base = self.repo.commit({"a.txt": "a\n"}, "start")
        self.repo.commit({"a.txt": "b\n"}, "web merge", GIT_COMMITTER_NAME="GitHub", GIT_COMMITTER_EMAIL="noreply@github.com")
        self.repo.commit({"a.txt": "c\n"}, "no address at all", GIT_AUTHOR_EMAIL="")
        sha = self.repo.commit({"a.txt": "d\n"}, "mine", GIT_AUTHOR_EMAIL=PERSONAL,
                               GIT_COMMITTER_EMAIL="alice" + "@" + "MacBook-Pro.local")
        code, out = self.check("--base", base)
        self.assertEqual(code, 1, out)
        self.assertIn(sha[:7] + " author e-mail ***@gmail.com is not a GitHub noreply address", out)
        self.assertIn(sha[:7] + " committer e-mail ***@MacBook-Pro.local is not a GitHub noreply address", out)
        self.assertIn("2 problem(s) in 1 of 3 commit(s)", out)

    def test_deleting_a_bad_line_passes(self):
        base = self.repo.commit({"a.txt": "keep\nat " + LINUX_HOME + "/x\n"}, "old history")
        self.repo.commit({"a.txt": "keep\n"}, "remove the path")
        code, out = self.check("--base", base)
        self.assertEqual(code, 0, out)

    def test_a_line_added_and_deleted_inside_the_range_still_fails(self):
        base = self.repo.commit({"a.txt": "a\n"}, "start")
        added = self.repo.commit({"a.txt": "a\nat " + LINUX_HOME + "/x\n"}, "oops")
        self.repo.commit({"a.txt": "a\n"}, "fix")
        code, out = self.check("--base", base)
        self.assertEqual(code, 1, out)
        self.assertIn(added[:7] + " a.txt:2: home-directory path /home/***", out)

    def test_commits_reachable_from_base_are_not_checked(self):
        self.repo.commit({"a.txt": "mail " + PERSONAL + "\n"}, "before the check existed")
        base = self.repo.commit({"b.txt": "b\n"}, "base")
        self.repo.commit({"c.txt": "c\n"}, "new")
        self.assertEqual(self.check("--base", base)[0], 0)
        code, out = self.check()  # no base: all history
        self.assertEqual(code, 1, out)
        self.assertIn("3 commit(s) to check (all history of HEAD)", out)

    def test_a_merge_is_checked_for_what_it_adds_itself(self):
        self.repo.commit({"readme.txt": "hello\n"}, "start")
        self.repo.git("checkout", "-q", "-b", "feature")
        self.repo.commit({"b.txt": "feature work\n"}, "feature")
        self.repo.git("checkout", "-q", "main")
        self.repo.commit({"a.txt": "mail " + PERSONAL + "\n"}, "on main before the check existed")
        self.repo.git("checkout", "-q", "feature")
        self.repo.git("merge", "-q", "--no-edit", "main")
        code, out = self.check("--base", "main", "--head", "feature")
        self.assertEqual(code, 0, out)  # the merge brings a.txt from main but does not add it
        self.assertIn("2 commit(s) to check", out)

        self.repo.git("checkout", "-q", "main")
        self.repo.commit({"c.txt": "clean\n"}, "more main")
        self.repo.git("checkout", "-q", "feature")
        self.repo.git("merge", "-q", "--no-ff", "--no-commit", "main")
        self.repo.write({"evil.txt": "at " + LINUX_HOME + "/x\n"})
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "merge main")
        merge = self.repo.git("rev-parse", "HEAD")
        code, out = self.check("--base", "main", "--head", "feature")
        self.assertEqual(code, 1, out)
        self.assertIn(merge[:7] + " evil.txt:1: home-directory path /home/***", out)
        self.assertNotIn("a.txt", out)
        self.assertIn("1 problem(s) in 1 of 3 commit(s)", out)

    def test_the_root_commit_is_read_in_full(self):
        root = self.repo.commit({"a.txt": "one\nat " + MAC_HOME + "/x\n"}, "first")
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn(root[:7] + " a.txt:2: home-directory path /Users/***", out)

    def test_hostile_git_config_unusual_names_and_bytes(self):
        with open(os.path.join(self.env["HOME"], ".gitconfig"), "w") as handle:
            handle.write("[color]\n\tui = always\n\tdiff = always\n[diff]\n\tnoprefix = true\n\tmnemonicPrefix = true\n"
                         "\trenames = false\n\texternal = no-such-diff-tool\n[core]\n\tquotePath = true\n")
        wide = "docs/" + chr(0x65E5) + chr(0x672C) + ".md"  # a file name in Japanese
        base = self.repo.commit({"a.txt": "a\n"}, "start")
        sha = self.repo.commit({wide: "see " + MAC_HOME + "/x\n",
                                "latin1.txt": b"caf\xe9 " + PERSONAL.encode("ascii") + b"\n",
                                "space name.txt": "+++ " + LINUX_HOME + "/y\n"})
        code, out = self.check("--base", base, PYTHONIOENCODING="ascii")  # like Python 3.6 under the C locale
        self.assertNotIn("Traceback", out)
        self.assertEqual(code, 1, out)
        self.assertIn(sha[:7] + " " + wide + ":1: home-directory path /Users/***", out)
        self.assertIn(sha[:7] + " latin1.txt:1: e-mail address ***@gmail.com", out)
        self.assertIn(sha[:7] + " space name.txt:1: home-directory path /home/***", out)

    def test_a_renamed_file_is_not_read_again(self):
        self.repo.commit({"old.txt": "at " + LINUX_HOME + "/x\n" + "".join("line {}\n".format(i) for i in range(20))},
                         "before the check existed")
        base = self.repo.git("rev-parse", "HEAD")
        self.repo.git("mv", "old.txt", "new.txt")
        self.repo.commit(message="rename")
        code, out = self.check("--base", base)
        self.assertEqual(code, 0, out)

    def test_a_shallow_clone_is_an_error(self):
        self.repo.commit({"a.txt": "a\n"}, "one")
        self.repo.commit({"a.txt": "b\n"}, "two")
        shallow = os.path.join(self.tmp, "shallow")
        self.repo.git("clone", "-q", "--depth", "1", "file://" + self.repo.path, shallow)
        proc = subprocess.run([sys.executable, SCRIPT, "--repo", shallow, "--base", "HEAD"], env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("fetch-depth: 0", proc.stderr.decode("utf-8"))

    def test_an_unknown_revision_is_an_error(self):
        self.repo.commit({"a.txt": "a\n"}, "one")
        code, out = self.check("--base", "no-such-branch")
        self.assertEqual(code, 2, out)
        self.assertIn("unknown revision 'no-such-branch'", out)


@unittest.skipUnless(shutil.which("git"), "needs git (RHEL 8 images may not have it; CI installs it)")
class GitHubEvents(RepoCase):
    def history(self):
        """main: a (personal address) - b; feature from b: c - d. origin/main is main."""
        self.a = self.repo.commit({"a.txt": "mail " + PERSONAL + "\n"}, "before the check existed")
        self.b = self.repo.commit({"b.txt": "b\n"}, "b")
        self.repo.git("update-ref", "refs/remotes/origin/main", self.b)
        self.repo.git("checkout", "-q", "-b", "feature")
        self.c = self.repo.commit({"c.txt": "c\n"}, "c")
        self.d = self.repo.commit({"d.txt": "d\n"}, "d")

    def test_push_checks_before_to_after(self):
        self.history()
        code, out = self.github("push", {"ref": "refs/heads/feature", "before": self.c, "after": self.d,
                                         "repository": {"default_branch": "main"}})
        self.assertEqual(code, 0, out)
        self.assertIn("1 commit(s) to check (push {}..{})".format(self.c[:7], self.d[:7]), out)
        self.assertNotIn("warning", out)

    def test_a_new_branch_is_checked_from_where_it_left_the_default_branch(self):
        self.history()
        code, out = self.github("push", {"ref": "refs/heads/feature", "before": ZERO, "after": self.d,
                                         "repository": {"default_branch": "main"}})
        self.assertEqual(code, 0, out)
        self.assertIn("warning: this push creates refs/heads/feature", out)
        self.assertIn("2 commit(s) to check (push {}..{})".format(self.b[:7], self.d[:7]), out)

    def test_an_event_without_repository_details_assumes_main(self):
        self.history()
        code, out = self.github("push", {"ref": "refs/heads/feature", "before": ZERO, "after": self.d})
        self.assertEqual(code, 0, out)
        self.assertIn("since it left origin/main", out)

    def test_a_force_push_whose_old_tip_is_gone_falls_back_loudly(self):
        self.history()
        code, out = self.github("push", {"ref": "refs/heads/feature", "before": "1" * 40, "after": self.d,
                                         "repository": {"default_branch": "main"}})
        self.assertEqual(code, 0, out)
        self.assertIn("warning: the commit before this push (1111111) is not in the clone", out)
        self.assertIn("2 commit(s) to check", out)

    def test_a_push_to_the_default_branch_without_its_old_tip_checks_all_history(self):
        self.history()
        self.repo.git("checkout", "-q", "main")
        code, out = self.github("push", {"ref": "refs/heads/main", "before": "1" * 40, "after": self.b,
                                         "repository": {"default_branch": "main"}})
        self.assertEqual(code, 1, out)
        self.assertIn("checking every commit reachable from " + self.b[:7], out)
        self.assertIn(self.a[:7] + " a.txt:1: e-mail address ***@gmail.com", out)

    def test_pull_request_checks_base_to_head(self):
        self.history()
        payload = {"pull_request": {"base": {"sha": self.b}, "head": {"sha": self.d}}}
        code, out = self.github("pull_request", payload)
        self.assertEqual(code, 0, out)
        self.assertIn("2 commit(s) to check (pull request {}..{})".format(self.b[:7], self.d[:7]), out)
        self.repo.commit({"e.txt": "at " + LINUX_HOME + "/x\n"}, "e")
        payload["pull_request"]["head"]["sha"] = self.repo.git("rev-parse", "HEAD")
        self.assertEqual(self.github("pull_request", payload)[0], 1)

    def test_a_deleted_ref_has_nothing_to_check(self):
        self.history()
        code, out = self.github("push", {"ref": "refs/heads/feature", "before": self.d, "after": ZERO})
        self.assertEqual(code, 0, out)
        self.assertIn("nothing to check", out)

    def test_other_events_are_an_error(self):
        self.history()
        code, out = self.github("workflow_dispatch", {})
        self.assertEqual(code, 2, out)
        self.assertIn("not push or pull_request", out)


class OwnFiles(unittest.TestCase):
    def test_the_check_passes_its_own_files(self):
        for path in (SCRIPT, os.path.abspath(__file__), WORKFLOW):
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as handle:
                for number, line in enumerate(handle, 1):
                    self.assertEqual(ci_privacy.line_findings(line), [], "{}:{}".format(path, number))


if __name__ == "__main__":
    unittest.main()
