"""Refresh the local NapCat documentation mirror and record hashes."""
import hashlib
import json
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
BASE = "https://raw.githubusercontent.com/NapNeko/NapCatDocs/main/src/"
FILES = {
    "onebot_index.md": "onebot/index.md",
    "onebot_api.md": "onebot/api/index.md",
    "onebot_event.md": "onebot/event.md",
    "onebot_segment.md": "onebot/segment.md",
    "onebot_basic_event.md": "onebot/basic_event.md",
}


def main():
    DOCS.mkdir(parents=True, exist_ok=True)
    manifest = {"source": BASE, "synced_on": date.today().isoformat(), "files": {}}
    for filename, relative_url in FILES.items():
        request = urllib.request.Request(BASE + relative_url, headers={"User-Agent": "qqbot-doc-sync"})
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
        if len(payload) < 200 or b"Package size exceeded" in payload:
            raise RuntimeError("invalid NapCat document: " + relative_url)
        (DOCS / filename).write_bytes(payload)
        manifest["files"][filename] = {
            "upstream": relative_url,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    (DOCS / "napcat-docs-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
