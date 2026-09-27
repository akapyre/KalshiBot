"""Fetch recent peptide headlines (Google News RSS) and papers (PubMed)."""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import requests

log = logging.getLogger(__name__)

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={q}+when:{days}d&hl=en-US&gl=US&ceid=US:en"
PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_SUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
TIMEOUT = 20
HEADERS = {"User-Agent": "skool-peptide-post-bot/1.0"}


@dataclass
class NewsItem:
    id: str          # stable key used for "already posted" dedup
    title: str
    url: str
    source: str
    published: str   # ISO date, may be ""
    kind: str        # "news" or "study"

    def to_dict(self) -> dict:
        return asdict(self)


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def parse_google_news_rss(xml_text: str) -> list[NewsItem]:
    items = []
    root = ET.fromstring(xml_text)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        source = (it.findtext("source") or "").strip()
        # Google appends " - Outlet" to titles; strip it since we keep source separately.
        if source and title.endswith(f" - {source}"):
            title = title[: -len(f" - {source}")]
        published = ""
        if it.findtext("pubDate"):
            try:
                published = parsedate_to_datetime(it.findtext("pubDate")).date().isoformat()
            except (TypeError, ValueError):
                pass
        if title and link:
            items.append(NewsItem(
                id="news:" + _norm_title(title)[:120],
                title=title, url=link, source=source or "Google News",
                published=published, kind="news",
            ))
    return items


def parse_pubmed_summary(data: dict) -> list[NewsItem]:
    result = data.get("result", {})
    items = []
    for pmid in result.get("uids", []):
        rec = result.get(pmid, {})
        title = (rec.get("title") or "").strip().rstrip(".")
        if not title:
            continue
        items.append(NewsItem(
            id=f"pmid:{pmid}",
            title=title,
            url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            source=rec.get("fulljournalname") or rec.get("source") or "PubMed",
            published=rec.get("sortpubdate", "")[:10].replace("/", "-"),
            kind="study",
        ))
    return items


def fetch_google_news(query: str, days: int) -> list[NewsItem]:
    url = GOOGLE_NEWS_RSS.format(q=quote_plus(query), days=days)
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return parse_google_news_rss(resp.text)


def fetch_pubmed(query: str, days: int, max_results: int) -> list[NewsItem]:
    search = requests.get(PUBMED_SEARCH, headers=HEADERS, timeout=TIMEOUT, params={
        "db": "pubmed", "term": query, "reldate": days, "datetype": "edat",
        "retmax": max_results, "sort": "date", "retmode": "json",
    })
    search.raise_for_status()
    ids = search.json().get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []
    summary = requests.get(PUBMED_SUMMARY, headers=HEADERS, timeout=TIMEOUT, params={
        "db": "pubmed", "id": ",".join(ids), "retmode": "json",
    })
    summary.raise_for_status()
    return parse_pubmed_summary(summary.json())


def gather(news_cfg: dict, already_used: set[str]) -> list[NewsItem]:
    """Pull every source, drop duplicates and anything already posted about.

    A failing source is logged and skipped so one outage doesn't kill the run.
    """
    days = int(news_cfg.get("lookback_days", 3))
    collected: list[NewsItem] = []
    for q in news_cfg.get("google_news_queries", []):
        try:
            collected += fetch_google_news(q, days)
        except Exception as e:  # noqa: BLE001 -- any source failure is non-fatal
            log.warning("Google News query %r failed: %s", q, e)
    if news_cfg.get("pubmed_query"):
        try:
            # PubMed indexing lags, so look back a bit further than news.
            collected += fetch_pubmed(news_cfg["pubmed_query"], max(days, 7),
                                      int(news_cfg.get("pubmed_max_results", 15)))
        except Exception as e:  # noqa: BLE001
            log.warning("PubMed query failed: %s", e)

    seen: set[str] = set()
    fresh = []
    for item in collected:
        if item.id in seen or item.id in already_used:
            continue
        seen.add(item.id)
        fresh.append(item)
    fresh.sort(key=lambda i: i.published, reverse=True)
    return fresh[: int(news_cfg.get("max_items_to_model", 25))]
