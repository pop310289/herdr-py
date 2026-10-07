#!/usr/bin/env python3
"""Privacy check for the commits of a push or a pull request (standard library, Python 3.6+).

This repository is public. A commit in the checked range fails when
  - its message contains "Co-Authored-By" or "Claude-Session" (any letter case, anywhere);
  - a line it adds, or a line of its message, contains a home-directory path (/Users/<name>,
    /home/<name>) or an e-mail address that is not allowed (below);
  - its author or committer e-mail is not allowed.

Allowed addresses: GitHub noreply addresses (<id>+<login>@users.noreply.github.com and
noreply@github.com), names reserved for documentation (example.com, example.net, example.org and
anything under .example, .test, .invalid or .localhost), and the ssh user "git" of a remote such
as git@github.com:owner/repo.git.

Every commit is checked on its own, so a line added by one commit and deleted by a later one still
fails: it stays in the history. A merge commit is checked for the lines it adds compared with every
parent, that is what the merge itself introduced. Binary files are not read.

usage:
  python3 scripts/ci_privacy.py --github             in GitHub Actions: the push or pull_request range
  python3 scripts/ci_privacy.py --base origin/main   the commits on HEAD that origin/main does not have
  python3 scripts/ci_privacy.py --base A --head B    the commits in A..B
  python3 scripts/ci_privacy.py                      every commit reachable from HEAD

Exit status: 0 nothing found, 1 problems found, 2 the range could not be checked.
Findings are printed masked, so the log does not repeat what it found.
"""
import argparse
import json
import os
import re
import subprocess
import sys

TRAILER_RE = re.compile(r"co-authored-by|claude-session", re.IGNORECASE)
HOME_RE = re.compile(r"/(Users|home)/([A-Za-z0-9_][A-Za-z0-9._-]*)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
HUNK_RE = re.compile(rb"^@@ -[0-9]+(?:,([0-9]+))? \+([0-9]+)(?:,([0-9]+))? @@")
ZERO_SHA_RE = re.compile(r"^0+$")
RESERVED_DOMAINS = ("example.com", "example.net", "example.org")
RESERVED_TLDS = ("example", "test", "invalid", "localhost")
MASK = "***"  # hides the whole name; keeping its first letter would itself match the home-path rule
DIFF_OPTIONS = ["-p", "-r", "-M", "-U0", "--no-commit-id", "--no-color", "--no-ext-diff", "--no-textconv",
                "--src-prefix=a/", "--dst-prefix=b/"]


class CheckError(Exception):
    """The range cannot be checked (exit status 2)."""


def say(text, stream=None):
    """Print UTF-8 whatever the locale (Python 3.6 under the C locale would print ASCII only)."""
    stream = stream or sys.stdout
    data = (text + "\n").encode("utf-8", "replace")
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        stream.write(data.decode("utf-8"))
        return
    stream.flush()
    buffer.write(data)
    buffer.flush()


def warn(text):
    """A fallback the reader must notice; GitHub shows ::warning:: lines on the run's summary page."""
    prefix = "::warning::" if os.environ.get("GITHUB_ACTIONS") == "true" else "warning: "
    say(prefix + text, sys.stderr)


def git(repo, *args):
    """Run git in repo and return its standard output as bytes."""
    proc = subprocess.run(["git", "-C", repo, "-c", "core.quotePath=false"] + list(args),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise CheckError("git {} failed: {}".format(" ".join(args), detail or "exit status {}".format(proc.returncode)))
    return proc.stdout


def resolve(repo, rev):
    """The full id of rev's commit, or None when this clone does not have it."""
    try:
        return git(repo, "rev-parse", "--verify", "--quiet", rev + "^{commit}").decode("ascii").strip() or None
    except CheckError:
        return None


def email_allowed(address):
    local, _, domain = address.rpartition("@")
    domain = domain.lower().rstrip(".")
    if domain == "users.noreply.github.com" or address.lower() == "noreply@github.com":
        return True
    if local == "git":  # the ssh user of a remote such as git@github.com:owner/repo.git
        return True
    if any(domain == d or domain.endswith("." + d) for d in RESERVED_DOMAINS):
        return True
    return domain.rsplit(".", 1)[-1] in RESERVED_TLDS


def line_findings(text):
    """What a single added line must not contain, masked for printing."""
    found = []
    for match in HOME_RE.finditer(text):
        found.append("home-directory path /{}/{}".format(match.group(1), MASK))
    for match in EMAIL_RE.finditer(text):
        address = match.group(0)
        if not email_allowed(address):
            found.append("e-mail address {}@{}".format(MASK, address.rpartition("@")[2]))
    return found


def message_findings(message):
    """The trailers, plus the same home paths and e-mail addresses as in added lines."""
    found = []
    for number, line in enumerate(message.split("\n"), 1):
        items = ['contains "{}"'.format(match.group(0)) for match in TRAILER_RE.finditer(line)]
        found += ["message line {}: {}".format(number, item) for item in items + line_findings(line)]
    return found


def identity_findings(role, identity):
    """identity is a raw "Name <e-mail> time zone" header value."""
    match = re.search(r"<([^>]*)>", identity)
    address = match.group(1).strip() if match else ""
    if not address or email_allowed(address):
        return []
    _, at, domain = address.rpartition("@")
    shown = "{}@{}".format(MASK, domain) if at else MASK
    return ["{} e-mail {} is not a GitHub noreply address".format(role, shown)]


def added_lines(diff):
    """Yield (path, line number, text) for every line a unified diff (bytes) adds.

    Hunk lengths are counted, so an added line that itself starts with "++" is not taken for a file header.
    """
    path, old_left, new_left, number = "", 0, 0, 0
    for raw in diff.split(b"\n"):
        if old_left > 0 or new_left > 0:
            if raw.startswith(b"+"):
                yield path, number, raw[1:].decode("utf-8", "replace")
                number += 1
                new_left -= 1
            elif raw.startswith(b"-"):
                old_left -= 1
            elif raw.startswith(b" "):
                old_left -= 1
                new_left -= 1
                number += 1
            elif not raw.startswith(b"\\"):  # "\ No newline at end of file" belongs to the hunk
                old_left = new_left = 0
            continue
        if raw.startswith(b"+++ "):
            name = raw[4:].decode("utf-8", "replace")
            if name.endswith("\t"):  # git ends the name with a tab when it contains a space
                name = name[:-1]
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1]
            path = name[2:] if name.startswith("b/") else name
        elif raw.startswith(b"@@ "):
            hunk = HUNK_RE.match(raw)
            if hunk:
                old_left = int(hunk.group(1)) if hunk.group(1) is not None else 1
                number = int(hunk.group(2))
                new_left = int(hunk.group(3)) if hunk.group(3) is not None else 1


def read_commit(repo, sha):
    """(parents, author, committer, message) from the raw commit object."""
    raw = git(repo, "cat-file", "commit", sha)
    header, _, message = raw.partition(b"\n\n")
    parents, people = [], {}
    for line in header.split(b"\n"):
        key, _, value = line.partition(b" ")
        if key == b"parent":
            parents.append(value.decode("ascii"))
        elif key in (b"author", b"committer"):
            people[key.decode("ascii")] = value.decode("utf-8", "replace")
    return parents, people.get("author", ""), people.get("committer", ""), message.decode("utf-8", "replace")


def commit_added_lines(repo, sha, parents):
    """The lines the commit adds; for a merge, only the lines new compared with every parent."""
    if len(parents) < 2:
        return list(added_lines(git(repo, "diff-tree", "--root", *(DIFF_OPTIONS + [sha]))))
    common = None
    for parent in parents:
        lines = set(added_lines(git(repo, "diff-tree", *(DIFF_OPTIONS + [parent, sha]))))
        common = lines if common is None else common & lines
    return sorted(common)


def check_commit(repo, sha):
    parents, author, committer, message = read_commit(repo, sha)
    found = message_findings(message)
    found += identity_findings("author", author) + identity_findings("committer", committer)
    for path, number, text in commit_added_lines(repo, sha, parents):
        found += ["{}:{}: {}".format(path, number, item) for item in line_findings(text)]
    return found


def commits_in_range(repo, base, head):
    """Oldest first; base None means every commit reachable from head."""
    args = ["rev-list", "--reverse", "--topo-order", head] + (["^" + base] if base else [])
    return git(repo, *args).decode("ascii").split()


def merge_base(repo, a, b):
    try:
        return git(repo, "merge-base", a, b).decode("ascii").strip() or None
    except CheckError:
        return None  # no common history


def github_range(repo, event_name, event):
    """(base, head, description) for a push or pull_request event; head None means nothing to check."""
    if event_name == "pull_request":
        pr = event.get("pull_request") or {}
        base, head = (pr.get("base") or {}).get("sha"), (pr.get("head") or {}).get("sha")
        if not base or not head:
            raise CheckError("the pull_request event has no base or head sha")
        return base, head, "pull request {}..{}".format(base[:7], head[:7])
    if event_name != "push":
        raise CheckError("event {!r} is not push or pull_request; run with --base and --head".format(event_name))
    before, after = event.get("before") or "", event.get("after") or ""
    if not after or ZERO_SHA_RE.match(after):
        return None, None, "deleted ref"
    if before and not ZERO_SHA_RE.match(before) and resolve(repo, before):
        return before, after, "push {}..{}".format(before[:7], after[:7])
    default = (event.get("repository") or {}).get("default_branch") or "main"
    default_ref = "refs/remotes/origin/" + default
    if before and not ZERO_SHA_RE.match(before):
        reason = "the commit before this push ({}) is not in the clone, a force push?".format(before[:7])
    else:
        reason = "this push creates {}".format(event.get("ref") or "a new ref")
    if event.get("ref") == "refs/heads/" + default or not resolve(repo, default_ref):
        warn("{}; checking every commit reachable from {}".format(reason, after[:7]))
        return None, after, "push, all history of {}".format(after[:7])
    base = merge_base(repo, default_ref, after)
    if base is None:
        warn("{}; no history in common with origin/{}, checking every commit reachable from {}".format(
            reason, default, after[:7]))
        return None, after, "push, all history of {}".format(after[:7])
    warn("{}; checking the commits since it left origin/{} ({})".format(reason, default, base[:7]))
    return base, after, "push {}..{}".format(base[:7], after[:7])


def run(repo, base, head, description):
    """Check the range; print findings; return the exit status."""
    if git(repo, "rev-parse", "--is-shallow-repository").decode("ascii").strip() == "true":
        raise CheckError("the clone is shallow, so the range may be incomplete: check out with fetch-depth: 0")
    if head is None:
        say("ci_privacy: nothing to check ({})".format(description))
        return 0
    head_sha = resolve(repo, head)
    if head_sha is None:
        raise CheckError("unknown revision {!r} (in CI: is the checkout complete, fetch-depth: 0?)".format(head))
    base_sha = None
    if base:
        base_sha = resolve(repo, base)
        if base_sha is None:
            raise CheckError("unknown revision {!r} (in CI: is the checkout complete, fetch-depth: 0?)".format(base))
    commits = commits_in_range(repo, base_sha, head_sha)
    say("ci_privacy: {} commit(s) to check ({})".format(len(commits), description))
    bad = 0
    total = 0
    for sha in commits:
        found = check_commit(repo, sha)
        if found:
            bad += 1
            total += len(found)
        for item in found:
            say("{} {}".format(sha[:7], item))
    if total:
        say("ci_privacy: {} problem(s) in {} of {} commit(s). This repository is public: rewrite those commits "
            "(for example git rebase -i, then reword or edit) and push again.".format(total, bad, len(commits)))
        return 1
    say("ci_privacy: ok")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=".", help="the repository to check (default: the current folder)")
    parser.add_argument("--github", action="store_true",
                        help="take the range from GITHUB_EVENT_NAME and GITHUB_EVENT_PATH (push or pull_request)")
    parser.add_argument("--base", help="skip the commits reachable from BASE (default: check all history)")
    parser.add_argument("--head", default="HEAD", help="the newest commit to check (default: HEAD)")
    args = parser.parse_args(argv)
    try:
        if args.github:
            if args.base:
                parser.error("--github takes the range from the event; leave out --base")
            event_name, event_path = os.environ.get("GITHUB_EVENT_NAME"), os.environ.get("GITHUB_EVENT_PATH")
            if not event_name or not event_path:
                raise CheckError("--github needs GITHUB_EVENT_NAME and GITHUB_EVENT_PATH (set by GitHub Actions)")
            with open(event_path, encoding="utf-8") as handle:
                event = json.load(handle)
            base, head, description = github_range(args.repo, event_name, event)
        else:
            base, head = args.base, args.head
            description = "{}..{}".format(base, head) if base else "all history of {}".format(head)
        return run(args.repo, base, head, description)
    except CheckError as exc:
        say("ci_privacy: error: {}".format(exc), sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
