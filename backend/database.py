"""Small SQLite repository for the prototype. It can later be replaced by PostgreSQL."""
import json
import re
import sqlite3
from datetime import date
from hashlib import sha1
from pathlib import Path

from .models import Supplier

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "foodsource.sqlite3"
SEED_PATH = ROOT / "data" / "suppliers.seed.json"
SOURCES_PATH = ROOT / "data" / "sources.json"
PHONE_PATTERN = re.compile(r"\+?\d[\d\s()\-]{8,}\d")
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", re.I)


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS suppliers (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, payload TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS supplier_states (
          supplier_id TEXT PRIMARY KEY, favourite INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'new', note TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS search_history (
          id INTEGER PRIMARY KEY AUTOINCREMENT, query_text TEXT NOT NULL,
          normalized_query TEXT NOT NULL, filters TEXT NOT NULL,
          results_count INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        if db.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0] == 0:
            upsert_many(reviewed_source_seed())
    # Safe id migration for databases created by an earlier collector version.
    migrate_legacy_reviewed_cards()
    backfill_supplier_fields()


def normalize_supplier_fields(item: dict) -> dict:
    """Keep published phone, email and address separate without inventing facts."""
    result = dict(item)
    contact = str(result.get("contact", ""))
    if not result.get("phone"):
        phone = PHONE_PATTERN.search(contact)
        result["phone"] = phone.group(0).strip() if phone else ""
    if not result.get("email"):
        email = EMAIL_PATTERN.search(contact)
        result["email"] = email.group(0).strip() if email else ""
    if not result.get("address"):
        # Only lift an address when the published contact explicitly contains an address marker.
        address_marker = re.search(r"(?:ул\.|улиц|проспект|пр-т|шоссе|пер\.|переул|офис|строен|дом\s*№?)", contact, re.I)
        result["address"] = contact.strip() if address_marker else ""
    if not result.get("contact"):
        result["contact"] = " · ".join(value for value in (result.get("phone"), result.get("email")) if value)
    result.setdefault("source_url", result.get("source", ""))
    result.setdefault("source_name", result.get("name", ""))
    result.setdefault("phone", "")
    result.setdefault("email", "")
    result.setdefault("address", "")
    return result


def reviewed_source_seed() -> list[dict]:
    """Initial catalog of reviewed public sources for an offline-stable demo.

    The scheduled collector replaces the same ids with automatically refreshed
    data when a source allows access. No facts are invented: data comes only from
    the reviewed source registry and remains linked to its URL.
    """
    if not SOURCES_PATH.exists():
        return json.loads(SEED_PATH.read_text(encoding="utf-8"))
    items = []
    for source in json.loads(SOURCES_PATH.read_text(encoding="utf-8")):
        item = {
            "id": "web-" + sha1(source["url"].encode()).hexdigest()[:10],
            "name": source["url"], "type": "Поставщик", "categories": [source["category"]],
            "products": [], "regions": [source["region"]], "city": source["region"],
            "description": "Данные из проверенной карточки публичного источника.",
            "minimumOrder": "Уточнить у поставщика", "priceRange": "По запросу",
            "delivery": "Уточнить у поставщика", "certificates": [], "contact": "",
            "website": source["url"], "source": source["url"], "updated": date.today().isoformat(),
            "completeness": 65, "collection_mode": "reviewed_metadata",
        }
        for key, value in source.get("overrides", {}).items():
            if value not in (None, "", [], {}):
                item[key] = value
        item["categories"] = list(dict.fromkeys(item["categories"] + [source["category"]]))
        item["regions"] = list(dict.fromkeys(item["regions"] + [source["region"]]))
        items.append(item)
    return items


def upsert_many(items: list[dict]) -> int:
    with connect() as db:
        # A CSV/XLSX export can be imported back into the service. Reuse the
        # existing card when its source URL is already known instead of
        # creating a visually identical second supplier.
        existing_sources = {}
        for row in db.execute("SELECT id, payload FROM suppliers").fetchall():
            payload = json.loads(row["payload"])
            source = payload.get("source_url") or payload.get("source") or payload.get("website")
            if source:
                existing_sources[source.rstrip("/")] = row["id"]
        for item in items:
            item = normalize_supplier_fields(item)
            source = item.get("source_url") or item.get("source") or item.get("website")
            if source and source.rstrip("/") in existing_sources:
                item["id"] = existing_sources[source.rstrip("/")]
            db.execute(
                "INSERT OR REPLACE INTO suppliers(id,name,payload,updated_at) VALUES(?,?,?,datetime('now'))",
                (item["id"], item["name"], json.dumps(item, ensure_ascii=False)),
            )
        db.commit()
    return len(items)


def backfill_supplier_fields() -> int:
    """Add structured contact fields to catalog records created before this change."""
    changed = 0
    with connect() as db:
        rows = db.execute("SELECT id, payload FROM suppliers").fetchall()
        for row in rows:
            current = json.loads(row["payload"])
            normalized = normalize_supplier_fields(current)
            if normalized == current:
                continue
            db.execute(
                "UPDATE suppliers SET payload=?, updated_at=datetime('now') WHERE id=?",
                (json.dumps(normalized, ensure_ascii=False), row["id"]),
            )
            changed += 1
        db.commit()
    return changed


def list_suppliers() -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT payload FROM suppliers ORDER BY name").fetchall()
    return [json.loads(row["payload"]) for row in rows]


def delete_fixture_suppliers() -> int:
    """Delete the original portfolio-only records (ids prefixed with sup-)."""
    with connect() as db:
        result = db.execute("DELETE FROM suppliers WHERE id LIKE 'sup-%'")
        db.commit()
    return result.rowcount


def migrate_legacy_reviewed_cards() -> int:
    """Remove pre-migration reviewed cards while retaining a user's state.

    Older collector versions used a ``reviewed-`` id for robots-restricted
    sources. Current reviewed and automatically collected cards share a stable
    ``web-`` id derived from the source URL, so both modes update one record.
    """
    migrated = 0
    with connect() as db:
        legacy_rows = db.execute(
            "SELECT id, payload FROM suppliers WHERE id LIKE 'reviewed-%'"
        ).fetchall()
        for row in legacy_rows:
            source = json.loads(row["payload"]).get("source", "")
            target_id = "web-" + sha1(source.encode()).hexdigest()[:10]
            target_exists = db.execute(
                "SELECT 1 FROM suppliers WHERE id = ?", (target_id,)
            ).fetchone()
            if not source or not target_exists:
                continue
            legacy_state = db.execute(
                "SELECT favourite, status, note, updated_at FROM supplier_states WHERE supplier_id = ?",
                (row["id"],),
            ).fetchone()
            if legacy_state:
                db.execute(
                    """INSERT OR IGNORE INTO supplier_states
                    (supplier_id, favourite, status, note, updated_at) VALUES(?,?,?,?,?)""",
                    (target_id, legacy_state["favourite"], legacy_state["status"],
                     legacy_state["note"], legacy_state["updated_at"]),
                )
            db.execute("DELETE FROM supplier_states WHERE supplier_id = ?", (row["id"],))
            db.execute("DELETE FROM suppliers WHERE id = ?", (row["id"],))
            migrated += 1
        db.commit()
    return migrated


def get_supplier(supplier_id: str) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT payload FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
    return json.loads(row["payload"]) if row else None


def list_categories() -> list[str]:
    return sorted({category for item in list_suppliers() for category in item.get("categories", [])})


def list_types() -> list[str]:
    return sorted({item.get("type", "Поставщик") for item in list_suppliers()})


def list_states() -> dict[str, dict]:
    with connect() as db:
        rows = db.execute("SELECT supplier_id, favourite, status, note, updated_at FROM supplier_states").fetchall()
    return {row["supplier_id"]: {"favourite": bool(row["favourite"]), "status": row["status"], "note": row["note"], "updated_at": row["updated_at"]} for row in rows}


def record_search(query_text: str, normalized: dict, filters: dict, results_count: int) -> None:
    """Store a compact local history for meaningful free-text searches only."""
    if not query_text.strip():
        return
    normalized_json = json.dumps(normalized, ensure_ascii=False)
    filters_json = json.dumps(filters, ensure_ascii=False, sort_keys=True)
    with connect() as db:
        last = db.execute(
            "SELECT query_text, filters FROM search_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last and last["query_text"] == query_text.strip() and last["filters"] == filters_json:
            return
        db.execute(
            "INSERT INTO search_history(query_text, normalized_query, filters, results_count) VALUES(?,?,?,?)",
            (query_text.strip(), normalized_json, filters_json, results_count),
        )
        db.commit()


def list_search_history(limit: int = 5) -> list[dict]:
    with connect() as db:
        rows = db.execute(
            "SELECT query_text, normalized_query, filters, results_count, created_at FROM search_history ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 20)),),
        ).fetchall()
    return [{
        "query": row["query_text"], "normalized_query": json.loads(row["normalized_query"]),
        "filters": json.loads(row["filters"]), "results_count": row["results_count"],
        "created_at": row["created_at"],
    } for row in rows]


def clear_search_history() -> int:
    with connect() as db:
        result = db.execute("DELETE FROM search_history")
        db.commit()
        return result.rowcount


def get_state(supplier_id: str) -> dict:
    return list_states().get(supplier_id, {"favourite": False, "status": "new", "note": "", "updated_at": ""})


def update_state(supplier_id: str, payload: dict) -> dict:
    current = get_state(supplier_id)
    next_state = {**current, **{key: value for key, value in payload.items() if value is not None}}
    valid_statuses = {"new", "check", "contact", "selected"}
    if next_state["status"] not in valid_statuses:
        raise ValueError("Недопустимый статус")
    with connect() as db:
        db.execute("""INSERT INTO supplier_states(supplier_id, favourite, status, note, updated_at)
            VALUES(?,?,?,?,datetime('now'))
            ON CONFLICT(supplier_id) DO UPDATE SET favourite=excluded.favourite, status=excluded.status,
            note=excluded.note, updated_at=excluded.updated_at""",
            (supplier_id, int(bool(next_state["favourite"])), next_state["status"], next_state["note"]))
        db.commit()
    return get_state(supplier_id)
