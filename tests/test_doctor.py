from lib import doctor as D


def test_parse_admin_access_true():
    j = '{"AttachedPolicies":[{"PolicyName":"AdministratorAccess","PolicyArn":"arn:aws:iam::aws:policy/AdministratorAccess"}]}'
    assert D.parse_admin_access(j) is True


def test_parse_admin_access_false():
    assert D.parse_admin_access('{"AttachedPolicies":[{"PolicyName":"ReadOnlyAccess"}]}') is False


def test_parse_admin_access_garbage():
    assert D.parse_admin_access("not json") is False


def test_parse_linger():
    assert D.parse_linger("Linger=yes\nFoo=bar") is True
    assert D.parse_linger("Linger=no") is False
    assert D.parse_linger("") is False


def test_summarize():
    ok, lines = D.summarize([("a", True, "fine"), ("b", False, "broken")])
    assert ok is False
    assert any("FAIL" in l and "b" in l for l in lines)
    ok2, _ = D.summarize([("a", True, "fine")])
    assert ok2 is True


def test_parse_classic_scopes():
    assert D.parse_classic_scopes("repo, workflow, read:org") == ["repo", "workflow", "read:org"]
    assert D.parse_classic_scopes("") == []


def test_dangerous_classic():
    assert D.dangerous_classic(["repo", "read:org", "workflow"]) == ["repo", "workflow"]
    assert D.dangerous_classic(["read:org", "gist"]) == []


def test_repo_admin():
    assert D.repo_admin('{"permissions":{"admin":true,"push":true}}') is True
    assert D.repo_admin('{"permissions":{"admin":false,"push":true}}') is False
    assert D.repo_admin("garbage") is False


# --- fence verification (gates job execution in tick.py) ----------------------
def _fence_dir(tmp_path, *, settings=True, guard=True, worker_user="baton"):
    import os
    paths = {
        "managed-settings": str(tmp_path / "managed-settings.json"),
        "guard": str(tmp_path / "guard.py"),
        "worker-user": str(tmp_path / "worker-user"),
    }
    if settings:
        open(paths["managed-settings"], "w").write("{}"); os.chmod(paths["managed-settings"], 0o444)
    if guard:
        open(paths["guard"], "w").write("# guard"); os.chmod(paths["guard"], 0o444)
    if worker_user is not None:
        open(paths["worker-user"], "w").write(worker_user); os.chmod(paths["worker-user"], 0o444)
    return paths


def test_fence_active_all_present_and_matching(tmp_path):
    paths = _fence_dir(tmp_path, worker_user="baton")
    assert D.fence_active("baton", paths) is True


def test_fence_inactive_when_file_missing(tmp_path):
    paths = _fence_dir(tmp_path, guard=False, worker_user="baton")
    assert D.fence_active("baton", paths) is False


def test_fence_inactive_when_worker_user_mismatch(tmp_path):
    # guard.py self-gates off if the worker-user file doesn't name the run user -> fence inert
    paths = _fence_dir(tmp_path, worker_user="someoneelse")
    assert D.fence_active("baton", paths) is False


def test_fence_inactive_when_writable_by_run_user(tmp_path):
    import os
    paths = _fence_dir(tmp_path, worker_user="baton")
    os.chmod(paths["guard"], 0o666)   # worker could edit the guard -> not a fence
    assert D.fence_active("baton", paths) is False
