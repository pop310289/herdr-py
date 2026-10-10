"""Make one bash script that writes every file of a commit of this repository: for a machine that cannot clone it and
takes files one at a time. Copy the script there and run it; it ends by checking every file's sha256, so a copy that
changed on the way (a paste, a mail) says which files differ. Standard library only, Python 3.6+.

    python3 scripts/oneshot.py                    # HEAD: herdr-py-<commit>-packed.sh and herdr-py-<commit>-files.sh
    python3 scripts/oneshot.py --rev v1.0 --kind packed --out /tmp

On the other machine:

    bash herdr-py-<commit>-packed.sh [FOLDER]     # default FOLDER: ./herdr-py (it must not exist yet)
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
MARK = "HERDR_PY_EOF"
LONG = 1000  # bytes: a terminal takes 4096 at most on one line; some take fewer


def git(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), stdout=subprocess.PIPE, check=True).stdout


def members(repo, rev):
    """(the commit as a tar, [(path, bytes, mode)] of its files, sorted)."""
    raw = git(repo, "archive", "--format=tar", rev)
    out = []
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        for m in tar.getmembers():
            if m.isfile():
                out.append((m.name, tar.extractfile(m).read(), m.mode & 0o777))
            elif not m.isdir():
                raise SystemExit("not a file or a folder: " + m.name)
    return raw, sorted(out)


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


def head(commit, how, needs):
    return """#!/bin/bash
# herdr-py {commit} (https://github.com/pop310289/herdr-py): every file of the repository, {how}.
# usage: bash this-script [FOLDER]   (default: ./herdr-py; the folder must not exist yet), then
#        cd herdr-py && ./scripts/install.sh
# Needs {needs}. Ends by checking every file's sha256.
set -eu
dest="${{1:-herdr-py}}"
if [ -e "$dest" ]; then echo "$dest already exists" >&2; exit 1; fi
mkdir -p "$dest"
cd "$dest"
""".format(commit=commit, how=how, needs=needs)


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


def files_script(commit, files):
    parts = [head(commit, "written with mkdir and cat", "bash, mkdir, cat, base64, chmod, mktemp, grep and sha256sum (or shasum)")]
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


def packed_script(commit, raw, files):
    gz = io.BytesIO()
    with gzip.GzipFile(fileobj=gz, mode="wb", compresslevel=9, mtime=0) as handle:
        handle.write(raw)
    return (head(commit, "packed as a tar.gz in base64", "bash, base64, tar, gzip, mktemp, grep and sha256sum (or shasum)")
            + "base64 -d <<'{}' | tar -xzf -\n{}{}\n".format(MARK, b64_lines(gz.getvalue()), MARK) + tail(files))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rev", default="HEAD", help="the commit (default HEAD)")
    ap.add_argument("--repo", default=ROOT, help="the repository (default: the one this script is in)")
    ap.add_argument("--out", default=".", help="the folder the scripts are written to (default: here)")
    ap.add_argument("--kind", choices=("packed", "files", "both"), default="both")
    a = ap.parse_args(argv)
    commit = git(a.repo, "rev-parse", "--short", a.rev).decode("ascii").strip()
    raw, files = members(a.repo, a.rev)
    made = []
    for kind in ("packed", "files"):
        if a.kind not in (kind, "both"):
            continue
        script = packed_script(commit, raw, files) if kind == "packed" else files_script(commit, files)
        path = os.path.join(a.out, "herdr-py-{}-{}.sh".format(commit, kind))
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(script)
        os.chmod(path, 0o755)
        made.append(path)
        print("{}: {} files, {:,} bytes, {:,} lines".format(path, len(files), len(script.encode("utf-8")), script.count("\n")))
    return 0 if made else 1


if __name__ == "__main__":
    sys.exit(main())
