"""Synchronize compact NapCat and UApiS OpenAPI contract snapshots."""

import argparse
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "api-contracts.json"
NAPCAT_URL = "https://raw.githubusercontent.com/NapNeko/NapCatDocs/main/src/api/4.18.13/openapi.json"
UAPI_URL = "https://uapis.cn/openapi.json"


def load_json(source):
    path = Path(source)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8-sig"))
    request = urllib.request.Request(source, headers={"User-Agent": "xiaoxi-bot-contract-sync"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def compact(napcat, uapi):
    napcat_actions = sorted(path.lstrip("/") for path in napcat.get("paths", {}))
    pricing = {}
    for path, methods in uapi.get("paths", {}).items():
        relative = path.removeprefix("/api/v1")
        costs = [operation.get("x-uapi-pricing-credits") for method, operation in methods.items()
                 if method.lower() in {"get", "post", "put", "delete"}
                 and isinstance(operation, dict)
                 and operation.get("x-uapi-pricing-credits") is not None]
        if costs:
            pricing[relative] = max(int(value) for value in costs)
    return {
        "napcat": {
            "version": napcat.get("info", {}).get("version"),
            "actions": napcat_actions,
        },
        "uapi": {
            "version": uapi.get("info", {}).get("version"),
            "pricing": dict(sorted(pricing.items())),
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--napcat-source", default=NAPCAT_URL)
    parser.add_argument("--uapi-source", default=UAPI_URL)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    generated = compact(load_json(args.napcat_source), load_json(args.uapi_source))
    text = json.dumps(generated, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != text:
            raise SystemExit("API contract snapshot is out of date; run scripts/sync_api_contracts.py")
        return
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
