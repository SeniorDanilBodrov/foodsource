"""Allowlisted, rate-limited supplier collection job.

Run manually with: python -m backend.collector
Only URLs in data/sources.json are requested. This makes collection repeatable,
auditable and safe for a portfolio prototype.
"""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from .database import migrate_legacy_reviewed_cards, safe_upsert_many
from .parsers.generic import parse_supplier

ROOT = Path(__file__).resolve().parents[1]
SOURCES_PATH = ROOT / "data" / "sources.json"
REPORT_PATH = ROOT / "data" / "collection-report.json"


def merge_collected(item: dict, source: dict) -> dict:
    """Keep facts from an explicitly reviewed source registry over generic guesses."""
    overrides = source.get("overrides", {})
    for key, value in overrides.items():
        if value not in (None, "", [], {}):
            item[key] = value
    item["categories"] = list(dict.fromkeys(item.get("categories", []) + [source["category"]]))
    item["regions"] = list(dict.fromkeys(item.get("regions", []) + [source["region"]]))
    item["source"] = source["url"]
    item["website"] = source["url"]
    confidence = item.setdefault("field_confidence", {})
    confidence["categories"] = "high"
    confidence["regions"] = "high"
    for key in overrides:
        confidence[key] = "high"
    return item


def reviewed_source_item(source: dict) -> dict:
    """Create a traceable record from facts explicitly reviewed in sources.json.

    This is used only when a site rejects automated collection. It never performs
    another request to that site; the data quality screen marks it accordingly.
    """
    from hashlib import sha1
    from datetime import date
    from .search import missing_fields

    item = {
        "id": "web-" + sha1(source["url"].encode()).hexdigest()[:10],
        "name": source["url"], "type": "Поставщик", "categories": [source["category"]],
        "products": [], "regions": [source["region"]], "city": source["region"],
        "description": "Данные из проверенной карточки публичного источника.",
        "minimumOrder": "Уточнить у поставщика", "priceRange": "По запросу",
        "delivery": "Уточнить у поставщика", "certificates": [], "contact": "",
        "website": source["url"], "source": source["url"], "updated": date.today().isoformat(),
        "collection_mode": "reviewed_metadata",
    }
    item = merge_collected(item, source)
    item["completeness"] = max(40, 100 - len(missing_fields(item)) * 10)
    return item


async def collect_sources() -> dict:
    sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    # Add a curated, traceable base card for every allowlisted source first.
    # A temporary HTTP error must not make an already reviewed supplier vanish
    # from the catalogue. A successful parse below replaces that base card,
    # because both records use the same stable URL-derived id.
    items = {item["id"]: item for item in map(reviewed_source_item, sources)}
    automatic, failures, reviewed = [], [], []
    for index, source in enumerate(sources):
        try:
            item = await parse_supplier(source["url"], source["category"], source["region"])
            item = merge_collected(item, source)
            item["collection_mode"] = "automatic"
            items[item["id"]] = item
            automatic.append(item)
        except Exception as exc:  # The report lets a reviewer see what was skipped and why.
            message = str(exc)
            if "robots.txt" in message:
                reviewed.append({"url": source["url"], "reason": message})
            else:
                failures.append({"url": source["url"], "error": message})
        if index < len(sources) - 1:
            await asyncio.sleep(2)
    safe_result = {"imported": 0, "pending_review": 0}
    if items:
        safe_result = safe_upsert_many(list(items.values()), source_mode="automatic")
        migrate_legacy_reviewed_cards()
    report = {
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "attempted": len(sources),
        "imported": safe_result["imported"],
        "archived_skipped": safe_result.get("archived_skipped", 0),
        "automatic": len(automatic),
        "pending_review": safe_result["pending_review"],
        "reviewed_metadata": reviewed,
        "failures": failures,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(asyncio.run(collect_sources()), ensure_ascii=False, indent=2))
