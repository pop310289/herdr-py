"""Permission policy: ordered rules, first match wins.

A rule matches on the permission type (`bash`, `edit`, `external_directory`, ... or `*`), optionally a regular
expression that must match the whole target (the shell command for `bash`, otherwise the requested patterns joined
by spaces), and optionally an agent name (shell-style glob). Actions:

  allow   reply "once"
  always  reply "always" (OpenCode stops asking for this pattern in this session)
  deny    reply "reject" with a message the agent can read
  ask     leave it pending for a human (TUI, web UI or CLI)

Example policy file (TOML):

    default = "ask"
    [[rule]]
    permission = "bash"
    match = '(python3 [\\w./-]+\\.py( [\\w./-]+)*|ls( -\\w+)*( [\\w./-]+)*)'
    action = "allow"
    [[rule]]
    permission = "external_directory"
    action = "deny"
    message = "Stay inside the project folder."
"""
import fnmatch
import re

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

ACTIONS = {"allow", "always", "deny", "ask"}
DEFAULT_MESSAGE = "Not allowed by the herdr-py policy."


class PolicyError(ValueError):
    pass


def target_of(request):
    """What a rule's `match` is compared with: the command for bash, otherwise the requested patterns."""
    meta = request.get("metadata") or {}
    if request.get("permission") == "bash" and meta.get("command"):
        return str(meta["command"]).strip()
    return " ".join(str(p) for p in request.get("patterns") or []).strip()


def describe(request):
    """Human-readable request: type + target. The command alone misleads: an `ls` run outside the project
    folder arrives as permission=external_directory with metadata.command='ls'."""
    perm, meta = request.get("permission"), request.get("metadata") or {}
    if perm == "bash":
        return f"run {target_of(request)}"
    if perm == "external_directory":
        via = f" (via {meta['command']})" if meta.get("command") else ""
        return f"access outside the project: {target_of(request)}{via}"
    return f"{perm} {target_of(request)}".strip()


class Policy:
    def __init__(self, rules=(), default="ask"):
        if default not in ACTIONS:
            raise PolicyError(f"default action must be one of {sorted(ACTIONS)}, got {default!r}")
        self.default = default
        self.rules = []
        for i, rule in enumerate(rules):
            action = rule.get("action")
            if action not in ACTIONS:
                raise PolicyError(f"rule {i + 1}: action must be one of {sorted(ACTIONS)}, got {action!r}")
            try:
                pattern = re.compile(rule["match"]) if rule.get("match") else None
            except re.error as exc:
                raise PolicyError(f"rule {i + 1}: bad regular expression: {exc}") from None
            self.rules.append({"permission": rule.get("permission", "*"), "match": pattern, "agent": rule.get("agent"),
                               "action": action, "message": rule.get("message") or DEFAULT_MESSAGE, "index": i + 1})

    @classmethod
    def load(cls, path):
        data = load_config(path)
        return cls(data.get("rule", []), data.get("default", "ask"))

    def decide(self, request, agent=None):
        """(action, message, rule_index or None) for a permission request."""
        target = target_of(request)
        for rule in self.rules:
            if rule["permission"] not in ("*", request.get("permission")):
                continue
            if rule["agent"] and not (agent and fnmatch.fnmatch(agent, rule["agent"])):
                continue
            if rule["match"] is not None and rule["match"].fullmatch(target) is None:
                continue
            return rule["action"], rule["message"], rule["index"]
        return self.default, DEFAULT_MESSAGE, None


REPLIES = {"allow": "once", "always": "always", "deny": "reject"}


def load_config(path):
    """Read a .json file on any Python 3.6+, or a .toml file on Python 3.11+ (tomllib)."""
    if path.endswith(".json"):
        import json
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    if tomllib is None:
        raise PolicyError(f"{path}: TOML needs Python 3.11+ (tomllib); on older Pythons (RHEL 8's 3.6) use the .json form")
    with open(path, "rb") as handle:
        return tomllib.load(handle)
