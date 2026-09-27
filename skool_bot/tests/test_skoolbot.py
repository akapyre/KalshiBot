import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import yaml

from skoolbot import news, writer

CFG = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yaml").read_text())

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>FDA weighs new rule on compounded peptides - Reuters</title>
<link>https://news.example/a</link><source url="https://reuters.com">Reuters</source>
<pubDate>Fri, 26 Sep 2026 14:00:00 GMT</pubDate></item>
<item><title>FDA weighs new rule on compounded peptides - Reuters</title>
<link>https://news.example/dupe</link><source url="https://reuters.com">Reuters</source></item>
</channel></rss>"""

PUBMED = {"result": {"uids": ["123"], "123": {
    "title": "Tirzepatide and lean mass in resistance-trained adults.",
    "fulljournalname": "Obesity", "sortpubdate": "2026/09/20 00:00"}}}


def test_parse_google_news_strips_outlet_and_dates():
    items = news.parse_google_news_rss(RSS)
    assert items[0].title == "FDA weighs new rule on compounded peptides"
    assert items[0].source == "Reuters"
    assert items[0].published == "2026-09-26"


def test_parse_pubmed():
    [item] = news.parse_pubmed_summary(PUBMED)
    assert item.id == "pmid:123"
    assert item.url == "https://pubmed.ncbi.nlm.nih.gov/123/"
    assert item.published == "2026-09-20"
    assert not item.title.endswith(".")


def test_gather_dedupes_and_skips_used(monkeypatch):
    rss_items = news.parse_google_news_rss(RSS)
    monkeypatch.setattr(news, "fetch_google_news", lambda q, d: rss_items)
    monkeypatch.setattr(news, "fetch_pubmed", lambda q, d, n: news.parse_pubmed_summary(PUBMED))
    out = news.gather(CFG["news"], already_used={"pmid:123"})
    assert [i.url for i in out] == ["https://news.example/a"]


def test_source_failure_is_not_fatal(monkeypatch):
    def boom(*a):
        raise ConnectionError("down")
    monkeypatch.setattr(news, "fetch_google_news", boom)
    monkeypatch.setattr(news, "fetch_pubmed", lambda q, d, n: news.parse_pubmed_summary(PUBMED))
    assert [i.id for i in news.gather(CFG["news"], set())] == ["pmid:123"]


def test_sanitize_drops_invented_sources():
    items = news.parse_pubmed_summary(PUBMED)
    post = {"title": "t", "body": "b", "poll_options": [], "image_idea": "i",
            "sources": [{"title": "real", "url": items[0].url},
                        {"title": "made up", "url": "https://fake.example"}],
            "used_item_ids": ["pmid:123", "pmid:999"]}
    out = writer.sanitize(post, items, CFG)
    assert [s["title"] for s in out["sources"]] == ["real"]
    assert out["used_item_ids"] == ["pmid:123"]


def test_pillar_rotates_daily():
    pillars = CFG["creative_pillars"]
    d = dt.date(2026, 9, 27)
    names = {writer.pick_pillar(pillars, d + dt.timedelta(days=k))["name"] for k in range(len(pillars))}
    assert len(names) == len(pillars)


def test_write_post_parses_structured_output():
    items = news.parse_pubmed_summary(PUBMED)
    payload = ('{"title": "Keep your gains on GLP-1s", "body": "Body.",'
               ' "poll_options": [], "image_idea": "barbell", "sources": [{"title": "x", "url": "%s"}],'
               ' "used_item_ids": ["pmid:123"]}' % items[0].url)
    captured = {}

    def create(**kw):
        captured.update(kw)
        return SimpleNamespace(stop_reason="end_turn", stop_details=None,
                               content=[SimpleNamespace(type="text", text=payload)])
    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))

    post = writer.write_post("news", items, CFG, dt.date(2026, 9, 27), [], client=client)
    assert post["title"] == "Keep your gains on GLP-1s"
    assert captured["output_config"]["format"]["type"] == "json_schema"
    md = writer.to_markdown(post, "morning", dt.date(2026, 9, 27))
    assert "# Keep your gains on GLP-1s" in md and "**Sources:**" in md


def test_style_examples_go_into_system_prompt(tmp_path):
    (tmp_path / "README.md").write_text("instructions, not a post")
    (tmp_path / "01.md").write_text("yo. BPC in rats was wild")
    examples = writer.load_style_examples(tmp_path)
    assert examples == ["yo. BPC in rats was wild"]
    system = writer.build_system(CFG, examples)
    assert "<example_post>\nyo. BPC in rats was wild\n</example_post>" in system
    assert CFG["community"]["name"] not in system
    assert "End the post body with" not in system
