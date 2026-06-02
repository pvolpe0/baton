import subprocess
import pytest
from runner import tick


def _g(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _seed_bare(tmp_path, name):
    """A bare remote with one commit on main, returned as a path usable as a clone source."""
    bare = tmp_path / f"{name}.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    seed = tmp_path / f"seed-{name}"
    subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True)
    _g(["config", "user.email", "t@t"], seed); _g(["config", "user.name", "t"], seed)
    (seed / "f.txt").write_text("base-" + name); _g(["add", "-A"], seed); _g(["commit", "-qm", "base"], seed)
    _g(["branch", "-M", "main"], seed); _g(["push", "-qu", "origin", "main"], seed)
    return bare


def test_reproduce_repos_handles_multiple_repos(tmp_path):
    """The multi-repo (polyrepo) case: the worker must reproduce the wip branch in EVERY repo, not
    just the first. Build two real repos with a pushed wip branch and confirm reproduce_repos checks
    both out at the exact origin SHA."""
    remotes, work = tmp_path / "remotes", tmp_path / "work"
    remotes.mkdir(); work.mkdir()
    wip = "wip/handoff-xyz"
    expected = {}
    for name in ("repoA", "repoB"):
        bare = remotes / f"{name}.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        seed = tmp_path / f"seed-{name}"
        subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True)
        _g(["config", "user.email", "t@t"], seed); _g(["config", "user.name", "t"], seed)
        (seed / "f.txt").write_text("base"); _g(["add", "-A"], seed); _g(["commit", "-qm", "base"], seed)
        _g(["branch", "-M", "main"], seed); _g(["push", "-qu", "origin", "main"], seed)
        _g(["checkout", "-qb", wip], seed); (seed / "f.txt").write_text("wip-" + name)
        _g(["add", "-A"], seed); _g(["commit", "-qm", "wip"], seed); _g(["push", "-qu", "origin", wip], seed)
        expected[name] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(seed),
                                        capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "clone", "-q", str(bare), str(work / name)], check=True)   # worker clone, on main

    tick.reproduce_repos({"repos": [{"repo": "repoA", "wip_branch": wip},
                                    {"repo": "repoB", "wip_branch": wip}]},
                         {"roots": {"pi": str(work)}})

    for name in ("repoA", "repoB"):
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(work / name), capture_output=True, text=True).stdout.strip()
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(work / name), capture_output=True, text=True).stdout.strip()
        assert branch == wip, f"{name} on {branch}, expected {wip}"
        assert head == expected[name], f"{name} HEAD {head} != origin {expected[name]}"


def test_ensure_repos_clones_missing_single_repo(tmp_path, monkeypatch):
    """Single-repo (roots.pi IS the repo): a fresh job auto-clones the repo into roots.pi."""
    bare = _seed_bare(tmp_path, "myrepo")
    proot = tmp_path / "work" / "myrepo"                      # does not exist yet
    monkeypatch.setattr(tick, "_clone_url", lambda project, repo: str(bare))
    tick.ensure_repos({}, {"name": "p", "owner": "o", "roots": {"pi": str(proot)}})
    assert (proot / ".git").is_dir()
    assert (proot / "f.txt").read_text() == "base-myrepo"


def test_ensure_repos_idempotent_when_present(tmp_path, monkeypatch):
    """A repo that already has .git is skipped — never re-cloned (so it can't clobber local work)."""
    proot = tmp_path / "repo"; (proot / ".git").mkdir(parents=True)
    def _boom(*a, **k):
        raise AssertionError("ensure_repos tried to clone an already-present repo")
    monkeypatch.setattr(tick, "_clone_url", _boom)
    tick.ensure_repos({}, {"name": "p", "owner": "o", "roots": {"pi": str(proot)}})  # no raise == skipped


def test_ensure_repos_skips_never_mirror(tmp_path, monkeypatch):
    """Polyrepo: members on never_mirror are not cloned."""
    bares = {n: _seed_bare(tmp_path, n) for n in ("repoA", "repoB")}
    proot = tmp_path / "parent"
    monkeypatch.setattr(tick, "_clone_url", lambda project, repo: str(bares[repo]))
    tick.ensure_repos({"repos": [{"repo": "repoA"}, {"repo": "repoB"}]},
                      {"name": "p", "owner": "o", "never_mirror": ["repoB"], "roots": {"pi": str(proot)}})
    assert (proot / "repoA" / ".git").is_dir()
    assert not (proot / "repoB").exists()


def test_ensure_repos_no_owner_raises(tmp_path):
    """A missing repo with no 'owner' to build a clone URL fails loudly (run_tick blocks the job)."""
    with pytest.raises(RuntimeError, match="owner"):
        tick.ensure_repos({}, {"name": "p", "roots": {"pi": str(tmp_path / "nope")}})


def test_clone_url_single_vs_polyrepo():
    proj = {"owner": "acme", "roots": {"pi": "/home/baton/work/widget"}}
    assert tick._clone_url(proj, ".") == "https://github.com/acme/widget.git"        # single-repo: basename
    assert tick._clone_url(proj, "api") == "https://github.com/acme/api.git"          # polyrepo: member name


def test_pick_oldest_by_id(tmp_path):
    (tmp_path / "queue" / "20260601T1000Z-aaaa").mkdir(parents=True)
    (tmp_path / "queue" / "20260601T0900Z-bbbb").mkdir(parents=True)
    assert tick.pick_next(str(tmp_path)) == "20260601T0900Z-bbbb"   # older id first


def test_pick_next_none_when_empty(tmp_path):
    (tmp_path / "queue").mkdir(parents=True)
    assert tick.pick_next(str(tmp_path)) is None


def test_pick_next_and_has_running_ignore_hidden_temp_dirs(tmp_path):
    """The doctor's writable-set probe leaves `.doctor-probe-*` scratch under running/; a stray temp
    dir must never look like a real job (else the next tick false-finalizes it as crashed)."""
    (tmp_path / "queue" / ".doctor-probe-abc").mkdir(parents=True)
    (tmp_path / "running" / ".doctor-proot-xyz").mkdir(parents=True)
    assert tick.pick_next(str(tmp_path)) is None       # hidden queue dir ignored
    assert tick.has_running(str(tmp_path)) is False    # hidden running dir ignored
    (tmp_path / "queue" / "20260602T1000Z-aaaa").mkdir()
    assert tick.pick_next(str(tmp_path)) == "20260602T1000Z-aaaa"   # real job still picked


def test_has_running(tmp_path):
    (tmp_path / "running" / "20260601T0900Z-bbbb").mkdir(parents=True)
    assert tick.has_running(str(tmp_path)) is True


def test_no_running_when_dir_absent(tmp_path):
    assert tick.has_running(str(tmp_path)) is False


def test_job_props_writable_set_is_jobdir_not_state_root(tmp_path):
    """tick must put the JOB DIR (running/<jid>) in the confined unit's writable set, never the whole
    state clone — the regression guard for the fence-escape hole. Also confirms the repo is writable
    (the agent does its work there) while the state root and running/ parent are NOT."""
    rdir = str(tmp_path / "running" / "jid1")
    props = tick._job_props({"id": "x"}, proot="/work/repo", rdir=rdir,
                            worker_home=str(tmp_path / "home"))
    rw = [p[len("--property=ReadWritePaths="):] for p in props if p.startswith("--property=ReadWritePaths=")]
    assert rdir in rw and "/work/repo" in rw
    assert str(tmp_path) not in rw                       # state root absent
    assert str(tmp_path / "running") not in rw           # only the job dir, not the running/ parent
    assert "--property=ProtectHome=read-only" in props


def test_worker_cmd_runs_engine_isolated():
    cmd = tick._worker_cmd()
    assert cmd[1:3] == ["-B", "-s"]                 # no __pycache__ to read-only /opt/baton; no ~/.local
    assert cmd[-1].endswith("/runner/worker.py")    # from CODE (the deployed engine tree)


def test_classify_sdk_completion_table():
    # no sentinel + unit terminal -> hard crash; + non-terminal -> still running (fail-closed)
    assert tick.classify_sdk(done_present=False, unit_terminal=True,  result=None, blocked_present=False)[0] == "crashed"
    assert tick.classify_sdk(done_present=False, unit_terminal=False, result=None, blocked_present=False)[0] == "running"
    # sentinel present but result.json missing/corrupt -> crashed, NOT a false 'done' (unsafe direction)
    assert tick.classify_sdk(done_present=True, unit_terminal=True, result=None, blocked_present=False)[0] == "crashed"
    # sentinel present + clean result -> done (summary from result)
    s, summ = tick.classify_sdk(done_present=True, unit_terminal=True,
                                result={"is_error": False, "result": "all good"}, blocked_present=False)
    assert s == "done" and summ == "all good"
    # sentinel + is_error -> blocked; sentinel + BLOCKED.txt -> blocked even if not is_error
    assert tick.classify_sdk(done_present=True, unit_terminal=True,
                             result={"is_error": True, "result": "x"}, blocked_present=False)[0] == "blocked"
    assert tick.classify_sdk(done_present=True, unit_terminal=True,
                             result={"is_error": False, "result": "x"}, blocked_present=True)[0] == "blocked"


def test_write_report_blocked_surfaces_blocked_txt_reason(tmp_path):
    (tmp_path / "BLOCKED.txt").write_text("needs the staging DB password\n")
    tick._write_report(str(tmp_path), "jid1", "blocked", {"total_cost_usd": 0.01, "result": "I stopped."})
    report = (tmp_path / "report.md").read_text()
    assert "Blocked reason:" in report and "staging DB password" in report   # the canonical reason
    assert "I stopped." in report                                            # plus the agent's summary


def test_write_report_crashed_includes_err_tail(tmp_path):
    (tmp_path / "err.txt").write_text("killed by signal 15 (RuntimeMaxSec/stop)\n")
    tick._write_report(str(tmp_path), "jid2", "blocked", None)               # res None -> crashed report
    report = (tmp_path / "report.md").read_text()
    assert "crashed jid2" in report and "killed by signal 15" in report


def test_launch_cmd():
    m = {"id": "x-1", "model": "opus", "effort": "high", "project": "example"}
    cmd = tick.launch_cmd(m, claude="/c/claude", brief_path="/q/x-1/brief.md",
                          charter_path="/h/profile/worker-charter.md",
                          blocked_path="/r/x-1/BLOCKED.txt")
    assert cmd[:2] == ["/c/claude", "-p"]
    assert "/r/x-1/BLOCKED.txt" in " ".join(cmd)
    assert "--model" in cmd and "opus" in cmd
    assert "--effort" in cmd and "high" in cmd
    assert "--output-format" in cmd and "json" in cmd
    assert "--permission-mode" in cmd and "bypassPermissions" in cmd
    assert "--append-system-prompt-file" in cmd and "/h/profile/worker-charter.md" in cmd
    assert "/q/x-1/brief.md" in " ".join(cmd)
