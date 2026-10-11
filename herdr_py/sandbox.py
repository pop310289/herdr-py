"""Running code that agents wrote: inside a container with no network, a read-only root, no capabilities and no way to
gain privileges, as this machine's user, with memory, CPU and process limits. Standard library only; Python 3.6+.

Judges that run a member's code use it (examples/self_improve/patch_judge.py, examples/dag/judge.py):

    box = Sandbox.parse("docker:python:3-slim")   # or "podman:IMAGE", or "none"
    box.check(work)                                 # SandboxError when it cannot run there, with what to do
    code, output = box.run(["python3", "-m", "unittest"], work, timeout=120, cwd="tests", path_env={"PYTHONPATH": "."})

`work` is the only folder the container sees (at /work, writable: judges give it a throwaway copy). check() starts one
container that looks for a file it just wrote there: with colima only folders under $HOME are shared, and a folder
from elsewhere arrives empty without an error. "none" runs the command on this machine as it is; only the person can
ask for it, and judges say so in their detail. A command that does not finish in time is killed with its container.
"""
import itertools
import os
import posixpath
import secrets
import shutil
import subprocess

ENGINES = ("docker", "podman")
INSIDE = "/work"
HARDENING = ["--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
             "--tmpfs", "/tmp:rw,exec,size=512m", "--memory", "2g", "--cpus", "2", "--pids-limit", "256"]
PROBE = ".herdr-sandbox-probe"
_count = itertools.count(1)


class SandboxError(Exception):
    """There is no sandbox to run in; the message says what to do."""


class Sandbox:
    def __init__(self, engine, image):
        self.engine, self.image = engine, image

    @classmethod
    def parse(cls, spec):
        """'docker:IMAGE', 'podman:IMAGE' or 'none'."""
        spec = (spec or "").strip()
        if spec == "none":
            return cls(None, None)
        engine, _, image = spec.partition(":")
        if engine not in ENGINES or not image:
            raise SandboxError(f"sandbox {spec!r}: give docker:IMAGE, podman:IMAGE, or none (this machine, unsafe)")
        return cls(engine, image)

    @property
    def on_host(self):
        return self.engine is None

    def describe(self):
        return "on this machine, without a sandbox" if self.on_host else f"in {self.engine} ({self.image}), no network"

    def check(self, work):
        """Raise SandboxError unless commands can run in this sandbox with `work` mounted."""
        if self.on_host:
            return
        if not shutil.which(self.engine):
            raise SandboxError(f"{self.engine} is not installed: install it, or choose sandbox none to run the members' "
                               f"code on this machine (unsafe)")
        marker = secrets.token_hex(8)
        path = os.path.join(work, PROBE)
        with open(path, "w") as handle:
            handle.write(marker)
        try:
            code, out = self.run(["cat", PROBE], work, timeout=120)
        finally:
            os.remove(path)
        if code != 0 or out.strip() != marker:
            raise SandboxError(f"{self.engine} cannot run {self.image} with {work} mounted (exit {code}: "
                               f"{' '.join(out.split())[-300:]}); with colima only folders under $HOME are shared")

    def argv(self, argv, work, cwd=".", env=None, path_env=None, name=None):
        """The command line that runs argv: in a container, or as it is for sandbox none."""
        if self.on_host:
            return list(argv)
        inside = posixpath.normpath(posixpath.join(INSIDE, cwd.replace(os.sep, "/")))
        if inside != INSIDE and not inside.startswith(INSIDE + "/"):
            raise SandboxError(f"cwd {cwd!r} is outside the work folder")
        user = ["--userns=keep-id"] if self.engine == "podman" else ["--user", f"{os.getuid()}:{os.getgid()}"]
        out = [self.engine, "run", "--rm"] + (["--name", name] if name else []) + HARDENING + user
        out += ["-e", "HOME=/tmp", "-v", f"{os.path.abspath(work)}:{INSIDE}", "-w", inside]
        for key, value in sorted((env or {}).items()):
            out += ["-e", f"{key}={value}"]
        for key, rel in sorted((path_env or {}).items()):
            out += ["-e", f"{key}={posixpath.normpath(posixpath.join(INSIDE, rel))}"]
        return out + [self.image] + list(argv)

    def run(self, argv, work, timeout, cwd=".", env=None, path_env=None):
        """(exit code, or None when it did not finish within timeout seconds; stdout and stderr together). env adds
        variables; path_env adds variables holding paths relative to `work`."""
        name = None if self.on_host else f"herdr-sandbox-{os.getpid()}-{next(_count)}-{secrets.token_hex(3)}"
        if self.on_host:
            extra = dict(env or {})
            extra.update({k: os.path.normpath(os.path.join(work, v)) for k, v in (path_env or {}).items()})
            options = {"cwd": os.path.normpath(os.path.join(work, cwd)), "env": dict(os.environ, **extra)}
        else:
            options = {}
        try:
            p = subprocess.run(self.argv(argv, work, cwd, env, path_env, name), stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, universal_newlines=True, timeout=timeout, **options)
            return p.returncode, p.stdout
        except subprocess.TimeoutExpired as exc:
            if name:  # killing the client leaves the container running: stop it by name
                subprocess.run([self.engine, "kill", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            return None, out
