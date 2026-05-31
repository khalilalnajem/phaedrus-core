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
        until = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        record["later_until"] = until
    with TRIAGE_LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")


def item_key(session_id: str, kind: str, text: str) -> str:
    import hashlib
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    return f"{session_id}:{kind}:{h}"


def get_triage_candidates(limit=7, max_per_project=2):
    cards = load_all_cards()
    decisions = get_triage_decisions()
    candidates = []
    for card in cards:
        for item in card.get("open_items", []) or []:
            item["_kind"] = "open_item"
            candidates.append((item, card))
        for spark in card.get("sparks", []) or []:
            spark["_kind"] = "spark"
            candidates.append((spark, card))

    type_weights = {
        "commitment": 1.0, "worry": 0.7, "question": 0.5,
        "idea": 0.4, "observation": 0.3, "tangent": 0.3, "side-question": 0.5,
    }
    scored = []
    for item, card in candidates:
        key = item_key(card["_session_id"], item["_kind"], item.get("text", ""))
        if key in decisions:
            d = decisions[key]
            if d.get("decision") in ("keep", "ignore", "done"):
                continue
            if d.get("decision") == "later":
                until = d.get("later_until")
                if until and datetime.now(timezone.utc).isoformat() < until:
                    continue
        score = type_weights.get(item.get("type", ""), 0.3)
        try:
            conf = float(card.get("project_match", {}).get("confidence", 0))
            if conf > 0.7:
                score += 0.2
        except (TypeError, ValueError):
            pass
        scored.append((score, item, card, key))
    scored.sort(key=lambda x: -x[0])

    # Apply per-project cap
    seen = {}
    out = []
    for s, item, card, key in scored:
        proj = item.get("project") or card.get("project_match", {}).get("id", "none")
        if seen.get(proj, 0) >= max_per_project:
            continue
        out.append((s, item, card, key))
        seen[proj] = seen.get(proj, 0) + 1
        if len(out) >= limit:
            break
    return out


# === HTML template (Tailwind via CDN, HTMX for interactivity) ===

BASE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Phaedrus — {{ page_title }}</title>
<script src="https://unpkg.com/htmx.org@1.9.10"></script>
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
    --accent: #c2603d;
    --accent-ink: #9c441f;
    --accent-soft: #f3e2d6;
    --ok: #4a7a52; --ok-soft: #e6efe4;
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
    glyph = _PROVIDER_GLYPH.get(src, "")
    inner = f'<span style="display:inline-flex;align-items:center;gap:5px">{glyph}{label}</span>'
    style = "background:var(--accent-soft);color:var(--accent-ink)"
    if link and url:
        return f'<a href="{url}" target="_blank" rel="noopener" class="chip" style="{style};text-decoration:none">{inner}</a>'
    return f'<span class="chip" style="{style}">{inner}</span>'


# === Pages ===

@app.route("/")
def page_triage():
    items = get_triage_candidates(limit=7, max_per_project=2)
    today = datetime.now().strftime("%A, %d %B %Y")
    cards_html = ""
    if not items:
        cards_html = """
        <div class="empty">
          <div class="big">Nothing to triage right now.</div>
          <div>You're clear. New conversations surface here after the next extraction.</div>
        </div>
        """
    else:
        for score, item, card, key in items:
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
            where = (item.get("where_to_use") or card.get("where_to_use") or "").strip()
            where_html = f'<div class="meta" style="margin-top:10px;color:var(--accent-ink)">↳ {where}</div>' if where else ""
            session_id = card["_session_id"]
            safe_text = text.replace('"', "&quot;").replace("'", "&#39;")
            cards_html += f"""
            <div class="card card-hover fade-in" style="border-left:3px solid {accent}">
                <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:13px">
                    <span class="pill {pill_class}"><span class="pdot"></span>{label}</span>
                    <span style="display:flex;align-items:center;gap:8px">{src_chip}{date_meta}</span>
                </div>
                <div class="quote" style="font-size:19px">{text}</div>
                <div class="meta" style="margin-top:7px">from “{topic}”{proj_txt}</div>
                {ctx_html}
                {where_html}
                <a href="/cards/{session_id}" class="meta" style="display:inline-block;margin-top:9px;color:var(--accent-ink)">Open full card →</a>
                <div style="display:flex;gap:8px;margin-top:15px" hx-target="this" hx-swap="outerHTML">
                    <button class="btn btn-keep" hx-post="/triage/decide" hx-vals='{{"key": "{key}", "decision": "keep", "text": "{safe_text[:200]}", "session_id": "{session_id}"}}'>Keep</button>
                    <button class="btn btn-ghost" hx-post="/triage/decide" hx-vals='{{"key": "{key}", "decision": "ignore", "text": "{safe_text[:200]}", "session_id": "{session_id}"}}'>Ignore</button>
                    <button class="btn btn-soft" hx-post="/triage/decide" hx-vals='{{"key": "{key}", "decision": "later", "text": "{safe_text[:200]}", "session_id": "{session_id}"}}'>Later</button>
                </div>
            </div>
            """
    plural = "s" if len(items) != 1 else ""
    content = f"""
    <div class="narrow">
      <h1 class="page-h">Today's triage</h1>
      <p class="page-sub">{today} · {len(items)} item{plural} to decide</p>
      {cards_html}
    </div>
    """
    return render_template_string(BASE, page_title="Triage", active="triage", content=content)


@app.route("/triage/decide", methods=["POST"])
def triage_decide():
    key = request.form.get("key", "")
    decision = request.form.get("decision", "")
    text = request.form.get("text", "")
    session_id = request.form.get("session_id", "")
    log_triage_decision(key, decision, text, session_id)
    styles = {
        "keep":   ("var(--ok)", "var(--ok-soft)", "Kept"),
        "ignore": ("var(--ink-3)", "var(--surface-2)", "Ignored"),
        "later":  ("var(--warn)", "var(--warn-soft)", "Saved for later"),
    }
    color, bg, label = styles.get(decision, ("var(--ink-2)", "var(--surface-2)", decision.capitalize()))
    return (f'<div class="fade-in" style="display:flex;align-items:center;gap:9px;padding:15px 20px;'
            f'border-radius:var(--radius);background:{bg};color:{color};font-size:14px;font-weight:600">'
            f'<span>✓</span><span>{label}</span></div>')


@app.route("/cards")
def page_cards():
    q = request.args.get("q", "").strip().lower()
    cards = load_all_cards()
    if q:
        cards = [c for c in cards if q in json.dumps(c).lower()]
    SOURCE_LABEL = {"claude": "Claude", "chatgpt": "ChatGPT", "gemini": "Gemini", "grok": "Grok",
                    "perplexity": "Perplexity", "deepseek": "DeepSeek", "obsidian": "Obsidian",
                    "apple-notes": "Apple Notes", "codex": "Codex", "claude-code": "Claude Code"}
    rows = ""
    for card in cards[:120]:
        topic = (card.get("topic") or "Untitled")[:90]
        proj = card.get("project_match", {}).get("id", "")
        meta = card.get("_meta", {})
        extracted = (meta.get("extracted_at", "") or "")[:10]
        src = (meta.get("source") or "").lower()
        opens = len(card.get("open_items", []) or [])
        sparks = len(card.get("sparks", []) or [])
        links = len(card.get("links", []) or [])
        bits = []
        if opens: bits.append(f"{opens} open")
        if sparks: bits.append(f"{sparks} spark" + ("s" if sparks != 1 else ""))
        if links: bits.append(f"{links} link" + ("s" if links != 1 else ""))
        stats = " · ".join(bits)
        proj_chip = f'<span class="chip">{proj}</span>' if proj and proj not in ("none", "new") else ""
        src_chip = provider_chip(src, link=False)
        rows += f"""
        <a href="/cards/{card['_session_id']}" class="card card-hover" style="display:block">
            <div style="display:flex;align-items:baseline;justify-content:space-between;gap:14px;margin-bottom:9px">
                <span style="font-family:var(--serif);font-size:16.5px;font-weight:600;color:var(--ink)">{topic}</span>
                <span class="meta" style="white-space:nowrap">{extracted}</span>
            </div>
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                {src_chip}{proj_chip}{f'<span class="meta">{stats}</span>' if stats else ''}
            </div>
        </a>
        """
    empty = '<div class="empty"><div class="big">No cards yet.</div><div>Chat on any AI platform, then run an extraction.</div></div>'
    content = f"""
    <h1 class="page-h">Cards</h1>
    <p class="page-sub">{len(cards)} extracted from your conversations &amp; notes</p>
    <form method="get" style="display:flex;gap:8px;margin-bottom:22px">
        <input type="search" name="q" value="{q}" placeholder="Search topics, sparks, links, people…">
        <button class="btn btn-keep" style="white-space:nowrap">Search</button>
    </form>
    <div style="display:grid;gap:12px">{rows or empty}</div>
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

    def _items_html(items, key_field=None):
        html = '<div style="display:grid;gap:9px">'
        for it in items:
            if isinstance(it, dict):
                primary = it.get(key_field) if key_field else (it.get("text") or it.get("url") or it.get("name") or it.get("path") or "")
                ctx = it.get("context") or it.get("trigger_context") or it.get("role_or_context") or ""
                tag = it.get("type") or ""
                pj = it.get("project") or ""
                if key_field == "url" and it.get("url"):
                    primary_html = f'<a href="{primary}" target="_blank" rel="noopener" style="color:var(--accent-ink);text-decoration:underline;word-break:break-all">{primary}</a>'
                else:
                    primary_html = primary
                html += f'<div style="background:var(--surface-2);border-radius:9px;padding:11px 13px"><div style="font-size:14.5px">{primary_html}</div>'
                if ctx: html += f'<div class="meta" style="margin-top:3px">{ctx}</div>'
                if tag or pj: html += f'<div class="meta" style="margin-top:3px">{tag}{" · " + pj if pj else ""}</div>'
                html += '</div>'
            else:
                html += f'<div style="background:var(--surface-2);border-radius:9px;padding:11px 13px;font-size:14.5px">{it}</div>'
        html += '</div>'
        return html

    # Essence sections: always open (the stuff you actually act on).
    def section(title, items, key_field=None):
        if not items:
            return ""
        return f'<h3 style="{LABEL};margin:26px 0 10px">{title}</h3>{_items_html(items, key_field)}'

    # Reference sections: collapsed by default — just a count, expand on demand.
    # This is the anti-overwhelm fix: a 78-link card opens calm, depth is one click.
    def fold(title, items, key_field=None):
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
        open_convo = (f'<a href="{url}" target="_blank" rel="noopener" class="btn btn-keep" '
                      f'style="display:inline-flex;align-items:center;gap:7px;text-decoration:none;margin-bottom:18px">'
                      f'{_PROVIDER_GLYPH.get(src, "")} Open the original conversation →</a>')

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

    card_counts = {}
    for c in load_all_cards():
        pid = (c.get("project_match", {}) or {}).get("id", "")
        if pid:
            card_counts[pid] = card_counts.get(pid, 0) + 1

    child_slugs = set()
    for _slug, info in config.items():
        for ch in (info.get("children") or []):
            child_slugs.add(ch)

    top = {"active": [], "on-hold": []}
    hidden_archived = hidden_dormant = 0
    for slug, info in config.items():
        status = info.get("status", "dormant")
        if status == "archived":
            hidden_archived += 1; continue
        if status == "dormant":
            hidden_dormant += 1; continue
        if slug in child_slugs:
            continue
        top.setdefault(status, []).append((slug, info))

    def status_pill(status, small=False):
        label, color, bg = STATUS_META.get(status, ("?", "var(--ink-3)", "var(--surface-2)"))
        size = "font-size:10.5px;padding:2px 8px" if small else ""
        return f'<span class="pill" style="background:{bg};color:{color};{size}">{label}</span>'

    def render_project(slug, info):
        desc = info.get("description") or ""
        children = info.get("children") or []
        n = card_counts.get(slug, 0) + sum(card_counts.get(ch, 0) for ch in children)
        count_chip = f'<span class="meta">{n} card{"s" if n != 1 else ""}</span>' if n else ""
        kids_html = ""
        if children:
            kid_rows = ""
            for ch in sorted(children):
                cinfo = config.get(ch, {})
                cn = card_counts.get(ch, 0)
                cn_html = f'<span class="meta">{cn}</span>' if cn else ""
                kid_rows += (
                    '<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;'
                    'padding:8px 0;border-top:1px solid var(--line)">'
                    f'<span style="font-size:13.5px;color:var(--ink-2)">{ch}</span>'
                    f'<span style="display:flex;align-items:center;gap:9px">{cn_html}{status_pill(cinfo.get("status", "dormant"), small=True)}</span></div>'
                )
            kids_html = f'<div style="margin-top:13px">{kid_rows}</div>'
        desc_html = f'<p style="margin:7px 0 0;color:var(--ink-2);font-size:14px">{desc}</p>' if desc else ""
        sub_note = ""
        if children and not desc:
            sub_note = f'<p style="margin:7px 0 0;color:var(--ink-3);font-size:13px">{len(children)} sub-project{"s" if len(children) != 1 else ""}</p>'
        return f"""
        <div class="card card-hover">
          <div style="display:flex;align-items:center;justify-content:space-between;gap:12px">
            <span style="font-family:var(--serif);font-size:17px;font-weight:600">{slug}</span>
            <span style="display:flex;align-items:center;gap:10px">{count_chip}{status_pill(info.get('status', 'dormant'))}</span>
          </div>
          {desc_html}{sub_note}{kids_html}
        </div>"""

    def render_section(name, items):
        if not items:
            return ""
        cards = "".join(render_project(s, i) for s, i in sorted(items))
        return (f'<h2 style="font-family:var(--serif);font-size:18px;font-weight:600;margin:30px 0 12px">{name} '
                f'<span class="meta" style="font-family:var(--sans);font-weight:400">({len(items)})</span></h2>'
                f'<div style="display:grid;gap:12px">{cards}</div>')

    shown = len(top["active"]) + len(top["on-hold"])
    content = f"""
    <h1 class="page-h">Projects</h1>
    <p class="page-sub">{shown} active &amp; on-hold · {hidden_archived} archived and {hidden_dormant} dormant hidden</p>
    {render_section("Active", top["active"])}
    {render_section("On hold", top["on-hold"])}
    <p class="meta" style="margin-top:32px">Grouping and visibility live in <code>~/.phaedrus/projects.yml</code> — edit it freely.</p>
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
                f'<span class="pill pill-idea"><span class="pdot"></span>Automatic</span>'
                f'<span class="chip" style="background:var(--ok-soft);color:var(--ok)">{p["auto"]["eta"]}</span></div>'
                f'<div class="meta">{p["auto"]["note"]}</div></div>'
            )
        else:
            auto_cell = (
                f'<div style="flex:1;min-width:200px">'
                f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">'
                f'<span class="pill pill-neutral"><span class="pdot"></span>Automatic</span>'
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
            f'<span class="pill pill-question"><span class="pdot"></span>Export file</span>'
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
        return '<div class="card" style="border-left:3px solid var(--warn)">No file received. Pick an export file and try again.</div>'
    raw = f.read()
    if not raw:
        return '<div class="card" style="border-left:3px solid var(--warn)">That file was empty.</div>'

    try:
        result = import_exports.import_bytes(raw, filename=f.filename or "")
    except Exception as e:
        return f'<div class="card" style="border-left:3px solid var(--warn)">Could not parse that file: {e}</div>'

    records = result.get("records", [])
    if not records:
        errs = "; ".join(result.get("errors", [])) or "no conversations recognised in that file"
        return (f'<div class="card" style="border-left:3px solid var(--warn)">'
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
    return (f'<div class="card fade-in" style="border-left:3px solid var(--ok)">'
            f'<div style="font-weight:600;font-size:15px">Imported {written} conversation{"s" if written != 1 else ""}</div>'
            f'<div class="meta" style="margin-top:5px">{bp}</div>'
            f'<div class="meta" style="margin-top:8px">Saved locally to <code>~/.phaedrus/web_captures/</code>. '
            f'They’ll become cards on the next extraction run.</div></div>')


# === Boot ===

def open_browser():
    webbrowser.open_new("http://localhost:5050/")


if __name__ == "__main__":
    print("Phaedrus dashboard starting at http://localhost:5050")
    print("Press Ctrl+C to stop.")
    Timer(1.5, open_browser).start()
    app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=False)
