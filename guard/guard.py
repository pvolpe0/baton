"""baton PreToolUse fence (slim).

The REAL containment is OS-level: each job runs as a CONFINED transient systemd service (writes
restricted to the repo + job dir + claude's own state, the rest of the filesystem read-only, no
privilege escalation, private /tmp; network intentionally OPEN so the agent can fetch/scrape/call
any host), as the dedicated unprivileged `baton` user, with a scoped non-admin PAT, no cloud
credentials, and server-side branch protection. That stack is what bounds the blast radius.

This hook only adds what the OS sandbox does NOT cover:
  - MCP tools: a read-only allowlist. The agent runs unattended, and MCP writes hit external
    systems (Linear/Notion/Gmail/…) that live OUTSIDE the sandboxed box and are often irreversible,
    so non-read MCP tools are denied by default (opt-in per project if a user wants writes).
  - Bash: a SOFT, best-effort guardrail that fails fast on a small global denied-command list
    (/opt/baton/denied.json). This is explicitly NOT a security boundary — obfuscation can evade it;
    the OS sandbox + no-cloud-creds posture are what actually contain a command. We do not try to
    statically out-parse bash (that's a losing game); we just catch the obvious cases early.

User-gated by OS uid (pwd.getpwuid(os.getuid())) — enforced only for the worker user, a transparent
pass-through for the human. Fail-closed on error."""
import json, os, pwd, re, sys


def _worker_user():
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "worker-user")
        return open(p).read().strip() or "baton"
    except Exception:
        return "baton"


WORKER_USER = _worker_user()


def enforced_for(user):
    return user == WORKER_USER


# ---- Bash: soft denied-command guardrail (NOT the security boundary) -------
def classify_bash(command, policy):
    """Fail fast if a project-denied command appears as a command head. Best-effort only — the OS
    sandbox + no-cloud-creds are the actual containment, so we keep this simple and don't chase
    obfuscation (which is why it matches the basename of each segment's first token, not arg text,
    so a denied name inside a commit message or filename doesn't trip it)."""
    denied = policy.get("denied_commands", [])
    if not denied:
        return ("allow", "ok")
    for seg in re.split(r"[;&|\n]+", command):
        toks = seg.split()
        if toks and os.path.basename(toks[0].strip("'\"\\")) in denied:
            return ("deny", f"denied command (soft guardrail): {os.path.basename(toks[0])}")
    return ("allow", "ok")


# ---- MCP: read-only allowlist (the real external-mutation guard) -----------
MCP_READ_PREFIXES = ("get_", "get-", "list_", "list-", "search_", "search-", "read_", "read-",
                     "describe_", "describe-", "filter_", "filter-", "notion-fetch", "notion-search",
                     "notion-get")
MCP_READ_EXACT = {"fetch", "search", "ping", "notion-fetch", "notion-search"}
MCP_MUTATION = {"create", "update", "delete", "save", "move", "duplicate", "purge", "remove", "set",
                "add", "insert", "write", "upload", "send", "archive", "restore", "terminate",
                "promote", "replace", "merge", "close", "cancel", "revoke", "rotate", "disable",
                "enable", "destroy", "drop", "wipe", "reset", "patch", "post", "put", "complete",
                "label", "unlabel", "import", "sync", "run", "exec", "batch", "upsert", "edit"}


def _classify_mcp(tool_name):
    op = tool_name.lower().split("__")[-1]
    words = re.split(r"[-_]", op)
    if any(w in MCP_MUTATION for w in words):             # a mutation verb anywhere -> deny (even if it
        return ("deny", f"MCP tool has a mutation verb: {tool_name}")   # starts with a read prefix)
    if op in MCP_READ_EXACT or any(op.startswith(p) for p in MCP_READ_PREFIXES):
        return ("allow", "read-shaped mcp tool")
    return ("deny", f"MCP tool not on the read allowlist: {tool_name}")


def classify(tool_name, tool_input, policy):
    if tool_name == "Bash":
        return classify_bash(tool_input.get("command", ""), policy)
    if tool_name.startswith("mcp__"):
        return _classify_mcp(tool_name)
    return ("allow", "non-bash non-mcp tool")


# The soft denied-command list is PROJECT-INDEPENDENT: one root-owned file next to the deployed guard
# (/opt/baton/denied.json), read relative to this file so the worker can't edit the guardrail it is
# fenced by. It's a soft guardrail, not a boundary — a missing/unreadable file yields an empty list
# (NEVER fail a job closed over it; the OS sandbox is the real fence). This is why adding a project
# needs nothing under /opt/baton: the fence no longer reads any per-project config.
_DENIED_FILE = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "denied.json"))


def _load_policy():
    try:
        with open(_DENIED_FILE) as f:
            return {"denied_commands": json.load(f).get("denied_commands", [])}
    except Exception:
        return {"denied_commands": []}


def _emit(decision, reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,            # "allow" | "deny"
        "permissionDecisionReason": reason,
    }}))


def main():
    real_user = pwd.getpwuid(os.getuid()).pw_name
    if not enforced_for(real_user):
        _emit("allow", f"not the worker user ({real_user}); fence inactive")
        return
    try:
        event = json.load(sys.stdin)
        decision, reason = classify(event.get("tool_name", ""), event.get("tool_input", {}) or {}, _load_policy())
    except Exception as e:
        _emit("deny", f"guard error (fail-closed): {e}")
        return
    _emit(decision, reason)


if __name__ == "__main__":
    main()
