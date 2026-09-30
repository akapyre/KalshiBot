"""Generate one Skool post.

    python -m skoolbot --slot morning          # news post
    python -m skoolbot --slot evening          # creative post
    python -m skoolbot --slot morning --preview  # print the prompt, no API call
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
from pathlib import Path

import yaml

from . import news, writer
from .deliver import deliver

ROOT = Path(__file__).resolve().parent.parent
USED_PATH = ROOT / "data" / "used_items.json"
DRAFTS_DIR = ROOT / "drafts"
RECENT_TITLES_KEPT = 30


def load_used() -> dict:
    if USED_PATH.exists():
        return json.loads(USED_PATH.read_text())
    return {"item_ids": [], "recent_titles": []}


def save_used(used: dict) -> None:
    USED_PATH.parent.mkdir(parents=True, exist_ok=True)
    used["item_ids"] = used["item_ids"][-500:]
    used["recent_titles"] = used["recent_titles"][-RECENT_TITLES_KEPT:]
    USED_PATH.write_text(json.dumps(used, indent=2) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--slot", required=True, help="slot name from config.yaml, e.g. morning / evening")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--date", help="override date (YYYY-MM-DD), mostly for testing")
    ap.add_argument("--preview", action="store_true", help="fetch news and print the prompt; don't call Claude")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = yaml.safe_load(Path(args.config).read_text())
    slot_type = cfg["slots"].get(args.slot)
    if slot_type not in ("news", "creative"):
        raise SystemExit(f"Unknown slot {args.slot!r}; config.yaml slots: {list(cfg['slots'])}")
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()

    used = load_used()
    items = news.gather(cfg["news"], set(used["item_ids"]))
    logging.info("%d fresh news/study items", len(items))
    if slot_type == "news" and not items:
        raise SystemExit("No fresh news items found -- check the log for source errors.")

    if args.preview:
        print(writer.build_system(cfg), "\n\n---\n")
        print(writer.build_request(slot_type, items, cfg, day, used["recent_titles"]))
        return

    post = writer.write_post(slot_type, items, cfg, day, used["recent_titles"])
    md = writer.to_markdown(post, args.slot, day)
    deliver(post, md, DRAFTS_DIR, day.isoformat(), args.slot)

    used["item_ids"] += post["used_item_ids"]
    used["recent_titles"].append(post["title"])
    save_used(used)
    print(md)


if __name__ == "__main__":
    main()
