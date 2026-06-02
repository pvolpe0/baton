# Contributing

Thanks for your interest in baton! It's a small, focused codebase — contributions that keep it that
way are very welcome.

## Architecture in a paragraph

A **producer** (your laptop) hands a task off to a **worker** (an always-on Linux box). They share
only a **git remote**: the producer writes a job to a git-backed queue (and, for mid-task work, pushes
your uncommitted changes on a `wip/` branch); the worker drains the queue on a `systemd` timer, runs
`claude -p` headless inside an OS sandbox, and opens a draft PR. The fence **confines effects, not
commands** — see [SECURITY.md](SECURITY.md). For the full walkthrough see
[`instructions.md`](instructions.md), and the repo layout in the [README](README.md).

## Dev setup

```bash
git clone <your-fork> baton && cd baton
python3 -m pytest          # the suite is pure-Python, no extra deps
```

The pure decision logic (queue ordering, manifest, guard classification, doctor parsers) is
unit-tested. The side-effecting worker orchestration (launching the sandboxed job, git sync) is
verified live on a real worker rather than mocked — please match that split when adding code.

## Conventions

- Keep changes small and focused; add a test for new behavior wherever it's unit-testable.
- Match the surrounding style — concise comments that explain *why*, not *what*.
- Open a PR against `main`, and run `python3 -m pytest` first.
