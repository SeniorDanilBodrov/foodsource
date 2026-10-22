"""Persistent repository with PostgreSQL support and a local SQLite fallback."""
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date
from hashlib import sha1
from pathlib import Path
from urllib.parse import urlparse

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # PostgreSQL is optional during lightweight local tests.
    psycopg = None
    dict_row = None

from .models import Supplier
from .quality import assess_supplier, calculate_completeness, confidence_rank

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "foodsource.sqlite3"
SEED_PATH = ROOT / "data" / "suppliers.seed.json"
SOURCES_PATH = ROOT / "data" / "sources.json"
PHONE_PATTERN = re.compile(r"\+?\d[\d\s()\-]{8,}\d")
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", re.I)


def uses_postgres() -> bool:
    return bool(os.getenv("DATABASE_URL", "").strip())


def storage_status() -> dict:
    return {
        "engine": "postgresql" if uses_postgres() else "sqlite",
        "persistent": uses_postgres(),
    }


class DatabaseSession:
    """Small DB-API compatibility layer for SQLite and psycopg."""

    def __init__(self, connection, postgres: bool):
        self.connection = connection
        self.postgres = postgres

    def execute(self, statement: str, parameters=()):
        if self.postgres:
            # Timestamp columns intentionally stay TEXT in both backends so the
            # API returns one stable JSON shape. PostgreSQL needs an explicit
            # cast when CURRENT_TIMESTAMP is written into a TEXT column.
            statement = statement.replace("datetime('now')", "CURRENT_TIMESTAMP::text")
            statement = statement.replace("?", "%s")
        return self.connection.execute(statement, parameters)

    def commit(self):
        self.connection.commit()

    def close(self):
        self.connection.close()


@contextmanager
def connect():
    database_url = os.getenv("DATABASE_URL", "").strip()
    if database_url:
        if psycopg is None:
            raise RuntimeError("Для DATABASE_URL установите зависимость psycopg")
        raw_connection = psycopg.connect(database_url, row_factory=dict_row)
        connection = DatabaseSession(raw_connection, postgres=True)
    else:
        raw_connection = sqlite3.connect(DB_PATH)
        raw_connection.row_factory = sqlite3.Row
        connection = DatabaseSession(raw_connection, postgres=False)
    try:
        yield connection
    finally:
        connection.close()


def init_db() -> None:
    should_seed = False
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
        history_id = "BIGSERIAL PRIMARY KEY" if db.postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
        pending_id = "BIGSERIAL PRIMARY KEY" if db.postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
        now_default = "(CURRENT_TIMESTAMP::text)" if db.postgres else "(datetime('now'))"
        db.execute(f"""CREATE TABLE IF NOT EXISTS search_history (
          id {history_id}, query_text TEXT NOT NULL,
          normalized_query TEXT NOT NULL, filters TEXT NOT NULL,
          results_count INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT {now_default}
        )""")
        db.execute(f"""CREATE TABLE IF NOT EXISTS pending_supplier_updates (
          id {pending_id}, supplier_id TEXT NOT NULL,
          source_url TEXT NOT NULL DEFAULT '', current_payload TEXT NOT NULL DEFAULT '{{}}',
          candidate_payload TEXT NOT NULL, reasons TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          created_at TEXT NOT NULL DEFAULT {now_default}, reviewed_at TEXT
        )""")
        db.execute("""CREATE INDEX IF NOT EXISTS idx_pending_supplier_status
          ON pending_supplier_updates(status, supplier_id)""")
        db.execute("""CREATE TABLE IF NOT EXISTS archived_suppliers (
          id TEXT PRIMARY KEY, payload TEXT NOT NULL, state_payload TEXT NOT NULL DEFAULT '{}',
          archived_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        should_seed = db.execute("SELECT COUNT(*) AS count FROM suppliers").fetchone()["count"] == 0
        db.commit()
    if should_seed:
        upsert_many(reviewed_source_seed())
    # Safe id migration for databases created by an earlier collector version.
    migrate_legacy_reviewed_cards()
    backfill_supplier_fields()
    ensure_source_registry()


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
    result["completeness"] = calculate_completeness(result)
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


def ensure_source_registry() -> int:
    """Add newly reviewed sources without downgrading existing collected cards."""
    reviewed = reviewed_source_seed()
    with connect() as db:
        existing_rows = db.execute("SELECT id, payload FROM suppliers").fetchall()
        archived_rows = db.execute("SELECT id, payload FROM archived_suppliers").fetchall()
    existing_domains = {
        canonical_domain(
            json.loads(row["payload"]).get("source_url")
            or json.loads(row["payload"]).get("source")
            or json.loads(row["payload"]).get("website", "")
        )
        for row in [*existing_rows, *archived_rows]
    }
    existing_ids = {row["id"] for row in [*existing_rows, *archived_rows]}
    missing = [
        item for item in reviewed
        if item["id"] not in existing_ids
        and canonical_domain(item.get("source") or item.get("website", "")) not in existing_domains
    ]
    if missing:
        upsert_many(missing)
    return len(missing)


def upsert_many(items: list[dict]) -> int:
    with connect() as db:
        # A CSV/XLSX export can be imported back into the service. Reuse the
        # existing card when its source URL is already known instead of
        # creating a visually identical second supplier.
        existing_sources = {}
        existing_phones = {}
        existing_names = {}
        for row in db.execute("SELECT id, payload FROM suppliers").fetchall():
            payload = json.loads(row["payload"])
            source = payload.get("source_url") or payload.get("source") or payload.get("website")
            if source:
                existing_sources[canonical_domain(source)] = row["id"]
            phone = canonical_phone(payload.get("phone") or payload.get("contact", ""))
            if phone:
                existing_phones[phone] = row["id"]
            name = canonical_name(payload.get("name", ""))
            if name:
                existing_names[name] = row["id"]
        for item in items:
            item = normalize_supplier_fields(item)
            source = item.get("source_url") or item.get("source") or item.get("website")
            source_key = canonical_domain(source)
            phone_key = canonical_phone(item.get("phone") or item.get("contact", ""))
            name_key = canonical_name(item.get("name", ""))
            duplicate_id = (
                existing_sources.get(source_key)
                or existing_phones.get(phone_key)
                or existing_names.get(name_key)
            )
            if duplicate_id:
                item["id"] = duplicate_id
            db.execute(
                """INSERT INTO suppliers(id,name,payload,updated_at)
                VALUES(?,?,?,datetime('now'))
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                payload=excluded.payload, updated_at=excluded.updated_at""",
                (item["id"], item["name"], json.dumps(item, ensure_ascii=False)),
            )
            if source_key:
                existing_sources[source_key] = item["id"]
            if phone_key:
                existing_phones[phone_key] = item["id"]
            if name_key:
                existing_names[name_key] = item["id"]
        db.commit()
    return len(items)


def canonical_domain(value: str) -> str:
    """Use a stable hostname as the strongest supplier identity."""
    try:
        host = (urlparse(str(value or "")).hostname or "").lower()
    except ValueError:
        return ""
    return host.removeprefix("www.")


def canonical_phone(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-10:] if len(digits) >= 10 else ""


def canonical_name(value: str) -> str:
    normalized = re.sub(r"[^a-zа-яё0-9]", "", str(value or "").casefold())
    return normalized if len(normalized) >= 5 else ""


PROTECTED_AUTOMATIC_FIELDS = {
    "name", "description", "categories", "products", "regions", "city",
    "phone", "email", "address", "delivery", "certificates",
}


def _queue_review(item: dict, current: dict | None, reasons: list[str]) -> int:
    with connect() as db:
        db.execute(
            "DELETE FROM pending_supplier_updates WHERE supplier_id=? AND status='pending'",
            (item["id"],),
        )
        cursor = db.execute(
            """INSERT INTO pending_supplier_updates
            (supplier_id, source_url, current_payload, candidate_payload, reasons)
            VALUES(?,?,?,?,?) RETURNING id""",
            (
                item["id"], item.get("source_url") or item.get("source") or item.get("website", ""),
                json.dumps(current or {}, ensure_ascii=False),
                json.dumps(item, ensure_ascii=False),
                json.dumps(reasons, ensure_ascii=False),
            ),
        )
        row = cursor.fetchone()
        cursor.close()
        db.commit()
        return int(row["id"])


def safe_upsert(item: dict, source_mode: str = "automatic") -> dict:
    """Apply validated fields and quarantine uncertain destructive changes."""
    original = normalize_supplier_fields(item)
    with connect() as db:
        archived = db.execute(
            "SELECT payload FROM archived_suppliers WHERE id = ?", (original["id"],)
        ).fetchone()
    if archived:
        archived_item = normalize_supplier_fields(json.loads(archived["payload"]))
        return {
            "item": archived_item, "review_required": False, "review_id": None,
            "warnings": [], "archived": True,
        }
    safe, report = assess_supplier(original)
    source = safe.get("source_url") or safe.get("source") or safe.get("website", "")
    current = None
    if source:
        normalized_source = source.rstrip("/")
        current = next(
            (existing for existing in list_suppliers()
             if (existing.get("source_url") or existing.get("source") or existing.get("website", "")).rstrip("/") == normalized_source),
            None,
        )
    if current:
        safe["id"] = current["id"]
        original["id"] = current["id"]

    review_reasons = [f"{warning['field']}: {warning['reason']}" for warning in report["warnings"]]
    accepted = dict(safe)
    if current:
        incoming_confidence = safe.get("field_confidence", {})
        current_confidence = current.get("field_confidence", {})
        accepted_confidence = dict(incoming_confidence)
        for field in PROTECTED_AUTOMATIC_FIELDS:
            old_value, new_value = current.get(field), safe.get(field)
            if new_value in (None, "", [], {}) or old_value in (None, "", [], {}) or new_value == old_value:
                continue
            incoming_rank = confidence_rank(incoming_confidence.get(field, "medium"))
            current_rank = confidence_rank(current_confidence.get(field, "high" if current.get("collection_mode") in {"reviewed_metadata", "operator_import"} else "medium"))
            if incoming_rank < current_rank or (source_mode == "automatic" and incoming_rank < 3):
                accepted[field] = old_value
                if field in current_confidence:
                    accepted_confidence[field] = current_confidence[field]
                review_reasons.append(f"{field}: новое значение не заменило более надёжное")
        accepted["field_confidence"] = accepted_confidence

    accepted["review_required"] = bool(review_reasons)
    accepted["collection_mode"] = source_mode
    upsert_many([accepted])
    if review_reasons:
        review_id = _queue_review(original, current, review_reasons)
    else:
        review_id = None
        with connect() as db:
            db.execute(
                "UPDATE pending_supplier_updates SET status='superseded', reviewed_at=datetime('now') "
                "WHERE supplier_id=? AND status='pending'",
                (accepted["id"],),
            )
            db.commit()
    return {
        "item": accepted,
        "review_required": bool(review_reasons),
        "review_id": review_id,
        "warnings": review_reasons,
    }


def safe_upsert_many(items: list[dict], source_mode: str = "automatic") -> dict:
    results = [safe_upsert(item, source_mode=source_mode) for item in items]
    return {
        "imported": sum(not result.get("archived", False) for result in results),
        "archived_skipped": sum(result.get("archived", False) for result in results),
        "pending_review": sum(result["review_required"] for result in results),
        "results": results,
    }


def list_pending_updates(limit: int = 50) -> list[dict]:
    with connect() as db:
        rows = db.execute(
            """SELECT id, supplier_id, source_url, current_payload, candidate_payload,
            reasons, status, created_at FROM pending_supplier_updates
            WHERE status='pending' ORDER BY id DESC LIMIT ?""",
            (max(1, min(limit, 200)),),
        ).fetchall()
    return [{
        "id": row["id"], "supplier_id": row["supplier_id"], "source_url": row["source_url"],
        "current": json.loads(row["current_payload"]),
        "candidate": json.loads(row["candidate_payload"]),
        "reasons": json.loads(row["reasons"]), "status": row["status"],
        "created_at": row["created_at"],
    } for row in rows]


def resolve_pending_update(review_id: int, approve: bool) -> dict | None:
    with connect() as db:
        row = db.execute(
            "SELECT * FROM pending_supplier_updates WHERE id=? AND status='pending'", (review_id,)
        ).fetchone()
    if not row:
        return None
    if approve:
        candidate = json.loads(row["candidate_payload"])
        candidate["review_required"] = False
        candidate["collection_mode"] = "operator_reviewed"
        upsert_many([candidate])
    status = "approved" if approve else "rejected"
    with connect() as db:
        db.execute(
            "UPDATE pending_supplier_updates SET status=?, reviewed_at=datetime('now') WHERE id=?",
            (status, review_id),
        )
        db.commit()
    return {"id": review_id, "status": status}


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
    return [normalize_supplier_fields(json.loads(row["payload"])) for row in rows]


def delete_fixture_suppliers() -> int:
    """Delete the original portfolio-only records (ids prefixed with sup-)."""
    with connect() as db:
        # Keep wildcard patterns in parameters: psycopg treats a literal ``%``
        # in a query string as placeholder syntax, while SQLite does not.
        result = db.execute("DELETE FROM suppliers WHERE id LIKE ?", ("sup-%",))
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
            "SELECT id, payload FROM suppliers WHERE id LIKE ?", ("reviewed-%",)
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
                    """INSERT INTO supplier_states
                    (supplier_id, favourite, status, note, updated_at) VALUES(?,?,?,?,?)
                    ON CONFLICT(supplier_id) DO NOTHING""",
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
    return normalize_supplier_fields(json.loads(row["payload"])) if row else None


def archive_supplier(supplier_id: str) -> dict | None:
    """Move a supplier out of the catalogue while keeping it recoverable."""
    with connect() as db:
        row = db.execute("SELECT payload FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
        if not row:
            return None
        state_row = db.execute(
            "SELECT favourite, status, note, updated_at FROM supplier_states WHERE supplier_id = ?",
            (supplier_id,),
        ).fetchone()
        state = dict(state_row) if state_row else {}
        db.execute(
            """INSERT INTO archived_suppliers(id, payload, state_payload, archived_at)
            VALUES(?,?,?,datetime('now'))
            ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,
            state_payload=excluded.state_payload, archived_at=excluded.archived_at""",
            (supplier_id, row["payload"], json.dumps(state, ensure_ascii=False)),
        )
        db.execute("DELETE FROM supplier_states WHERE supplier_id = ?", (supplier_id,))
        db.execute("DELETE FROM pending_supplier_updates WHERE supplier_id = ?", (supplier_id,))
        db.execute("DELETE FROM suppliers WHERE id = ?", (supplier_id,))
        archived_at = db.execute(
            "SELECT archived_at FROM archived_suppliers WHERE id = ?", (supplier_id,)
        ).fetchone()["archived_at"]
        db.commit()
    item = normalize_supplier_fields(json.loads(row["payload"]))
    return {"item": item, "archived_at": archived_at, "can_delete": item.get("collection_mode") in {"manual_parse", "operator_import"}}


def list_archived_suppliers() -> list[dict]:
    with connect() as db:
        rows = db.execute(
            "SELECT id, payload, archived_at FROM archived_suppliers ORDER BY archived_at DESC"
        ).fetchall()
    result = []
    for row in rows:
        item = normalize_supplier_fields(json.loads(row["payload"]))
        result.append({
            "item": item,
            "archived_at": row["archived_at"],
            "can_delete": item.get("collection_mode") in {"manual_parse", "operator_import"},
        })
    return result


def restore_archived_supplier(supplier_id: str) -> dict | None:
    with connect() as db:
        row = db.execute(
            "SELECT payload, state_payload FROM archived_suppliers WHERE id = ?", (supplier_id,)
        ).fetchone()
        if not row:
            return None
        item = normalize_supplier_fields(json.loads(row["payload"]))
        db.execute(
            """INSERT INTO suppliers(id,name,payload,updated_at)
            VALUES(?,?,?,datetime('now'))
            ON CONFLICT(id) DO UPDATE SET name=excluded.name,
            payload=excluded.payload, updated_at=excluded.updated_at""",
            (item["id"], item["name"], json.dumps(item, ensure_ascii=False)),
        )
        state = json.loads(row["state_payload"] or "{}")
        if state:
            db.execute(
                """INSERT INTO supplier_states
                (supplier_id, favourite, status, note, updated_at) VALUES(?,?,?,?,?)
                ON CONFLICT(supplier_id) DO UPDATE SET favourite=excluded.favourite,
                status=excluded.status, note=excluded.note, updated_at=excluded.updated_at""",
                (
                    supplier_id, int(bool(state.get("favourite"))), state.get("status", "new"),
                    state.get("note", ""), state.get("updated_at") or date.today().isoformat(),
                ),
            )
        db.execute("DELETE FROM archived_suppliers WHERE id = ?", (supplier_id,))
        db.commit()
    return item


def delete_archived_supplier(supplier_id: str) -> dict | None:
    """Permanently delete only cards created by a manual parse or operator import."""
    with connect() as db:
        row = db.execute("SELECT payload FROM archived_suppliers WHERE id = ?", (supplier_id,)).fetchone()
        if not row:
            return None
        item = normalize_supplier_fields(json.loads(row["payload"]))
        if item.get("collection_mode") not in {"manual_parse", "operator_import"}:
            raise ValueError("Окончательно удалить можно только вручную добавленную карточку")
        db.execute("DELETE FROM archived_suppliers WHERE id = ?", (supplier_id,))
        db.commit()
    return item


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
