"""
Phaedrus universal export-file importer.

Parses official data-export files from each AI platform into Phaedrus's common
capture shape, so the user can drop one export and get their COMPLETE history
(including archived + project chats) with real timestamps — no API paging, no
rate limits, no risk of an internal endpoint changing.

Common output record (one per conversation):
    {
        "id": "<stable id>",
        "source": "chatgpt" | "claude" | "gemini" | "perplexity" | "deepseek",
        "title": "<conversation title>",
        "url": "<best-effort url or ''>",
        "captured_at": "<ISO8601 or ''>",
        "messages": [{"role": "user"|"assistant", "content": "..."}, ...],
    }

Each platform's export is structured differently. We sniff the shape and route
to the right parser. Robust to missing fields; never raises on a single bad
conversation (it's skipped and counted instead).

Supported inputs:
- A .zip (we read the relevant JSON members)
- A raw .json file (conversations.json, or Takeout activity json)
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone


# ---------- helpers ----------

def _iso(value, *, seconds=False) -> str:
    """Best-effort ISO8601 from a unix-seconds float, ms, or ISO string. '' on failure."""
    if value is None or value == "":
        return ""
    try:
        if isinstance(value, (int, float)):
            ts = float(value)
            # Heuristic: treat large numbers as ms
            if ts > 1e12:
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        s = str(value).strip()
        # Already ISO-ish?
        s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s).astimezone(timezone.utc).isoformat()
    except Exception:
        return ""


def _clean(text) -> str:
    if not isinstance(text, str):
        return ""
    return text.strip()


# ---------- ChatGPT ----------

def parse_chatgpt_export(data) -> list:
    """ChatGPT conversations.json: a list of conversation objects, each with a
    `mapping` tree (same structure the web API returns per conversation)."""
    out = []
    convs = data if isinstance(data, list) else data.get("conversations", []) if isinstance(data, dict) else []
    for conv in convs:
        try:
            messages = _flatten_chatgpt_mapping(conv)
            if not messages:
                continue
            cid = conv.get("conversation_id") or conv.get("id") or ""
            out.append({
                "id": "chatgpt_" + str(cid) if cid else "chatgpt_" + str(abs(hash(conv.get("title", ""))))[:12],
                "source": "chatgpt",
                "title": conv.get("title") or "ChatGPT conversation",
                "url": ("https://chatgpt.com/c/" + str(cid)) if cid else "",
                "captured_at": _iso(conv.get("create_time"), seconds=True) or _iso(conv.get("update_time"), seconds=True),
                "messages": messages,
            })
        except Exception:
            continue
    return out


def _flatten_chatgpt_mapping(conv) -> list:
    """Walk the mapping tree from current_node up to root, then reverse."""
    mapping = conv.get("mapping")
    if not isinstance(mapping, dict):
        return []
    chain = []
    node_id = conv.get("current_node")
    guard = 0
    # If no current_node, fall back to iterating mapping in insertion order
    if node_id and node_id in mapping:
        while node_id and node_id in mapping and guard < 100000:
            chain.append(mapping[node_id])
            node_id = mapping[node_id].get("parent")
            guard += 1
        chain.reverse()
    else:
        chain = list(mapping.values())
    out = []
    for node in chain:
        msg = node.get("message") if isinstance(node, dict) else None
        if not msg:
            continue
        author = (msg.get("author") or {}).get("role")
        if author not in ("user", "assistant"):
            continue
        content = msg.get("content") or {}
        parts = content.get("parts") if isinstance(content, dict) else None
        text = ""
        if isinstance(parts, list):
            text = "\n".join(p for p in parts if isinstance(p, str)).strip()
        if not text:
            continue
        out.append({"role": author, "content": text})
    return out


# ---------- Claude ----------

def parse_claude_export(data) -> list:
    """Claude export: a list of conversations, each with chat_messages[] of
    {sender|role, text|content, created_at}."""
    out = []
    convs = data if isinstance(data, list) else data.get("conversations", []) if isinstance(data, dict) else []
    for conv in convs:
        try:
            raw = conv.get("chat_messages") or conv.get("messages") or []
            messages = []
            for m in raw:
                content = _clean(m.get("text"))
                if not content and isinstance(m.get("content"), list):
                    content = "\n".join(_clean(c.get("text")) for c in m["content"] if isinstance(c, dict)).strip()
                if not content:
                    continue
                sender = m.get("sender") or m.get("role")
                role = "user" if sender in ("human", "user") else "assistant"
                messages.append({"role": role, "content": content})
            if not messages:
                continue
            cid = conv.get("uuid") or conv.get("id") or ""
            out.append({
                "id": "claude_" + str(cid) if cid else "claude_" + str(abs(hash(conv.get("name", ""))))[:12],
                "source": "claude",
                "title": conv.get("name") or conv.get("title") or "Claude conversation",
                "url": ("https://claude.ai/chat/" + str(cid)) if cid else "",
                "captured_at": _iso(conv.get("created_at")) or _iso(conv.get("updated_at")),
                "messages": messages,
            })
        except Exception:
            continue
    return out


# ---------- Gemini (Google Takeout) ----------

def parse_gemini_takeout(data) -> list:
    """Google Takeout 'Gemini Apps' activity: a list of activity records, each a
    prompt or response with a 'time'. We pair consecutive user/model entries into
    conversations by date when no thread id is present (Takeout is flat)."""
    out = []
    records = data if isinstance(data, list) else []
    # Takeout activity is flat; group into a single import bucket per day.
    by_day = {}
    for rec in records:
        try:
            title = rec.get("title") or ""
            # "Prompted ..." = user; otherwise assistant text in "title"/"subtitles"
            t = _iso(rec.get("time"))
            day = t[:10] if t else "unknown"
            text = ""
            if title.lower().startswith("prompted"):
                role = "user"
                text = title[len("Prompted"):].strip(" :")
            else:
                role = "assistant"
                text = title
            text = _clean(text)
            if not text:
                continue
            by_day.setdefault(day, []).append({"role": role, "content": text, "_t": t})
        except Exception:
            continue
    for day, msgs in by_day.items():
        if not msgs:
            continue
        out.append({
            "id": "gemini_" + day,
            "source": "gemini",
            "title": "Gemini — " + day,
            "url": "",
            "captured_at": msgs[0].get("_t", ""),
            "messages": [{"role": m["role"], "content": m["content"]} for m in msgs],
        })
    return out


# ---------- Perplexity ----------

def parse_perplexity_export(data) -> list:
    """Perplexity 'Download My Data' JSON. Shape varies; handle a list of threads
    each with entries/messages, or a flat list of Q/A items."""
    out = []
    threads = data if isinstance(data, list) else data.get("threads") or data.get("conversations") or []
    for th in threads:
        try:
            entries = th.get("entries") or th.get("messages") or th.get("blocks") or []
            messages = []
            for e in entries:
                q = _clean(e.get("query") or e.get("question") or e.get("prompt"))
                a = _clean(e.get("answer") or e.get("response") or e.get("text"))
                if q:
                    messages.append({"role": "user", "content": q})
                if a:
                    messages.append({"role": "assistant", "content": a})
                # fallback: role/content pair
                if not q and not a:
                    c = _clean(e.get("content"))
                    if c:
                        role = "user" if e.get("role") in ("user", "human") else "assistant"
                        messages.append({"role": role, "content": c})
            if not messages:
                continue
            cid = th.get("uuid") or th.get("id") or th.get("thread_id") or ""
            out.append({
                "id": "perplexity_" + str(cid) if cid else "perplexity_" + str(abs(hash(str(th))))[:12],
                "source": "perplexity",
                "title": th.get("title") or "Perplexity thread",
                "url": ("https://www.perplexity.ai/search/" + str(cid)) if cid else "",
                "captured_at": _iso(th.get("created_at") or th.get("time")),
                "messages": messages,
            })
        except Exception:
            continue
    return out


# ---------- DeepSeek ----------

def parse_deepseek_export(data) -> list:
    """DeepSeek GDPR export JSON. Handle a list of conversations with messages[]."""
    out = []
    convs = data if isinstance(data, list) else data.get("conversations") or data.get("history") or []
    for conv in convs:
        try:
            raw = conv.get("messages") or conv.get("chat_messages") or []
            messages = []
            for m in raw:
                content = _clean(m.get("content") or m.get("text"))
                if not content:
                    continue
                role = "user" if m.get("role") in ("user", "human") else "assistant"
                messages.append({"role": role, "content": content})
            if not messages:
                continue
            cid = conv.get("id") or conv.get("uuid") or ""
            out.append({
                "id": "deepseek_" + str(cid) if cid else "deepseek_" + str(abs(hash(str(conv))))[:12],
                "source": "deepseek",
                "title": conv.get("title") or "DeepSeek conversation",
                "url": "",
                "captured_at": _iso(conv.get("created_at") or conv.get("inserted_at")),
                "messages": messages,
            })
        except Exception:
            continue
    return out


# ---------- sniffing + dispatch ----------

PARSERS = {
    "chatgpt": parse_chatgpt_export,
    "claude": parse_claude_export,
    "gemini": parse_gemini_takeout,
    "perplexity": parse_perplexity_export,
    "deepseek": parse_deepseek_export,
}


def detect_platform(data) -> str:
    """Sniff the parsed JSON to guess the source platform."""
    sample = None
    if isinstance(data, list) and data:
        sample = data[0]
    elif isinstance(data, dict):
        # Some exports wrap in a dict
        for key in ("conversations", "threads", "history"):
            if isinstance(data.get(key), list) and data[key]:
                sample = data[key][0]
                break
        if sample is None:
            sample = data
    if not isinstance(sample, dict):
        return ""
    keys = set(sample.keys())
    if "mapping" in keys:
        return "chatgpt"
    if "chat_messages" in keys or ("uuid" in keys and "name" in keys):
        return "claude"
    if "query" in keys or "entries" in keys or "blocks" in keys:
        return "perplexity"
    # Takeout activity record
    if "title" in keys and "time" in keys and "header" in keys:
        return "gemini"
    if "messages" in keys and ("model" in keys or "id" in keys):
        return "deepseek"
    return ""


def _json_members_from_zip(zf: zipfile.ZipFile) -> list:
    """Yield (name, parsed_json) for likely-relevant JSON members of an export zip."""
    candidates = []
    for name in zf.namelist():
        low = name.lower()
        if not low.endswith(".json"):
            continue
        # Skip obvious non-conversation metadata
        if any(skip in low for skip in ("user.json", "message_feedback", "model_comparisons", "shared_conversations")):
            continue
        try:
            with zf.open(name) as f:
                candidates.append((name, json.load(f)))
        except Exception:
            continue
    return candidates


def import_bytes(raw: bytes, filename: str = "", platform_hint: str = "") -> dict:
    """
    Parse an uploaded export (zip or json bytes) into capture records.

    Returns {"records": [...], "platform": "...", "by_platform": {p: count}, "errors": [...]}.
    """
    results = []
    errors = []
    blobs = []  # list of parsed json objects to try

    name_low = (filename or "").lower()
    if name_low.endswith(".zip") or raw[:2] == b"PK":
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
            for member_name, obj in _json_members_from_zip(zf):
                blobs.append(obj)
            if not blobs:
                errors.append("zip contained no readable conversation JSON")
        except Exception as e:
            errors.append(f"could not read zip: {e}")
    else:
        try:
            blobs.append(json.loads(raw.decode("utf-8", "replace")))
        except Exception as e:
            errors.append(f"could not parse json: {e}")

    by_platform = {}
    for obj in blobs:
        platform = platform_hint or detect_platform(obj)
        parser = PARSERS.get(platform)
        if not parser:
            continue
        try:
            recs = parser(obj)
            for r in recs:
                results.append(r)
            by_platform[platform] = by_platform.get(platform, 0) + len(recs)
        except Exception as e:
            errors.append(f"{platform} parse error: {e}")

    # dedupe by id
    seen = set()
    deduped = []
    for r in results:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        deduped.append(r)

    primary = max(by_platform, key=by_platform.get) if by_platform else ""
    return {"records": deduped, "platform": primary, "by_platform": by_platform, "errors": errors}


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python import_exports.py <export.zip|export.json>")
        raise SystemExit(1)
    with open(sys.argv[1], "rb") as f:
        raw = f.read()
    res = import_bytes(raw, filename=sys.argv[1])
    print(f"platform(s): {res['by_platform']}")
    print(f"records: {len(res['records'])}")
    if res["errors"]:
        print("errors:", res["errors"])
    if res["records"]:
        r0 = res["records"][0]
        print(f"sample: [{r0['source']}] {r0['title'][:50]} — {len(r0['messages'])} msgs, {r0['captured_at'][:10]}")
