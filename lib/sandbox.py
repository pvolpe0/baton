"""OS-confinement properties for a job's transient systemd unit — factored out so tick.py (which
LAUNCHES jobs) and doctor (which PROBES the writable set) build the SAME sandbox, and so the writable
set is unit-tested and can't silently drift.

A job may run any command and reach any host, but its file WRITES are confined to exactly: the
project repo, its own job dir (running/<jid>), and the agent's own state/cache dirs. Crucially the
writable set does NOT include the engine clone or the rest of the state tree — so a job cannot edit
the code that the next (unconfined) tick executes. That is the latent fence-escape the SDK design
pass surfaced; narrowing the set here is what closes it on the current CLI worker."""
import os

# Home subtrees the agent's tooling (claude/npm/pip/gh/git) must write so caches don't fail.
CACHE_DIRS = (".claude", ".config", ".cache", ".local", ".npm")

# Subtrees INSIDE the writable cache set that hold code/units executed OUTSIDE the job sandbox and so
# must NOT be job-writable. ~/.config is granted for tool config (gh/git), but ~/.config/systemd/user
# holds the UNCONFINED drain timer's own unit — a job that could rewrite its ExecStart (or drop a new
# enabled unit) would re-introduce the exact unconfined-next-tick escape this narrowing closes. We
# re-protect just that subtree read-only; systemd applies the deeper path over the ~/.config rw mount.
PROTECTED_SUBPATHS = (".config/systemd",)

# Static confinement props, independent of the job. ProtectSystem=strict mounts the FS read-only;
# ProtectHome=read-only is kept explicitly (on some systemd builds strict alone left /home writable
# — verified empirically on the Pi). ReadWritePaths then re-opens exactly the writable set.
BASE_PROPS = ("ProtectSystem=strict", "ProtectHome=read-only", "NoNewPrivileges=yes",
              "PrivateTmp=yes", "RestrictSUIDSGID=yes")


def cache_paths(home):
    """The agent cache dirs under the worker's OS home."""
    return [os.path.join(home, d) for d in CACHE_DIRS]


def ensure_cache_dirs(home):
    """Create the agent cache dirs if absent. They go in the job's ReadWritePaths, and systemd-run
    fails the unit if a ReadWritePaths entry doesn't exist — so on a fresh worker (where e.g. ~/.npm
    hasn't been created yet) both the real launcher AND the doctor probe must make them first."""
    for p in cache_paths(home):
        try:
            os.makedirs(p, exist_ok=True)
        except OSError:
            pass


def protected_paths(home):
    """Subtrees re-protected read-only even though their parent is in the writable set (the drain
    timer's user unit dir). Kept out of a job's reach so it can't repoint the next unconfined tick."""
    return [os.path.join(home, p) for p in PROTECTED_SUBPATHS]


def _is_ancestor(a, b):
    """True if dir `a` strictly contains `b`."""
    try:
        return os.path.commonpath([a, b]) == a and a != b
    except ValueError:                       # different roots / mixed absolute+relative
        return False


def proot_conflict(proot, *, state, code_root, home):
    """A project root goes verbatim into the job's ReadWritePaths, so it must not re-open a protected
    location. Returns a human label for the location it collides with, or '' if safe. proot UNDER the
    home is normal (e.g. ~/work/repo); the danger is proot being the home root, or equalling /
    containing / sitting inside the engine deploy (code_root) or the state clone."""
    rp, hp = os.path.realpath(proot), os.path.realpath(home)
    if rp == hp or _is_ancestor(rp, hp):
        return f"the home root ({hp})"
    for label, other in (("the engine deploy", code_root), ("the state clone", state)):
        op = os.path.realpath(other)
        if rp == op or _is_ancestor(rp, op) or _is_ancestor(op, rp):
            return f"{label} ({op})"
    return ""


def rw_paths(proot, jobdir, home):
    """ReadWritePaths for a job: the repo, THIS job's dir only (not queue/done/blocked/.git or the
    engine clone), and the agent caches. `jobdir` is running/<jid> — the only state subtree a job may
    write (its result/sentinel; without it a successful job false-crashes writing result.json)."""
    return [proot, jobdir, *cache_paths(home)]


def job_properties(*, proot, jobdir, home, max_runtime, tasks_max, mem_max=None, envfile=None):
    """The full `--property=...` list for `systemd-run` (pure → unit-tested, and identical between
    the launcher and the doctor probe). `envfile` is included only when non-None; the caller decides
    presence so this stays pure."""
    props = [f"--property={p}" for p in BASE_PROPS]
    props.append(f"--property=RuntimeMaxSec={max_runtime}")
    props.append(f"--property=WorkingDirectory={proot}")
    props += [f"--property=ReadWritePaths={p}" for p in rw_paths(proot, jobdir, home)]
    # re-protect the user-unit dir read-only (deeper path wins over the ~/.config rw mount); the `-`
    # prefix makes systemd ignore it if absent. Closes the ~/.config/systemd unconfined-next-tick hole.
    props += [f"--property=ReadOnlyPaths=-{p}" for p in protected_paths(home)]
    props.append(f"--property=TasksMax={tasks_max}")
    if mem_max:
        props.append(f"--property=MemoryMax={mem_max}")
    if envfile:
        props.append(f"--property=EnvironmentFile={envfile}")
    return props
