# baton — Setup & Usage

> **The easy path: `./setup.sh`** — one command. It asks the role (worker / producer / both) and does it: **worker** setup is admin-run (creates the `baton` user, installs claude+gh, writes your PAT + a Claude `setup-token`, deploys the root-owned fence, starts the timer, runs `doctor`); **producer** installs the handoff + add-project skills. The sections below are the manual / reference version.

## Roles

- **Producer** — your laptop. Runs the `handoff` skill.
- **Worker** — an always-on box (Raspberry Pi, server). Runs jobs as a dedicated, unprivileged **`baton`** user.

A machine can be both. They share only a **git remote** (the instance repo). No peer-to-peer.

## Prerequisites

- `git`; **Claude Code** + a Claude subscription on the worker; **Python 3**; a Linux + systemd worker.
- GitHub adapter: the `gh` CLI and a **fine-grained PAT** (below).
- Both machines clone the same **private instance repo** (this repo). Secrets live in `~/.baton.env` (gitignored) — never committed.

## 1. Mint the worker's GitHub token (fine-grained PAT)

GitHub → *Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate*:

- **Resource owner:** your org/account.
- **Repository access:** *Only select repositories* → your project's repos.
- **Permissions:** **Contents: Read and write**, **Pull requests: Read and write**, Metadata: Read (auto). **No Administration, Workflows, or Secrets.**
- Set an expiry.

Check it before using it:
```bash
GH_TOKEN=<pat> ~/baton/bin/baton token       # reports fine-grained/acceptable, or warns + lists dangerous scopes
```
Give baton's git the PAT over HTTPS (as the `baton` user):
```bash
git config --global credential.helper store
printf 'https://baton:%s@github.com\n' "<pat>" > ~/.git-credentials && chmod 600 ~/.git-credentials
```

## 2. Worker setup (the always-on box)

> Easiest: **`./setup.sh`** (choose *worker*) does all of §2 in one command. The steps below are the reference for what it does.

Create the dedicated, unprivileged user (this is what bounds the blast radius — jobs never run as *you*):
```bash
sudo useradd -m baton
sudo loginctl enable-linger baton          # so the timer survives logout/reboot
```
As the `baton` user:
```bash
git clone <instance-repo> ~/baton
cp ~/baton/.baton.env.template ~/.baton.env && chmod 600 ~/.baton.env   # fill GH_TOKEN + CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`); optional SMTP_*
# enable the drain timer (or just let ./setup.sh do this + the fence below):
mkdir -p ~/.config/systemd/user && cp ~/baton/systemd/baton-tick.* ~/.config/systemd/user/ && systemctl --user enable --now baton-tick.timer
```
Deploy the **root-owned fence** (one-time, needs sudo — the worker cannot edit these):
```bash
sudo install -D -m0755 /home/baton/baton/guard/guard.py            /opt/baton/guard/guard.py
sudo install -D -m0644 /home/baton/baton/profile/denied.json       /opt/baton/denied.json   # global soft denied list (project-independent)
sudo install -D -m0644 /home/baton/baton/profile/managed-settings.json /etc/claude-code/managed-settings.json
printf 'baton' | sudo tee /opt/baton/worker-user >/dev/null   # the guard self-gates to this OS user
```
Verify — a node is **inert until this is all green**:
```bash
~/baton/bin/baton doctor worker
```

## 3. (Optional) branch protection

baton's model is wip-branch + **draft PR** — the agent opens PRs and never pushes to `main`, and the non-admin PAT can't merge. So you don't *need* branch protection. If you want a hard server-side guarantee anyway, protect the default branch via *Settings → Rules → Rulesets* (restrict updates to `main`, bypass = org admins) or `gh api`. Note it's **unavailable on free private repos** (GitHub Pro / org / public only) — which is exactly why baton doesn't rely on it.

## 4. Producer setup (your laptop)

```bash
git clone <instance-repo> ~/baton
./baton/setup.sh                   # choose 'producer' — installs the handoff + add-project skills into ~/.claude/skills/
# (or by hand:  for s in handoff add-project; do mkdir -p ~/.claude/skills/$s && cp ~/baton/engine/skill/$s/* ~/.claude/skills/$s/; done)
```

## 5. Add a project

**Easiest:** in a Claude Code session from inside the project's repo, say **"add this project"**. The
`add-project` skill infers the name / `owner` / default branch from the repo, proposes the worker path
+ model, writes `projects/<name>.json`, commits, and pushes. The worker **auto-clones the repo on the
first handoff** — *nothing to run on the Pi.* (The fence is project-independent — §3 — so there's no
`/opt/baton` step per project.)

Under the hood it's just a config file (no engine changes). Manual equivalent:

1. Write `projects/<name>.json`: `host`, `owner` (GitHub org/user — used to auto-clone), `roots`
   (laptop + worker paths), `default_branch`, `protected_branches`, `never_mirror`, `default_model`.
   Single-repo: point `roots` at the repo. Polyrepo: point `roots` at the parent and add a `repos` list.
2. Commit + push. The worker pulls it on the next tick and auto-clones the repo(s) on the first job.
3. `baton doctor worker` → green.

## 6. Hand off work

In any Claude Code session on your laptop, say **"hand this off"** (or `/handoff`). The skill will:
- resolve the project, **propose the in-scope repos** (you confirm — it never sweeps everything),
- **propose model + effort** (`sonnet`/`opus` × `low|medium|high|xhigh|max`; override freely),
- write a brief, commit + push your in-progress work to `wip/handoff-<id>`, and queue the job.

The worker picks it up within ~90s. **Fresh task** = just a brief; **mid-task** = your uncommitted changes travel on the wip branch. Default leans Sonnet (cheaper against the monthly autonomous-credit pool).

## 7. Monitor & resume

- **GitHub emails you** about the draft PR (done) or the `[BLOCKED]` draft PR (blocked) — enable *"email about your own activity"* in GitHub notification settings, since the worker opens PRs as your account.
- **Optional direct email:** if you'd rather not enable that (global) GitHub setting, set `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASS` + `NOTIFY_EMAIL` in `~/.baton.env` and baton will also email you on done/blocked. Unset = GitHub-native only.
- Full report: `done/<id>/report.md` or `blocked/<id>/report.md` in the instance repo (pull to read).
- **Done** → review the draft PR. **Blocked** → the report states the question; answer by re-handing-off.

## 8. Pause / uninstall

- **Pause** (keep everything installed): `systemctl --user disable --now baton-tick.timer`.
- **Full decommission (one admin command):** `./teardown.sh` — stops the worker first, then removes the root-owned fence and the `baton` account + its home (clones, `~/.baton.env`, creds). The reverse of `setup.sh`. **Revoke the PAT on GitHub afterward.**
- **Lighter / non-destructive:** `./teardown.sh --soft` — just stops the drain timer + deregisters the node (keeps the user, fence, clones). Neither touches your real repos or your own account.

## 9. Troubleshooting

- `baton doctor worker` — re-run anytime; lists exactly what's failing.
- `GH_TOKEN=<pat> baton token` — confirm the token isn't over-privileged.
- Job stuck queued? `systemctl --user status baton-tick.timer` (as `baton`).
- No email? Either enable *"email about your own activity"* at `github.com/settings/notifications` (GitHub-native — the worker opens PRs as your account), or set `SMTP_*` + `NOTIFY_EMAIL` in `~/.baton.env` for a direct email.

## Security model (recap)

baton **confines effects, not commands** — the agent may run anything and reach any host; the blast radius is bounded by layers:

- Jobs run in an **OS sandbox** (a confined `systemd` service): file writes restricted to the repo + job dir, the rest of the machine (incl. the `/opt/baton` fence) read-only, no privilege escalation, capped memory + PIDs. `doctor` smoke-tests that the confinement actually works on the host.
- As the unprivileged **`baton`** user — **you are never fenced**; SSH in and you keep full control.
- A **scoped non-admin PAT** (opens PRs, can't merge or change settings) + the **wip-branch + draft-PR workflow** (the agent opens PRs; it never pushes to `main`) + **no cloud credentials** on the box.
- A slim root-owned `PreToolUse` guard adds a **read-only allowlist for MCP tools** (the one thing the OS sandbox can't reach). The worker can't disable or edit its own fence (root-owned `/opt/baton` + `/etc/claude-code`).

Network is intentionally **open** (jobs can fetch/scrape/install). Honest residual: exfiltration isn't *prevented*, only bounded by the low-privilege token + absent cloud secrets. *(Why not classify commands instead? We tried — an autonomous agent can always phrase around a text classifier.)*
