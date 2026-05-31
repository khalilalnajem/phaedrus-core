"""
Import a LOCAL export zip (Grok account export, Google Takeout/Gemini) into
Phaedrus web_captures. Handles the big nested zips the web drop-zone isn't ideal
for (multi-GB Takeout, xAI's prod-grok-backend.json).

Usage:
    python scripts/phaedrus/import_local_zip.py grok   ~/Downloads/<grok>.zip
    python scripts/phaedrus/import_local_zip.py gemini ~/Downloads/takeout-*.zip

Writes one capture per conversation to ~/.phaedrus/web_captures/<source>_<id>.json
(same shape the extension produces, so the existing ingester turns them into cards).
"""
import json
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import import_exports as ie

PHAEDRUS_HOME = Path.home() / ".phaedrus"
WEB_CAPTURES = PHAEDRUS_HOME / "web_captures"
WEB_CAPTURES.mkdir(parents=True, exist_ok=True)


def _write(records: list) -> int:
    written = 0
    for r in records:
        try:
            src = "".join(c for c in str(r.get("source", "import")) if c.isalnum() or c in "-._") or "import"
            rid = str(r.get("id", ""))
            if rid.startswith(src + "_"):
                rid = rid[len(src) + 1:]
            safe_id = "".join(c for c in rid if c.isalnum() or c in "-._")[:80] or str(int(time.time() * 1000))
            rec = {
                "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "source": r.get("source"), "id": r.get("id"),
                "url": r.get("url", ""), "title": r.get("title", ""),
                "captured_at": r.get("captured_at", ""),
                "messages": r.get("messages", []),
                "raw": {"imported_from_export": True, **r},
            }
            (WEB_CAPTURES / f"{src}_{safe_id}.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False))
            written += 1
        except Exception as e:
            print(f"  write error: {e}")
    return written


def import_grok(zip_path: Path) -> int:
    with zipfile.ZipFile(zip_path) as zf:
        member = next((n for n in zf.namelist() if n.endswith("prod-grok-backend.json")), None)
        if not member:
            print("No prod-grok-backend.json in zip"); return 0
        data = json.loads(zf.read(member))
    records = ie.parse_grok_export(data)
    print(f"Grok: parsed {len(records)} conversations")
    return _write(records)


def import_gemini(zip_path: Path) -> int:
    total = []
    with zipfile.ZipFile(zip_path) as zf:
        # Personal: Takeout/My Activity/Gemini Apps/*.json ; Workspace: conversation_*.txt
        members = [n for n in zf.namelist()
                   if ("Gemini" in n and (n.endswith(".json") or n.endswith(".txt"))
                       and "conversation" in n.lower())
                   or ("My Activity" in n and "Gemini" in n and n.endswith(".json"))]
        for n in members:
            try:
                obj = json.loads(zf.read(n))
            except Exception:
                continue
            if isinstance(obj, dict) and "conversation_turns" in obj:
                total += ie.parse_gemini_workspace(obj)
            elif isinstance(obj, list):  # My Activity flat
                total += ie.parse_gemini_takeout(obj)
    print(f"Gemini: parsed {len(total)} conversations from {len(members) if 'members' in dir() else '?'} files")
    return _write(total)


def main():
    if len(sys.argv) < 3:
        print(__doc__); raise SystemExit(1)
    source, path = sys.argv[1], Path(sys.argv[2]).expanduser()
    if not path.exists():
        print(f"file not found: {path}"); raise SystemExit(1)
    if source == "grok":
        n = import_grok(path)
    elif source == "gemini":
        n = import_gemini(path)
    else:
        print(f"unknown source: {source} (use grok|gemini)"); raise SystemExit(1)
    print(f">>> {n} {source} conversations written to {WEB_CAPTURES}")


if __name__ == "__main__":
    main()
