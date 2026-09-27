"""Turn the news pool + a slot type into a ready-to-paste Skool post via Claude."""

from __future__ import annotations

import datetime as dt
import json
import logging

import anthropic

from .news import NewsItem

log = logging.getLogger(__name__)

GUARDRAILS = """\
Non-negotiable content rules (these protect the community and the owner):
- Educational content only. Never give dosing, cycling, stacking, reconstitution
  or injection instructions, and never recommend where to buy peptides or name vendors.
- Be honest about evidence level. Say clearly when something is only shown in
  animals/cells, is a small or early human trial, or is anecdote.
- Be accurate about regulatory status (e.g. FDA-approved drug vs. research
  chemical / not approved for human use) when it matters to the topic.
- Only cite sources from the provided list, by exact URL. Never invent studies,
  numbers, quotes or headlines. If a headline alone doesn't tell you the result,
  don't guess it -- frame the post around the question instead.
- Encourage members to work with a qualified clinician for anything medical.
- End the post body with a short line: "Educational only, not medical advice."
"""

POST_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Skool post title, under 80 characters, scroll-stopping."},
        "body": {"type": "string", "description": "Post body in plain text with line breaks. 120-300 words. Ends with a question or call to comment."},
        "poll_options": {"type": "array", "items": {"type": "string"},
                         "description": "3-5 poll options if this post should be a poll, else empty."},
        "image_idea": {"type": "string", "description": "One-line idea for a cover image or graphic to attach."},
        "sources": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "url": {"type": "string"}},
            "required": ["title", "url"], "additionalProperties": False}},
        "used_item_ids": {"type": "array", "items": {"type": "string"},
                          "description": "ids of the provided items this post is built on."},
    },
    "required": ["title", "body", "poll_options", "image_idea", "sources", "used_item_ids"],
    "additionalProperties": False,
}


def pick_pillar(pillars: list[dict], day: dt.date) -> dict:
    return pillars[day.toordinal() % len(pillars)]


def build_system(cfg: dict) -> str:
    c = cfg["community"]
    return (
        f"You write daily posts for the Skool community \"{c['name']}\".\n\n"
        f"Audience: {c['audience'].strip()}\n\nVoice: {c['voice'].strip()}\n\n{GUARDRAILS}"
    )


def build_request(slot_type: str, items: list[NewsItem], cfg: dict, day: dt.date,
                  recent_titles: list[str]) -> str:
    pool = json.dumps([i.to_dict() for i in items], indent=1)
    recent = "\n".join(f"- {t}" for t in recent_titles) or "(none yet)"
    if slot_type == "news":
        task = (
            "Write today's NEWS post. Pick the single most interesting, relevant item "
            "for this audience from the pool (favor human data, big regulatory moves, and "
            "anything gym-relevant). Explain what happened, why a lifter should care, and "
            "what we still don't know. If nothing in the pool is worth a post, write a "
            "'what I'm watching this week' roundup of 2-3 items instead."
        )
    else:
        pillar = pick_pillar(cfg["creative_pillars"], day)
        task = (
            f"Write today's CREATIVE post in the \"{pillar['name']}\" format: {pillar['brief']}\n"
            "You may riff on an item from the pool if it fits, but you don't have to. "
            "If you use one, cite it. Fill poll_options only if the format calls for a poll."
        )
    return (
        f"Date: {day.isoformat()}\n\n{task}\n\n"
        f"Don't repeat these recent post titles/angles:\n{recent}\n\n"
        f"News pool (JSON):\n{pool}"
    )


def write_post(slot_type: str, items: list[NewsItem], cfg: dict, day: dt.date,
               recent_titles: list[str], client: anthropic.Anthropic | None = None) -> dict:
    client = client or anthropic.Anthropic()
    model_cfg = cfg.get("model", {})
    response = client.beta.messages.create(
        model=model_cfg.get("name", "claude-opus-5"),
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # re-run on a fallback model if a safety classifier declines
        thinking={"type": "adaptive"},
        output_config={
            "effort": model_cfg.get("effort", "high"),
            "format": {"type": "json_schema", "schema": POST_SCHEMA},
        },
        system=build_system(cfg),
        messages=[{"role": "user", "content": build_request(slot_type, items, cfg, day, recent_titles)}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Model declined to write this post: {response.stop_details}")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Post generation hit max_tokens before finishing.")

    text = next(b.text for b in response.content if b.type == "text")
    post = json.loads(text)
    return sanitize(post, items, cfg)


def sanitize(post: dict, items: list[NewsItem], cfg: dict) -> dict:
    """Drop any source or item id the model didn't get from us, and add the signoff."""
    allowed_urls = {i.url for i in items}
    allowed_ids = {i.id for i in items}
    dropped = [s for s in post["sources"] if s["url"] not in allowed_urls]
    if dropped:
        log.warning("Dropping %d source(s) not in the news pool: %s", len(dropped), dropped)
    post["sources"] = [s for s in post["sources"] if s["url"] in allowed_urls]
    post["used_item_ids"] = [i for i in post["used_item_ids"] if i in allowed_ids]
    signoff = (cfg["community"].get("signoff") or "").strip()
    if signoff and signoff not in post["body"]:
        post["body"] = post["body"].rstrip() + "\n\n" + signoff
    return post


def to_markdown(post: dict, slot: str, day: dt.date) -> str:
    lines = [f"<!-- {day.isoformat()} {slot} -->", f"# {post['title']}", "", post["body"].strip(), ""]
    if post["poll_options"]:
        lines += ["**Poll options:**"] + [f"- {o}" for o in post["poll_options"]] + [""]
    if post["sources"]:
        lines += ["**Sources:**"] + [f"- [{s['title']}]({s['url']})" for s in post["sources"]] + [""]
    lines += [f"_Image idea: {post['image_idea']}_", ""]
    return "\n".join(lines)
