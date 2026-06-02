# Security Policy

baton runs an autonomous coding agent on your own hardware. Its security model is **confine the
*effects*, not the *commands*** — the agent may run any command and reach any host; what's bounded is
the blast radius.

## The model

Each job runs inside layered containment:

- **OS sandbox** — a confined transient `systemd` service: file writes are restricted to the project
  repo + the job's state dir; the rest of the filesystem (including baton's own root-owned fence) is
  read-only; privilege escalation is blocked (`NoNewPrivileges`); `/tmp` is private; and memory, PIDs,
  and runtime are capped.
- **Unprivileged user** — jobs run as a dedicated `baton` user, never your account.
- **Scoped, non-admin credential** — a fine-grained GitHub PAT limited to Contents + Pull requests on
  your repos. It cannot merge PRs, change settings, or reach other repos.
- **No cloud credentials** on the box — deploys / DB access have nothing to authenticate with.
- **wip-branch + draft-PR workflow** — the agent opens a draft PR for you to review; it never pushes
  to `main`.
- **MCP read-only allowlist** — a root-owned `PreToolUse` guard denies external (MCP) mutations, the
  one class of effect the OS sandbox can't reach.

## Honest residual

The network is intentionally **open** (so jobs can fetch dependencies, call APIs, scrape). That means
data **exfiltration is not _prevented_** — only bounded by the low-privilege token and the absence of
cloud secrets on the box. If you need egress locked down, an opt-in network allowlist is on the
roadmap. Run baton on hardware you own, with a repo-scoped PAT, and review the draft PRs it opens.

## Reporting a vulnerability

Please report security issues **privately** via GitHub's **Security → Report a vulnerability** (private
advisory) on this repository, rather than opening a public issue. Include reproduction steps and the
impact you observe. Thanks for helping keep baton safe.
