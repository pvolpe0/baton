#!/usr/bin/env python3
"""baton handoff helper (Mac producer). The handoff skill calls this AFTER it has
confirmed scope/model/effort with the user. It packages the brief + (continue-mode) the
in-progress work into the queue and pushes; the Pi's tick picks it up within ~90s.

No `git add -A` — only tracked-modified files are staged (-u). Untracked files the user
wants included must be `git add`ed by the skill explicitly before calling this."""
import argparse, datetime, json, os, subprocess, sys

HOME = os.environ.get("BATON_HOME", os.path.expanduser("~/baton"))
sys.path.insert(0, HOME)
from lib import manifest as M  # noqa: E402


def sh(args, cwd=None):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)


def push_with_rebase(cwd, tries=4):
    """The worker also pushes job-state transitions to this same branch, so origin may have
    advanced since our last pull. Rebase our commit on top and retry, instead of a bare push
    that rejects (non-fast-forward) and silently strands the queue entry."""
    last = None
    for _ in range(tries):
        subprocess.run(["git", "pull", "--rebase", "--autostash"], cwd=cwd, capture_output=True, text=True)
        last = subprocess.run(["git", "push"], cwd=cwd, capture_output=True, text=True)
        if last.returncode == 0:
            return
    raise RuntimeError(f"git push failed after {tries} rebase attempts: {last.stderr if last else ''}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="example")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--brief-file", required=True)
    ap.add_argument("--repo", action="append", default=[],
                    help="in-scope repo (repeatable); omit entirely for a fresh task")
    a = ap.parse_args()

    cfg = json.load(open(os.path.join(HOME, "projects", a.project + ".json")))
    now = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%MZ")
    jid = M.new_id(now=now)
    repos, mode = [], "fresh"
    for repo in a.repo:
        rd = os.path.join(cfg["roots"]["mac"], repo)
        orig = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=rd).stdout.strip()
        br = f"wip/handoff-{jid}"
        sh(["git", "checkout", "-b", br], cwd=rd)     # carry uncommitted work onto a fresh wip branch
        sh(["git", "add", "-u"], cwd=rd)
        sh(["git", "commit", "-m", f"baton handoff {jid}"], cwd=rd)
        sha = sh(["git", "rev-parse", "HEAD"], cwd=rd).stdout.strip()
        sh(["git", "push", "-u", "origin", br], cwd=rd)
        sh(["git", "checkout", orig], cwd=rd)          # leave the user's working branch as it was
        repos.append({"repo": repo, "wip_branch": br, "base_sha": sha, "base_branch": orig})
        mode = "continue"

    qd = os.path.join(HOME, "queue", jid)
    os.makedirs(qd)
    with open(os.path.join(qd, "brief.md"), "w") as f:
        f.write(open(a.brief_file).read())
    M.write(os.path.join(qd, "manifest.json"),
            M.build(id=jid, project=a.project, model=a.model, effort=a.effort,
                    mode=mode, repos=repos, created_at=now))
    sh(["git", "add", "-A"], cwd=HOME)
    sh(["git", "commit", "-m", f"queue {jid}"], cwd=HOME)
    push_with_rebase(HOME)
    print(jid)


if __name__ == "__main__":
    main()
