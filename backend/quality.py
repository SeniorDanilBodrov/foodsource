"""Validation and confidence rules for automatically collected supplier data."""
from __future__ import annotations

import re
from datetime import datetime, timezone


PHONE_RE = re.compile(r"^\+?\d[\d\s()\-]{8,}\d$")
PHONE_IN_TEXT_RE = re.compile(r"\+?\d[\d\s()\-]{8,}\d")
EMAIL_RE = re.compile(r"^[\w.+-]+@[\w.-]+\.[a-z]{2,}$", re.I)
ADDRESS_RE = re.compile(
    r"(?:^|[\s,])(?:ул(?:ица)?\.?|проспект|пр-т|шоссе|переулок|пер\.|проезд|тракт|"
    r"набережная|наб\.|площадь|пл\.|бульвар|бул\.|дом|д\.|строение|стр\.|корпус|корп\.)(?=\s|,|\d|$)",
    re.I,
)
PRICE_RE = re.compile(r"(?:₽|руб(?:\.|лей)?|\d+[\s,.]*\d*\s*/\s*(?:шт|кг|г|л|мл))", re.I)
NOISY_DESCRIPTION_RE = re.compile(
    r"(?:cookie|каталог каталог|контакты контакты|новости новости|войти зарегистрироваться|личный кабинет корзина)",
    re.I,
)

UNKNOWN_VALUES = {
    "", "не указано", "по запросу", "уточнить", "уточнить у поставщика", "не найдено",
}


def _known(value: object) -> bool:
    if isinstance(value, (list, tuple, set)):
        return bool(value)
    return str(value or "").strip().lower() not in UNKNOWN_VALUES


def calculate_completeness(item: dict) -> int:
    """Calculate one deterministic completeness score for API and imported data.

    The score is derived from the currently available fields, so old values saved
    in SQLite cannot make every card display the same historical percentage.
    """
    checks = (
        (10, _known(item.get("name"))),
        (10, _known(item.get("description"))),
        (10, _known(item.get("categories"))),
        (15, _known(item.get("products"))),
        (10, _known(item.get("regions"))),
        (15, any(_known(item.get(field)) for field in ("phone", "email", "contact"))),
        (5, _known(item.get("address"))),
        (5, _known(item.get("minimumOrder"))),
        (5, _known(item.get("priceRange"))),
        (10, _known(item.get("delivery"))),
        (5, _known(item.get("certificates"))),
    )
    return sum(weight for weight, present in checks if present)


def confidence_rank(value: str) -> int:
    return {"low": 1, "medium": 2, "high": 3}.get(value, 1)


def assess_supplier(item: dict) -> tuple[dict, dict]:
    """Return a sanitized card and a transparent field-level quality report."""
    safe = dict(item)
    confidence = dict(item.get("field_confidence") or {})
    warnings: list[dict] = []

    def reject(field: str, reason: str) -> None:
        value = safe.get(field)
        if value not in (None, "", [], {}):
            warnings.append({"field": field, "reason": reason, "value": str(value)[:180]})
        safe[field] = [] if isinstance(value, list) else ""
        confidence[field] = "low"

    name = " ".join(str(safe.get("name", "")).split())
    if len(name) < 3 or name.startswith(("http://", "https://")):
        warnings.append({"field": "name", "reason": "название не удалось надёжно определить", "value": name[:180]})
        confidence["name"] = "low"

    phone = " ".join(str(safe.get("phone", "")).split())
    if phone and not PHONE_RE.fullmatch(phone):
        reject("phone", "значение не похоже на телефон")
    elif phone:
        confidence.setdefault("phone", "high")

    email = str(safe.get("email", "")).strip()
    if email and not EMAIL_RE.fullmatch(email):
        reject("email", "некорректный формат email")
    elif email:
        confidence.setdefault("email", "high")

    address = " ".join(str(safe.get("address", "")).split())
    # Contact blocks often put a phone immediately after the postal address.
    # Keep the address portion instead of rejecting the entire concatenated line.
    phone_suffix = PHONE_IN_TEXT_RE.search(address)
    if phone_suffix:
        address = address[:phone_suffix.start()].rstrip(" ,;—-")
    if address and (PRICE_RE.search(address) or not ADDRESS_RE.search(address)):
        reject("address", "значение не похоже на почтовый адрес")
    elif address:
        safe["address"] = address
        confidence.setdefault("address", "high")

    description = " ".join(str(safe.get("description", "")).split())
    if description and (NOISY_DESCRIPTION_RE.search(description) or len(description) > 500):
        reject("description", "описание содержит навигационный или технический текст")
    elif description:
        confidence.setdefault("description", "medium")

    for field in ("categories", "products", "regions"):
        if safe.get(field):
            confidence.setdefault(field, "medium")
    for field in ("name", "contact", "delivery", "minimumOrder", "priceRange"):
        if safe.get(field):
            confidence.setdefault(field, "medium")

    safe["phone"] = safe.get("phone", "")
    safe["email"] = safe.get("email", "")
    safe["address"] = safe.get("address", "")
    safe["contact"] = " · ".join(value for value in (safe["phone"], safe["email"]) if value) or safe.get("contact", "")
    safe["field_confidence"] = confidence
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "review" if warnings else "accepted",
        "warnings": warnings,
        "field_confidence": confidence,
    }
    safe["data_quality"] = report
    return safe, report
