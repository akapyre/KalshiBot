"""Get the finished draft to you: always a file, plus email / webhook if configured.

Skool has no public API for creating posts, so the bot hands you a
ready-to-paste draft instead of posting on its own.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

import requests

log = logging.getLogger(__name__)


def save_file(markdown: str, drafts_dir: Path, day: str, slot: str) -> Path:
    drafts_dir.mkdir(parents=True, exist_ok=True)
    path = drafts_dir / f"{day}-{slot}.md"
    path.write_text(markdown, encoding="utf-8")
    return path


def send_email(post: dict, markdown: str, slot: str) -> bool:
    """Send via SMTP (e.g. Gmail + app password). Skipped unless SMTP_* env vars are set."""
    host, user, password, to = (os.getenv(k) for k in
                                ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "POST_EMAIL_TO"))
    if not all((host, user, password, to)):
        return False
    msg = EmailMessage()
    msg["Subject"] = f"[Skool {slot} post] {post['title']}"
    msg["From"] = user
    msg["To"] = to
    msg.set_content(markdown)
    with smtplib.SMTP_SSL(host, int(os.getenv("SMTP_PORT", "465"))) as s:
        s.login(user, password)
        s.send_message(msg)
    return True


def send_webhook(post: dict, markdown: str, slot: str) -> bool:
    """POST the draft to a Zapier/Make/Discord/Slack webhook. Skipped unless POST_WEBHOOK_URL is set."""
    url = os.getenv("POST_WEBHOOK_URL")
    if not url:
        return False
    payload = {"slot": slot, "title": post["title"], "body": post["body"],
               "poll_options": post["poll_options"], "sources": post["sources"],
               "image_idea": post["image_idea"],
               # "content" makes Discord/Slack-style webhooks show something readable.
               "content": markdown[:1900], "text": markdown}
    requests.post(url, json=payload, timeout=20).raise_for_status()
    return True


def deliver(post: dict, markdown: str, drafts_dir: Path, day: str, slot: str) -> None:
    path = save_file(markdown, drafts_dir, day, slot)
    log.info("Draft saved to %s", path)
    for name, fn in (("email", send_email), ("webhook", send_webhook)):
        try:
            if fn(post, markdown, slot):
                log.info("Draft sent by %s", name)
        except Exception as e:  # noqa: BLE001 -- the file copy already exists
            log.error("Sending by %s failed: %s", name, e)
