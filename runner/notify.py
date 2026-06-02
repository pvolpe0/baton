"""baton notifications.

DEFAULT — GitHub-native, zero config: the worker opens a (draft) PR and GitHub emails you about it
(enable "email about your own activity", since the worker acts as your account). A blocked job opens
a `[BLOCKED]` draft PR for the same reason.

OPTIONAL — a direct email, for people who'd rather not enable GitHub's (global) own-activity emails.
Set SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS + NOTIFY_EMAIL in ~/.baton.env and baton will ALSO email
you on done/blocked. If SMTP_HOST is unset this is a no-op. Secrets live in ~/.baton.env, never
committed. (notify() is also the seam for a future channel — a bot account, a webhook.)"""
import os, smtplib
from email.message import EmailMessage


def format_message(*, status, job_id, summary, url=None):
    line = f"[baton] {status.upper()} {job_id}: {summary}"
    return line + (f"\n{url}" if url else "")


def _env(k, default=None):
    return os.environ.get(k, default)


def send_email(subject, text):
    host, user = _env("SMTP_HOST"), _env("SMTP_USER")
    if not host or not user:          # optional — needs at least host + user, else no-op (use GitHub)
        return False
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = _env("SMTP_FROM") or user     # default From to the login (seamless; Gmail pins it anyway)
    m["To"] = _env("NOTIFY_EMAIL") or user     # default To to yourself, so you needn't set a separate one
    m.set_content(text)
    with smtplib.SMTP(host, int(_env("SMTP_PORT", "587"))) as s:
        s.starttls()
        s.login(user, _env("SMTP_PASS", ""))
        s.send_message(m)
    return True


def notify(*, status, job_id, summary, url=None):
    text = format_message(status=status, job_id=job_id, summary=summary, url=url)
    send_email(f"baton {status}: {job_id}", text)   # optional; no-op unless SMTP_* is set in ~/.baton.env
    return text
