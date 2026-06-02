"""Pure parsers + summary for `baton doctor`. The command orchestration (running
ssh/aws/loginctl/gh) lives in bin/baton; these pieces are pure so they're unit-tested."""
import json, os


# Canonical fence locations on a deployed worker (root-owned; the worker user can't edit them).
FENCE_PATHS = {
    "managed-settings": "/etc/claude-code/managed-settings.json",
    "guard": "/opt/baton/guard/guard.py",
    "worker-user": "/opt/baton/worker-user",
}


def verify_fence(run_user, paths=None):
    """Check the safety fence is actually in place AND immutable by the run user. Returns a list of
    (name, ok, detail). The fence is the linchpin of the threat model, so doctor must verify the
    guard + worker-user file exist and are NOT writable by the worker — not just managed-settings —
    and that worker-user names THIS user (else guard.py self-gates off and silently no-ops)."""
    paths = paths or FENCE_PATHS
    checks = []
    for name in ("managed-settings", "guard", "worker-user"):
        p = paths[name]
        present = os.path.exists(p)
        immutable = present and not os.access(p, os.W_OK)   # run user must not be able to edit it
        detail = p if (present and immutable) else ("missing" if not present else "writable by run user!")
        checks.append((f"fence:{name}", present and immutable, detail))
    try:
        wu = open(paths["worker-user"]).read().strip()
    except Exception:
        wu = ""
    checks.append(("fence:worker-user-matches", wu == run_user,
                   f"jobs run as '{run_user}'" if wu == run_user else f"worker-user '{wu}' != run user '{run_user}' — guard inactive!"))
    return checks


def fence_active(run_user, paths=None):
    """True only if every fence check passes — used to GATE job execution in tick.py so a worker
    can never run bypassPermissions jobs with the fence missing/inert."""
    return all(ok for _, ok, _ in verify_fence(run_user, paths))


# --- engine immutability ------------------------------------------------------
# EVERY engine module that runs UNCONFINED as the worker is deployed root-owned to /opt/baton and run
# from there. doctor + the per-tick gate assert each is present, root-owned, and NOT writable by the
# run user — AND so is its parent dir (write on the dir lets the worker delete+recreate the file even
# if it's 0644). Else a job (or anything running as the worker) could edit code the next tick executes
# unconfined while building the fence (the latent escape the SDK design surfaced).
ENGINE_RELPATHS = ("runner/tick.py", "runner/notify.py", "lib/doctor.py", "lib/sandbox.py",
                   "lib/manifest.py", "lib/nodes.py", "guard/guard.py", "bin/baton")


def engine_code_paths(base="/opt/baton"):
    """The engine files that run unconfined, under a deploy base (pass CODE on a worker so the gate
    verifies the tree tick ACTUALLY runs from, not a hardcoded one)."""
    return tuple(os.path.join(base, r) for r in ENGINE_RELPATHS)


ENGINE_CODE = engine_code_paths("/opt/baton")          # default deploy target (callers/tests)


def _engine_verdict(present, root_owned, writable, path):
    if not present:
        return (False, "missing")
    if writable:
        return (False, "writable by run user!")
    if not root_owned:
        return (False, "not root-owned!")
    return (True, path)


def _path_state(p):
    present = os.path.exists(p)
    try:
        root_owned = present and os.stat(p).st_uid == 0
    except OSError:
        root_owned = False
    writable = present and os.access(p, os.W_OK)
    return present, root_owned, writable


def _engine_name(p):
    return p.split("/opt/baton/")[-1] if "/opt/baton/" in p else os.path.join(
        os.path.basename(os.path.dirname(p)), os.path.basename(p))


def verify_engine_immutable(paths=ENGINE_CODE):
    """(name, ok, detail) for each engine file AND its parent dir: present + root-owned + not
    run-user-writable."""
    checks = [(f"engine:{_engine_name(p)}", *_engine_verdict(*_path_state(p), p)) for p in paths]
    for d in sorted({os.path.dirname(p) for p in paths}):
        checks.append((f"engine-dir:{_engine_name(d)}", *_engine_verdict(*_path_state(d), d)))
    return checks


def engine_immutable(paths=ENGINE_CODE):
    """True iff every engine file + parent dir is present, root-owned, and not writable by the run
    user. Cheap file checks — used to GATE job execution in tick.py (fail-closed)."""
    return all(ok for _, ok, _ in verify_engine_immutable(paths))


# --- writable-set probe (the live doctor probe parses this) -------------------
_PROBE_LEAK_TOKENS = ("STATE_OPEN", "OPT_OPEN", "HOME_OPEN", "CONFIG_SYSTEMD_OPEN", "ETC_OPEN")


def parse_writable_probe(output):
    """A throwaway confined unit (built with the REAL job props) prints JOBDIR_OK if it could write
    its job dir, and *_OPEN for every surface it should NOT have written. Confinement is correct iff
    it wrote ONLY its job dir."""
    leaked = [t for t in _PROBE_LEAK_TOKENS if t in output]
    if "JOBDIR_OK" not in output:
        return (False, "job could NOT write its own job dir — a real job would false-crash writing result.json")
    if leaked:
        return (False, "writable set TOO BROAD — job can write: " + ", ".join(leaked))
    return (True, "job writes confined to its job dir; engine/state/system read-only")


def parse_admin_access(iam_list_json):
    """True if AdministratorAccess is attached to the identity (dangerous on a worker)."""
    try:
        pols = json.loads(iam_list_json).get("AttachedPolicies", [])
    except Exception:
        return False
    return any(p.get("PolicyName") == "AdministratorAccess" for p in pols)


def parse_linger(loginctl_show):
    """True if systemd user lingering is enabled (worker survives logout/reboot)."""
    for line in loginctl_show.splitlines():
        if line.startswith("Linger="):
            return line.split("=", 1)[1].strip() == "yes"
    return False


def summarize(checks):
    """checks: list of (name, ok, detail). Returns (all_ok, printable_lines)."""
    lines = [f"[{'OK ' if ok else 'FAIL'}] {name}: {detail}" for name, ok, detail in checks]
    return (all(ok for _, ok, _ in checks), lines)


# --- GitHub token safety -------------------------------------------------------
# baton needs only contents:read/write + pull_requests:write + metadata:read.
# These classic scopes are dangerous for a worker (admin/CI/secrets/webhook/over-broad).
DANGEROUS_CLASSIC_SCOPES = {
    "repo",                                   # over-broad: full control incl. admin-ish
    "admin:org", "write:org",
    "workflow",                               # modify CI = arbitrary code + deploys
    "delete_repo",
    "admin:repo_hook", "write:repo_hook",     # webhooks = push exfiltration
    "admin:public_key", "admin:gpg_key",
    "write:packages", "delete:packages",
    "admin:enterprise",
}


def parse_classic_scopes(x_oauth_scopes_header):
    """Classic PATs return scopes in the X-OAuth-Scopes header (comma-separated)."""
    return [s.strip() for s in (x_oauth_scopes_header or "").split(",") if s.strip()]


def dangerous_classic(scopes):
    return sorted(set(scopes) & DANGEROUS_CLASSIC_SCOPES)


def repo_admin(repo_json):
    """True if the token has admin on the repo (GET /repos/{o}/{r} .permissions.admin) —
    dangerous: admin can disable the very branch protection that fences baton."""
    try:
        return bool(json.loads(repo_json).get("permissions", {}).get("admin"))
    except Exception:
        return False
