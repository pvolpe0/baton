"""Handoff manifest: the per-job contract. Carries model/effort (per-handoff) and the
capabilities seam (empty in v1). mode='continue' carries your uncommitted work as a
pushed wip branch + base SHA; mode='fresh' is just a brief."""
import json, os, secrets


def new_id(now):
    """now: UTC stamp like 20260601T1432Z. Globally-unique, never-reused id."""
    return f"{now}-{secrets.token_hex(2)}"


def build(*, id, project, model, effort, mode, repos, created_at, engine="sdk", max_turns=None):
    return {
        "id": id,
        "project": project,
        "model": model,            # "sonnet" | "opus"
        "effort": effort,          # low | medium | high | xhigh | max
        "mode": mode,              # "fresh" | "continue"
        "repos": repos,            # [{repo, wip_branch, base_sha}]
        "engine": engine,          # "sdk" (in-process Agent SDK worker.py, default) | "cli" (claude -p, fallback)
        "max_turns": max_turns,    # optional SDK turn cap (None = unlimited)
        "capabilities": [],        # growth seam — empty in v1
        "created_at": created_at,
    }


def write(path, m):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(m, f, indent=2, sort_keys=True)


def read(path):
    with open(path) as f:
        return json.load(f)
