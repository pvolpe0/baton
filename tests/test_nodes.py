from lib import nodes


def test_register_and_runnable(tmp_path):
    rec = nodes.register(str(tmp_path), hostname="worker1", role="worker",
                         doctor_ok=True, created_at="2026-06-01T00:00:00Z")
    assert rec["role"] == "worker"
    assert nodes.runnable(str(tmp_path), "worker1") is True
    assert [n["hostname"] for n in nodes.read_all(str(tmp_path))] == ["worker1"]


def test_not_runnable_when_doctor_failed(tmp_path):
    nodes.register(str(tmp_path), hostname="x", role="worker", doctor_ok=False, created_at="t")
    assert nodes.runnable(str(tmp_path), "x") is False


def test_runnable_false_when_unregistered(tmp_path):
    assert nodes.runnable(str(tmp_path), "ghost") is False
