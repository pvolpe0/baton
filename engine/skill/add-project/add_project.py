#!/usr/bin/env python3
"""baton add-project helper (Mac producer). The add-project skill calls this AFTER confirming the
fields with the user. It writes projects/<name>.json into the instance repo, commits, and pushes;
the worker auto-clones the repo on the first handoff (the fence is project-independent, so there is
NO manual /opt/baton step). Mirrors handoff.py (sh, push_with_rebase)."""
import argparse, json, os, subprocess, sys

HOME = os.environ.get("BATON_HOME", os.path.expanduser("~/baton"))


def sh(args, cwd=None):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)


def push_with_rebase(cwd, tries=4):
    """The worker also pushes job-state transitions to this same branch, so origin may have advanced
    since our last pull. Rebase our commit on top and retry, instead of a bare push that rejects."""
    last = None
    for _ in range(tries):
        subprocess.run(["git", "pull", "--rebase", "--autostash"], cwd=cwd, capture_output=True, text=True)
        last = subprocess.run(["git", "push"], cwd=cwd, capture_output=True, text=True)
        if last.returncode == 0:
            return
    raise RuntimeError(f"git push failed after {tries} rebase attempts: {last.stderr if last else ''}")


def build_config(*, name, owner, mac_root, pi_root, default_branch, model, repos, never_mirror, host):
    """The project config the worker reads. `owner` lets the worker auto-clone; `repos` (polyrepo only)
    lists member repos under the parent root — omitted for a single-repo project. No denied_commands:
    the soft guardrail is global now (/opt/baton/denied.json), not per-project."""
    cfg = {
        "name": name,
        "host": host,
        "owner": owner,
        "roots": {"mac": mac_root, "pi": pi_root},
        "default_branch": default_branch,
        "protected_branches": [default_branch],
        "never_mirror": never_mirror,
        "default_model": model,
    }
    if repos:
        cfg["repos"] = repos
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--owner", required=True, help="GitHub org/user (used to auto-clone on the worker)")
    ap.add_argument("--mac-root", required=True)
    ap.add_argument("--pi-root", required=True)
    ap.add_argument("--default-branch", default="main")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--host", default="github")
    ap.add_argument("--repo", action="append", default=[], help="polyrepo member (repeatable); omit for single-repo")
    ap.add_argument("--never-mirror", action="append", default=[])
    ap.add_argument("--force", action="store_true", help="overwrite an existing project config")
    a = ap.parse_args()

    path = os.path.join(HOME, "projects", a.name + ".json")
    if os.path.exists(path) and not a.force:
        print(f"ERROR: projects/{a.name}.json already exists (use --force to overwrite)", file=sys.stderr)
        sys.exit(2)

    cfg = build_config(name=a.name, owner=a.owner, mac_root=a.mac_root, pi_root=a.pi_root,
                       default_branch=a.default_branch, model=a.model, repos=a.repo,
                       never_mirror=a.never_mirror, host=a.host)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
        f.write("\n")
    sh(["git", "add", "-A"], cwd=HOME)
    sh(["git", "commit", "-m", f"add project {a.name}"], cwd=HOME)
    push_with_rebase(HOME)
    print(f"added project '{a.name}' (owner {a.owner}) -> projects/{a.name}.json; "
          f"the worker auto-clones {a.owner}/{os.path.basename(a.pi_root)} on the first handoff")


if __name__ == "__main__":
    main()
