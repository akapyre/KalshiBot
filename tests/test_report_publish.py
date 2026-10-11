import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot import publish
from kalshibot.publish import SitePublisher
from kalshibot.report import _nice_ticks, build_html, load_rule_texts

RULES = load_rule_texts(Path(__file__).resolve().parents[1] / "config" / "rules.yaml")


def bet(i, status, pnl, team="Detroit Lions", rule="nfl_moderate_favorite", sport="nfl"):
    return {"order_id": str(i), "ticker": "T", "team": team, "sport": sport, "rule": rule, "contracts": 37,
            "cost": 14.8, "fee": 0.62, "placed_at": f"2026-10-0{i}T20:00:00+00:00", "status": status,
            "pnl": pnl, "settled_at": f"2026-10-0{i}T23:00:00+00:00" if pnl is not None else None,
            "game": "Detroit Lions @ Carolina Panthers", "pregame_odds": -225, "live_odds": 123, "price_cents": 40}


def page(bets, live=()):
    return build_html(bets, RULES, list(live), mode="live", generated_at=datetime(2026, 10, 11, tzinfo=timezone.utc))


def test_overview_numbers():
    html = page([bet(1, "won", 21.58), bet(2, "lost", -15.42), bet(3, "open", None)])
    assert "+$6.16" in html            # net
    assert "1&ndash;1" in html          # record
    assert "50.0%" in html              # win rate
    assert 'id="pnl-data"' in html      # chart drawn with 2 settled bets


def test_team_names_are_escaped():
    html = page([bet(1, "open", None, team='<script>alert(1)</script>')])
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html


def test_empty_page_and_plain_rule_text():
    html = page([])
    assert "No bets yet." in html and "The chart starts" in html
    assert "nfl_favorite_fade" not in html.split('id="rules"')[1].split("</section>")[0]


def test_nice_ticks():
    assert _nice_ticks(-7.6, 102.6) == [0, 50, 100]
    assert _nice_ticks(-20, 20) == [-20, -10, 0, 10, 20]


def test_publisher_writes_locally_and_uploads_only_when_due(tmp_path, monkeypatch):
    calls = []

    class Resp:
        status_code, ok, text = 200, True, ""

        def json(self):
            return {"content": {"sha": "s2"}}

    monkeypatch.setattr(publish.requests, "get", lambda *a, **k: type("R", (), {"status_code": 404})())
    monkeypatch.setattr(publish.requests, "put", lambda url, headers, json, timeout: calls.append(json) or Resp())
    p = SitePublisher(tmp_path / "index.html", repo="me/results", token="t", every_s=600)
    p.update("<html>1</html>")
    p.update("<html>2</html>")               # too soon: local only
    assert (tmp_path / "index.html").read_text() == "<html>2</html>"
    assert len(calls) == 1 and "sha" not in calls[0]
    p.update("<html>3</html>", force=True)   # a bet settled: upload now, with the new sha
    assert len(calls) == 2 and calls[1]["sha"] == "s2"
    assert p.url == "https://me.github.io/results/"


def test_publisher_without_settings_stays_local(tmp_path, monkeypatch):
    monkeypatch.setattr(publish.requests, "put", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    p = SitePublisher(tmp_path / "index.html", repo="", token="")
    p.update("<html/>", force=True)
    assert (tmp_path / "index.html").exists() and not p.enabled
