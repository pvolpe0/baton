"""One drain tick (run by a systemd timer, as the `baton` user). Single job at a time.
- if a job is running: poll it; on exit, read result.json -> report -> notify
- else: pick the oldest queued job, reproduce its state, launch it headless and detached

Completion = the `claude -p` process exits and writes result.json (spike-confirmed).
The launched job runs as `baton` in a systemd transient scope so it survives the 90s tick.

CODE vs STATE: this file runs from the DEPLOYED, root-owned engine tree (/opt/baton on a worker),
derived from __file__ — NEVER from a job-writable clone, so a job that overwrote ~/baton/runner/
tick.py can't make the next tick execute poisoned code unconfined. STATE is the worker-writable git
clone (queue/running/done/blocked/.git/nodes/projects), addressed separately."""
import json, os, pwd, shlex, subprocess, sys, time

CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # where the engine RUNS FROM
sys.path.insert(0, CODE)   # import lib/runner from the code tree (root-owned), not from writable state
STATE = os.environ.get("BATON_STATE") or os.environ.get("BATON_HOME") or os.path.expanduser("~/baton")
CLAUDE = os.environ.get("BATON_CLAUDE", os.path.expanduser("~/.local/bin/claude"))
CHARTER = os.path.join(CODE, "profile", "worker-charter.md")   # root-owned charter, not job-editable
DEFAULT_MAX_RUNTIME = 3600          # seconds; a job scope past this is killed so it can't wedge the queue
DEFAULT_TASKS_MAX = 768             # PID cap per job (fork-bomb guard; generous for normal builds)
FENCE_MARKER = os.path.join(STATE, ".fence-down")   # local-only (gitignored): de-dupes the fence-down alert


def _mem_max():
    """A memory cap leaving ~512MB headroom below total RAM, so a runaway job can't OOM the whole
    box (the cgroup OOM-kills the job's scope instead; it then finalizes as crashed). Bytes, or
    None on a host we can't read (then no cap is set)."""
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemTotal:"):
                total = int(line.split()[1]) * 1024
                return max(total - 512 * 1024 * 1024, 512 * 1024 * 1024)
    except Exception:
        return None
    return None


# ---- pure decision logic (unit-tested) ----
def _job_dirs(d):
    """Job ids under a state dir, ignoring hidden/temp entries (e.g. the doctor's `.doctor-probe-*`
    scratch dirs) so a stray temp dir is never mistaken for a real job and false-finalized."""
    if not os.path.isdir(d):
        return []
    return [e for e in os.listdir(d) if not e.startswith(".") and os.path.isdir(os.path.join(d, e))]


def pick_next(home):
    ids = _job_dirs(os.path.join(home, "queue"))
    return sorted(ids)[0] if ids else None      # timestamp-prefixed ids sort chronologically


def has_running(home):
    return bool(_job_dirs(os.path.join(home, "running")))


def launch_cmd(manifest, *, claude, brief_path, charter_path, blocked_path):
    # blocked_path is an ABSOLUTE path inside the job dir: the agent's cwd is the repo root, so a
    # bare "BLOCKED.txt" lands in the repo and the poller (which checks the job dir) never sees it.
    prompt = (f"Read {brief_path} and execute the task to completion. "
              f"Work autonomously; make reasonable assumptions; if genuinely blocked, "
              f"write a one-line reason to the file {blocked_path} (use that exact absolute path) and stop.")
    # bypassPermissions, NOT auto: the auto classifier blocks legitimate wip-branch pushes in
    # headless runs (smoke-confirmed), stalling every job. baton's fence is the root-owned guard
    # hook + branch protection + scoped creds + the baton OS user — not the permission classifier —
    # and the PreToolUse guard fires under bypass.
    return [claude, "-p", prompt,
            "--permission-mode", "bypassPermissions",
            "--model", manifest["model"],
            "--effort", manifest["effort"],
            "--append-system-prompt-file", charter_path,
            "--output-format", "json"]


def _job_props(man, *, proot, rdir, worker_home, envfile=None):
    """The confined unit's --property list (delegates to the shared, unit-tested lib.sandbox so the
    launcher and the doctor probe build the SAME sandbox). rdir (running/<jid>) is the ONLY state
    subtree the job may write — NOT the engine clone or the rest of STATE — which is what closes the
    fence-escape. Ensures the agent's cache dirs exist (proot/rdir already do)."""
    from lib import sandbox
    sandbox.ensure_cache_dirs(worker_home)         # ReadWritePaths entries must exist or systemd errors
    return sandbox.job_properties(
        proot=proot, jobdir=rdir, home=worker_home,
        max_runtime=man.get("max_runtime", DEFAULT_MAX_RUNTIME),
        tasks_max=man.get("max_tasks", DEFAULT_TASKS_MAX),
        mem_max=man.get("max_memory") or _mem_max(),
        envfile=envfile)


SDK_PYTHON = "/opt/baton-sdk/bin/python"        # root-owned venv holding claude_agent_sdk (setup.sh)


def _worker_cmd():
    """The SDK worker entrypoint, run from the root-owned engine (manifest.engine == 'sdk'). The
    interpreter is the ROOT-OWNED venv that holds claude_agent_sdk — the worker can't tamper it, and
    nothing else has the SDK on its path (run_tick blocks the job with an actionable reason if the venv
    is missing, rather than ImportError-crashing under a different interpreter). `-B`: no __pycache__
    writes to read-only /opt/baton. `-s`: drop the per-user site dir (~/.local is in the job's WRITABLE
    set) so a job can't poison the worker's imports."""
    return [SDK_PYTHON, "-B", "-s", os.path.join(CODE, "runner", "worker.py")]


def classify_sdk(*, done_present, unit_terminal, result, blocked_present):
    """Pure SDK completion classification (finally pure + unit-testable). done.json is the sentinel
    worker.py writes LAST, atomically, so when present result.json is guaranteed well-formed. Absent +
    the unit terminal ⇒ a hard crash (the SIGTERM handler writes the sentinel even on a RuntimeMaxSec
    kill, so a *missing* sentinel means SIGKILL/OOM/segfault/reboot); absent + non-terminal ⇒ still
    running (fail-closed). Returns (status, summary) where status ∈ done|blocked|crashed|running."""
    if not done_present:
        return ("crashed" if unit_terminal else "running", "")
    if not result:                                  # sentinel written but result.json missing/corrupt
        return ("crashed", "")                      # (e.g. its write ENOSPC'd) — must NOT pass as 'done'
    if blocked_present or result.get("is_error"):
        return ("blocked", (result.get("result") or "")[:200])
    return ("done", (result.get("result") or "")[:200])


def _err_tail(rdir, n=20):
    """Last n lines of the job's err.txt (RuntimeMaxSec/OOM/segfault/import-error are distinguishable),
    for the crashed report. '' if there's nothing."""
    try:
        return "\n".join(open(os.path.join(rdir, "err.txt"), errors="replace").read().splitlines()[-n:])
    except OSError:
        return ""


def _write_report(rdir, jid, status, res):
    """report.md for a finalized job. res is the parsed result dict (or None ⇒ crashed: include the
    err.txt tail so a kill/OOM is diagnosable rather than a blank 'no usable result'). For a blocked
    job, surface the canonical one-line reason the agent wrote to BLOCKED.txt."""
    path = os.path.join(rdir, "report.md")
    if res is not None:
        reason = ""
        if status == "blocked":
            try:
                reason = open(os.path.join(rdir, "BLOCKED.txt")).read().strip()
            except OSError:
                pass
        open(path, "w").write(
            f"# {status} {jid}\n\ncost: ${(res.get('total_cost_usd') or 0):.4f}\n\n"
            + (f"**Blocked reason:** {reason}\n\n" if reason else "")
            + f"{res.get('result', '')}\n")
    else:
        tail = _err_tail(rdir)
        open(path, "w").write(
            f"# crashed {jid}\n\nThe job ended without a usable result (killed, OOM, reboot, or never "
            f"launched)." + (f"\n\nLast stderr:\n\n```\n{tail}\n```\n" if tail else "")
            + "\n\nRe-hand-off if needed.\n")


# ---- side-effecting orchestration (verified live on the Pi, not unit-tested) ----
def _sh(args, cwd=None):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def _git(*args, cwd):
    return _sh(["git", *args], cwd=cwd)


def _commit_push(msg, tries=4):
    """Persist a state transition to the shared branch. The producer pushes queue entries to
    the same branch, so origin may have advanced since our pull — rebase on top and retry,
    rather than a bare push that rejects and silently strands the transition (origin would
    then show the job stuck in its old state). State is already committed locally either way."""
    _git("add", "-A", cwd=STATE)
    _git("commit", "-m", msg, cwd=STATE)
    for _ in range(tries):
        if _git("pull", "--rebase", "--autostash", cwd=STATE).returncode != 0:
            _git("rebase", "--abort", cwd=STATE)   # a conflict leaves a detached HEAD mid-rebase;
            continue                               # abort so we never commit onto a dangling head
        if _git("push", cwd=STATE).returncode == 0:
            return
    # left committed locally on a clean branch; the next tick's recover+rebase-pull reconciles.


def _git_recover():
    """If a prior tick was interrupted mid-rebase (conflict, kill, reboot), the repo is left in
    .git/rebase-merge on a detached HEAD — and a bare commit there silently desyncs the worker
    from origin forever. Abort any in-progress rebase and get back onto the branch first."""
    g = os.path.join(STATE, ".git")
    if os.path.isdir(os.path.join(g, "rebase-merge")) or os.path.isdir(os.path.join(g, "rebase-apply")):
        _git("rebase", "--abort", cwd=STATE)
    if _git("rev-parse", "--abbrev-ref", "HEAD", cwd=STATE).stdout.strip() == "HEAD":
        _git("checkout", "main", cwd=STATE)


def reproduce_repos(man, project):
    """continue-mode: deterministic checkout of each wip branch under the baton-owned root."""
    for r in man.get("repos", []):
        rd = os.path.join(project["roots"]["pi"], r["repo"])
        _git("fetch", "origin", r["wip_branch"], cwd=rd)
        _git("reset", "--hard", cwd=rd)
        _git("checkout", "-B", r["wip_branch"], f"origin/{r['wip_branch']}", cwd=rd)
        head = _git("rev-parse", "HEAD", cwd=rd).stdout.strip()
        exp = _git("rev-parse", f"origin/{r['wip_branch']}", cwd=rd).stdout.strip()
        if head != exp:
            raise RuntimeError(f"{r['repo']}: HEAD {head} != origin {exp}")


def _clone_url(project, repo):
    """Clone URL for auto-provisioning. v1: GitHub. repo='.' (single-repo) takes the name from
    roots.pi's basename; a named repo (polyrepo) uses the name directly."""
    name = os.path.basename(project["roots"]["pi"].rstrip("/")) if repo == "." else repo
    return f"https://github.com/{project['owner']}/{name}.git"


def ensure_repos(man, project):
    """Auto-provision: clone any repo this job needs that isn't on disk yet (as the baton user), so a
    freshly-added project Just Works on the first handoff with no manual clone on the worker.
    Idempotent (skips repos that already have .git); honors never_mirror; needs project['owner'] to
    build the clone URL. Repo set = the manifest's repos (continue-mode), else the project's explicit
    `repos` list (polyrepo), else a single repo at roots.pi ('.')."""
    proot = project["roots"]["pi"]
    nm = set(project.get("never_mirror", []))
    repos = [r["repo"] for r in man.get("repos", [])] or project.get("repos") or ["."]
    for repo in repos:
        if repo in nm:
            continue
        target = proot if repo == "." else os.path.join(proot, repo)
        if os.path.isdir(os.path.join(target, ".git")):
            continue                                    # already cloned — nothing to do
        if not project.get("owner"):
            raise RuntimeError(f"cannot auto-clone '{repo}': project '{project.get('name')}' has no 'owner'")
        parent = os.path.dirname(target.rstrip("/"))
        os.makedirs(parent, exist_ok=True)
        r = _git("clone", _clone_url(project, repo), target, cwd=parent)
        if r.returncode != 0:
            raise RuntimeError(f"auto-clone failed for '{repo}': {r.stderr.strip()[:200]}")


def _block_prep_failure(jid, rdir, err):
    """Repo prep (auto-clone / reproduce / clean) failed BEFORE launch — finalize to blocked/ with the
    reason instead of launching into a missing or half-built repo, so the user gets an actionable
    report rather than a generic crash on the next poll."""
    from runner import notify
    open(os.path.join(rdir, "report.md"), "w").write(
        f"# blocked {jid}\n\nCould not prepare the repo before launch:\n\n    {err}\n\n"
        f"Fix the project config (e.g. add `owner`, check repo access) and re-hand-off.\n")
    os.rename(rdir, os.path.join(STATE, "blocked", jid))
    _commit_push(f"blocked {jid}")
    try:
        notify.notify(status="blocked", job_id=jid, summary=f"repo prep failed: {err}")
    except Exception:
        pass


def _clean_base(project):
    """Fresh-mode prep: reset the project's repo(s) to a clean default branch so a fresh job starts
    from `main`, not from whatever branch/files a previous job left behind (otherwise it commits onto
    a stale wip branch and reuses its PR). Handles a single-repo root and a polyrepo parent root."""
    proot = project["roots"]["pi"]
    branch = project.get("default_branch", "main")
    if os.path.isdir(os.path.join(proot, ".git")):
        repos = [proot]
    elif os.path.isdir(proot):
        repos = [os.path.join(proot, d) for d in os.listdir(proot)
                 if os.path.isdir(os.path.join(proot, d, ".git"))]
    else:
        return
    for rd in repos:
        _git("fetch", "origin", cwd=rd)
        _git("checkout", branch, cwd=rd)
        _git("reset", "--hard", f"origin/{branch}", cwd=rd)
        _git("clean", "-fd", cwd=rd)


def _fence_warn(run_user):
    """Fence is missing/inert (or the engine is worker-editable): log every tick, but notify only once
    (marker) to avoid 90s spam."""
    from lib import doctor as D
    failed = [f"{n} ({d})" for n, ok, d in D.verify_fence(run_user) if not ok]
    failed += [f"{n} ({d})" for n, ok, d in D.verify_engine_immutable(D.engine_code_paths(CODE)) if not ok]
    msg = "refusing to run jobs — safety fence not active: " + "; ".join(failed)
    with open(os.path.join(STATE, "tick.log"), "a") as f:
        f.write(msg + "\n")
    if not os.path.exists(FENCE_MARKER):
        open(FENCE_MARKER, "w").write(msg)
        try:
            from runner import notify
            notify.notify(status="blocked", job_id="fence", summary=msg)
        except Exception:
            pass


def run_tick():
    from lib import manifest as M
    from lib import doctor as D
    run_user = pwd.getpwuid(os.getuid()).pw_name           # spoof-proof (uid, not $USER)
    # NEVER run bypassPermissions jobs unfenced — OR with worker-editable engine code (a job could
    # then overwrite the code the next tick runs unconfined while building the fence). Verify the tree
    # tick ACTUALLY runs from (CODE), not a hardcoded path, so the gate is meaningful even if launched
    # from elsewhere.
    if not D.fence_active(run_user) or not D.engine_immutable(D.engine_code_paths(CODE)):
        _fence_warn(run_user)
        return
    try:
        os.remove(FENCE_MARKER)                            # fence is back; re-arm the alert
    except OSError:
        pass
    _git_recover()                                      # heal a stuck/detached state from a prior tick
    _git("pull", "--rebase", "--autostash", cwd=STATE)  # --rebase (not --ff-only): a prior tick may
                                                        # have a locally-committed transition not yet pushed
    if has_running(STATE):
        _poll_running()
        return
    jid = pick_next(STATE)
    if not jid:
        return
    rdir = os.path.join(STATE, "running", jid)
    os.rename(os.path.join(STATE, "queue", jid), rdir)
    man = M.read(os.path.join(rdir, "manifest.json"))
    project = json.load(open(os.path.join(STATE, "projects", man["project"] + ".json")))
    try:
        ensure_repos(man, project)          # auto-clone first-seen repos so onboarding needs no manual clone
        if man["mode"] == "continue":
            reproduce_repos(man, project)
        else:
            _clean_base(project)            # fresh job: start from a clean default branch, not leftovers
    except Exception as e:                  # prep failed (no owner, clone/fetch error) — block cleanly with a
        _block_prep_failure(jid, rdir, e)   # real reason instead of launching into a missing/half-built repo
        return
    proot = project["roots"]["pi"]
    worker_home = pwd.getpwuid(os.getuid()).pw_dir         # the agent's caches live under the OS home
    from lib import sandbox
    conflict = sandbox.proot_conflict(proot, state=STATE, code_root=CODE, home=worker_home)
    if conflict:                                           # a misconfigured roots.pi that overlaps the
        _block_prep_failure(jid, rdir, RuntimeError(       # engine/state/home would re-open protected
            f"project root {proot!r} overlaps {conflict}; refusing to launch — its writable set would "
            f"re-open protected engine/state paths to the job. Fix roots.pi in the project config."))
        return                                             # paths to the job — block instead (fail-closed)
    if man.get("engine") == "sdk" and not os.path.exists(SDK_PYTHON):
        _block_prep_failure(jid, rdir, RuntimeError(       # the only interpreter with the SDK; without it
            f"engine 'sdk' requested but the SDK venv {SDK_PYTHON} is missing — run setup.sh (it "
            f"installs claude-agent-sdk) or set the job's engine to 'cli'."))   # the worker would just
        return                                             # ImportError-crash, so block with a real reason
    base = f"baton-job-{jid}"
    # record the unit name first (a transient SERVICE: `systemd-run --unit=B` -> `B.service`).
    open(os.path.join(rdir, "unit.txt"), "w").write(base + ".service")
    # CONTAINMENT (the fence): run the job as a confined transient service — NOT --scope, so the
    # systemd exec-sandbox actually applies. The agent may run ANY command and reach ANY host
    # (network is intentionally open), but the OS confines the *effects*: writes are restricted to
    # the repo + THIS job's dir (rdir) + claude's own caches; the engine clone, the rest of the state
    # tree, /opt/baton, and the system stay read-only; escalation is blocked; /tmp is private. A job
    # therefore cannot edit the code the next tick runs unconfined. Damage is bounded without
    # restricting the work.
    envfile = os.path.expanduser("~/.baton.env")           # GH_TOKEN etc. — via EnvironmentFile, not argv
    props = _job_props(man, proot=proot, rdir=rdir, worker_home=worker_home,
                       envfile=envfile if os.path.exists(envfile) else None)
    setenvs = [f"--setenv=BATON_PROJECT={man['project']}", f"--setenv=BATON_HOME={STATE}"]
    errp = os.path.join(rdir, "err.txt")
    if man.get("engine") == "sdk":
        # SDK worker.py writes result.json + done.json (sentinel) itself, atomically. We only redirect
        # stderr (import/early errors) into err.txt as a backstop; `exec` makes python the unit's main
        # process so RuntimeMaxSec's SIGTERM reaches worker.py's handler directly.
        setenvs += [f"--setenv=BATON_RDIR={rdir}", f"--setenv=BATON_PROOT={proot}",
                    f"--setenv=BATON_CHARTER={CHARTER}", "--setenv=PYTHONNOUSERSITE=1"]
        if man.get("resume_session"):
            setenvs.append(f"--setenv=BATON_RESUME={man['resume_session']}")
        wc = " ".join(shlex.quote(c) for c in _worker_cmd())
        run_args = ["bash", "-lc", f"exec {wc} 2>> {shlex.quote(errp)}"]
    else:
        # CLI worker (legacy fallback): claude -p with the shell redirect into the job dir.
        cmd = launch_cmd(man, claude=CLAUDE, brief_path=os.path.join(rdir, "brief.md"),
                         charter_path=CHARTER, blocked_path=os.path.join(rdir, "BLOCKED.txt"))
        inner = " ".join(shlex.quote(c) for c in cmd) + f" > {shlex.quote(os.path.join(rdir, 'result.json'))} 2> {shlex.quote(errp)}"
        run_args = ["bash", "-lc", inner]
    subprocess.Popen(["systemd-run", "--user", f"--unit={base}", *setenvs, *props, *run_args],
                     env={**os.environ, "BATON_PROJECT": man["project"]})
    _commit_push(f"start {jid}")


def _poll_running():
    """Finalize finished jobs. Robust to dead/malformed/orphaned entries so one bad
    running/ dir can never wedge the worker."""
    from runner import notify
    rroot = os.path.join(STATE, "running")
    if not os.path.isdir(rroot):
        return
    for jid in _job_dirs(rroot):
        rdir = os.path.join(rroot, jid)
        try:
            unit_path = os.path.join(rdir, "unit.txt")
            rpath = os.path.join(rdir, "result.json")
            try:
                from lib import manifest as M
                engine = M.read(os.path.join(rdir, "manifest.json")).get("engine", "cli")
            except Exception:
                engine = "cli"
            terminal = True
            if os.path.exists(unit_path):
                # only finalize on a DEFINITIVE terminal state. is-active returns active/activating/
                # deactivating while the unit is alive, and "" on a transient D-Bus hiccup. Treat
                # every non-terminal value as "still running" (fail-closed) so we never rename
                # running/ out from under a live job. No unit.txt -> never tracked -> crash recovery.
                unit = open(unit_path).read().strip()
                active = _sh(["systemctl", "--user", "is-active", unit]).stdout.strip()
                terminal = active in ("inactive", "failed")
                if not terminal:
                    # watchdog: if RuntimeMaxSec somehow didn't fire, kill a unit that outlived the
                    # ceiling so one hung job can't wedge the queue forever. unit.txt mtime ~= launch.
                    try:
                        if time.time() - os.path.getmtime(unit_path) > DEFAULT_MAX_RUNTIME + 120:
                            _sh(["systemctl", "--user", "stop", unit])
                    except OSError:
                        pass
                    continue
            blocked_file = os.path.exists(os.path.join(rdir, "BLOCKED.txt"))
            if engine == "sdk":
                # sentinel-driven: done.json (written LAST, atomically) means result.json is well-formed.
                done_present = os.path.exists(os.path.join(rdir, "done.json"))
                res = None
                if done_present:
                    try:
                        res = json.load(open(rpath))
                    except (ValueError, OSError):
                        res = None
                status, summary = classify_sdk(done_present=done_present, unit_terminal=terminal,
                                               result=res, blocked_present=blocked_file)
                if status == "running":                     # done absent + non-terminal (defensive)
                    continue
                if status == "crashed":
                    status, summary, res = "blocked", "job crashed - no usable result produced", None
                _write_report(rdir, jid, status, res)
            else:                                           # CLI: parse defensively (shell `> result.json`
                res = None                                  # can leave an empty/partial file on a kill)
                if os.path.exists(rpath) and os.path.getsize(rpath) > 0:
                    try:
                        res = json.load(open(rpath))
                    except ValueError:
                        res = None
                if res is not None:
                    status = "blocked" if (blocked_file or res.get("is_error")) else "done"
                    summary = (res.get("result") or "")[:200]
                else:
                    status, summary = "blocked", "job crashed - no usable result produced"
                _write_report(rdir, jid, status, res)
            os.rename(rdir, os.path.join(STATE, status, jid))
            _commit_push(f"{status} {jid}")                 # durable state first
            try:
                notify.notify(status=status, job_id=jid, summary=summary)
            except Exception:
                pass                                        # notify is best-effort
        except Exception:
            import traceback
            with open(os.path.join(STATE, "tick.log"), "a") as f:
                f.write(f"poll error for {jid}:\n{traceback.format_exc()}\n")
            continue                                        # one bad entry never wedges the loop


if __name__ == "__main__":
    import fcntl
    lock = open(os.path.join(STATE, ".tick.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)   # another tick is already running
    try:
        run_tick()
    except Exception:
        import traceback
        with open(os.path.join(STATE, "tick.log"), "a") as f:
            f.write(traceback.format_exc() + "\n")
        sys.exit(0)   # log + exit clean so the systemd service/timer never enters a failed state
