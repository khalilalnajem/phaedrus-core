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
<title>Phaedrus — {{ page_title }}</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://unpkg.com/htmx.org@1.9.10"></script>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
  .card { transition: all 0.15s ease; }
  .card:hover { box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
</style>
</head>
<body class="bg-stone-50 text-stone-900 min-h-screen">
<nav class="bg-white border-b border-stone-200 sticky top-0 z-10">
  <div class="max-w-6xl mx-auto px-6 py-3 flex items-center justify-between">
    <div class="flex items-center gap-2">
      <span class="text-2xl">🦞</span>
      <a href="/" class="text-lg font-semibold tracking-tight">Phaedrus</a>
    </div>
    <div class="flex gap-1">
      <a href="/" class="px-3 py-1.5 rounded text-sm hover:bg-stone-100 {% if active == 'triage' %}bg-stone-900 text-white hover:bg-stone-800{% endif %}">Triage</a>
      <a href="/cards" class="px-3 py-1.5 rounded text-sm hover:bg-stone-100 {% if active == 'cards' %}bg-stone-900 text-white hover:bg-stone-800{% endif %}">Cards</a>
      <a href="/projects" class="px-3 py-1.5 rounded text-sm hover:bg-stone-100 {% if active == 'projects' %}bg-stone-900 text-white hover:bg-stone-800{% endif %}">Projects</a>
      <a href="/settings" class="px-3 py-1.5 rounded text-sm hover:bg-stone-100 {% if active == 'settings' %}bg-stone-900 text-white hover:bg-stone-800{% endif %}">Settings</a>
    </div>
  </div>
</nav>
<main class="max-w-6xl mx-auto px-6 py-8">
{{ content | safe }}
</main>
<footer class="max-w-6xl mx-auto px-6 py-6 text-xs text-stone-400">
  Phaedrus daily review · local-host dashboard · data at <code>~/.phaedrus/</code>
</footer>
</body>
</html>
"""

TYPE_BADGES = {
    "commitment":   ("bg-rose-50 text-rose-700 border-rose-200", "📌 Commitment"),
    "worry":        ("bg-amber-50 text-amber-700 border-amber-200", "⚠ Worry"),
    "question":     ("bg-sky-50 text-sky-700 border-sky-200", "❓ Question"),
    "idea":         ("bg-emerald-50 text-emerald-700 border-emerald-200", "💡 Idea"),
    "observation":  ("bg-stone-100 text-stone-700 border-stone-200", "📝 Observation"),
    "tangent":      ("bg-stone-100 text-stone-700 border-stone-200", "📝 Tangent"),
    "side-question":("bg-sky-50 text-sky-700 border-sky-200", "❓ Side question"),
}

STATUS_BADGES = {
    "active":   "bg-emerald-100 text-emerald-700",
    "on-hold":  "bg-amber-100 text-amber-700",
    "dormant":  "bg-stone-200 text-stone-600",
    "finished": "bg-sky-100 text-sky-700",
    "archived": "bg-stone-100 text-stone-500",
}


# === Pages ===

@app.route("/")
def page_triage():
    items = get_triage_candidates(limit=7, max_per_project=2)
    today = datetime.now().strftime("%A, %d %B %Y")
    cards_html = ""
    if not items:
        cards_html = """
        <div class="bg-white rounded-lg border border-stone-200 p-12 text-center">
            <p class="text-stone-500">No items to triage right now. Ingest more conversations or check back later.</p>
        </div>
        """
    else:
        for i, (score, item, card, key) in enumerate(items, 1):
            t = item.get("type", "")
            badge_class, badge_label = TYPE_BADGES.get(t, ("bg-stone-100 text-stone-700 border-stone-200", t or "—"))
            topic = (card.get("topic") or "Untitled").strip()
            text = (item.get("text") or "").strip()
            proj = item.get("project") or card.get("project_match", {}).get("id", "none")
            proj_tag = f'<span class="text-xs text-stone-500 ml-2">{proj}</span>' if proj not in ("none", "new") else ""
            session_id = card["_session_id"]
            cards_html += f"""
            <div class="card bg-white rounded-lg border border-stone-200 p-5 mb-3">
                <div class="flex items-center justify-between mb-2">
                    <span class="inline-block px-2 py-0.5 text-xs font-medium rounded border {badge_class}">{badge_label}</span>
                    {proj_tag}
                </div>
                <div class="text-sm text-stone-500 mb-1">{topic}</div>
                <div class="text-base text-stone-900 mb-4">{text}</div>
                <div class="flex gap-2" hx-target="this" hx-swap="outerHTML">
                    <button hx-post="/triage/decide" hx-vals='{{"key": "{key}", "decision": "keep", "text": "{text[:200]}", "session_id": "{session_id}"}}'
                            class="px-3 py-1.5 text-sm rounded-md bg-emerald-600 text-white hover:bg-emerald-700">Keep</button>
                    <button hx-post="/triage/decide" hx-vals='{{"key": "{key}", "decision": "ignore", "text": "{text[:200]}", "session_id": "{session_id}"}}'
                            class="px-3 py-1.5 text-sm rounded-md bg-stone-200 text-stone-700 hover:bg-stone-300">Ignore</button>
                    <button hx-post="/triage/decide" hx-vals='{{"key": "{key}", "decision": "later", "text": "{text[:200]}", "session_id": "{session_id}"}}'
                            class="px-3 py-1.5 text-sm rounded-md bg-amber-100 text-amber-800 hover:bg-amber-200">Later (7d)</button>
                </div>
            </div>
            """
    content = f"""
    <div class="mb-6">
        <h1 class="text-2xl font-semibold mb-1">Today's triage</h1>
        <p class="text-stone-500 text-sm">{today} · {len(items)} item(s)</p>
    </div>
    {cards_html}
    """
    return render_template_string(BASE, page_title="Triage", active="triage", content=content)


@app.route("/triage/decide", methods=["POST"])
def triage_decide():
    key = request.form.get("key", "")
    decision = request.form.get("decision", "")
    text = request.form.get("text", "")
    session_id = request.form.get("session_id", "")
    log_triage_decision(key, decision, text, session_id)
    badge_color = {"keep": "bg-emerald-100 text-emerald-700", "ignore": "bg-stone-100 text-stone-600", "later": "bg-amber-100 text-amber-700"}[decision]
    return f'<div class="text-sm py-2 px-3 rounded {badge_color}">✓ {decision.capitalize()}</div>'


@app.route("/cards")
def page_cards():
    q = request.args.get("q", "").strip().lower()
    cards = load_all_cards()
    if q:
        cards = [c for c in cards if q in json.dumps(c).lower()]
    rows = ""
    for card in cards[:100]:
        topic = (card.get("topic") or "Untitled")[:80]
        proj = card.get("project_match", {}).get("id", "")
        meta = card.get("_meta", {})
        extracted = meta.get("extracted_at", "")[:10]
        opens = len(card.get("open_items", []) or [])
        sparks = len(card.get("sparks", []) or [])
        links = len(card.get("links", []) or [])
        rows += f"""
        <a href="/cards/{card['_session_id']}" class="block bg-white rounded border border-stone-200 p-4 hover:border-stone-400">
            <div class="flex items-center justify-between mb-1">
                <span class="font-medium text-stone-900">{topic}</span>
                <span class="text-xs text-stone-500">{extracted}</span>
            </div>
            <div class="flex gap-3 text-xs text-stone-500">
                <span>📁 {proj}</span>
                <span>📌 {opens}</span>
                <span>💡 {sparks}</span>
                <span>🔗 {links}</span>
            </div>
        </a>
        """
    content = f"""
    <div class="mb-6">
        <h1 class="text-2xl font-semibold mb-3">Cards ({len(cards)})</h1>
        <form method="get" class="flex gap-2">
            <input type="search" name="q" value="{q}" placeholder="Search topic, context, sparks, links..."
                   class="flex-1 px-3 py-2 rounded border border-stone-300 text-sm">
            <button class="px-4 py-2 rounded bg-stone-900 text-white text-sm">Search</button>
        </form>
    </div>
    <div class="grid gap-3">{rows or '<p class="text-stone-500">No cards yet.</p>'}</div>
    """
    return render_template_string(BASE, page_title="Cards", active="cards", content=content)


@app.route("/cards/<session_id>")
def page_card_detail(session_id):
    card_path = CARDS_DIR / f"{session_id}.json"
    if not card_path.exists():
        return f"Card not found: {session_id}", 404
    card = json.loads(card_path.read_text())
    def section(title, items, key_field=None):
        if not items:
            return ""
        html = f'<h3 class="font-semibold text-stone-800 mt-6 mb-2">{title}</h3><div class="grid gap-2">'
        for it in items:
            if isinstance(it, dict):
                primary = it.get(key_field) if key_field else (it.get("text") or it.get("url") or it.get("name") or it.get("path") or "")
                ctx = it.get("context") or it.get("trigger_context") or it.get("role_or_context") or ""
                tag = it.get("type") or ""
                proj = it.get("project") or ""
                html += f'<div class="bg-stone-50 rounded p-3 text-sm"><div class="font-medium">{primary}</div>'
                if ctx: html += f'<div class="text-xs text-stone-500 mt-1">{ctx}</div>'
                if tag or proj: html += f'<div class="text-xs text-stone-400 mt-1">{tag} · {proj}</div>'
                html += '</div>'
        html += '</div>'
        return html

    content = f"""
    <a href="/cards" class="text-sm text-stone-500 hover:text-stone-700">← Back to cards</a>
    <h1 class="text-2xl font-semibold mt-2 mb-1">{card.get('topic', 'Untitled')}</h1>
    <div class="text-sm text-stone-500 mb-6">{card.get('project_match', {}).get('id', '')} · {session_id}</div>
    <div class="bg-white rounded-lg border border-stone-200 p-6">
        <h3 class="font-semibold mb-2">Context</h3>
        <p class="text-stone-700 mb-4">{card.get('context', '—')}</p>
        <h3 class="font-semibold mb-2">Conclusion</h3>
        <p class="text-stone-700">{card.get('conclusion', '—')}</p>
        {section('Open items', card.get('open_items', []))}
        {section('Sparks', card.get('sparks', []))}
        {section('Links', card.get('links', []), key_field='url')}
        {section('People', card.get('people', []), key_field='name')}
        {section('References', card.get('references', []), key_field='name')}
        {section('Numbers', card.get('numbers', []), key_field='value')}
        {section('Files', card.get('files', []), key_field='path')}
    </div>
    """
    return render_template_string(BASE, page_title="Card", active="cards", content=content)


@app.route("/projects")
def page_projects():
    config = load_config()
    grouped = {"active": [], "on-hold": [], "dormant": [], "archived": []}
    for slug, info in config.items():
        s = info.get("status", "dormant")
        grouped.setdefault(s, []).append((slug, info))

    def render_group(name, key, items):
        if not items:
            return ""
        badge = STATUS_BADGES.get(key, "bg-stone-100 text-stone-700")
        rows = ""
        for slug, info in sorted(items):
            children = info.get("children") or []
            desc = info.get("description") or ""
            child_html = ""
            if children:
                child_html = '<div class="text-xs text-stone-500 mt-1">└─ ' + ", ".join(children[:6]) + (f" +{len(children)-6}" if len(children) > 6 else "") + '</div>'
            rows += f"""
            <div class="bg-white rounded border border-stone-200 p-4 mb-2">
                <div class="flex items-center justify-between mb-1">
                    <span class="font-medium">{slug}</span>
                    <span class="px-2 py-0.5 text-xs rounded {badge}">{key}</span>
                </div>
                {f'<p class="text-sm text-stone-600 mb-1">{desc}</p>' if desc else '<p class="text-sm text-stone-400 italic mb-1">(no description)</p>'}
                {child_html}
            </div>
            """
        return f'<h2 class="text-lg font-semibold text-stone-800 mt-6 mb-2">{name} ({len(items)})</h2>{rows}'

    content = f"""
    <div class="mb-6 flex items-start justify-between">
        <div>
            <h1 class="text-2xl font-semibold mb-1">Projects</h1>
            <p class="text-stone-500 text-sm">{len(config)} total · edit at <code>~/.phaedrus/projects.yml</code></p>
        </div>
        <a href="/settings#projects" class="px-3 py-1.5 text-sm rounded bg-stone-900 text-white">Open YAML</a>
    </div>
    {render_group("Active", "active", grouped["active"])}
    {render_group("On hold", "on-hold", grouped["on-hold"])}
    {render_group("Archived", "archived", grouped.get("archived", []))}
    <div class="mt-8 text-xs text-stone-400">{len(grouped["dormant"])} dormant projects hidden.</div>
    """
    return render_template_string(BASE, page_title="Projects", active="projects", content=content)


@app.route("/settings")
def page_settings():
    prefs = load_preferences()
    rules = load_rules()
    recent = load_recent_corrections(n=10)

    rules_html = "<p class='text-stone-500 text-sm'>No deterministic rules yet.</p>"
    if rules:
        rules_html = "".join(
            f'<div class="bg-white border border-stone-200 rounded p-3 mb-2 text-sm"><div class="font-medium">{r.get("name", "unnamed")}</div><div class="text-xs text-stone-500">applies to: {r.get("applies_to")} · pattern: <code>{r.get("pattern")}</code> · action: {r.get("action")}</div></div>'
            for r in rules
        )

    corrections_html = "<p class='text-stone-500 text-sm'>No corrections yet.</p>"
    if recent:
        corrections_html = "".join(
            f'<div class="bg-white border border-stone-200 rounded p-3 mb-2 text-sm"><div class="text-xs text-stone-500">{r.get("ts", "")}</div><div>{r.get("field")}: <span class="line-through text-stone-400">{r.get("original", "")[:80]}</span> → <span class="font-medium">{r.get("corrected", "")[:80]}</span></div></div>'
            for r in recent
        )

    content = f"""
    <h1 class="text-2xl font-semibold mb-6">Settings & learning</h1>

    <section class="mb-8">
      <h2 class="text-lg font-semibold mb-2">Preferences</h2>
      <p class="text-sm text-stone-500 mb-2">Plain-English rules Phaedrus reads on every extraction.</p>
      <div class="bg-white border border-stone-200 rounded p-4 whitespace-pre-wrap text-sm font-mono">{prefs}</div>
      <p class="text-xs text-stone-400 mt-2">Edit at <code>~/.phaedrus/preferences.md</code></p>
    </section>

    <section class="mb-8">
      <h2 class="text-lg font-semibold mb-2">Deterministic rules ({len(rules)})</h2>
      <p class="text-sm text-stone-500 mb-2">Post-processing rules the LLM cannot bypass.</p>
      {rules_html}
      <p class="text-xs text-stone-400 mt-2">Edit at <code>~/.phaedrus/rules.jsonl</code></p>
    </section>

    <section class="mb-8" id="projects">
      <h2 class="text-lg font-semibold mb-2">Project config</h2>
      <p class="text-sm text-stone-500 mb-2">All project organisation lives in one YAML file.</p>
      <p class="text-xs text-stone-400">Edit at <code>~/.phaedrus/projects.yml</code></p>
    </section>

    <section>
      <h2 class="text-lg font-semibold mb-2">Recent corrections ({len(recent)})</h2>
      {corrections_html}
      <p class="text-xs text-stone-400 mt-2">Log at <code>~/.phaedrus/corrections.jsonl</code></p>
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


# === Boot ===

def open_browser():
    webbrowser.open_new("http://localhost:5050/")


if __name__ == "__main__":
    print("Phaedrus dashboard starting at http://localhost:5050")
    print("Press Ctrl+C to stop.")
    Timer(1.5, open_browser).start()
    app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=False)
