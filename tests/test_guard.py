import json
from guard import guard as G
from guard.guard import classify, classify_bash, enforced_for

POLICY = {"denied_commands": ["acme-deploy", "wipe-staging", "deploy.sh"]}


def allow(tool, inp):
    d = classify(tool, inp, POLICY)
    assert d[0] == "allow", f"expected allow, got {d}"


def deny(tool, inp):
    d = classify(tool, inp, POLICY)
    assert d[0] == "deny", f"expected deny, got {d}"


# ---- user gate -------------------------------------------------------------
def test_user_gate():
    assert enforced_for("baton") is True
    assert enforced_for("pablo") is False   # the human is never fenced


# ---- Bash: soft guardrail (NOT the security boundary — the OS sandbox is) ---
def test_bash_allows_normal_work():
    # the guard no longer classifies bash for security; the OS sandbox contains effects.
    for cmd in ["pytest -q", "git push origin main", "curl https://anywhere.example/scrape",
                "python3 -c 'print(1)'", "bash -c 'whatever'", "ls -la | grep x",
                'git commit -m "mention deploy.sh in a message"']:
        assert classify_bash(cmd, POLICY)[0] == "allow", cmd


def test_bash_soft_guardrail_blocks_named_commands():
    assert classify_bash("acme-deploy --prod", POLICY)[0] == "deny"
    assert classify_bash("./deploy.sh", POLICY)[0] == "deny"
    assert classify_bash("echo hi; acme-deploy", POLICY)[0] == "deny"
    assert classify_bash("npm run build && wipe-staging", POLICY)[0] == "deny"


# ---- MCP: read-only allowlist (the real external-mutation guard) -----------
def test_mcp_reads_allowed():
    allow("mcp__claude_ai_Linear__list_issues", {})
    allow("mcp__claude_ai_Linear__get_issue", {})
    allow("mcp__claude_ai_Notion__notion-fetch", {})
    allow("mcp__claude_ai_Notion__notion-search", {})
    allow("mcp__claude_ai_Airtable__search_records", {})
    allow("mcp__claude_ai_TickTick__filter_tasks", {})
    allow("mcp__claude_ai_TickTick__list_completed_tasks_by_date", {})


def test_mcp_writes_denied():
    deny("mcp__claude_ai_Linear__save_issue", {})
    deny("mcp__claude_ai_Notion__notion-update-page", {})
    deny("mcp__claude_ai_Linear__create_attachment", {})
    deny("mcp__claude_ai_TickTick__add_comment", {})
    deny("mcp__claude_ai_TickTick__complete_task", {})
    deny("mcp__claude_ai_Gmail__label_message", {})
    deny("mcp__claude_ai_Google_Drive__copy_file", {})
    deny("mcp__claude_ai_Airtable__delete_records_for_table", {})


def test_mcp_mutation_verb_anywhere_denied():
    deny("mcp__claude_ai_Notion__notion-getattachments-and-purge", {})
    deny("mcp__srv__get_and_delete_pod", {})
    deny("mcp__srv__search_and_replace", {})
    deny("mcp__srv__read_replica_promote", {})


def test_non_bash_non_mcp_tools_allowed():
    allow("Edit", {"file_path": "x.py"})
    allow("Write", {"file_path": "x.py"})
    allow("Read", {"file_path": "x.py"})


# ---- denied list is now GLOBAL + project-independent (one root-owned /opt/baton/denied.json) ----
def test_load_policy_reads_global_denied_file(tmp_path, monkeypatch):
    f = tmp_path / "denied.json"; f.write_text(json.dumps({"denied_commands": ["deploy.sh", "wipe"]}))
    monkeypatch.setattr(G, "_DENIED_FILE", str(f))
    assert G._load_policy()["denied_commands"] == ["deploy.sh", "wipe"]


def test_load_policy_missing_is_empty_not_failclosed(tmp_path, monkeypatch):
    # a soft guardrail must NEVER wedge a job: a missing file yields an empty list, not an error.
    monkeypatch.setattr(G, "_DENIED_FILE", str(tmp_path / "does-not-exist.json"))
    assert G._load_policy() == {"denied_commands": []}
