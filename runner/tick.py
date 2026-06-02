"""One drain tick (run by a systemd timer, as the `baton` user). Single job at a time.
- if a job is running: poll it; on exit, read result.json -> report -> notify
- else: pick the oldest queued job, reproduce its state, launch it headless and detached

Completion = the `claude -p` process exits and writes result.json (spike-confirmed).
The launched job runs as `baton` in a systemd transient scope so it survives the 90s tick."""
import json, os, pwd, shlex, subprocess, sys, time

HOME = os.environ.get("BATON_HOME", os.path.expanduser("~/baton"))
sys.path.insert(0, HOME)   # so `from lib import ...` / `from runner import ...` work when run as a script
CLAUDE = os.environ.get("BATON_CLAUDE", os.path.expanduser("~/.local/bin/claude"))
CHARTER = os.path.join(HOME, "profile", "worker-charter.md")
DEFAULT_MAX_RUNTIME = 3600          # seconds; a job scope past this is killed so it can't wedge the queue
DEFAULT_TASKS_MAX = 768             # PID cap per job (fork-bomb guard; generous for normal builds)
FENCE_MARKER = os.path.join(HOME, ".fence-down")   # local-only (gitignored): de-dupes the fence-down alert


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
def pick_next(home):
    q = os.path.join(home, "queue")
    ids = [d for d in os.listdir(q) if os.path.isdir(os.path.join(q, d))] if os.path.isdir(q) else []
    return sorted(ids)[0] if ids else None      # timestamp-prefixed ids sort chronologically


def has_running(home):
    r = os.path.join(home, "running")
    return os.path.isdir(r) and any(os.path.isdir(os.path.join(r, d)) for d in os.listdir(r))


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
    _git("add", "-A", cwd=HOME)
    _git("commit", "-m", msg, cwd=HOME)
    for _ in range(tries):
        if _git("pull", "--rebase", "--autostash", cwd=HOME).returncode != 0:
            _git("rebase", "--abort", cwd=HOME)   # a conflict leaves a detached HEAD mid-rebase;
            continue                              # abort so we never commit onto a dangling head
        if _git("push", cwd=HOME).returncode == 0:
            return
    # left committed locally on a clean branch; the next tick's recover+rebase-pull reconciles.


def _git_recover():
    """If a prior tick was interrupted mid-rebase (conflict, kill, reboot), the repo is left in
    .git/rebase-merge on a detached HEAD — and a bare commit there silently desyncs the worker
    from origin forever. Abort any in-progress rebase and get back onto the branch first."""
    g = os.path.join(HOME, ".git")
    if os.path.isdir(os.path.join(g, "rebase-merge")) or os.path.isdir(os.path.join(g, "rebase-apply")):
        _git("rebase", "--abort", cwd=HOME)
    if _git("rev-parse", "--abbrev-ref", "HEAD", cwd=HOME).stdout.strip() == "HEAD":
        _git("checkout", "main", cwd=HOME)


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
    os.rename(rdir, os.path.join(HOME, "blocked", jid))
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
    """Fence is missing/inert: log every tick, but notify only once (marker) to avoid 90s spam."""
    from lib import doctor as D
    failed = [f"{n} ({d})" for n, ok, d in D.verify_fence(run_user) if not ok]
    msg = "refusing to run jobs — safety fence not active: " + "; ".join(failed)
    with open(os.path.join(HOME, "tick.log"), "a") as f:
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
    if not D.fence_active(run_user):                       # NEVER run bypassPermissions jobs unfenced
        _fence_warn(run_user)
        return
    try:
        os.remove(FENCE_MARKER)                            # fence is back; re-arm the alert
    except OSError:
        pass
    _git_recover()                                      # heal a stuck/detached state from a prior tick
    _git("pull", "--rebase", "--autostash", cwd=HOME)   # --rebase (not --ff-only): a prior tick may
                                                        # have a locally-committed transition not yet pushed
    if has_running(HOME):
        _poll_running()
        return
    jid = pick_next(HOME)
    if not jid:
        return
    rdir = os.path.join(HOME, "running", jid)
    os.rename(os.path.join(HOME, "queue", jid), rdir)
    man = M.read(os.path.join(rdir, "manifest.json"))
    project = json.load(open(os.path.join(HOME, "projects", man["project"] + ".json")))
    try:
        ensure_repos(man, project)          # auto-clone first-seen repos so onboarding needs no manual clone
        if man["mode"] == "continue":
            reproduce_repos(man, project)
        else:
            _clean_base(project)            # fresh job: start from a clean default branch, not leftovers
    except Exception as e:                  # prep failed (no owner, clone/fetch error) — block cleanly with a
        _block_prep_failure(jid, rdir, e)   # real reason instead of launching into a missing/half-built repo
        return
    env = {**os.environ, "BATON_PROJECT": man["project"], "BATON_HOME": HOME}
    cmd = launch_cmd(man, claude=CLAUDE, brief_path=os.path.join(rdir, "brief.md"),
                     charter_path=CHARTER, blocked_path=os.path.join(rdir, "BLOCKED.txt"))
    base = f"baton-job-{jid}"
    inner = " ".join(shlex.quote(c) for c in cmd) + f" > {rdir}/result.json 2> {rdir}/err.txt"
    # record the unit name first (a transient SERVICE: `systemd-run --unit=B` -> `B.service`).
    open(os.path.join(rdir, "unit.txt"), "w").write(base + ".service")
    # CONTAINMENT (the fence): run the job as a confined transient service — NOT --scope, so the
    # systemd exec-sandbox actually applies. The agent may run ANY command and reach ANY host
    # (network is intentionally open), but the OS confines the *effects*: writes are restricted to
    # the repo + job dir + claude's own state, the rest of the filesystem is read-only, privilege
    # escalation is blocked, and /tmp is private. Damage is bounded without restricting the work.
    proot = project["roots"]["pi"]
    mr = man.get("max_runtime", DEFAULT_MAX_RUNTIME)       # systemd kills a runaway so it can't wedge the queue
    props = ["--property=ProtectSystem=strict", "--property=ProtectHome=read-only",
             "--property=NoNewPrivileges=yes", "--property=PrivateTmp=yes",
             "--property=RestrictSUIDSGID=yes", f"--property=RuntimeMaxSec={mr}",
             f"--property=WorkingDirectory={proot}"]
    # writable: the repo, the job-state dir, and the agent's own state/cache dirs (so npm/pip/gh/git
    # caches don't fail). Everything else — incl. /opt/baton (the fence) and the system — stays
    # read-only, so a bad command can't tamper the fence, persist, or touch the machine.
    rw = [proot, HOME] + [os.path.expanduser(d) for d in
                          ("~/.claude", "~/.config", "~/.cache", "~/.local", "~/.npm")]
    for p in rw[1:]:                                       # ensure home state/cache dirs exist (not proot)
        try:
            os.makedirs(p, exist_ok=True)
        except OSError:
            pass
    props += [f"--property=ReadWritePaths={p}" for p in rw]
    props.append(f"--property=TasksMax={man.get('max_tasks', DEFAULT_TASKS_MAX)}")   # fork-bomb guard
    _mm = man.get("max_memory") or _mem_max()              # so a runaway can't OOM the whole box
    if _mm:
        props.append(f"--property=MemoryMax={_mm}")
    envfile = os.path.expanduser("~/.baton.env")           # GH_TOKEN etc. — via EnvironmentFile, not argv
    if os.path.exists(envfile):
        props.append(f"--property=EnvironmentFile={envfile}")
    subprocess.Popen(["systemd-run", "--user", f"--unit={base}",
                      f"--setenv=BATON_PROJECT={man['project']}", f"--setenv=BATON_HOME={HOME}",
                      *props, "bash", "-lc", inner], env=env)
    _commit_push(f"start {jid}")


def _poll_running():
    """Finalize finished jobs. Robust to dead/malformed/orphaned entries so one bad
    running/ dir can never wedge the worker."""
    from runner import notify
    rroot = os.path.join(HOME, "running")
    if not os.path.isdir(rroot):
        return
    for jid in [d for d in os.listdir(rroot) if os.path.isdir(os.path.join(rroot, d))]:
        rdir = os.path.join(rroot, jid)
        try:
            unit_path = os.path.join(rdir, "unit.txt")
            rpath = os.path.join(rdir, "result.json")
            if os.path.exists(unit_path):
                # only finalize on a DEFINITIVE terminal state. is-active returns active/activating/
                # deactivating while the scope is alive, and "" on a transient D-Bus hiccup. Treat
                # every non-terminal value as "still running" (fail-closed) so we never rename
                # running/ out from under a live job. No unit.txt -> never tracked -> crash recovery.
                active = _sh(["systemctl", "--user", "is-active", open(unit_path).read().strip()]).stdout.strip()
                if active not in ("inactive", "failed"):
                    # watchdog: if RuntimeMaxSec somehow didn't fire, kill a scope that outlived the
                    # ceiling so one hung job can't wedge the queue forever. unit.txt mtime ~= launch.
                    try:
                        if time.time() - os.path.getmtime(unit_path) > DEFAULT_MAX_RUNTIME + 120:
                            _sh(["systemctl", "--user", "stop", open(unit_path).read().strip()])
                    except OSError:
                        pass
                    continue
            res = None                                      # parse the result defensively: a job killed
            if os.path.exists(rpath) and os.path.getsize(rpath) > 0:  # mid-run leaves an empty/partial
                try:                                        # result.json (the shell truncates it via
                    res = json.load(open(rpath))            # `> result.json` before claude writes), so
                except ValueError:                          # "exists" is not enough — it must parse.
                    res = None
            if res is not None:                             # normal completion
                status = "blocked" if (os.path.exists(os.path.join(rdir, "BLOCKED.txt")) or res.get("is_error")) else "done"
                summary = (res.get("result") or "")[:200]
                open(os.path.join(rdir, "report.md"), "w").write(
                    f"# {status} {jid}\n\ncost: ${res.get('total_cost_usd', 0):.4f}\n\n{res.get('result', '')}\n")
            else:                                           # missing/empty/corrupt result -> crashed
                status, summary = "blocked", "job crashed - no usable result produced"
                open(os.path.join(rdir, "report.md"), "w").write(
                    f"# crashed {jid}\n\nThe job ended without a usable result (killed, OOM, reboot, or never launched). Re-hand-off if needed.\n")
            os.rename(rdir, os.path.join(HOME, status, jid))
            _commit_push(f"{status} {jid}")                 # durable state first
            try:
                notify.notify(status=status, job_id=jid, summary=summary)
            except Exception:
                pass                                        # notify is best-effort
        except Exception:
            import traceback
            with open(os.path.join(HOME, "tick.log"), "a") as f:
                f.write(f"poll error for {jid}:\n{traceback.format_exc()}\n")
            continue                                        # one bad entry never wedges the loop


if __name__ == "__main__":
    import fcntl
    lock = open(os.path.join(HOME, ".tick.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)   # another tick is already running
    try:
        run_tick()
    except Exception:
        import traceback
        with open(os.path.join(HOME, "tick.log"), "a") as f:
            f.write(traceback.format_exc() + "\n")
        sys.exit(0)   # log + exit clean so the systemd service/timer never enters a failed state
