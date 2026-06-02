#!/usr/bin/env python3
"""baton confined SDK worker — the job entrypoint `systemd-run` launches (replacing
`bash -lc "claude -p … > result.json"`). Runs the Agent SDK `query()` loop in-process and writes a
structured result.json WE control, then done.json LAST as the completion sentinel. Run from /opt/baton
(root-owned, read-only) with `python3 -B`.

Fence (unchanged): the OS sandbox composes over the SDK + the bundled `claude` engine subprocess + every
tool subprocess, and the ROOT-OWNED managed-settings PreToolUse guard hook is the external-mutation
boundary. Verified empirically on the Pi: the SDK's bundled engine loads /etc/claude-code/
managed-settings.json, fires that hook, and a hook-DENY wins over `bypassPermissions`. (We do NOT pass
an in-process `can_use_tool` callback: SDK 0.2.87 requires streaming-mode input for it, and it was only
ever convenience/logging — the root-owned hook is the real and sufficient boundary.)

Importable without the SDK installed (the SDK import is deferred into `_run`, and env is read only in
`main`) so the pure helpers are unit-tested on any machine."""
import json, os, signal, time, traceback


def _atomic(rdir, name, obj):
    """Write rdir/name atomically (tmp + fsync + os.replace) so a reader never sees a partial file —
    this is what kills the result.json truncation race the CLI worker had."""
    tmp = os.path.join(rdir, name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, os.path.join(rdir, name))   # atomic on the same fs


def _log(rdir, o):
    o["ts"] = time.time()
    with open(os.path.join(rdir, "stream.log"), "a") as f:   # live progress visibility (the thing
        f.write(json.dumps(o) + "\n")                        # missing during the "hang" debugging)


def build_result(*, phase, r, sid, t0, blocked):
    """Pure: assemble the result.json dict from a ResultMessage-like object `r` (or None on error).
    Pure so it is unit-tested without the SDK; tick reads this typed, guaranteed-well-formed file."""
    return {
        "schema": 1,
        "phase": phase,                                          # "finished" | "errored"
        "is_error": bool(getattr(r, "is_error", True)) if r is not None else True,
        "blocked": bool(blocked),
        "result": (getattr(r, "result", "") or "") if r is not None else "",
        "session_id": sid,
        "total_cost_usd": getattr(r, "total_cost_usd", None) if r is not None else None,
        "num_turns": getattr(r, "num_turns", None) if r is not None else None,
        "wall_ms": int((time.time() - t0) * 1000),
    }


async def _run(rdir, proot, resume):
    """The SDK query() loop. Returns (session_id, ResultMessage|None)."""
    from claude_agent_sdk import query, ClaudeAgentOptions

    charter = open(os.environ["BATON_CHARTER"]).read()
    man = json.load(open(os.path.join(rdir, "manifest.json")))
    blocked_path = os.path.join(rdir, "BLOCKED.txt")

    prompt = (f"Read {os.path.join(rdir, 'brief.md')} and execute the task to completion. Work "
              f"autonomously; make reasonable assumptions; if genuinely blocked, write a one-line "
              f"reason to the file {blocked_path} (use that exact absolute path) and stop.")
    opts = ClaudeAgentOptions(
        system_prompt=charter, permission_mode="bypassPermissions", model=man["model"],
        effort=man.get("effort"), cwd=proot, resume=resume, max_turns=man.get("max_turns"))
    sid, r = None, None
    async for msg in query(prompt=prompt, options=opts):
        cls = type(msg).__name__
        if cls == "SystemMessage" and getattr(msg, "subtype", "") == "init":
            sid = (getattr(msg, "data", {}) or {}).get("session_id")
        elif cls == "ResultMessage":
            r = msg
            sid = sid or getattr(msg, "session_id", None)   # fallback: session_id on the result too
        else:
            _log(rdir, {"t": "msg", "kind": cls})
    return sid, r


def main():
    rdir = os.environ["BATON_RDIR"]                      # abs running/<jid> — the ONLY dir we write
    proot = os.environ["BATON_PROOT"]                    # project root == agent cwd
    resume = os.environ.get("BATON_RESUME") or None
    blocked = os.path.join(rdir, "BLOCKED.txt")
    t0 = time.time()

    def on_sigterm(signum, frame):
        """RuntimeMaxSec/`systemctl stop` sends SIGTERM (then SIGKILL after a grace period). Without a
        handler that is a BLANK kill (verified on the Pi). Write a typed errored result + the sentinel
        SYNCHRONOUSLY and hard-exit — don't await SDK cleanup (which can hang), so a timeout-kill still
        leaves a diagnostic instead of "crashed — no usable result"."""
        try:
            open(os.path.join(rdir, "err.txt"), "a").write(f"killed by signal {signum} (RuntimeMaxSec/stop)\n")
        except Exception:
            pass
        try:
            _atomic(rdir, "result.json", build_result(phase="errored", r=None, sid=None, t0=t0, blocked=os.path.exists(blocked)))
        except Exception:
            pass
        try:
            _atomic(rdir, "done.json", {"ts": time.time(), "reason": "sigterm"})
        except Exception:
            pass
        os._exit(143)                                   # 128 + SIGTERM; skip further (hangable) cleanup

    signal.signal(signal.SIGTERM, on_sigterm)
    import asyncio
    try:
        sid, r = asyncio.run(_run(rdir, proot, resume))
        if r is None:                                   # query() ended without a ResultMessage (early
            open(os.path.join(rdir, "err.txt"), "a").write(   # stream end / upstream cut) — abnormal,
                "query() completed without a ResultMessage\n")  # record it + finalize as errored, not
            _atomic(rdir, "result.json", build_result(phase="errored", r=None, sid=sid, t0=t0, blocked=os.path.exists(blocked)))
        else:                                           # 'finished' (which would be is_error-contradictory)
            _atomic(rdir, "result.json", build_result(phase="finished", r=r, sid=sid, t0=t0, blocked=os.path.exists(blocked)))
    except BaseException:                                # any crash -> typed err.txt-backed report
        open(os.path.join(rdir, "err.txt"), "a").write(traceback.format_exc())
        _atomic(rdir, "result.json", build_result(phase="errored", r=None, sid=None, t0=t0, blocked=os.path.exists(blocked)))
    finally:
        try:
            _atomic(rdir, "done.json", {"ts": time.time()})   # SENTINEL — written LAST, always
        except Exception:
            pass


if __name__ == "__main__":
    main()
