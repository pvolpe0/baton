---
name: add-project
description: Use when the user says "add this project" or "/add-project" — register the repo in the current directory as a baton project so it can be handed off to the worker. Writes the project config, commits, and pushes; the worker auto-clones the repo on the first handoff (no manual setup on the Pi).
---

# Add project

Register the current repo as a baton project. After this, "hand this off" works for it, and the
worker **auto-clones the repo on the first job** — nothing to run on the Pi (the fence is
project-independent). Steps:

1. **Gather from the current repo** (run in the user's cwd):
   - `git rev-parse --show-toplevel` → the repo root (the Mac root).
   - `git remote get-url origin` → parse `owner` and repo `name`. Handle both
     `git@github.com:owner/name.git` and `https://github.com/owner/name(.git)`.
   - `git symbolic-ref --short refs/remotes/origin/HEAD` (fallback: the current branch) → default branch.
   - Project `name` defaults to the repo name.

2. **Propose the config and confirm.** Show the user, and let them override any field:
   - `name`, `owner`, `default_branch`, `model` (default `sonnet`),
   - Mac root = the repo root, **Pi root = `/home/baton/work/<name>`** (the worker path),
   - single-repo (the default) vs polyrepo. Only treat it as polyrepo if the user says so; then ask for
     the member repo names (`--repo` each) and any `--never-mirror`.
   Do not invent values silently.

3. **Write it:**
   ```
   python3 ~/.claude/skills/add-project/add_project.py \
     --name <name> --owner <owner> \
     --mac-root <repo-root> --pi-root /home/baton/work/<name> \
     --default-branch <branch> --model <sonnet|opus> \
     [--repo <member> ...] [--never-mirror <repo> ...] [--force]
   ```
   It writes `projects/<name>.json`, commits, and pushes to the instance repo. It refuses to overwrite
   an existing project unless `--force`.

4. **Report**: the project is registered; the worker auto-clones `<owner>/<name>` on the first handoff
   (within ~90s of the first job) — nothing to run on the Pi. The user can now say "hand this off" to it.
