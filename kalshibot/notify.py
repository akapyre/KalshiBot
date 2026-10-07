"""Phone notifications through ntfy (https://ntfy.sh): install the free ntfy
app, subscribe to your topic, and put the same topic in .env as NTFY_TOPIC.
No topic set = notifications are off.

Anyone who knows a topic name can read it, so use a long random one
(`python -m kalshibot.diagnose notify` suggests one). Messages contain bet
details only -- never keys or balances beyond what's shown here.
"""
from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger("kalshibot.notify")

NTFY_URL = "https://ntfy.sh/"


def topic() -> str | None:
    return (os.environ.get("NTFY_TOPIC") or "").strip() or None


def send(title: str, message: str, tags: list[str] | None = None, priority: int = 3) -> bool:
    """Push one notification. Never raises -- a notification failing must not
    stop the bot. JSON publishing (not headers) so names like "Čilić" and
    the ¢ sign come through intact."""
    t = topic()
    if t is None:
        return False
    try:
        resp = requests.post(
            NTFY_URL,
            json={"topic": t, "title": title, "message": message, "tags": tags or [], "priority": priority},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.warning("Phone notification failed (%s): %s", title, e)
        return False
