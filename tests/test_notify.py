from runner import notify


def test_format_done():
    msg = notify.format_message(status="done", job_id="x-0001",
                                summary="opened draft PR #12", url="https://github.com/o/r/pull/12")
    assert "done" in msg.lower() and "x-0001" in msg and "pull/12" in msg


def test_format_blocked_includes_reason():
    msg = notify.format_message(status="blocked", job_id="x-0002",
                                summary="needs DB migration applied")
    assert "blocked" in msg.lower() and "needs DB migration" in msg


def test_send_email_is_noop_without_smtp(monkeypatch):
    # optional channel: with no SMTP_HOST configured it's a no-op and never raises (we rely on GitHub)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    assert notify.send_email("subj", "body") is False


def test_notify_returns_message_and_never_raises(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    out = notify.notify(status="done", job_id="x-1", summary="opened PR", url="http://pr/1")
    assert "x-1" in out and "done" in out.lower()
