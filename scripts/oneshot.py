"""Make one bash script that writes every file of this repository: for a machine that cannot clone it and takes files
one at a time. Copy the script there and run it; it ends by checking every file's sha256, so a copy that changed on the
way (a paste, a mail) says which files differ. Standard library only, Python 3.6+.

The repository keeps one at its top, herdr-py-oneshot.sh (the files kind below): every file of the commit it is in,
except itself. Whoever changes a file runs --update before committing; --check (and the tests) fail while it is behind.

    python3 scripts/oneshot.py --update           # rewrite herdr-py-oneshot.sh from the files as they are now
    python3 scripts/oneshot.py --check            # exit 1 when herdr-py-oneshot.sh is not what --update would write
    python3 scripts/oneshot.py                    # a commit (HEAD): herdr-py-<commit>-packed.sh and herdr-py-<commit>-files.sh
    python3 scripts/oneshot.py --rev v1.0 --kind packed --out /tmp

On the other machine:

    bash herdr-py-oneshot.sh [FOLDER]             # default FOLDER: ./herdr-py (it must not exist yet)
    cd herdr-py && ./scripts/install.sh

Two kinds:
- packed: the files as a tar.gz in base64, about 40% of the files' size; needs bash, base64, tar, gzip, mktemp, grep
  and sha256sum (or shasum).
- files: a mkdir for the folders and a quoted here-document (cat > path <<'MARK') for each text file, readable as it
  is; a file a here-document cannot carry exactly (binary, no final newline, carriage returns, a line as long as
  LONG or a line equal to the mark) goes as base64; executables get chmod 755. Needs no tar or gzip.
No line of either script is LONG bytes or more, so a paste into a terminal does not cut one."""
import argparse
import base64
import gzip
import hashlib
import io
import os
import shlex
import subprocess
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ONESHOT = "herdr-py-oneshot.sh"  # the one the repository keeps at its top; never inside another
MARK = "HERDR_PY_EOF"
LONG = 1000  # bytes: a terminal takes 4096 at most on one line; some take fewer


def git(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), stdout=subprocess.PIPE, check=True).stdout


def members(repo, rev):
    """[(path, bytes, mode)] of the files of a commit, sorted (the repository's own one-shot script left out)."""
    raw = git(repo, "archive", "--format=tar", rev)
    out = []
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        for m in tar.getmembers():
            if m.isfile():
                if m.name != ONESHOT:
                    out.append((m.name, tar.extractfile(m).read(), m.mode & 0o777))
            elif not m.isdir():
                raise SystemExit("not a file or a folder: " + m.name)
    return sorted(out)


def checkout_members(repo):
    """[(path, bytes, mode)] of the files as they are now: the tracked ones and the new ones git does not ignore (what
    `git add -A` would commit), read from the folder, sorted (the one-shot script left out)."""
    out = []
    for name in git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard").decode("utf-8").split("\0"):
        path = os.path.join(repo, name)
        if not name or name == ONESHOT or os.path.islink(path) or not os.path.isfile(path):
            continue  # a file deleted since it was added is not there to write
        with open(path, "rb") as handle:
            data = handle.read()
        out.append((name, data, 0o755 if os.stat(path).st_mode & 0o100 else 0o644))
    return sorted(set(out))


def tar_of(files):
    """The files as a tar, the same bytes for the same files (no times, owners or folders: tar makes the folders)."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, data, mode in files:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), mode, 0
            tar.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def as_text(data):
    """The text of a file a quoted here-document carries exactly, or None."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text.endswith("\n") or any(c in text for c in "\r\x00\x0b\x0c\x1b"):
        return None
    lines = text.split("\n")
    if any(line == MARK or len(line.encode("utf-8")) >= LONG for line in lines):
        return None
    return text


def head(which, how, needs):
    return """#!/bin/bash
# {which}, {how} (https://github.com/pop310289/herdr-py).
# usage: bash this-script [FOLDER]   (default: ./herdr-py; the folder must not exist yet), then
#        cd herdr-py && ./scripts/install.sh
# Needs {needs}. Ends by checking every file's sha256.
set -eu
dest="${{1:-herdr-py}}"
if [ -e "$dest" ]; then echo "$dest already exists" >&2; exit 1; fi
mkdir -p "$dest"
cd "$dest"
""".format(which=which, how=how, needs=needs)


def tail(files):
    sums = "".join("{}  {}\n".format(hashlib.sha256(data).hexdigest(), name) for name, data, _ in files)
    return """sums="$(mktemp)"
cat > "$sums" <<'{mark}'
{sums}{mark}
if command -v sha256sum >/dev/null 2>&1; then check="sha256sum -c"; else check="shasum -a 256 -c"; fi
if $check "$sums" > "$sums.out" 2>/dev/null; then
  rm -f "$sums" "$sums.out"
  echo "herdr-py: {n} files written to $(pwd); every file's sha256 matches the original"
else
  grep -v ': OK$' "$sums.out" >&2 || echo "herdr-py: the sha256 check could not run" >&2
  rm -f "$sums" "$sums.out"
  echo "herdr-py: the files above differ from the original: this script changed on the way" >&2
  exit 1
fi
""".format(mark=MARK, sums=sums, n=len(files))


def b64_lines(data):
    return base64.encodebytes(data).decode("ascii")  # 76 characters a line


def files_script(which, files):
    parts = [head(which, "written with mkdir and cat", "bash, mkdir, cat, base64, chmod, mktemp, grep and sha256sum (or shasum)")]
    line = "mkdir -p"
    for folder in sorted({os.path.dirname(name) for name, _, _ in files} - {""}):
        word = " " + shlex.quote(folder)
        if len(line) + len(word) >= LONG:
            parts.append(line + "\n")
            line = "mkdir -p"
        line += word
    if line != "mkdir -p":
        parts.append(line + "\n")
    for name, data, mode in files:
        q = shlex.quote(name)
        text = as_text(data)
        if not data:
            parts.append(": > {}\n".format(q))
        elif text is not None:
            parts.append("cat > {} <<'{}'\n{}{}\n".format(q, MARK, text, MARK))
        else:
            parts.append("base64 -d > {} <<'{}'\n{}{}\n".format(q, MARK, b64_lines(data), MARK))
        if mode & 0o111:
            parts.append("chmod 755 {}\n".format(q))
    parts.append(tail(files))
    return "".join(parts)


def packed_script(which, files):
    gz = io.BytesIO()
    with gzip.GzipFile(fileobj=gz, mode="wb", compresslevel=9, mtime=0) as handle:
        handle.write(tar_of(files))
    return (head(which, "packed as a tar.gz in base64", "bash, base64, tar, gzip, mktemp, grep and sha256sum (or shasum)")
            + "base64 -d <<'{}' | tar -xzf -\n{}{}\n".format(MARK, b64_lines(gz.getvalue()), MARK) + tail(files))


def in_repository(repo):
    """What herdr-py-oneshot.sh holds when it is up to date: the files as they are now, but itself."""
    return files_script("herdr-py: every file of the commit this script is in, but itself", checkout_members(repo))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--update", action="store_true", help="rewrite the repository's " + ONESHOT)
    ap.add_argument("--check", action="store_true", help="exit 1 when the repository's " + ONESHOT + " is behind")
    ap.add_argument("--rev", default="HEAD", help="the commit (default HEAD)")
    ap.add_argument("--repo", default=ROOT, help="the repository (default: the one this script is in)")
    ap.add_argument("--out", default=".", help="the folder the scripts are written to (default: here)")
    ap.add_argument("--kind", choices=("packed", "files", "both"), default="both")
    a = ap.parse_args(argv)
    if a.update or a.check:
        script, path = in_repository(a.repo), os.path.join(a.repo, ONESHOT)
        if a.check:
            try:
                with open(path, encoding="utf-8", newline="") as handle:
                    same = handle.read() == script
            except OSError:
                same = False
            if not same:
                print("{} is not up to date with the files: run python3 scripts/oneshot.py --update".format(ONESHOT), file=sys.stderr)
                return 1
            print("{} is up to date".format(ONESHOT))
            return 0
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(script)
        os.chmod(path, 0o755)
        print("{}: {:,} bytes, {:,} lines".format(path, len(script.encode("utf-8")), script.count("\n")))
        return 0
    commit = git(a.repo, "rev-parse", "--short", a.rev).decode("ascii").strip()
    files = members(a.repo, a.rev)
    made = []
    for kind in ("packed", "files"):
        if a.kind not in (kind, "both"):
            continue
        which = "herdr-py {}: every file of that commit".format(commit)
        script = packed_script(which, files) if kind == "packed" else files_script(which, files)
        path = os.path.join(a.out, "herdr-py-{}-{}.sh".format(commit, kind))
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(script)
        os.chmod(path, 0o755)
        made.append(path)
        print("{}: {} files, {:,} bytes, {:,} lines".format(path, len(files), len(script.encode("utf-8")), script.count("\n")))
    return 0 if made else 1


if __name__ == "__main__":
    sys.exit(main())
