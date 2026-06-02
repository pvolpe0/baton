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


# --- engine immutability (the code that runs UNCONFINED must be root-owned + worker-read-only) ---
def test_engine_verdict_truth_table():
    assert D._engine_verdict(present=True,  root_owned=True,  writable=False, path="/opt/baton/x")[0] is True
    assert D._engine_verdict(present=False, root_owned=False, writable=False, path="/opt/baton/x") == (False, "missing")
    assert D._engine_verdict(present=True,  root_owned=True,  writable=True,  path="/opt/baton/x")[0] is False
    assert "writable" in D._engine_verdict(present=True, root_owned=True, writable=True, path="/x")[1]
    assert D._engine_verdict(present=True,  root_owned=False, writable=False, path="/opt/baton/x")[0] is False
    assert "root" in D._engine_verdict(present=True, root_owned=False, writable=False, path="/x")[1]


def test_verify_engine_immutable_flags_writable(tmp_path):
    import os
    f = tmp_path / "tick.py"
    f.write_text("# code")
    os.chmod(f, 0o666)                                  # worker-writable == escape
    checks = D.verify_engine_immutable([str(f)])
    assert checks[0][1] is False                        # not ok
    assert "writable" in checks[0][2]
    # a test-owned file is never root-owned, so a present+read-only file fails on ownership
    os.chmod(f, 0o444)
    checks = D.verify_engine_immutable([str(f)])
    assert checks[0][1] is False and "root" in checks[0][2]


def test_verify_engine_immutable_missing(tmp_path):
    checks = D.verify_engine_immutable([str(tmp_path / "nope.py")])
    assert checks[0][1] is False and checks[0][2] == "missing"


def test_engine_immutable_false_when_missing(tmp_path):
    assert D.engine_immutable([str(tmp_path / "nope.py")]) is False


def test_engine_code_paths_uses_base_and_covers_all_unconfined_modules():
    paths = D.engine_code_paths("/opt/baton")
    for rel in ("runner/tick.py", "runner/notify.py", "lib/doctor.py", "lib/sandbox.py",
                "lib/manifest.py", "lib/nodes.py", "guard/guard.py", "bin/baton"):
        assert f"/opt/baton/{rel}" in paths
    assert "/home/baton/baton/runner/tick.py" in D.engine_code_paths("/home/baton/baton")


def test_verify_engine_immutable_also_checks_parent_dir(tmp_path):
    import os
    d = tmp_path / "runner"
    d.mkdir()
    f = d / "tick.py"
    f.write_text("# code")
    os.chmod(f, 0o444)                                   # file itself read-only...
    names = [n for n, ok, det in D.verify_engine_immutable([str(f)])]
    assert any(n.startswith("engine-dir:") for n in names)   # ...the containing dir is verified too


# --- writable-set probe output parser ---
def test_parse_writable_probe_confined():
    ok, detail = D.parse_writable_probe("JOBDIR_OK\n")
    assert ok is True


def test_parse_writable_probe_too_broad():
    ok, detail = D.parse_writable_probe("JOBDIR_OK\nSTATE_OPEN\nOPT_OPEN\n")
    assert ok is False and "STATE_OPEN" in detail and "OPT_OPEN" in detail


def test_parse_writable_probe_flags_config_systemd():
    ok, detail = D.parse_writable_probe("JOBDIR_OK\nCONFIG_SYSTEMD_OPEN\n")
    assert ok is False and "CONFIG_SYSTEMD_OPEN" in detail


def test_parse_writable_probe_too_narrow():
    ok, detail = D.parse_writable_probe("")            # job couldn't even write its own dir
    assert ok is False and "result.json" in detail
