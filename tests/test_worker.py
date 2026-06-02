"""Unit tests for the SDK worker's PURE helpers. worker.py defers the claude_agent_sdk import into
`_run` and reads env only in `main`, so this imports cleanly without the SDK installed."""
import json
import time
from runner import worker


class _Result:
    """A ResultMessage-like stand-in (the SDK isn't installed on the test machine)."""
    is_error = False
    result = "did the thing"
    total_cost_usd = 0.0123
    num_turns = 5
    session_id = "sess-1"


def test_build_result_finished_carries_fields():
    out = worker.build_result(phase="finished", r=_Result(), sid="sess-1", t0=time.time() - 1, blocked=False)
    assert out["schema"] == 1
    assert out["phase"] == "finished"
    assert out["is_error"] is False
    assert out["blocked"] is False
    assert out["result"] == "did the thing"
    assert out["session_id"] == "sess-1"
    assert out["total_cost_usd"] == 0.0123
    assert out["num_turns"] == 5
    assert out["wall_ms"] >= 900            # ~1s elapsed


def test_build_result_errored_when_no_result():
    out = worker.build_result(phase="errored", r=None, sid=None, t0=time.time(), blocked=True)
    assert out["phase"] == "errored"
    assert out["is_error"] is True          # no result object -> is_error true
    assert out["blocked"] is True
    assert out["result"] == ""
    assert out["session_id"] is None
    assert out["total_cost_usd"] is None and out["num_turns"] is None


def test_build_result_blocked_flag_independent_of_error():
    out = worker.build_result(phase="finished", r=_Result(), sid="s", t0=time.time(), blocked=True)
    assert out["is_error"] is False and out["blocked"] is True   # a clean run that self-blocked


def test_atomic_write_roundtrip_and_no_tmp_left(tmp_path):
    worker._atomic(str(tmp_path), "result.json", {"a": 1, "b": "x"})
    assert json.loads((tmp_path / "result.json").read_text()) == {"a": 1, "b": "x"}
    assert not (tmp_path / "result.json.tmp").exists()          # os.replace consumed the tmp


def test_atomic_overwrite_is_clean(tmp_path):
    worker._atomic(str(tmp_path), "done.json", {"ts": 1})
    worker._atomic(str(tmp_path), "done.json", {"ts": 2})
    assert json.loads((tmp_path / "done.json").read_text()) == {"ts": 2}
