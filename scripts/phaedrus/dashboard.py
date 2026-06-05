"""
Phaedrus local-host dashboard.

A lightweight Flask + HTMX + Tailwind (via CDN) web UI that runs entirely on
the user's machine. Reads Phaedrus's actual data from ~/.phaedrus/.

No npm, no build step, no React knowledge needed. Pure Python.

Run:
    python scripts/phaedrus/dashboard.py
    # then open http://localhost:5050 in your browser

Pages:
    /           — today's triage (k/i/l on each card)
    /projects   — visual project organiser
    /cards      — searchable card browser
    /settings   — preferences, rules, corrections
"""
import json
import os
import sys
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from threading import Timer

from flask import Flask, request, render_template_string, redirect, url_for, jsonify

sys.path.insert(0, str(Path(__file__).parent))
from projects_config import load_config, write_config, show_summary
from preferences import load_preferences, PREFS_PATH
from corrections import load_recent_corrections, load_rules, record_correction
from provider_icons import provider_svg  # real brand marks (lobehub/icons, MIT)

PHAEDRUS_HOME = Path.home() / ".phaedrus"
CARDS_DIR = PHAEDRUS_HOME / "cards"
TRIAGE_LOG = PHAEDRUS_HOME / "triage_log.jsonl"

app = Flask(__name__)


# === Helpers ===

def load_all_cards():
    cards = []
    if not CARDS_DIR.exists():
        return cards
    for p in sorted(CARDS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            card = json.loads(p.read_text())
            card["_session_id"] = p.stem
            card["_path"] = str(p)
            cards.append(card)
        except Exception:
            continue
    return cards


def get_triage_decisions():
    decided = {}
    if TRIAGE_LOG.exists():
        for line in TRIAGE_LOG.read_text().splitlines():
            try:
                d = json.loads(line)
                decided[d["key"]] = d
            except Exception:
                continue
    return decided


LATER_SCHEDULE_DAYS = [3, 7, 14, 30]  # escalating snooze — each repeat waits longer


def log_triage_decision(key: str, decision: str, item_text: str = "", session_id: str = ""):
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "key": key,
        "decision": decision,
        "item_text": item_text,
        "session_id": session_id,
    }
    if decision == "later":
        from datetime import timedelta
        # Smart "Later": the more times you snooze a card, the longer it waits
        # (3 → 7 → 14 → 30 days) rather than a flat 7. Count prior snoozes for this key.
        prior = get_triage_decisions().get(key, {})
        count = int(prior.get("later_count", 0) or 0) + 1
        days = LATER_SCHEDULE_DAYS[min(count - 1, len(LATER_SCHEDULE_DAYS) - 1)]
        record["later_count"] = count
        record["later_days"] = days
        record["later_until"] = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    with TRIAGE_LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")
    return record


def item_key(session_id: str, kind: str, text: str) -> str:
    import hashlib
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    return f"{session_id}:{kind}:{h}"


TYPE_WEIGHTS = {
    "commitment": 1.0, "worry": 0.7, "question": 0.5,
    "idea": 0.4, "observation": 0.3, "tangent": 0.3, "side-question": 0.5,
}


def _item_salience(it):
    """How much a single fragment deserves to represent its conversation."""
    s = TYPE_WEIGHTS.get(it.get("type", ""), 0.3)
    if it.get("_kind") == "open_item":   # things to act on rank above loose sparks
        s += 0.15
    return s


def get_triage_cards(limit=12, offset=0):
    """One triage entry per CONVERSATION, not per fragment.

    The old design surfaced every open_item and spark as its own decision —
    547 conversations became 5,600+ things to triage. A daily-review tool can't
    ask that. Here each card contributes a single representative line (its most
    salient item), so triage reads as "review this conversation", and the heavy
    detail stays one click away on the full card.

    Returns (total_undecided, batch) where batch is a list of
    (score, representative_item, card, card_key, item_count).
    """
    cards = load_all_cards()  # already newest-first, so ties break by recency
    decisions = get_triage_decisions()
    now_iso = datetime.now(timezone.utc).isoformat()

    ranked = []
    for card in cards:
        card_key = card["_session_id"]  # decisions are per-conversation now
        d = decisions.get(card_key)
        if d:
            if d.get("decision") in ("keep", "ignore", "done"):
                continue
            if d.get("decision") == "later":
                until = d.get("later_until")
                if until and now_iso < until:
                    continue

        items = []
        for it in (card.get("open_items") or []):
            # Completion-aware: finished work stays OUT of triage. Items with no
            # status yet (not re-judged) default to showing, as before.
            if (it.get("status") or "open").lower() == "done":
                continue
            it["_kind"] = "open_item"; items.append(it)
        for sp in (card.get("sparks") or []):
            sp["_kind"] = "spark"; items.append(sp)
        if not items:
            continue  # every actionable item is done (and no sparks left) — nothing to review

        rep = max(items, key=_item_salience)
        score = _item_salience(rep)
        try:
            if float(card.get("project_match", {}).get("confidence", 0)) > 0.7:
                score += 0.2
        except (TypeError, ValueError):
            pass
        ranked.append((score, rep, card, card_key, len(items)))

    ranked.sort(key=lambda x: -x[0])
    total = len(ranked)
    return total, ranked[offset:offset + limit]


# === HTML template (Tailwind via CDN, HTMX for interactivity) ===

BASE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Phaedrus — {{ page_title }}</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 50 50'%3E%3Crect width='50' height='50' rx='11' fill='%23221e18'/%3E%3Ccircle cx='25' cy='26' r='13' fill='none' stroke='%233f7355' stroke-width='3.6'/%3E%3Cline x1='25' y1='5.5' x2='25' y2='46.5' stroke='%233f7355' stroke-width='3.6' stroke-linecap='round'/%3E%3C/svg%3E">
<script src="/static/htmx.min.js"></script>
<style>
  :root {
    --bg: #f6f2ea;
    --surface: #fffdf8;
    --surface-2: #efe9dd;
    --ink: #221e18;
    --ink-2: #6b6353;
    --ink-3: #a59c88;
    --line: rgba(34,30,24,0.10);
    --line-2: rgba(34,30,24,0.16);
    --accent: #3f7355;
    --accent-ink: #2b5740;
    --accent-soft: #e2ece4;
    --ok: #2f7d70; --ok-soft: #dfeeea;
    --warn: #a9701f; --warn-soft: #f5ead2;
    --info: #3d6589; --info-soft: #e4ecf3;
    --serif: "Iowan Old Style","Palatino Linotype",Palatino,Georgia,"Times New Roman",serif;
    --sans: -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,Helvetica,Arial,sans-serif;
    --radius: 14px; --radius-sm: 9px;
    --shadow: 0 1px 2px rgba(33,31,27,0.04), 0 1px 1px rgba(33,31,27,0.03);
    --shadow-lift: 0 6px 22px rgba(33,31,27,0.09);
  }
  * { box-sizing: border-box; }
  html { -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font-family: var(--sans); font-size: 15px; line-height: 1.55; min-height: 100vh;
  }
  a { color: inherit; text-decoration: none; }
  code { font-family: ui-monospace,SFMono-Regular,Menlo,monospace; font-size: 0.85em;
         background: var(--surface-2); padding: 1px 6px; border-radius: 5px; color: var(--ink-2); }
  ::selection { background: var(--accent-soft); }

  /* Top bar */
  .nav { position: sticky; top: 0; z-index: 20; background: rgba(250,248,244,0.82);
         backdrop-filter: saturate(180%) blur(12px); border-bottom: 1px solid var(--line); }
  .nav-inner { max-width: 940px; margin: 0 auto; padding: 0 24px; height: 58px;
               display: flex; align-items: center; justify-content: space-between; }
  .brand { display: flex; align-items: baseline; gap: 9px; }
  .brand .mark { font-family: var(--serif); font-size: 21px; font-weight: 600; letter-spacing: -0.01em; }
  .brand .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--accent); display: inline-block; transform: translateY(-2px); }
  .brand .sub { font-size: 11px; color: var(--ink-3); letter-spacing: 0.08em; text-transform: uppercase; }
  .tabs { display: flex; gap: 2px; }
  .tab { padding: 7px 13px; border-radius: 999px; font-size: 13.5px; color: var(--ink-2);
         transition: background .15s, color .15s; }
  .tab:hover { background: var(--surface-2); color: var(--ink); }
  .tab.on { background: var(--ink); color: #fff; }

  main { max-width: 940px; margin: 0 auto; padding: 38px 24px 80px; }
  .narrow { max-width: 660px; margin-inline: auto; }
  .page-h { font-family: var(--serif); font-size: 27px; font-weight: 600; letter-spacing: -0.015em; margin: 0 0 4px; }
  .page-sub { color: var(--ink-2); font-size: 14px; margin: 0 0 26px; }

  /* Card surface */
  .card { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
          box-shadow: var(--shadow); padding: 20px 22px; }
  .card + .card { margin-top: 14px; }
  .card-hover { transition: box-shadow .18s, border-color .18s, transform .18s; }
  .card-hover:hover { box-shadow: var(--shadow-lift); border-color: var(--line-2); }

  /* Pills */
  .pill { display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px; font-weight: 600;
          letter-spacing: 0.02em; padding: 3px 10px; border-radius: 999px; }
  .pill .pdot { width: 6px; height: 6px; border-radius: 50%; }
  .pill-commitment { background: var(--accent-soft); color: var(--accent-ink); }
  .pill-commitment .pdot { background: var(--accent); }
  .pill-worry { background: var(--warn-soft); color: var(--warn); } .pill-worry .pdot { background: var(--warn); }
  .pill-question { background: var(--info-soft); color: var(--info); } .pill-question .pdot { background: var(--info); }
  .pill-idea { background: var(--ok-soft); color: var(--ok); } .pill-idea .pdot { background: var(--ok); }
  .pill-neutral { background: var(--surface-2); color: var(--ink-2); } .pill-neutral .pdot { background: var(--ink-3); }
  .chip { font-size: 12px; color: var(--ink-2); background: var(--surface-2); padding: 3px 9px; border-radius: 7px; }

  /* Buttons */
  .btn { font: inherit; font-size: 13.5px; font-weight: 500; border: 1px solid transparent;
         padding: 8px 18px; border-radius: 999px; cursor: pointer; transition: all .14s; }
  .btn-keep { background: var(--ink); color: #fff; }
  .btn-keep:hover { background: #000; }
  .btn-ghost { background: transparent; color: var(--ink-2); border-color: var(--line-2); }
  .btn-ghost:hover { background: var(--surface-2); color: var(--ink); }
  .btn-soft { background: var(--surface-2); color: var(--ink-2); }
  .btn-soft:hover { background: #ece7dd; color: var(--ink); }

  .quote { font-family: var(--serif); font-style: italic; color: var(--ink); font-size: 17px; line-height: 1.5; }
  .meta { font-size: 12.5px; color: var(--ink-3); }
  .empty { text-align: center; padding: 64px 24px; color: var(--ink-3); }
  .empty .big { font-family: var(--serif); font-size: 19px; color: var(--ink-2); margin-bottom: 6px; }
  input[type=search], input[type=text] { font: inherit; font-size: 14px; padding: 9px 13px;
      border: 1px solid var(--line-2); border-radius: var(--radius-sm); background: var(--surface); color: var(--ink); width: 100%; }
  input[type=search]:focus, input[type=text]:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }

  /* Light filter bar + list rows (UX: narrow, then scan — no wall of cards) */
  select { font: inherit; font-size: 13px; padding: 8px 11px; border: 1px solid var(--line-2);
           border-radius: var(--radius-sm); background: var(--surface); color: var(--ink-2); cursor: pointer; }
  select:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
  .filterbar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 18px; }
  .filterbar input[type=search] { flex: 1 1 200px; width: auto; }
  .lrow { display: flex; align-items: baseline; justify-content: space-between; gap: 16px;
          padding: 11px 6px; border-bottom: 1px solid var(--line); transition: background .12s; }
  .lrow:hover { background: var(--surface); }
  .lrow .t { font-family: var(--serif); font-size: 15.5px; font-weight: 600; color: var(--ink);
             overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .lrow .m { display: flex; align-items: center; gap: 10px; white-space: nowrap; flex-shrink: 0;
             color: var(--ink-3); font-size: 12.5px; }

  footer { max-width: 940px; margin: 0 auto; padding: 24px; color: var(--ink-3); font-size: 12px; border-top: 1px solid var(--line); }
  .fade-in { animation: fade .2s ease; } @keyframes fade { from { opacity: 0; transform: translateY(3px); } to { opacity: 1; } }
</style>
</head>
<body>
<nav class="nav">
  <div class="nav-inner">
    <a href="/" class="brand">
      <span class="dot"></span>
      <span class="mark">Phaedrus</span>
      <span class="sub">daily review</span>
    </a>
    <div class="tabs">
      <a href="/" class="tab {% if active == 'triage' %}on{% endif %}">Triage</a>
      <a href="/cards" class="tab {% if active == 'cards' %}on{% endif %}">Cards</a>
      <a href="/projects" class="tab {% if active == 'projects' %}on{% endif %}">Projects</a>
      <a href="/import" class="tab {% if active == 'import' %}on{% endif %}">Import</a>
      <a href="/settings" class="tab {% if active == 'settings' %}on{% endif %}">Settings</a>
      <a href="/guide" class="tab {% if active == 'guide' %}on{% endif %}">Guide</a>
    </div>
  </div>
</nav>
<main>
{{ content | safe }}
</main>
<footer>Phaedrus · runs entirely on your machine · data at <code>~/.phaedrus/</code></footer>
</body>
</html>
"""

# type -> (pill css class, label, left-accent colour)
TYPE_BADGES = {
    "commitment":   ("pill-commitment", "Commitment", "var(--accent)"),
    "worry":        ("pill-worry", "Worry", "var(--warn)"),
    "question":     ("pill-question", "Question", "var(--info)"),
    "idea":         ("pill-idea", "Idea", "var(--ok)"),
    "observation":  ("pill-neutral", "Observation", "var(--ink-3)"),
    "tangent":      ("pill-neutral", "Tangent", "var(--ink-3)"),
    "side-question":("pill-question", "Side question", "var(--info)"),
}

# status -> (label, text colour, soft background)
STATUS_META = {
    "active":   ("Active", "var(--ok)", "var(--ok-soft)"),
    "on-hold":  ("On hold", "var(--warn)", "var(--warn-soft)"),
    "dormant":  ("Dormant", "var(--ink-3)", "var(--surface-2)"),
    "finished": ("Finished", "var(--info)", "var(--info-soft)"),
    "archived": ("Archived", "var(--ink-3)", "var(--surface-2)"),
}

# platform source -> display label (shared by triage + cards)
SOURCE_LABEL = {
    "claude": "Claude", "claude-code": "Claude Code", "chatgpt": "ChatGPT",
    "gemini": "Gemini", "grok": "Grok", "perplexity": "Perplexity",
    "deepseek": "DeepSeek", "obsidian": "Obsidian", "apple-notes": "Apple Notes",
    "codex": "Codex",
}

# Tiny inline provider glyphs (monochrome, inherit currentColor) — a recognisable
# mark per source without shipping logo assets. 14px, sits inline before the label.
_PROVIDER_GLYPH = {
    # Claude — starburst
    "claude": '<svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor"><path d="M12 2l1.9 6.1L20 6.5l-4.3 4.6L20 17l-6.1-1.6L12 22l-1.9-6.6L4 17l4.3-5.9L4 6.5l6.1 1.6z"/></svg>',
    # ChatGPT — ring
    "chatgpt": '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="8"/></svg>',
    # Gemini — sparkle/diamond
    "gemini": '<svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor"><path d="M12 2c.6 5.4 4.6 9.4 10 10-5.4.6-9.4 4.6-10 10-.6-5.4-4.6-9.4-10-10 5.4-.6 9.4-4.6 10-10z"/></svg>',
    # Grok — slash
    "grok": '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 19L18 5M10 19l8-9"/></svg>',
    # Perplexity — node
    "perplexity": '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3.2"/><path d="M12 2v6M12 16v6M4 7l5 3M20 7l-5 3"/></svg>',
    # DeepSeek — whale curve
    "deepseek": '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 14c4 4 14 4 18-4-2 6-8 9-13 7"/></svg>',
    "obsidian": '<svg viewBox="0 0 24 24" width="13" height="13" fill="currentColor"><path d="M12 2l7 5-3 13H8L5 7z"/></svg>',
    "apple-notes": '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2"><rect x="5" y="3" width="14" height="18" rx="2"/><path d="M8 8h8M8 12h8M8 16h4"/></svg>',
    "codex": '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9 8l-4 4 4 4M15 8l4 4-4 4"/></svg>',
}
_PROVIDER_GLYPH["claude-code"] = _PROVIDER_GLYPH["codex"]


def provider_chip(src: str, url: str = "", *, link: bool = True) -> str:
    """A source chip with the provider glyph + label. If url + link, deep-links to
    the original conversation (opens new tab)."""
    src = (src or "").lower()
    if not src:
        return ""
    label = SOURCE_LABEL.get(src, src.title())
    glyph = provider_svg(src) or _PROVIDER_GLYPH.get(src, "")  # real mark, else hand-drawn fallback
    inner = f'<span style="display:inline-flex;align-items:center;gap:5px">{glyph}{label}</span>'
    style = "background:var(--accent-soft);color:var(--accent-ink)"
    if link and url:
        return f'<a href="{url}" target="_blank" rel="noopener" class="chip" style="{style};text-decoration:none">{inner}</a>'
    return f'<span class="chip" style="{style}">{inner}</span>'


# === Pages ===

TRIAGE_BATCH = 12  # conversations shown per "page" of triage


def _render_triage_card(score, item, card, card_key, item_count):
    """One conversation as a triage card: its sharpest line as the hero quote,
    the heavy detail kept one click away on the full card. Keep/Ignore/Later
    decide the whole conversation."""
    t = item.get("type", "")
    pill_class, label, accent = TYPE_BADGES.get(t, ("pill-neutral", t or "note", "var(--ink-3)"))
    topic = (card.get("topic") or "Untitled").strip()
    text = (item.get("text") or "").strip()
    meta = card.get("_meta", {}) or {}
    src = (meta.get("source") or "").lower()
    src_chip = provider_chip(src, link=False)
    date = (meta.get("extracted_at") or "")[:10]
    date_meta = f'<span class="meta">{date}</span>' if date else ""
    proj = item.get("project") or card.get("project_match", {}).get("id", "")
    proj_txt = f" · {proj}" if proj and proj not in ("none", "new") else ""
    ctx = (card.get("context") or "").strip()
    ctx_excerpt = (ctx[:170].rsplit(" ", 1)[0] + "…") if len(ctx) > 170 else ctx
    ctx_html = (f'<div style="margin-top:9px;font-size:13.5px;color:var(--ink-2);line-height:1.45">{ctx_excerpt}</div>'
                if ctx_excerpt else "")
    session_id = card["_session_id"]
    rest = item_count - 1
    more_txt = (f' · <span style="color:var(--ink-3)">{rest} more point{"s" if rest != 1 else ""} in this conversation</span>'
                if rest > 0 else "")

    # Completion-aware controls. An "uncertain" actionable item ASKS rather than
    # assumes — Khalil decides Yes-done / Not-yet. Everything else: Keep/Ignore/Later.
    status = (item.get("status") or "").lower()
    uncertain = item.get("_kind") == "open_item" and status == "uncertain"

    def _btn(decision, lbl, cls):
        return (f'<button class="btn {cls}" hx-post="/triage/decide" '
                f'hx-vals=\'{{"key": "{card_key}", "decision": "{decision}", "session_id": "{session_id}"}}\'>{lbl}</button>')

    if uncertain:
        reason = (item.get("status_reason") or "").strip()
        reason_txt = f" — {reason}" if reason else ""
        ask_html = (f'<div class="meta" style="margin-top:11px;color:var(--warn)">'
                    f'❓ Not sure this got done{reason_txt}. Did you finish it?</div>')
        buttons = _btn("done", "Yes, done", "btn-keep") + _btn("keep", "Not yet", "btn-ghost") + _btn("later", "Later", "btn-soft")
    else:
        ask_html = ""
        buttons = _btn("keep", "Keep", "btn-keep") + _btn("ignore", "Ignore", "btn-ghost") + _btn("later", "Later", "btn-soft")

    return f"""
    <div class="card card-hover fade-in">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:13px">
            <span style="font-size:11px;font-weight:600;letter-spacing:0.09em;text-transform:uppercase;color:{accent}">{label}</span>
            <span style="display:flex;align-items:center;gap:8px">{src_chip}{date_meta}</span>
        </div>
        <div class="quote" style="font-size:19px">{text}</div>
        <div class="meta" style="margin-top:7px">from “{topic}”{proj_txt}{more_txt}</div>
        {ctx_html}
        {ask_html}
        <a href="/cards/{session_id}" class="meta" style="display:inline-block;margin-top:9px;color:var(--accent-ink)">Open full card →</a>
        <div style="display:flex;gap:8px;margin-top:15px" hx-target="this" hx-swap="outerHTML">
            {buttons}
        </div>
    </div>
    """


def _more_slot(next_offset, total):
    """The 'show more' control — replaces itself with the next batch + a fresh slot."""
    remaining = total - next_offset
    if remaining <= 0:
        return ""
    return (f'<div id="more-slot" style="text-align:center;margin-top:22px">'
            f'<button class="btn btn-ghost" hx-get="/triage/batch?offset={next_offset}" '
            f'hx-target="#more-slot" hx-swap="outerHTML">'
            f'Show more · {remaining} conversation{"s" if remaining != 1 else ""} left</button></div>')


@app.route("/")
def page_triage():
    total, batch = get_triage_cards(limit=TRIAGE_BATCH, offset=0)
    today = datetime.now().strftime("%A, %d %B %Y")
    if not batch:
        body = """
        <div class="empty">
          <div class="big">Nothing to triage right now.</div>
          <div>You're clear. New conversations surface here after the next extraction.</div>
        </div>
        """
        sub = today
    else:
        cards_html = "".join(_render_triage_card(*b) for b in batch)
        body = f'<div id="triage-list">{cards_html}</div>{_more_slot(TRIAGE_BATCH, total)}'
        sub = f'{today} · {total} conversation{"s" if total != 1 else ""} to review'
    content = f"""
    <div class="narrow">
      <h1 class="page-h">Today's triage</h1>
      <p class="page-sub">{sub}</p>
      {body}
    </div>
    """
    return render_template_string(BASE, page_title="Triage", active="triage", content=content)


@app.route("/triage/batch")
def triage_batch():
    """Next page of triage cards, appended via HTMX when 'show more' is clicked."""
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    total, batch = get_triage_cards(limit=TRIAGE_BATCH, offset=offset)
    cards_html = "".join(_render_triage_card(*b) for b in batch)
    return cards_html + _more_slot(offset + TRIAGE_BATCH, total)


@app.route("/triage/decide", methods=["POST"])
def triage_decide():
    key = request.form.get("key", "")
    decision = request.form.get("decision", "")
    text = request.form.get("text", "")
    session_id = request.form.get("session_id", "")
    rec = log_triage_decision(key, decision, text, session_id)
    styles = {
        "keep":   ("var(--ok)", "var(--ok-soft)", "Kept"),
        "ignore": ("var(--ink-3)", "var(--surface-2)", "Ignored"),
        "done":   ("var(--ok)", "var(--ok-soft)", "Done"),
        "later":  ("var(--warn)", "var(--warn-soft)", f"Back in {rec.get('later_days', 7)} days"),
    }
    color, bg, label = styles.get(decision, ("var(--ink-2)", "var(--surface-2)", decision.capitalize()))
    return (f'<div class="fade-in" style="display:flex;align-items:center;gap:9px;padding:15px 20px;'
            f'border-radius:var(--radius);background:{bg};color:{color};font-size:14px;font-weight:600">'
            f'<span>✓</span><span>{label}</span></div>')


CARDS_PER_PAGE = 25


ITEM_TYPES = ["commitment", "worry", "question", "idea"]
DATE_WINDOWS = [("7", "Last 7 days"), ("30", "Last 30 days"), ("90", "Last 3 months")]


@app.route("/cards")
def page_cards():
    q = request.args.get("q", "").strip().lower()
    f_source = request.args.get("source", "").strip().lower()
    f_project = request.args.get("project", "").strip()
    f_status = request.args.get("status", "").strip()      # open | uncertain | done
    f_type = request.args.get("type", "").strip()          # commitment | worry | question | idea
    f_date = request.args.get("date", "").strip()          # 7 | 30 | 90
    group = request.args.get("group", "").strip()          # project | source
    sort = request.args.get("sort", "recent").strip()
    try:
        page = max(1, int(request.args.get("p", 1)))
    except (TypeError, ValueError):
        page = 1

    all_cards = load_all_cards()
    now = datetime.now(timezone.utc)

    src_counts, proj_counts = {}, {}
    for c in all_cards:
        s = (c.get("_meta", {}).get("source") or "").lower()
        if s:
            src_counts[s] = src_counts.get(s, 0) + 1
        p = c.get("project_match", {}).get("id", "")
        if p and p not in ("none", "new"):
            proj_counts[p] = proj_counts.get(p, 0) + 1

    def statuses(c):
        return [(it.get("status") or "open").lower() for it in (c.get("open_items") or [])]

    def all_types(c):
        return ([(it.get("type") or "").lower() for it in (c.get("open_items") or [])]
                + [(sp.get("type") or "").lower() for sp in (c.get("sparks") or [])])

    def age_days(c):
        ts = c.get("_meta", {}).get("extracted_at") or ""
        try:
            return (now - datetime.fromisoformat(ts.replace("Z", "+00:00"))).days
        except Exception:
            return None

    def keep(c):
        if q and q not in json.dumps(c, ensure_ascii=False).lower():
            return False
        if f_source and (c.get("_meta", {}).get("source") or "").lower() != f_source:
            return False
        if f_project and c.get("project_match", {}).get("id", "") != f_project:
            return False
        if f_type and f_type not in all_types(c):
            return False
        if f_date:
            d = age_days(c)
            if d is None or d > int(f_date):
                return False
        if f_status:
            sts = statuses(c)
            if f_status == "open" and not any(s in ("open", "uncertain") for s in sts):
                return False
            if f_status == "uncertain" and "uncertain" not in sts:
                return False
            if f_status == "done" and not (sts and all(s == "done" for s in sts)):
                return False
        return True

    cards = [c for c in all_cards if keep(c)]

    def n_items(c):
        return len(c.get("open_items") or []) + len(c.get("sparks") or [])

    def when(c):
        return c.get("_meta", {}).get("extracted_at") or ""

    if sort == "oldest":
        cards.sort(key=when)
    elif sort == "items":
        cards.sort(key=n_items, reverse=True)
    elif sort == "az":
        cards.sort(key=lambda c: (c.get("topic") or "").lower())
    else:
        cards.sort(key=when, reverse=True)

    def opt(val, cur, label):
        return f'<option value="{val}"{" selected" if val == cur else ""}>{label}</option>'

    src_opts = opt("", f_source, "All sources") + "".join(
        opt(s, f_source, f"{SOURCE_LABEL.get(s, s.title())} ({n})")
        for s, n in sorted(src_counts.items(), key=lambda x: -x[1]))
    proj_opts = opt("", f_project, "All projects") + "".join(
        opt(p, f_project, f"{p} ({n})") for p, n in sorted(proj_counts.items(), key=lambda x: -x[1]))
    type_opts = opt("", f_type, "Any type") + "".join(opt(t, f_type, t.capitalize() + "s") for t in ITEM_TYPES)
    date_opts = opt("", f_date, "Any time") + "".join(opt(v, f_date, l) for v, l in DATE_WINDOWS)
    status_opts = (opt("", f_status, "Any status") + opt("open", f_status, "Has open")
                   + opt("uncertain", f_status, "Has unsure") + opt("done", f_status, "All done"))
    sort_opts = "".join(opt(v, sort, l) for v, l in
                        [("recent", "Newest"), ("oldest", "Oldest"), ("items", "Most items"), ("az", "A–Z")])
    group_opts = opt("", group, "No grouping") + opt("project", group, "Group: project") + opt("source", group, "Group: source")

    filterbar = (
        '<form method="get" class="filterbar">'
        f'<input type="search" name="q" value="{q}" placeholder="Search…">'
        f'<select name="source" onchange="this.form.submit()">{src_opts}</select>'
        f'<select name="project" onchange="this.form.submit()">{proj_opts}</select>'
        f'<select name="type" onchange="this.form.submit()">{type_opts}</select>'
        f'<select name="status" onchange="this.form.submit()">{status_opts}</select>'
        f'<select name="date" onchange="this.form.submit()">{date_opts}</select>'
        f'<select name="sort" onchange="this.form.submit()">{sort_opts}</select>'
        f'<select name="group" onchange="this.form.submit()">{group_opts}</select>'
        '<noscript><button class="btn btn-keep">Apply</button></noscript>'
        '</form>'
    )

    def row(c):
        topic = (c.get("topic") or "Untitled")[:100]
        src = (c.get("_meta", {}).get("source") or "").lower()
        sts = statuses(c)
        n_open = sum(1 for s in sts if s in ("open", "uncertain"))
        if "uncertain" in sts:
            badge = '<span style="color:var(--warn)">unsure</span>'
        elif n_open:
            badge = f'<span style="color:var(--accent-ink)">{n_open} open</span>'
        elif sts:
            badge = '<span style="color:var(--ink-3)">done</span>'
        else:
            badge = ''
        proj = c.get("project_match", {}).get("id", "")
        proj_txt = f'<span>{proj}</span>' if proj and proj not in ("none", "new") and not group else ""
        return (f'<a href="/cards/{c["_session_id"]}" class="lrow">'
                f'<span class="t">{topic}</span>'
                f'<span class="m">{badge}{proj_txt}{provider_chip(src, link=False)}</span></a>')

    total = len(cards)

    # GROUPED view: collapse the whole list into a handful of expandable sections,
    # so 547 rows become ~12 project headers you open on demand — no endless scroll.
    if group in ("project", "source"):
        def bucket(c):
            if group == "source":
                return (c.get("_meta", {}).get("source") or "").lower() or "unknown"
            p = c.get("project_match", {}).get("id", "")
            return p if p and p not in ("none", "new") else "unsorted"
        buckets = {}
        for c in cards:
            buckets.setdefault(bucket(c), []).append(c)

        sections = ""
        CAP = 40
        for key, items in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
            label = SOURCE_LABEL.get(key, key.title()) if group == "source" else key
            inner = "".join(row(c) for c in items[:CAP])
            if len(items) > CAP and key not in ("unsorted", "unknown"):
                drill = f"?{group}={key}"
                inner += (f'<a href="{drill}" class="meta" style="display:block;padding:11px 6px;color:var(--accent-ink)">'
                          f'+ {len(items) - CAP} more in {label} — open all →</a>')
            sections += (
                f'<details style="border-bottom:1px solid var(--line)">'
                f'<summary style="cursor:pointer;list-style:none;display:flex;align-items:center;justify-content:space-between;'
                f'gap:12px;padding:13px 6px;font-family:var(--serif);font-size:16px;font-weight:600">'
                f'<span><span style="color:var(--accent-ink);font-family:var(--sans);font-size:12px">▸ </span>{label}</span>'
                f'<span class="meta">{len(items)}</span></summary>'
                f'<div style="padding-bottom:8px">{inner}</div></details>'
            )
        body = sections or '<div class="empty"><div class="big">Nothing matches.</div></div>'
        showing = f"{total} in {len(buckets)} {'sources' if group == 'source' else 'projects'}"
        content = f"""
        <h1 class="page-h">Cards</h1>
        <p class="page-sub">{showing} · click a {group} to expand</p>
        {filterbar}
        <div>{body}</div>
        """
        return render_template_string(BASE, page_title="Cards", active="cards", content=content)

    # FLAT view: paginated light rows.
    pages = max(1, (total + CARDS_PER_PAGE - 1) // CARDS_PER_PAGE)
    page = min(page, pages)
    window = cards[(page - 1) * CARDS_PER_PAGE: page * CARDS_PER_PAGE]
    rows = "".join(row(c) for c in window) or (
        '<div class="empty"><div class="big">Nothing matches.</div>'
        '<div>Clear a filter and try again.</div></div>')

    pager = ""
    if pages > 1:
        base = f"?q={q}&source={f_source}&project={f_project}&type={f_type}&status={f_status}&date={f_date}&sort={sort}"
        prev = (f'<a class="btn btn-ghost" href="{base}&p={page-1}">‹ Prev</a>' if page > 1
                else '<span class="btn btn-ghost" style="opacity:.35">‹ Prev</span>')
        nxt = (f'<a class="btn btn-ghost" href="{base}&p={page+1}">Next ›</a>' if page < pages
               else '<span class="btn btn-ghost" style="opacity:.35">Next ›</span>')
        pager = (f'<div style="display:flex;align-items:center;justify-content:space-between;margin-top:20px">'
                 f'{prev}<span class="meta">Page {page} of {pages}</span>{nxt}</div>')

    showing = f"{(page-1)*CARDS_PER_PAGE+1}–{min(page*CARDS_PER_PAGE, total)} of {total}" if total else "0"
    content = f"""
    <h1 class="page-h">Cards</h1>
    <p class="page-sub">{showing} · {len(all_cards)} conversations in all</p>
    {filterbar}
    <div>{rows}</div>
    {pager}
    """
    return render_template_string(BASE, page_title="Cards", active="cards", content=content)


@app.route("/cards/<session_id>")
def page_card_detail(session_id):
    card_path = CARDS_DIR / f"{session_id}.json"
    if not card_path.exists():
        return f"Card not found: {session_id}", 404
    card = json.loads(card_path.read_text())
    meta = card.get("_meta", {}) or {}
    src = (meta.get("source") or "").lower()
    url = meta.get("url") or ""
    proj = card.get("project_match", {}).get("id", "")

    LABEL = "font-size:12px;text-transform:uppercase;letter-spacing:0.07em;color:var(--ink-3)"

    def _primary(it, key_field=None):
        if not isinstance(it, dict):
            return str(it)
        if key_field:
            return it.get(key_field) or ""
        return it.get("text") or it.get("url") or it.get("name") or it.get("path") or ""

    def _clean(items, key_field=None):
        """Drop empties and exact-duplicate items within one card. Extraction
        often repeats the same spark/commitment several times; this collapses
        them so an opened card isn't padded with noise. Order preserved."""
        out, seen = [], set()
        for it in items or []:
            p = (_primary(it, key_field) or "").strip()
            if not p:
                continue
            norm = " ".join(p.lower().split())
            if norm in seen:
                continue
            seen.add(norm)
            out.append(it)
        return out

    def _one_item_html(it, key_field=None):
        if isinstance(it, dict):
            primary = _primary(it, key_field)
            ctx = it.get("context") or it.get("trigger_context") or it.get("role_or_context") or ""
            tag = it.get("type") or ""
            pj = it.get("project") or ""
            if key_field == "url" and it.get("url"):
                primary_html = f'<a href="{primary}" target="_blank" rel="noopener" style="color:var(--accent-ink);text-decoration:underline;word-break:break-all">{primary}</a>'
            else:
                primary_html = primary
            h = f'<div style="background:var(--surface-2);border-radius:9px;padding:11px 13px"><div style="font-size:14.5px">{primary_html}</div>'
            if ctx: h += f'<div class="meta" style="margin-top:3px">{ctx}</div>'
            if tag or pj: h += f'<div class="meta" style="margin-top:3px">{tag}{" · " + pj if pj else ""}</div>'
            return h + '</div>'
        return f'<div style="background:var(--surface-2);border-radius:9px;padding:11px 13px;font-size:14.5px">{it}</div>'

    def _items_html(items, key_field=None):
        return ('<div style="display:grid;gap:9px">'
                + "".join(_one_item_html(it, key_field) for it in items)
                + '</div>')

    # Essence sections: the stuff you act on, shown open — but a conversation
    # with 60 sparks would still bury you, so show the top few and fold the rest.
    SECTION_CAP = 6

    def section(title, items, key_field=None):
        items = _clean(items, key_field)
        if not items:
            return ""
        head = f'<h3 style="{LABEL};margin:26px 0 10px">{title}</h3>'
        if len(items) <= SECTION_CAP:
            return head + _items_html(items, key_field)
        shown, rest = items[:SECTION_CAP], items[SECTION_CAP:]
        more = (f'<details style="margin-top:9px"><summary style="cursor:pointer;{LABEL};list-style:none;color:var(--accent-ink)">'
                f'▸ Show {len(rest)} more</summary><div style="margin-top:9px">{_items_html(rest, key_field)}</div></details>')
        return head + _items_html(shown, key_field) + more

    # Reference sections: collapsed by default — just a count, expand on demand.
    # This is the anti-overwhelm fix: a 78-link card opens calm, depth is one click.
    def fold(title, items, key_field=None):
        items = _clean(items, key_field)
        if not items:
            return ""
        n = len(items)
        return (f'<details style="margin-top:14px;border-top:1px solid var(--line);padding-top:14px">'
                f'<summary style="cursor:pointer;{LABEL};list-style:none;display:flex;align-items:center;gap:8px">'
                f'<span style="color:var(--accent-ink)">▸</span>{title} '
                f'<span class="chip" style="text-transform:none;letter-spacing:0">{n}</span></summary>'
                f'<div style="margin-top:12px">{_items_html(items, key_field)}</div></details>')

    anchors = card.get("memory_anchors") or []
    where = (card.get("where_to_use") or "").strip()
    highlight = ""
    if anchors or where:
        a_html = ""
        if anchors:
            lis = "".join(f"<li style='margin:3px 0'>{a if isinstance(a, str) else a.get('text', '')}</li>" for a in anchors)
            a_html = f"<div style='margin-bottom:{'12px' if where else '0'}'><div style='{LABEL};margin-bottom:5px'>Remember</div><ul style='margin:0;padding-left:18px;font-size:14.5px;line-height:1.5'>{lis}</ul></div>"
        w_html = f"<div><div style='{LABEL};margin-bottom:5px'>Where to use</div><div style='font-size:14.5px;line-height:1.5'>{where}</div></div>" if where else ""
        highlight = f"<div style='background:var(--accent-soft);border-radius:var(--radius);padding:17px 19px;margin-bottom:18px'>{a_html}{w_html}</div>"

    # Prominent deep-link to the original conversation (not a tiny chip).
    open_convo = ""
    if url:
        glyph = provider_svg(src) or _PROVIDER_GLYPH.get(src, "")
        open_convo = (f'<a href="{url}" target="_blank" rel="noopener" class="btn btn-keep" '
                      f'style="display:inline-flex;align-items:center;gap:7px;text-decoration:none;margin-bottom:18px">'
                      f'{glyph} Open the original conversation →</a>')

    src_chip = provider_chip(src, url, link=False)  # chip in the meta row stays non-link (button does the linking)

    content = f"""
    <a href="/cards" class="meta" style="color:var(--ink-2)">← Back to cards</a>
    <h1 class="page-h" style="margin-top:12px">{card.get('topic', 'Untitled')}</h1>
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:18px">
        {f'<span class="chip">{proj}</span>' if proj else ''}{src_chip}
    </div>
    {open_convo}
    {highlight}
    <div class="card">
        <h3 style="{LABEL};margin:0 0 8px">Context</h3>
        <p style="margin:0 0 18px">{card.get('context', '—')}</p>
        <h3 style="{LABEL};margin:0 0 8px">Conclusion</h3>
        <p style="margin:0">{card.get('conclusion', '—')}</p>
        {section('Open items', card.get('open_items', []))}
        {section('Sparks', card.get('sparks', []))}
        {fold('Links', card.get('links', []), key_field='url')}
        {fold('People', card.get('people', []), key_field='name')}
        {fold('References', card.get('references', []), key_field='name')}
        {fold('Numbers', card.get('numbers', []), key_field='value')}
        {fold('Files', card.get('files', []), key_field='path')}
    </div>
    """
    return render_template_string(BASE, page_title="Card", active="cards", content=content)


@app.route("/projects")
def page_projects():
    config = load_config()
    f_status = request.args.get("status", "").strip()   # ""=active+on-hold, else a single status
    sort = request.args.get("sort", "cards").strip()

    card_counts = {}
    for c in load_all_cards():
        pid = (c.get("project_match", {}) or {}).get("id", "")
        if pid:
            card_counts[pid] = card_counts.get(pid, 0) + 1

    child_slugs = set()
    for _slug, info in config.items():
        for ch in (info.get("children") or []):
            child_slugs.add(ch)

    scount = {}
    for _slug, info in config.items():
        st = info.get("status", "dormant")
        scount[st] = scount.get(st, 0) + 1

    wanted = {f_status} if f_status else {"active", "on-hold"}

    def total_cards(slug, info):
        return card_counts.get(slug, 0) + sum(card_counts.get(ch, 0) for ch in (info.get("children") or []))

    rows_data = [(s, i) for s, i in config.items()
                 if s not in child_slugs and i.get("status", "dormant") in wanted]
    if sort == "az":
        rows_data.sort(key=lambda x: x[0].lower())
    else:
        rows_data.sort(key=lambda x: -total_cards(*x))

    def status_tag(status):
        label, color, _bg = STATUS_META.get(status, ("?", "var(--ink-3)", ""))
        return (f'<span style="font-size:10.5px;font-weight:600;letter-spacing:.06em;'
                f'text-transform:uppercase;color:{color}">{label}</span>')

    def row(slug, info):
        n = total_cards(slug, info)
        kids = info.get("children") or []
        count = f'<span class="meta">{n} card{"s" if n != 1 else ""}</span>' if n else ""
        sub = f'<span class="meta">{len(kids)} sub</span>' if kids else ""
        head = (f'<div class="lrow"><span class="t">{slug}</span>'
                f'<span class="m">{sub}{count}{status_tag(info.get("status", "dormant"))}</span></div>')
        if kids:
            kid_rows = "".join(
                f'<div class="lrow" style="padding-left:22px">'
                f'<span class="t" style="font-family:var(--sans);font-size:13.5px;font-weight:500;color:var(--ink-2)">{ch}</span>'
                f'<span class="m">{(str(card_counts.get(ch, 0)) if card_counts.get(ch, 0) else "")} '
                f'{status_tag(config.get(ch, {}).get("status", "dormant"))}</span></div>'
                for ch in sorted(kids))
            head += (f'<details><summary style="cursor:pointer;list-style:none;font-size:12px;'
                     f'color:var(--accent-ink);padding:6px 0 6px 22px">▸ {len(kids)} sub-projects</summary>{kid_rows}</details>')
        return head

    def opt(val, cur, label):
        return f'<option value="{val}"{" selected" if val == cur else ""}>{label}</option>'

    status_opts = (opt("", f_status, "Active + on-hold")
                   + opt("active", f_status, f"Active ({scount.get('active', 0)})")
                   + opt("on-hold", f_status, f"On hold ({scount.get('on-hold', 0)})")
                   + opt("dormant", f_status, f"Dormant ({scount.get('dormant', 0)})")
                   + opt("archived", f_status, f"Archived ({scount.get('archived', 0)})"))
    sort_opts = opt("cards", sort, "Most cards") + opt("az", sort, "A–Z")
    filterbar = (
        '<form method="get" class="filterbar">'
        f'<select name="status" onchange="this.form.submit()">{status_opts}</select>'
        f'<select name="sort" onchange="this.form.submit()">{sort_opts}</select>'
        '</form>'
    )

    body = "".join(row(s, i) for s, i in rows_data) or (
        '<div class="empty"><div class="big">No projects in this view.</div>'
        '<div>Switch the status filter above.</div></div>')
    content = f"""
    <h1 class="page-h">Projects</h1>
    <p class="page-sub">{len(rows_data)} shown · {len(config)} total</p>
    {filterbar}
    <div>{body}</div>
    <p class="meta" style="margin-top:28px">Grouping &amp; visibility live in <code>~/.phaedrus/projects.yml</code>.</p>
    """
    return render_template_string(BASE, page_title="Projects", active="projects", content=content)


@app.route("/settings")
def page_settings():
    prefs = load_preferences()
    rules = load_rules()
    recent = load_recent_corrections(n=10)

    rules_html = '<p class="meta">No deterministic rules yet.</p>'
    if rules:
        rules_html = "".join(
            f'<div class="card" style="padding:12px 15px"><div style="font-weight:600;font-size:14px">{r.get("name", "unnamed")}</div>'
            f'<div class="meta" style="margin-top:3px">applies to: {r.get("applies_to")} · pattern: <code>{r.get("pattern")}</code> · action: {r.get("action")}</div></div>'
            for r in rules
        )

    corrections_html = '<p class="meta">No corrections yet.</p>'
    if recent:
        corrections_html = "".join(
            f'<div class="card" style="padding:12px 15px"><div class="meta">{r.get("ts", "")}</div>'
            f'<div style="font-size:14px;margin-top:3px">{r.get("field")}: '
            f'<span style="text-decoration:line-through;color:var(--ink-3)">{(r.get("original", "") or "")[:80]}</span> → '
            f'<span style="font-weight:600">{(r.get("corrected", "") or "")[:80]}</span></div></div>'
            for r in recent
        )

    SH = "font-family:var(--serif);font-size:18px;font-weight:600;margin:0 0 6px"
    content = f"""
    <h1 class="page-h">Settings &amp; learning</h1>
    <p class="page-sub">How Phaedrus extracts, routes, and learns from your corrections.</p>

    <section style="margin-bottom:30px">
      <h2 style="{SH}">Preferences</h2>
      <p class="meta" style="margin:0 0 10px">Plain-English rules Phaedrus reads on every extraction.</p>
      <div class="card" style="white-space:pre-wrap;font-family:ui-monospace,Menlo,monospace;font-size:13px;color:var(--ink-2);line-height:1.6">{prefs}</div>
      <p class="meta" style="margin-top:8px">Edit at <code>~/.phaedrus/preferences.md</code></p>
    </section>

    <section style="margin-bottom:30px">
      <h2 style="{SH}">Deterministic rules ({len(rules)})</h2>
      <p class="meta" style="margin:0 0 10px">Post-processing rules the model cannot bypass.</p>
      <div style="display:grid;gap:9px">{rules_html}</div>
      <p class="meta" style="margin-top:8px">Edit at <code>~/.phaedrus/rules.jsonl</code></p>
    </section>

    <section style="margin-bottom:30px" id="projects">
      <h2 style="{SH}">Project config</h2>
      <p class="meta" style="margin:0 0 4px">All project organisation lives in one YAML file — families, status, visibility.</p>
      <p class="meta">Edit at <code>~/.phaedrus/projects.yml</code></p>
    </section>

    <section>
      <h2 style="{SH}">Recent corrections ({len(recent)})</h2>
      <div style="display:grid;gap:9px;margin-top:6px">{corrections_html}</div>
      <p class="meta" style="margin-top:8px">Log at <code>~/.phaedrus/corrections.jsonl</code></p>
    </section>
    """
    return render_template_string(BASE, page_title="Settings", active="settings", content=content)


@app.route("/guide")
def page_guide():
    SH = "font-family:var(--serif);font-size:20px;font-weight:600;margin:30px 0 8px"
    step = ("display:flex;gap:13px;align-items:flex-start;padding:14px 0;border-bottom:1px solid var(--line)")
    num = ("flex-shrink:0;width:26px;height:26px;border-radius:50%;background:var(--accent-soft);"
           "color:var(--accent-ink);font-weight:600;font-size:13px;display:flex;align-items:center;justify-content:center")

    def tab_row(name, what):
        return (f'<div style="display:flex;gap:13px;padding:11px 0;border-bottom:1px solid var(--line)">'
                f'<span style="flex-shrink:0;width:84px;font-weight:600;color:var(--ink)">{name}</span>'
                f'<span style="color:var(--ink-2);font-size:14px">{what}</span></div>')

    content = f"""
    <div class="narrow">
      <h1 class="page-h">How Phaedrus works</h1>
      <p class="page-sub">A two-minute daily habit so nothing important from your AI chats slips through.</p>

      <div class="card" style="background:var(--accent-soft);border:none">
        <div style="font-family:var(--serif);font-size:17px;font-weight:600;margin-bottom:5px">The idea</div>
        <div style="color:var(--ink-2);line-height:1.6">You talk to Claude, ChatGPT, Grok and the rest all day. Decisions, promises, worries
        and ideas get buried in those chats and forgotten. Phaedrus reads them, pulls out the things that matter,
        and shows you a short daily list to act on. You stop re-reading old conversations to remember what you said you'd do.</div>
      </div>

      <h2 style="{SH}">Your daily ritual</h2>
      <div style="{step}"><span style="{num}">1</span><div><b>Open Triage</b> once a day. It shows only conversations with something still unfinished.</div></div>
      <div style="{step}"><span style="{num}">2</span><div>For each card, read the one line it pulled out, then decide:
        <b>Keep</b> (it matters — file it), <b>Ignore</b> (not important), <b>Later</b> (remind me in a week).</div></div>
      <div style="{step}"><span style="{num}">3</span><div>When it isn't sure whether you already finished something, it <b>asks</b>
        — “Did you finish this?” — instead of guessing. You answer <b>Yes, done</b> or <b>Not yet</b>.</div></div>
      <div style="{step}"><span style="{num}">4</span><div>That's it. Close it. Anything you didn't reach is waiting tomorrow.</div></div>

      <h2 style="{SH}">What it pulls out</h2>
      <p style="color:var(--ink-2);line-height:1.6;margin:0 0 6px">From each conversation it makes one <b>card</b> with the commitments,
      worries, open questions and ideas. Each action gets a status:</p>
      <div style="display:flex;flex-direction:column;gap:7px;margin-top:10px">
        <div><span style="color:var(--accent-ink);font-weight:600">open</span> <span class="meta">— still to do, shows in triage</span></div>
        <div><span style="color:var(--warn);font-weight:600">unsure</span> <span class="meta">— it can't tell if you finished, so it asks you</span></div>
        <div><span style="color:var(--ink-3);font-weight:600">done</span> <span class="meta">— finished in the chat, hidden so it stops nagging</span></div>
      </div>
      <p class="meta" style="margin-top:10px">It never marks something “done” on a guess — if it isn't certain, it asks. That's deliberate.</p>

      <h2 style="{SH}">The tabs</h2>
      {tab_row("Triage", "Your daily list. The only place you need most days.")}
      {tab_row("Cards", "The full archive. Search, or narrow with the filters (source, project, type, status, date) and group by project so you never scroll forever.")}
      {tab_row("Projects", "Everything grouped by project, with counts.")}
      {tab_row("Import", "Pull your back-history in from each platform — automatic where possible, export-file where not.")}
      {tab_row("Settings", "Tune what gets extracted and how it learns from your Keep/Ignore choices.")}

      <h2 style="{SH}">When the list feels long</h2>
      <p style="color:var(--ink-2);line-height:1.6;margin:0">Go to <b>Cards</b>, set <b>Group: project</b> — the whole archive collapses to a
      handful of headings you expand one at a time. Or filter by <b>Last 7 days</b>, a single <b>source</b>, or a <b>type</b>
      like commitments. The goal is to never face a wall.</p>

      <p class="meta" style="margin-top:30px">Everything runs on your machine. Your conversations never leave it.</p>
    </div>
    """
    return render_template_string(BASE, page_title="Guide", active="guide", content=content)


# === Extension capture endpoint ===

WEB_CAPTURES_DIR = PHAEDRUS_HOME / "web_captures"
WEB_CAPTURES_DIR.mkdir(exist_ok=True)


@app.route("/capture", methods=["POST", "GET", "OPTIONS"])
def capture():
    """Receive captures from the Phaedrus browser extension."""
    if request.method == "OPTIONS":
        # CORS preflight
        resp = app.make_response("")
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return resp

    if request.method == "GET":
        # Health check + list recent captures
        files = sorted(WEB_CAPTURES_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return jsonify({
            "status": "phaedrus capture endpoint ready",
            "captures_dir": str(WEB_CAPTURES_DIR),
            "recent_captures": [p.name for p in files[:20]],
            "count": len(files),
        })

    # POST: store the capture
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception as e:
        return jsonify({"error": f"invalid json: {e}"}), 400

    source = payload.get("source") or request.headers.get("X-Phaedrus-Source") or "unknown"
    # The forked extension still self-labels with mem0's constant
    # ("OPENMEMORY_CHROME_EXTENSION") and carries the real provider in metadata.
    # Derive a clean source so files are e.g. claude_*.json, not OPENMEMORY_*.json.
    if str(source).upper().startswith("OPENMEMORY"):
        provider = (payload.get("metadata") or {}).get("provider")
        if provider:
            source = str(provider).lower()
    safe_source = "".join(c for c in str(source) if c.isalnum() or c in "-._") or "unknown"
    capture_id = payload.get("id") or f"{int(time.time()*1000)}"
    safe_id = "".join(c for c in str(capture_id) if c.isalnum() or c in "-._")[:60]

    record = {
        "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": source,
        "id": capture_id,
        "url": payload.get("url"),
        "title": payload.get("title"),
        "messages": payload.get("messages") or [],
        "raw": payload,
    }

    out_path = WEB_CAPTURES_DIR / f"{safe_source}_{safe_id}.json"
    out_path.write_text(json.dumps(record, indent=2, ensure_ascii=False))

    resp = jsonify({"ok": True, "saved_to": str(out_path), "source": source})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


# === Export-file import ===

@app.route("/import", methods=["GET"])
def page_import():
    SH = "font-family:var(--serif);font-size:18px;font-weight:600;margin:0 0 6px"

    # Per-platform: both paths, each with an HONEST time estimate AND a real
    # step-by-step guide so nobody hits an unestimated wait or hunts for a button
    # that doesn't exist. auto=None means no background pull. steps=[] for manual.
    PLATFORMS = [
        {
            "name": "Claude", "key": "claude",
            "auto": {"eta": "~10 min", "note": "Click the extension on claude.ai → “Pull my history”. Runs in the background."},
            "manual": {"eta": "a few hours", "url": "https://claude.ai/settings/data-privacy-controls", "btn": "Open Claude settings",
                       "steps": ["Settings → Privacy.", "Click “Export data”.", "Claude emails a link within a few hours (it expires in 24h — grab it same day).",
                                 "Download the .zip (don’t unzip).", "Drop it in the box above."]},
        },
        {
            "name": "ChatGPT", "key": "chatgpt",
            "auto": {"eta": "~30–40 min", "note": "Click the extension on chatgpt.com → “Pull my history”. Slow for 1000+ chats (ChatGPT rate-limits); export is faster for big histories."},
            "manual": {"eta": "minutes to a few hours", "url": "https://chatgpt.com/#settings/DataControls", "btn": "Open ChatGPT settings",
                       "steps": ["Settings → Data controls.", "Click “Export” next to Export data, then Confirm.", "ChatGPT emails a link (usually minutes).",
                                 "Download the .zip (don’t unzip).", "Drop it in the box above. Best path for big histories."]},
        },
        {
            "name": "Gemini", "key": "gemini",
            "auto": None,
            "manual": {"eta": "minutes to a few hours", "url": "https://takeout.google.com/", "btn": "Open Google Takeout",
                       "steps": ["Gemini has no export button — it’s in Google Takeout.",
                                 "Click “Deselect all” first (or you’ll get your whole Google account).",
                                 "Scroll down and tick ONLY “My Activity”.",
                                 "Under My Activity, choose “Multiple formats” → set Activity to JSON.",
                                 "Next step → Create export. Google emails a link.",
                                 "In the download, the chats are under My Activity → Gemini Apps. Drop the file above.",
                                 "Note: the category literally named “Gemini” is for saved Gems, not chats — you want My Activity → Gemini Apps."]},
        },
        {
            "name": "Perplexity", "key": "perplexity",
            "auto": {"eta": "latest ~20", "note": "The extension captures Perplexity going forward automatically, and grabs your most recent ~20 on demand. Full back-catalogue needs the email request (Perplexity caps the rest)."},
            "manual": {"eta": "1–3 days", "url": "mailto:support@perplexity.ai?subject=Data%20export%20request%20(GDPR%2FCCPA)&body=Hello%2C%0A%0AUnder%20GDPR%2FCCPA%20I%20request%20a%20full%20copy%20of%20my%20Perplexity%20thread%20history%20and%20account%20data%20in%20a%20machine-readable%20format%20(JSON%20preferred).%0A%0AThank%20you.", "btn": "Open pre-filled email",
                       "steps": ["Perplexity has no self-serve export and caps history at ~20 threads.",
                                 "Click below: opens a pre-written GDPR data-request email to support@perplexity.ai — just hit Send.",
                                 "They reply with a file; drop it in the box above when it arrives.",
                                 "Meanwhile the extension captures Perplexity going forward automatically."]},
        },
        {
            "name": "DeepSeek", "key": "deepseek",
            "auto": None,
            "manual": {"eta": "1–3 days", "url": "mailto:service@deepseek.com?subject=Data%20export%20request%20(GDPR%2FCCPA)&body=Hello%2C%0A%0AUnder%20GDPR%2FCCPA%20I%20request%20a%20full%20copy%20of%20my%20DeepSeek%20conversation%20history%20and%20account%20data%20in%20a%20machine-readable%20format%20(JSON%20preferred).%0A%0AThank%20you.", "btn": "Open pre-filled email",
                       "steps": ["DeepSeek has no self-serve export — you request it by email.",
                                 "Click the button below: it opens a pre-written GDPR data-request email to service@deepseek.com — just hit Send.",
                                 "They reply with a file, usually within a few days.",
                                 "Drop that file in the box above when it arrives.",
                                 "Meanwhile the extension captures DeepSeek going forward automatically."]},
        },
    ]

    rows = ""
    for p in PLATFORMS:
        # Automatic path cell
        if p["auto"]:
            auto_cell = (
                f'<div style="flex:1;min-width:200px">'
                f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">'
                f'<span style="font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:var(--ok)">Automatic</span>'
                f'<span class="chip" style="background:var(--ok-soft);color:var(--ok)">{p["auto"]["eta"]}</span></div>'
                f'<div class="meta">{p["auto"]["note"]}</div></div>'
            )
        else:
            auto_cell = (
                f'<div style="flex:1;min-width:200px">'
                f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">'
                f'<span style="font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:var(--ink-3)">Automatic</span>'
                f'<span class="chip">not available</span></div>'
                f'<div class="meta">No background pull — use the export steps.</div></div>'
            )
        # Manual path cell with expandable real steps
        m = p["manual"]
        steps_li = "".join(f'<li style="margin:5px 0;font-size:13px;color:var(--ink-2);line-height:1.5">{s}</li>' for s in m["steps"])
        _is_mail = m.get("url", "").startswith("mailto:")
        _tgt = "" if _is_mail else ' target="_blank" rel="noopener"'
        _arrow = "" if _is_mail else " ↗"
        open_btn = (f'<a href="{m["url"]}"{_tgt} class="btn btn-soft" '
                    f'style="display:inline-block;margin-top:10px;text-decoration:none;font-size:12.5px">{m["btn"]}{_arrow}</a>') if m.get("url") else ""
        manual_cell = (
            f'<div style="flex:1;min-width:200px">'
            f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">'
            f'<span style="font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:var(--info)">Export file</span>'
            f'<span class="chip" style="background:var(--info-soft);color:var(--info)">{m["eta"]}</span></div>'
            f'<details style="margin-top:2px"><summary style="cursor:pointer;font-size:13px;color:var(--accent-ink)">Show steps</summary>'
            f'<ol style="margin:8px 0 0;padding-left:18px">{steps_li}</ol>{open_btn}</details></div>'
        )
        rows += (
            f'<div class="card" style="margin-bottom:12px">'
            f'<div style="font-family:var(--serif);font-size:16.5px;font-weight:600;margin-bottom:11px">{p["name"]}</div>'
            f'<div style="display:flex;gap:24px;flex-wrap:wrap">{auto_cell}{manual_cell}</div>'
            f'</div>'
        )

    content = f"""
    <div class="narrow">
      <h1 class="page-h">Import your history</h1>
      <p class="page-sub">Two ways in, per platform — pick by how long you'll wait. Everything is parsed on your machine; nothing is uploaded.</p>

      <div class="card" style="background:var(--accent-soft);border:none;margin-bottom:26px">
        <div style="font-weight:600;margin-bottom:3px">Drop an export file</div>
        <div class="meta" style="margin-bottom:13px">Works for all five platforms. Got a .zip or .json from any of them? Drop it here.</div>
        <form id="imp" hx-post="/import" hx-encoding="multipart/form-data" hx-target="#imp-result" hx-swap="innerHTML">
          <label id="drop" for="file" style="display:block;border:1.5px dashed var(--line-2);border-radius:var(--radius-sm);
                 padding:30px 24px;text-align:center;cursor:pointer;background:var(--surface);transition:border-color .15s">
            <div style="font-family:var(--serif);font-size:16px;color:var(--ink);margin-bottom:4px">Choose a file</div>
            <div class="meta">.zip or .json</div>
            <input id="file" name="file" type="file" accept=".zip,.json" style="display:none"
                   onchange="document.getElementById('fname').textContent=this.files[0]?this.files[0].name:''">
            <div id="fname" class="chip" style="margin-top:11px;display:inline-block"></div>
          </label>
          <button class="btn btn-keep" style="margin-top:13px" type="submit">Import</button>
          <span id="imp-spin" class="htmx-indicator meta" style="margin-left:10px">Parsing…</span>
        </form>
        <div id="imp-result" style="margin-top:14px"></div>
      </div>

      <h2 style="{SH}">Per platform: which path, how long</h2>
      <p class="meta" style="margin:0 0 14px">Automatic needs nothing from you but can be slow. Export files are faster for big histories and the only option where automatic isn't available.</p>
      {rows}
    </div>
    <style>
      #imp.htmx-request #imp-spin {{ opacity:1 }}
      .htmx-indicator {{ opacity:0; transition:opacity .15s }}
      #drop:hover {{ border-color: var(--accent) }}
    </style>
    """
    return render_template_string(BASE, page_title="Import", active="import", content=content)


@app.route("/import", methods=["POST"])
def do_import():
    import import_exports

    f = request.files.get("file")
    if not f:
        return '<div class="card" style="background:var(--warn-soft)">No file received. Pick an export file and try again.</div>'
    raw = f.read()
    if not raw:
        return '<div class="card" style="background:var(--warn-soft)">That file was empty.</div>'

    try:
        result = import_exports.import_bytes(raw, filename=f.filename or "")
    except Exception as e:
        return f'<div class="card" style="background:var(--warn-soft)">Could not parse that file: {e}</div>'

    records = result.get("records", [])
    if not records:
        errs = "; ".join(result.get("errors", [])) or "no conversations recognised in that file"
        return (f'<div class="card" style="background:var(--warn-soft)">'
                f'<div style="font-weight:600">Nothing imported</div>'
                f'<div class="meta" style="margin-top:4px">{errs}. '
                f'Make sure it’s the conversations export (not the account-info file).</div></div>')

    # Write each conversation as a capture file the ingester already understands.
    written = 0
    for r in records:
        try:
            safe_source = "".join(c for c in str(r.get("source", "import")) if c.isalnum() or c in "-._") or "import"
            raw_id = str(r.get("id", ""))
            # The importer's ids already carry the source prefix (e.g. "chatgpt_abc").
            # Strip it so the on-disk filename matches the live backfill's
            # ("<source>_<id>"), giving true cross-path dedupe between an export
            # import and an API backfill of the same conversation.
            if raw_id.startswith(safe_source + "_"):
                raw_id = raw_id[len(safe_source) + 1:]
            safe_id = "".join(c for c in raw_id if c.isalnum() or c in "-._")[:80] or str(int(time.time() * 1000))
            record = {
                "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "source": r.get("source", "import"),
                "id": r.get("id", safe_id),
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "captured_at": r.get("captured_at", ""),
                "messages": r.get("messages", []),
                "raw": {"imported_from_export": True, **r},
            }
            (WEB_CAPTURES_DIR / f"{safe_source}_{safe_id}.json").write_text(
                json.dumps(record, indent=2, ensure_ascii=False)
            )
            written += 1
        except Exception:
            continue

    by_platform = result.get("by_platform", {})
    bp = " · ".join(f"{k}: {v}" for k, v in by_platform.items())
    return (f'<div class="card fade-in" style="background:var(--ok-soft)">'
            f'<div style="font-weight:600;font-size:15px">Imported {written} conversation{"s" if written != 1 else ""}</div>'
            f'<div class="meta" style="margin-top:5px">{bp}</div>'
            f'<div class="meta" style="margin-top:8px">Saved locally to <code>~/.phaedrus/web_captures/</code>. '
            f'They’ll become cards on the next extraction run.</div></div>')


# === Boot ===

def open_browser():
    webbrowser.open_new("http://localhost:5050/")


if __name__ == "__main__":
    print("Phaedrus dashboard starting at http://localhost:5050")
    # Auto-open the browser only on a manual launch. The login service sets
    # PHAEDRUS_NO_BROWSER=1 so it runs headless (no tab popping up every login).
    if not os.environ.get("PHAEDRUS_NO_BROWSER"):
        Timer(1.5, open_browser).start()
    app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=False)
