# 🪄 baton

[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE) [![Built with Claude Code](https://img.shields.io/badge/built%20with-Claude%20Code-d97757.svg)](https://claude.com/claude-code) ![Platform: Linux + systemd](https://img.shields.io/badge/platform-Linux%20%2B%20systemd-informational.svg) ![Python 3](https://img.shields.io/badge/python-3-blue.svg)

**An always-on agent that takes handoffs of your in-progress work and finishes them on your own hardware.**

You're coding on your laptop. You say *"hand this off."* Your work — including your *uncommitted, mid-task* changes — travels to an always-on box you own (a Raspberry Pi, a home server). A Claude Code agent there finishes the task autonomously, opens a draft PR, and pings you. You can close your laptop.

```
  laptop ──"hand this off"──▶  git (queue + wip branch)  ──▶  worker runs it autonomously
    ▲                                                              │
    └────────  GitHub PR email · review the draft PR  ◀──────────┘
```

## Why

Long-running coding tasks pin you to your laptop — you can't close the lid without killing the work. And the usual way to offload a task means starting it over from a clean checkout on infrastructure you don't control. baton takes a different path: it moves the work you're **in the middle of** — uncommitted changes and all — onto an always-on machine **you own**, finishes it autonomously, and opens a PR for review. Close your laptop; the work keeps going, on your hardware, on your terms.

## What you get

- **Mid-task handoff** — your *uncommitted* work travels (via a `wip/` branch), not just a task description.
- **Your hardware** — your code and data stay on a box you control.
- **Autonomous + sandboxed** — it runs to completion unattended inside an OS sandbox that confines what it can *affect* (below), without restricting what it can *run*.
- **Git-coordinated** — nodes talk only through a shared git remote; no peer-to-peer, no extra infra.
- **Host-pluggable** — GitHub today; the core handoff is plain git, so GitLab/Gitea/bare-repo are adapter additions.

## How it works

1. On your laptop, the `handoff` skill packages a brief + (for mid-task work) commits and pushes your changes to a `wip/handoff-<id>` branch, and writes a job to a git-backed queue.
2. The worker drains the queue (a systemd timer), reproduces the state, and runs the agent headless with your chosen **model + effort** (engine: the in-process Claude Agent SDK by default, or the `claude -p` CLI as a fallback).
3. It works to completion inside the OS sandbox, opens a **draft PR**, and writes a report.
4. **GitHub emails you about the PR** (done — or a `[BLOCKED]` draft PR if it's stuck); you review it. No notifier to set up — notifications are GitHub-native (enable "email about your own activity" once, since the worker opens PRs as your account). *Optional:* for a direct email instead, set just your address + an app password in `~/.baton.env` (`SMTP_HOST`/`SMTP_USER`/`SMTP_PASS`; From and To default to your address) and baton emails you on done/blocked.

## Architecture

- **Producer** (your laptop) and **Worker** (the always-on box) are roles; a machine can be either or both. They share only a **git remote**.
- **Two identities on the worker:** jobs run as a dedicated, unprivileged **`baton`** user — never your account. *You* keep full manual control when you SSH in; the fence applies only to `baton`.
- **The fence confines *effects*, not commands.** The agent may run any command and reach any host; it's the *blast radius* that's bounded, by layers: each job runs in an **OS sandbox** (a confined `systemd` service — file writes restricted to the project + job dir, the rest of the machine read-only, no privilege escalation) · the unprivileged **`baton`** user · a **scoped, non-admin git credential** (no admin, can't merge PRs or change settings) · **no cloud credentials** on the box · the **wip-branch + draft-PR workflow** (the agent opens PRs; it doesn't push to `main`). A slim `PreToolUse` guard adds a read-only allowlist for external (MCP) tools — the one thing the OS sandbox can't reach. *(We tried statically classifying every command instead; an autonomous agent can always phrase around a text classifier, so we confine at the OS layer.)*
- **Grows by extending, not rewriting:** per-job options live in the manifest (`model`, `effort`, `engine` — the `claude -p` CLI or the in-process Agent SDK — and an empty `capabilities` seam); projects are config files; the git host is an adapter.
- **Two worker engines, one fence.** A job runs either the in-process **Claude Agent SDK** worker (the default — it writes a structured result + a `done.json` completion sentinel and turns a timeout-kill into a typed diagnostic) or the `claude -p` CLI (`manifest.engine = cli`, the fallback). Both run inside the *same* OS sandbox under the *same* root-owned `PreToolUse` guard hook — the engine is an implementation detail, not a trust boundary.

## Requirements

- **git** + a remote both machines can reach (GitHub for v1).
- **Claude Code** + a Claude subscription (Pro/Max) on the worker.
- **Python 3** and an always-on worker — **Linux + systemd** (systemd itself provides the job sandbox; no extra packages to install).
- For the GitHub adapter: the `gh` CLI and a **fine-grained PAT** scoped to your repos (Contents + Pull requests; **no admin** — so the worker can open PRs but not merge them or change settings).

## Quickstart

One interactive command, on each machine:

```bash
git clone <your-repo> baton && cd baton
./setup.sh        # detects role, guides setup, validates each step, ends ready
```

**One script, both roles.** `setup.sh` asks what this machine is (worker / producer / both) and does it:
- **Worker** (admin / `sudo`): creates the dedicated `baton` user, installs Claude Code + `gh` for it, writes your GitHub PAT + a Claude auth token, deploys the root-owned sandbox + drain timer, runs `doctor`. You provide a fine-grained PAT and a Claude token (from `claude setup-token` — one browser login on any machine; no `/login` on the worker).
- **Producer**: installs the `hand this off` + `add this project` skills into your `~/.claude`.

`./teardown.sh` is the one-command reverse (`--soft` to just pause). Non-interactive: `BATON_ROLE=worker BATON_PAT=… BATON_CLAUDE_TOKEN=… ./setup.sh`.

Then, in any Claude Code session on your laptop:

```
add this project                          # one-time per repo: register it (the worker auto-clones it on first use)
hand this off: <what you want finished>   # send the current work to the worker
```

For the manual / reference walkthrough (what `setup.sh` does under the hood, adding a project, optional branch protection), see **[instructions.md](instructions.md)**.

## Safety — the boundary

baton's safety model is **confine the effects, not the commands.** The agent is semi-trusted (it's Claude doing your work), so the job is to bound what a mistake or an off-the-rails run can *affect* — not to police what it types. Concretely, a job:

- **Can't write outside the project** — file writes are confined to the repo + its job dir (OS sandbox); the rest of the machine, including baton's own fence, is read-only.
- **Can't escalate or run as you** — runs as the unprivileged `baton` user with `NoNewPrivileges`; never your account.
- **Can't merge or push to `main`** — its deliverable is a **draft PR** from a `wip/` branch; you review and merge. (It can't merge PRs or change repo settings — the credential is non-admin.)
- **Can't reach prod or the cloud** — there are no cloud credentials on the box, so deploys/DB-touches simply have nothing to authenticate with.
- **Won't mutate external systems** — MCP tools are restricted to a read-only allowlist (no creating/closing issues, sending mail, etc.) unless you opt in per project.

**What it *can* do, by design:** run any command and reach any website (network is open, so it can fetch deps, call APIs, scrape) — because the OS sandbox makes that safe. The honest residual: an open network plus readable repo credentials means data exfiltration isn't *prevented* (bounded by the scoped, low-privilege token + no cloud secrets). If you want egress locked down, an opt-in network allowlist is on the roadmap.

If a job is genuinely blocked, it stops, writes a reason, and notifies you.

## Status

**v1 — "author & PR".** The worker writes code, runs tests, and opens draft PRs — inside an OS sandbox, as an unprivileged user. Code work only. Deferred (by design): applying DB migrations / any prod-touching action, an opt-in network egress allowlist, a web portal, multi-worker.

## Repo layout

```
bin/baton            CLI: doctor · install · token
setup.sh             one command: worker (admin) and/or producer   teardown.sh   one-command reverse (--soft to pause)
lib/                 manifest (per-job contract) · doctor (fence + writable-set / engine-immutability checks) · sandbox (job confinement props) · nodes (registry)
guard/guard.py       slim PreToolUse hook: MCP read-only allowlist + soft denied-command guardrail
runner/              tick (drain · auto-clone · launch · poll · CLI/SDK engine branch) · worker (in-process Agent SDK job) · notify (GitHub-native + optional SMTP)
engine/skill/        laptop-side skills: handoff/ (run work) · add-project/ (register a repo)
profile/             worker-charter (system prompt) · managed-settings (fence wiring) · denied.json (global soft denied list)
projects/            per-project config (owner, paths, protected branches, host) — written by "add this project"
systemd/             the worker's drain timer
queue/ running/ done/ blocked/    git-backed job state
```

## License

MIT — see [LICENSE](LICENSE).
