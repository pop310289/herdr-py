"""herdr_py/sandbox.py and the example judges that use it: the command line it builds (no network, a read-only root, no
capabilities, no privileges, this machine's user, only the work folder), sandbox none, a container engine that is
missing or cannot see the folder, and, where docker answers, what a command inside can and cannot do. The image is
$HERDR_PY_TEST_IMAGE (default python:3-slim)."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from herdr_py.sandbox import HARDENING, INSIDE, Sandbox, SandboxError  # noqa: E402

IMAGE = os.environ.get("HERDR_PY_TEST_IMAGE", "python:3-slim")
DAG_JUDGE = os.path.join(ROOT, "examples", "dag", "judge.py")


def docker_answers():
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


DOCKER = docker_answers()


def home_folder():
    """A folder under $HOME: colima shares only that with its VM."""
    base = os.path.join(os.path.expanduser("~"), ".cache", "herdr-py", "test")
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(dir=base)


def fake_engine(folder, script):
    """A `docker` on PATH that runs this shell script instead (and nothing else from PATH but the basics)."""
    path = os.path.join(folder, "docker")
    with open(path, "w") as handle:
        handle.write("#!/bin/sh\n" + script)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return folder


class CommandLineTest(unittest.TestCase):
    def test_specs(self):
        self.assertTrue(Sandbox.parse("none").on_host)
        box = Sandbox.parse("docker:registry.example/py:3.12-slim")
        self.assertEqual((box.engine, box.image), ("docker", "registry.example/py:3.12-slim"))
        self.assertEqual(Sandbox.parse(" podman:img ").engine, "podman")
        for spec in ("", "docker", "docker:", "lxc:img", "None", "sh:img"):
            with self.assertRaises(SandboxError, msg=spec):
                Sandbox.parse(spec)

    def test_the_container_command(self):
        argv = Sandbox.parse("docker:img:1").argv(["python3", "-c", "1"], "/w/copy", cwd="a/b", env={"X": "1"},
                                                  path_env={"PYTHONPATH": "."}, name="n")
        self.assertEqual(argv[:5], ["docker", "run", "--rm", "--name", "n"])
        joined = " ".join(argv)
        self.assertIn(" ".join(HARDENING), joined)
        for flag in ("--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges",
                     f"--user {os.getuid()}:{os.getgid()}", "-v /w/copy:/work", "-w /work/a/b", "-e PYTHONPATH=/work",
                     "-e X=1", "-e HOME=/tmp"):
            self.assertIn(flag, joined)
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "-v"], ["/w/copy:/work"])  # only the copy
        self.assertEqual(argv[-4:], ["img:1", "python3", "-c", "1"])

    def test_podman_keeps_the_users_id(self):
        argv = Sandbox.parse("podman:img").argv(["true"], "/w")
        self.assertIn("--userns=keep-id", argv)
        self.assertNotIn("--user", argv)

    def test_the_command_stays_in_the_work_folder(self):
        box = Sandbox.parse("docker:img")
        self.assertIn("-w " + INSIDE, " ".join(box.argv(["true"], "/w", cwd="a/..")))
        for cwd in ("..", "../x", "a/../../b", "/etc", "../work2"):
            with self.assertRaises(SandboxError, msg=cwd):
                box.argv(["true"], "/w", cwd=cwd)

    def test_none_runs_here_in_the_work_folder(self):
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        os.makedirs(os.path.join(work, "sub"))
        box = Sandbox.parse("none")
        box.check(work)  # nothing to check
        code, out = box.run(["sh", "-c", "pwd; echo $P; echo $Q"], work, 30, cwd="sub", env={"Q": "q"}, path_env={"P": "."})
        self.assertEqual(code, 0)
        where, p, q = out.split()
        self.assertEqual(os.path.realpath(where), os.path.realpath(os.path.join(work, "sub")))
        self.assertEqual((p, q), (work, "q"))
        start = time.time()
        self.assertEqual(box.run(["sleep", "5"], work, 1)[0], None)
        self.assertLess(time.time() - start, 4)

    def test_no_engine_is_an_error_that_says_what_to_do(self):
        work, empty = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        self.addCleanup(shutil.rmtree, empty, True)
        with mock.patch.dict(os.environ, {"PATH": empty}):
            with self.assertRaises(SandboxError) as caught:
                Sandbox.parse("docker:img").check(work)
        self.assertIn("docker is not installed", str(caught.exception))
        self.assertIn("none", str(caught.exception))

    def test_a_folder_the_container_cannot_see_is_refused(self):
        work, bin_dir = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        self.addCleanup(shutil.rmtree, bin_dir, True)
        # colima mounts a folder outside $HOME as an empty one, and docker run still succeeds
        fake_engine(bin_dir, 'echo "cat: .herdr-sandbox-probe: No such file or directory"; exit 1\n')
        with mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + os.environ.get("PATH", "")}):
            with self.assertRaises(SandboxError) as caught:
                Sandbox.parse("docker:img").check(work)
        self.assertIn("cannot run img with %s mounted" % work, str(caught.exception))
        self.assertEqual(os.listdir(work), [])  # the probe file is gone
        fake_engine(bin_dir, 'echo something else\n')  # exit 0 but not the probe's contents: refused as well
        with mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + os.environ.get("PATH", "")}):
            with self.assertRaises(SandboxError):
                Sandbox.parse("docker:img").check(work)


@unittest.skipUnless(DOCKER, "docker does not answer here")
class DockerTest(unittest.TestCase):
    def setUp(self):
        self.work = home_folder()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.box = Sandbox.parse("docker:" + IMAGE)
        self.box.check(self.work)

    def run_in(self, script, timeout=120):
        return self.box.run(["sh", "-c", script], self.work, timeout)

    def test_what_the_code_inside_can_and_cannot_do(self):
        self.assertEqual(self.run_in("id -u")[1].strip(), str(os.getuid()))
        code, out = self.box.run(["python3", "-c", "import socket; socket.create_connection(('1.1.1.1', 53), timeout=5)"],
                                 self.work, 120)
        self.assertNotEqual(code, 0, out)  # no network
        code, out = self.run_in("touch /evil")
        self.assertNotEqual(code, 0, out)  # the root is read-only
        self.assertIn("Read-only file system", out)
        self.assertEqual(self.run_in("grep -c . /proc/self/status >/dev/null; grep CapEff /proc/self/status")[1].split()[-1],
                         "0000000000000000")  # no capabilities
        self.assertEqual(self.run_in("grep NoNewPrivs /proc/self/status")[1].split()[-1], "1")
        self.assertEqual(self.run_in("echo made > made.txt && cat made.txt"), (0, "made\n"))
        with open(os.path.join(self.work, "made.txt")) as handle:
            self.assertEqual(handle.read(), "made\n")  # the work folder is the copy the judge reads

    def test_a_command_that_runs_too_long_is_stopped_with_its_container(self):
        start = time.time()
        self.assertEqual(self.run_in("sleep 120", timeout=3)[0], None)
        self.assertLess(time.time() - start, 30)
        left = subprocess.run(["docker", "ps", "-q", "--filter", "name=herdr-sandbox-%d-" % os.getpid()],
                              stdout=subprocess.PIPE, universal_newlines=True).stdout.split()
        self.assertEqual(left, [])


class DagJudgeTest(unittest.TestCase):
    """examples/dag/judge.py: a step's tests are a member's code, so they run in the sandbox."""

    def setUp(self):
        self.step = home_folder()
        self.addCleanup(shutil.rmtree, self.step, True)
        self.marker = os.path.join(tempfile.mkdtemp(), "ran-here")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.marker), True)
        with open(os.path.join(self.step, "test_step.py"), "w") as handle:
            handle.write("import unittest\nopen(%r, 'w').close()  # where this runs shows here\n\n"
                         "class T(unittest.TestCase):\n    def test_ok(self):\n        pass\n" % self.marker)

    def judge(self, **env):
        p = subprocess.run([sys.executable, DAG_JUDGE, "reply.txt"], cwd=self.step, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, timeout=300,
                           env=dict(os.environ, HERDR_DAG_NODE="mul", PYTHONDONTWRITEBYTECODE="1", **env))
        return p.returncode, (json.loads(p.stdout) if p.returncode == 0 else None), p.stderr

    def test_no_sandbox_is_a_judge_error_and_the_tests_do_not_run_here(self):
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, True)
        code, verdict, err = self.judge(PATH=empty, HERDR_JUDGE_SANDBOX="docker:" + IMAGE)
        self.assertEqual(code, 2)
        self.assertIn("docker is not installed", err)
        self.assertFalse(os.path.exists(self.marker))

    def test_by_default_it_wants_docker(self):
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, True)
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("HERDR_JUDGE_SANDBOX", None)
            code, verdict, err = self.judge(PATH=empty)
        self.assertEqual(code, 2)
        self.assertIn("docker is not installed", err)
        self.assertFalse(os.path.exists(self.marker))

    def test_none_runs_here_and_says_so(self):
        code, verdict, err = self.judge(HERDR_JUDGE_SANDBOX="none")
        self.assertEqual((code, verdict["status"], verdict["score"]), (0, "valid", 1), err)
        self.assertTrue(verdict["detail"].startswith("[no sandbox] Ran 1 test"))
        self.assertTrue(os.path.exists(self.marker))

    @unittest.skipUnless(DOCKER, "docker does not answer here")
    def test_in_docker_the_tests_run_inside_and_pass(self):
        code, verdict, err = self.judge(HERDR_JUDGE_SANDBOX="docker:" + IMAGE)
        self.assertEqual(code, 0, err)
        self.assertEqual(verdict["status"], "invalid")  # the marker's folder is not there inside: the test cannot write it
        self.assertFalse(os.path.exists(self.marker))
        os.remove(os.path.join(self.step, "test_step.py"))
        with open(os.path.join(self.step, "test_fine.py"), "w") as handle:
            handle.write("import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n")
        code, verdict, err = self.judge(HERDR_JUDGE_SANDBOX="docker:" + IMAGE)
        self.assertEqual((code, verdict["status"]), (0, "valid"), err)
        self.assertTrue(verdict["detail"].startswith("Ran 1 test"), verdict["detail"])


if __name__ == "__main__":
    unittest.main()
