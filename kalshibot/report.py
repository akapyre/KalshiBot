"""The results website: one self-contained index.html built from the bet
ledger, the rules file and the games the bot is watching right now.

`publish.py` uploads it to GitHub Pages; it is also written locally to
site/index.html so it can be opened without publishing.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

import yaml

SPORT_LABELS = {
    "nfl": "NFL", "cfb": "College football", "mlb": "MLB", "nhl": "NHL",
    "soccer": "Soccer", "tennis": "Tennis",
}


def sport_label(sport: str) -> str:
    return SPORT_LABELS.get(sport, sport.upper())


def rule_label(rule_id: str) -> str:
    """"soccer_first_half_favorite" -> "First half favorite"."""
    words = rule_id.split("_")
    if words and words[0] in SPORT_LABELS:
        words = words[1:]
    return " ".join(words).capitalize() or rule_id


def money(x: float, signed: bool = True) -> str:
    sign = ("+" if x > 0 else "−" if x < 0 else "") if signed else ("−" if x < 0 else "")
    return f"{sign}${abs(x):,.2f}"


def odds(o: Any) -> str:
    return "—" if o is None else f"{int(o):+d}"


def _when(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return escape(iso)
    return dt.strftime("%b %-d, %-I:%M %p")


def load_rule_texts(path: str | Path = "config/rules.yaml") -> list[dict[str, str]]:
    raw = yaml.safe_load(Path(path).read_text())
    return [
        {"id": r["id"], "sport": r["sport"],
         "text": " ".join(str(r.get("summary") or r.get("description", "")).split())}
        for r in raw.get("rules", [])
    ]


def _stats(bets: list[dict[str, Any]]) -> dict[str, Any]:
    settled = [b for b in bets if b["status"] in ("won", "lost")]
    wins = sum(b["status"] == "won" for b in settled)
    net = sum(b["pnl"] for b in settled)
    outlay = sum(b["cost"] + (b.get("fee") or 0) for b in settled)
    return {
        "wins": wins, "losses": len(settled) - wins, "settled": len(settled), "net": net,
        "roi": (net / outlay * 100) if outlay else None,
        "win_rate": (wins / len(settled) * 100) if settled else None,
    }


def _nice_ticks(lo: float, hi: float, target: int = 4) -> list[float]:
    """Round axis values (e.g. 0, 25, 50, 75) inside lo..hi."""
    raw = (hi - lo) / target
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.ceil(lo / step) * step
    ticks, v = [], start
    while v <= hi + 1e-9:
        ticks.append(round(v, 2))
        v += step
    return ticks


def _axis_money(v: float) -> str:
    if v == 0:
        return "$0"
    return f"{'−' if v < 0 else ''}${abs(v):,.0f}" if abs(v) >= 10 else f"{'−' if v < 0 else ''}${abs(v):,.2f}"


def _chart(bets: list[dict[str, Any]]) -> str:
    settled = sorted(
        (b for b in bets if b["status"] in ("won", "lost")),
        key=lambda b: b.get("settled_at") or b["placed_at"],
    )
    if len(settled) < 2:
        return '<p class="empty">The chart starts once two bets have settled.</p>'

    cum, total = [], 0.0
    for b in settled:
        total += b["pnl"]
        cum.append(round(total, 2))
    w, h, left, right, top, bottom = 720, 240, 56, 70, 16, 28
    lo, hi = min(0.0, *cum), max(0.0, *cum)
    if hi - lo < 1:
        hi, lo = hi + 0.5, lo - 0.5
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad

    def x(i: int) -> float:
        return left + (w - left - right) * (i / (len(cum) - 1))

    def y(v: float) -> float:
        return top + (h - top - bottom) * (1 - (v - lo) / (hi - lo))

    grid = []
    for v in _nice_ticks(lo, hi):
        grid.append(
            f'<line class="grid" x1="{left}" x2="{w - right}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
            f'<text class="tick" x="{left - 8}" y="{y(v) + 4:.1f}" text-anchor="end">{escape(_axis_money(v))}</text>'
        )
    path = " ".join(f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(v):.1f}" for i, v in enumerate(cum))
    pts = [
        {"x": round(x(i), 1), "y": round(y(v), 1), "cum": money(v), "pnl": money(b["pnl"]),
         "team": b["team"], "when": _when(b.get("settled_at") or b["placed_at"]),
         "result": "Won" if b["status"] == "won" else "Lost"}
        for i, (v, b) in enumerate(zip(cum, settled))
    ]
    end_x, end_y = x(len(cum) - 1), y(cum[-1])
    # "</" can't appear inside <script>, or a team name could end the block.
    data = json.dumps(pts).replace("</", "<\\/")
    return f"""
<div class="chart" id="pnl-chart">
  <svg viewBox="0 0 {w} {h}" role="img" aria-label="Cumulative profit and loss over {len(cum)} settled bets, ending at {escape(money(cum[-1]))}">
    {''.join(grid)}
    <line class="zero" x1="{left}" x2="{w - right}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>
    <path class="line" d="{path}"/>
    <circle class="end-dot" cx="{end_x:.1f}" cy="{end_y:.1f}" r="4"/>
    <text class="end-label" x="{end_x + 8:.1f}" y="{end_y + 4:.1f}">{escape(money(cum[-1]))}</text>
    <text class="tick" x="{left}" y="{h - 8}">{escape(pts[0]['when'])}</text>
    <text class="tick" x="{w - right}" y="{h - 8}" text-anchor="end">{escape(pts[-1]['when'])}</text>
    <line class="crosshair" id="pnl-cross" x1="0" x2="0" y1="{top}" y2="{h - bottom}" visibility="hidden"/>
    <circle class="hover-dot" id="pnl-dot" r="5" visibility="hidden"/>
    <rect class="hit" x="{left}" y="{top}" width="{w - left - right}" height="{h - top - bottom}"/>
  </svg>
  <div class="tooltip" id="pnl-tip" hidden></div>
  <script type="application/json" id="pnl-data">{data}</script>
</div>"""


def _table(headers: list[str], rows: list[str], empty: str, cls: str = "") -> str:
    if not rows:
        return f'<p class="empty">{escape(empty)}</p>'
    head = "".join(f"<th>{h}</th>" for h in headers)
    return f'<div class="table-wrap"><table class="{cls}"><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def _pill(text: str, kind: str) -> str:
    return f'<span class="pill {kind}">{escape(text)}</span>'


def build_html(bets: list[dict[str, Any]], rules: list[dict[str, str]], live: list[dict[str, Any]],
               *, mode: str, generated_at: datetime | None = None) -> str:
    generated_at = generated_at or datetime.now(timezone.utc)
    st = _stats(bets)
    open_bets = [b for b in bets if b["status"] == "open"]
    at_risk = sum(b["cost"] + (b.get("fee") or 0) for b in open_bets)

    def tile(label: str, value: str, sub: str = "", tone: str = "") -> str:
        return (f'<div class="tile"><div class="tile-label">{label}</div>'
                f'<div class="tile-value {tone}">{value}</div><div class="tile-sub">{sub}</div></div>')

    tone = "pos" if st["net"] > 0 else "neg" if st["net"] < 0 else ""
    tiles = "".join([
        tile("Total profit / loss", escape(money(st["net"])),
             "—" if st["roi"] is None else f'{st["roi"]:+.1f}% ROI', tone),
        tile("Record", f'{st["wins"]}&ndash;{st["losses"]}', f'{st["settled"]} settled bets'),
        tile("Win rate", "—" if st["win_rate"] is None else f'{st["win_rate"]:.1f}%', "of settled bets"),
        tile("Open bets", str(len(open_bets)), f"{escape(money(at_risk, signed=False))} at stake"),
    ])

    def price(b: dict[str, Any]) -> str:
        return f'{b["price_cents"]}¢' if b.get("price_cents") else "—"

    def outlay(b: dict[str, Any]) -> str:
        return escape(money(b["cost"] + (b.get("fee") or 0), signed=False))

    def team_cell(b: dict[str, Any]) -> str:
        return f'<td><strong>{escape(b["team"])}</strong><div class="sub">{escape(b.get("game", ""))}</div></td>'

    open_rows = [
        f'<tr><td>{_when(b["placed_at"])}</td><td>{escape(sport_label(b["sport"]))}</td>{team_cell(b)}'
        f'<td>{escape(rule_label(b["rule"]))}</td>'
        f'<td class="num">{odds(b.get("pregame_odds"))} &rarr; {odds(b.get("live_odds"))}</td>'
        f'<td class="num">{price(b)}</td><td class="num">{outlay(b)}</td>'
        f'<td class="num">{escape(money(b["contracts"], signed=False))}</td></tr>'
        for b in sorted(open_bets, key=lambda b: b["placed_at"], reverse=True)
    ]

    live_rows = [
        f'<tr><td>{escape(sport_label(g["sport"]))}</td>'
        f'<td><strong>{escape(g["favorite"])}</strong><div class="sub">{escape(g["game"])}</div></td>'
        f'<td class="num">{odds(g["pregame"])}</td><td class="num">{odds(g["live"])}</td>'
        f'<td class="num">{escape(g["score"])}</td><td>{escape(g["period"])}</td>'
        f'<td>{escape(", ".join(rule_label(r) for r in g["rules"]))}</td></tr>'
        for g in live
    ]

    by_rule = []
    for r in rules:
        rb = [b for b in bets if b["rule"] == r["id"]]
        s = _stats(rb)
        if not rb:
            continue
        net_cls = "pos" if s["net"] > 0 else "neg" if s["net"] < 0 else ""
        win_rate = "—" if s["win_rate"] is None else "{:.0f}%".format(s["win_rate"])
        roi = "—" if s["roi"] is None else "{:+.1f}%".format(s["roi"])
        by_rule.append(
            f'<tr><td>{escape(rule_label(r["id"]))}</td><td>{escape(sport_label(r["sport"]))}</td>'
            f'<td class="num">{len(rb)}</td><td class="num">{s["wins"]}&ndash;{s["losses"]}</td>'
            f'<td class="num">{win_rate}</td><td class="num {net_cls}">{escape(money(s["net"]))}</td>'
            f'<td class="num">{roi}</td></tr>'
        )

    rule_cards = []
    for sport in SPORT_LABELS:
        mine = [r for r in rules if r["sport"] == sport]
        if not mine:
            continue
        items = "".join(
            f'<li><div class="rule-name">{escape(rule_label(r["id"]))}</div><p>{escape(r["text"])}</p></li>'
            for r in mine
        )
        rule_cards.append(f'<div class="rule-card"><h3>{escape(sport_label(sport))}</h3><ul>{items}</ul></div>')

    def result_pill(b: dict[str, Any]) -> str:
        return {"won": _pill("Won", "pos"), "lost": _pill("Lost", "neg"),
                "void": _pill("Void", "muted"), "open": _pill("Open", "info")}[b["status"]]

    history = sorted(bets, key=lambda b: b["placed_at"], reverse=True)
    def pnl_cell(b: dict[str, Any]) -> str:
        if b["pnl"] is None:
            return '<td class="num">—</td>'
        cls = "pos" if b["pnl"] > 0 else "neg" if b["pnl"] < 0 else ""
        return f'<td class="num {cls}">{escape(money(b["pnl"]))}</td>'

    hist_rows = [
        f'<tr data-sport="{escape(b["sport"])}" data-rule="{escape(b["rule"])}">'
        f'<td>{_when(b["placed_at"])}</td><td>{escape(sport_label(b["sport"]))}</td>{team_cell(b)}'
        f'<td>{escape(rule_label(b["rule"]))}</td><td class="num">{odds(b.get("live_odds"))}</td>'
        f'<td class="num">{price(b)}</td><td class="num">{outlay(b)}</td>'
        f'<td>{result_pill(b)}</td>{pnl_cell(b)}</tr>'
        for b in history
    ]
    sports_in = sorted({b["sport"] for b in bets})
    rules_in = sorted({b["rule"] for b in bets})
    sport_opts = "".join(f'<option value="{escape(s)}">{escape(sport_label(s))}</option>' for s in sports_in)
    rule_opts = "".join(f'<option value="{escape(r)}">{escape(rule_label(r))}</option>' for r in rules_in)

    mode_pill = _pill("Live trading", "pos") if mode == "live" else _pill("Logging only", "muted")
    updated = generated_at.astimezone().strftime("%b %-d, %Y at %-I:%M %p %Z")

    return PAGE.format(
        mode_pill=mode_pill, updated=escape(updated), tiles=tiles, chart=_chart(bets),
        open_table=_table(["Placed", "Sport", "Team", "Rule", "Pregame &rarr; live", "Price", "Cost", "Pays if win"],
                          open_rows, "No open bets right now."),
        live_table=_table(["Sport", "Favorite", "Pregame", "Live", "Score", "Period", "Rules in play"],
                          live_rows, "No game with a qualifying favorite is in progress."),
        rule_table=_table(["Rule", "Sport", "Bets", "W&ndash;L", "Win %", "Net", "ROI"],
                          by_rule, "No bets yet."),
        rule_cards="".join(rule_cards),
        sport_opts=sport_opts, rule_opts=rule_opts,
        history_table=_table(["Placed", "Sport", "Team", "Rule", "Odds", "Price", "Cost", "Result", "P/L"],
                             hist_rows, "No bets yet.", cls="history"),
    )


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta http-equiv="refresh" content="60">
<title>Kalshi Bot Results</title>
<style>
  :root {{
    color-scheme: light;
    --page: #f3f5f9; --surface: #ffffff; --ink: #111827; --ink-2: #4b5563; --muted: #8a93a3;
    --border: rgba(17, 24, 39, 0.08); --grid: #e8ecf2; --axis: #c9d1dd;
    --accent: #2a78d6; --accent-wash: rgba(42, 120, 214, 0.10);
    --pos: #047857; --pos-tint: #e6f6ef; --neg: #c2362f; --neg-tint: #fdecea;
    --info: #2a6fc9; --info-tint: rgba(42, 120, 214, 0.12); --mute-tint: rgba(75, 85, 99, 0.12);
    --head: #f6f8fb; --hover: #f7f9fc;
    --band-from: #0d1b36; --band-to: #173a6e;
    --shadow: 0 1px 2px rgba(17, 24, 39, 0.05), 0 8px 24px rgba(17, 24, 39, 0.05);
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      color-scheme: dark;
      --page: #0a101c; --surface: #111a2b; --ink: #e9eef7; --ink-2: #a8b4c8; --muted: #74829b;
      --border: rgba(255, 255, 255, 0.07); --grid: #1c2740; --axis: #2a3954;
      --accent: #3987e5; --accent-wash: rgba(57, 135, 229, 0.14);
      --pos: #34d399; --pos-tint: rgba(52, 211, 153, 0.12); --neg: #f87171; --neg-tint: rgba(248, 113, 113, 0.12);
      --info: #7ab3f2; --info-tint: rgba(75, 147, 234, 0.16); --mute-tint: rgba(168, 180, 200, 0.14);
      --head: #0f182a; --hover: #152035; --band-from: #0b1428; --band-to: #13284d;
      --shadow: 0 1px 2px rgba(0, 0, 0, 0.3);
    }}
  }}
  :root[data-theme="dark"] {{
    color-scheme: dark;
    --page: #0a101c; --surface: #111a2b; --ink: #e9eef7; --ink-2: #a8b4c8; --muted: #74829b;
    --border: rgba(255, 255, 255, 0.07); --grid: #1c2740; --axis: #2a3954;
    --accent: #3987e5; --accent-wash: rgba(57, 135, 229, 0.14);
    --pos: #34d399; --pos-tint: rgba(52, 211, 153, 0.12); --neg: #f87171; --neg-tint: rgba(248, 113, 113, 0.12);
    --info: #7ab3f2; --info-tint: rgba(75, 147, 234, 0.16); --mute-tint: rgba(168, 180, 200, 0.14);
    --head: #0f182a; --hover: #152035; --band-from: #0b1428; --band-to: #13284d;
    --shadow: 0 1px 2px rgba(0, 0, 0, 0.3);
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--page); color: var(--ink);
    font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; -webkit-font-smoothing: antialiased; }}
  .band {{ background: linear-gradient(120deg, var(--band-from), var(--band-to)); color: #fff; padding: 28px 16px 64px; }}
  .wrap {{ max-width: 1080px; margin: 0 auto; }}
  .band h1 {{ margin: 0 0 4px; font-size: 26px; letter-spacing: -0.01em; }}
  .band .meta {{ color: rgba(255,255,255,0.72); font-size: 13px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
  nav {{ display: flex; gap: 6px; margin-top: 18px; flex-wrap: wrap; }}
  nav a {{ color: #fff; text-decoration: none; font-size: 13px; padding: 6px 12px; border-radius: 999px; background: rgba(255,255,255,0.10); }}
  nav a:hover {{ background: rgba(255,255,255,0.18); }}
  main {{ max-width: 1080px; margin: -44px auto 40px; padding: 0 16px; display: grid; gap: 16px; }}
  .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 14px; box-shadow: var(--shadow); padding: 18px; min-width: 0; }}
  .card h2 {{ margin: 0 0 2px; font-size: 17px; }}
  .card .hint {{ margin: 0 0 14px; color: var(--muted); font-size: 13px; }}
  .tiles {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }}
  .tile {{ background: var(--surface); border: 1px solid var(--border); border-radius: 14px; box-shadow: var(--shadow); padding: 16px; }}
  .tile-label {{ color: var(--ink-2); font-size: 13px; }}
  .tile-value {{ font-size: 28px; font-weight: 650; letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }}
  .tile-sub {{ color: var(--muted); font-size: 13px; }}
  .pos {{ color: var(--pos); }} .neg {{ color: var(--neg); }}
  .table-wrap {{ overflow-x: auto; margin: 0 -18px; padding: 0 18px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  .table-wrap table {{ min-width: 640px; }}  /* phones scroll the table instead of squashing it */
  td:first-child {{ white-space: nowrap; }}
  th {{ text-align: left; font-weight: 600; color: var(--ink-2); font-size: 12px; background: var(--head); padding: 8px 10px; white-space: nowrap; }}
  td {{ padding: 9px 10px; border-top: 1px solid var(--grid); vertical-align: top; }}
  tbody tr:hover {{ background: var(--hover); }}
  td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
  .sub {{ color: var(--muted); font-size: 12px; }}
  .pill {{ display: inline-block; font-size: 12px; font-weight: 600; padding: 2px 9px; border-radius: 999px; }}
  .pill.pos {{ background: var(--pos-tint); color: var(--pos); }}
  .pill.neg {{ background: var(--neg-tint); color: var(--neg); }}
  .pill.info {{ background: var(--info-tint); color: var(--info); }}
  .pill.muted {{ background: var(--mute-tint); color: var(--ink-2); }}
  .band .pill.pos {{ background: rgba(52, 211, 153, 0.18); color: #a7f3d0; }}
  .band .pill.muted {{ background: rgba(255,255,255,0.14); color: #fff; }}
  .empty {{ color: var(--muted); margin: 6px 0; }}
  .chart {{ position: relative; }}
  .chart svg {{ width: 100%; height: auto; display: block; overflow: visible; }}
  .chart .grid {{ stroke: var(--grid); stroke-width: 1; }}
  .chart .zero {{ stroke: var(--axis); stroke-width: 1; }}
  .chart .tick {{ fill: var(--muted); font-size: 11px; }}
  .chart .line {{ fill: none; stroke: var(--accent); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }}
  .chart .end-dot, .chart .hover-dot {{ fill: var(--accent); stroke: var(--surface); stroke-width: 2; }}
  .chart .end-label {{ fill: var(--ink); font-size: 12px; font-weight: 600; }}
  .chart .crosshair {{ stroke: var(--axis); stroke-width: 1; }}
  .chart .hit {{ fill: transparent; cursor: crosshair; }}
  .tooltip {{ position: absolute; pointer-events: none; background: var(--surface); color: var(--ink); border: 1px solid var(--border);
    box-shadow: var(--shadow); border-radius: 10px; padding: 8px 10px; font-size: 12px; min-width: 150px; transform: translate(-50%, calc(-100% - 12px)); }}
  .tooltip strong {{ display: block; font-size: 13px; }}
  .rules {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
  .rule-card {{ border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px; }}
  .rule-card h3 {{ margin: 0 0 6px; font-size: 14px; }}
  .rule-card ul {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; }}
  .rule-name {{ font-weight: 600; font-size: 13px; }}
  .rule-card p {{ margin: 2px 0 0; color: var(--ink-2); font-size: 13px; }}
  .filters {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }}
  select {{ font: inherit; font-size: 13px; color: var(--ink); background: var(--surface); border: 1px solid var(--axis); border-radius: 8px; padding: 6px 8px; }}
  footer {{ text-align: center; color: var(--muted); font-size: 12px; padding: 0 16px 32px; }}
  @media (max-width: 760px) {{
    .tiles {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .rules {{ grid-template-columns: 1fr; }}
    .tile-value {{ font-size: 22px; }}
  }}
</style>
</head>
<body>
<header class="band">
  <div class="wrap">
    <h1>Kalshi Bot</h1>
    <div class="meta">{mode_pill}<span>Updated {updated} · refreshes every minute</span></div>
    <nav><a href="#overview">Overview</a><a href="#open">Open bets</a><a href="#live">Live</a><a href="#by-rule">By rule</a><a href="#rules">Rules</a><a href="#history">History</a></nav>
  </div>
</header>
<main>
  <section id="overview" class="tiles">{tiles}</section>
  <section class="card">
    <h2>Cumulative profit / loss</h2>
    <p class="hint">Every settled bet, in the order it settled</p>
    {chart}
  </section>
  <section id="open" class="card">
    <h2>Open bets</h2>
    <p class="hint">Placed and waiting on the result</p>
    {open_table}
  </section>
  <section id="live" class="card">
    <h2>Live games</h2>
    <p class="hint">In progress, with a favorite that qualifies for a rule (pregame odds locked)</p>
    {live_table}
  </section>
  <section id="by-rule" class="card">
    <h2>Results by rule</h2>
    <p class="hint">How each rule has done</p>
    {rule_table}
  </section>
  <section id="rules" class="card">
    <h2>Rules</h2>
    <p class="hint">$15 per bet, buying the pregame favorite on Kalshi</p>
    <div class="rules">{rule_cards}</div>
  </section>
  <section id="history" class="card">
    <h2>Trade history</h2>
    <p class="hint">Every bet the bot has placed, newest first</p>
    <div class="filters">
      <select id="f-sport" aria-label="Filter by sport"><option value="">All sports</option>{sport_opts}</select>
      <select id="f-rule" aria-label="Filter by rule"><option value="">All rules</option>{rule_opts}</select>
    </div>
    {history_table}
  </section>
</main>
<footer>Bets placed automatically by the bot on Kalshi. Bets placed by hand are not included.</footer>
<script>
(function () {{
  var s = document.getElementById("f-sport"), r = document.getElementById("f-rule");
  function apply() {{
    document.querySelectorAll("table.history tbody tr").forEach(function (tr) {{
      tr.hidden = (s.value && tr.dataset.sport !== s.value) || (r.value && tr.dataset.rule !== r.value);
    }});
  }}
  if (s && r) {{ s.onchange = apply; r.onchange = apply; }}

  var data = document.getElementById("pnl-data");
  if (!data) return;
  var pts = JSON.parse(data.textContent), svg = document.querySelector("#pnl-chart svg");
  var cross = document.getElementById("pnl-cross"), dot = document.getElementById("pnl-dot");
  var tip = document.getElementById("pnl-tip"), hit = svg.querySelector(".hit");
  function show(evt) {{
    var box = svg.getBoundingClientRect(), vb = svg.viewBox.baseVal;
    var x = (evt.clientX - box.left) * vb.width / box.width, best = pts[0];
    pts.forEach(function (p) {{ if (Math.abs(p.x - x) < Math.abs(best.x - x)) best = p; }});
    cross.setAttribute("x1", best.x); cross.setAttribute("x2", best.x); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", best.x); dot.setAttribute("cy", best.y); dot.setAttribute("visibility", "visible");
    tip.replaceChildren();
    var head = document.createElement("strong"); head.textContent = best.cum + " total";
    var line1 = document.createElement("div"); line1.textContent = best.result + " " + best.pnl + " · " + best.team;
    var line2 = document.createElement("div"); line2.className = "sub"; line2.textContent = best.when;
    tip.append(head, line1, line2);
    tip.style.left = (best.x * box.width / vb.width) + "px";
    tip.style.top = (best.y * box.height / vb.height) + "px";
    tip.hidden = false;
  }}
  function hide() {{ cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); tip.hidden = true; }}
  hit.addEventListener("pointermove", show);
  hit.addEventListener("pointerleave", hide);
}})();
</script>
</body>
</html>
"""
