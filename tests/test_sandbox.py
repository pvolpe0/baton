import os
from lib import sandbox


def test_rw_paths_is_repo_jobdir_and_caches_only():
    """The writable set is exactly: the repo, THIS job's dir, and the agent caches — NOT the engine
    clone or the rest of the state tree. This is the hole-closing invariant (a job must not be able
    to write the code that runs unconfined next tick)."""
    rw = sandbox.rw_paths("/work/repo", "/state/running/jid1", "/home/baton")
    assert rw[0] == "/work/repo"
    assert rw[1] == "/state/running/jid1"
    assert set(rw[2:]) == {f"/home/baton/{d}" for d in sandbox.CACHE_DIRS}
    # the engine clone / state root must NOT be writable
    assert "/state" not in rw
    assert not any(p == "/home/baton" for p in rw)


def test_job_properties_narrows_to_jobdir_not_state_root():
    props = sandbox.job_properties(proot="/work/repo", jobdir="/state/running/jid1",
                                   home="/home/baton", max_runtime=3600, tasks_max=768)
    rw = [p[len("--property=ReadWritePaths="):] for p in props if p.startswith("--property=ReadWritePaths=")]
    assert "/state/running/jid1" in rw
    assert "/work/repo" in rw
    assert "/state" not in rw and "/home/baton" not in rw          # the hole: must be absent
    assert "--property=ProtectSystem=strict" in props
    assert "--property=ProtectHome=read-only" in props
    assert "--property=NoNewPrivileges=yes" in props
    assert "--property=RuntimeMaxSec=3600" in props
    assert "--property=WorkingDirectory=/work/repo" in props
    assert "--property=TasksMax=768" in props


def test_job_properties_carves_config_systemd_readonly():
    """The drain timer's own systemd unit lives under ~/.config/systemd/user; ~/.config must stay
    writable (gh/git config) but that subtree must NOT be — else a job repoints ExecStart and
    re-introduces the unconfined-next-tick escape this whole change closes."""
    props = sandbox.job_properties(proot="/r", jobdir="/j", home="/home/baton",
                                   max_runtime=10, tasks_max=8)
    assert "--property=ReadOnlyPaths=-/home/baton/.config/systemd" in props
    rw = [p[len("--property=ReadWritePaths="):] for p in props if p.startswith("--property=ReadWritePaths=")]
    assert "/home/baton/.config" in rw          # the rest of ~/.config stays writable for tool config


def test_ensure_cache_dirs_creates_missing(tmp_path):
    """On a fresh worker the agent cache dirs may not exist; they must be created before launch (and
    before the doctor probe) since systemd-run fails on a non-existent ReadWritePaths entry."""
    home = tmp_path / "home"
    home.mkdir()
    sandbox.ensure_cache_dirs(str(home))
    for d in sandbox.CACHE_DIRS:
        assert (home / d).is_dir()
    sandbox.ensure_cache_dirs(str(home))      # idempotent — no error on a second call


def test_proot_conflict_flags_overlap_with_protected_paths():
    """proot goes verbatim into ReadWritePaths; it must not equal/contain/sit-inside the engine
    deploy, the state clone, or the home root (any of which would re-open protected paths to a job)."""
    state, code, home = "/home/baton/baton", "/opt/baton", "/home/baton"
    assert sandbox.proot_conflict("/home/baton/work/repo", state=state, code_root=code, home=home) == ""
    assert sandbox.proot_conflict("/home/baton", state=state, code_root=code, home=home)          # home root
    assert sandbox.proot_conflict(state, state=state, code_root=code, home=home)                  # the clone
    assert sandbox.proot_conflict("/home/baton/baton/running", state=state, code_root=code, home=home)  # inside it
    assert sandbox.proot_conflict("/opt/baton", state=state, code_root=code, home=home)           # the engine
    assert sandbox.proot_conflict("/opt", state=state, code_root=code, home=home)                 # parent of engine
    assert sandbox.proot_conflict("/", state=state, code_root=code, home=home)                    # fs root


def test_job_properties_optional_mem_and_envfile(tmp_path):
    envf = tmp_path / ".baton.env"
    envf.write_text("X=1")
    props = sandbox.job_properties(proot="/r", jobdir="/j", home="/h", max_runtime=10,
                                   tasks_max=8, mem_max=123, envfile=str(envf))
    assert "--property=MemoryMax=123" in props
    assert f"--property=EnvironmentFile={envf}" in props
    # omitted when None
    p2 = sandbox.job_properties(proot="/r", jobdir="/j", home="/h", max_runtime=10, tasks_max=8)
    assert not any(p.startswith("--property=MemoryMax") for p in p2)
    assert not any(p.startswith("--property=EnvironmentFile") for p in p2)
