"""Validation and confidence rules for automatically collected supplier data."""
from __future__ import annotations

import re
from datetime import datetime, timezone


PHONE_RE = re.compile(r"^\+?\d[\d\s()\-]{8,}\d$")
EMAIL_RE = re.compile(r"^[\w.+-]+@[\w.-]+\.[a-z]{2,}$", re.I)
ADDRESS_RE = re.compile(
    r"\b(?:ул(?:ица)?|проспект|пр-т|шоссе|переулок|пер\.|проезд|тракт|"
    r"набережная|наб\.|площадь|пл\.|бульвар|бул\.|дом|д\.|строение|стр\.|корпус|корп\.)\b",
    re.I,
)
PRICE_RE = re.compile(r"(?:₽|руб(?:\.|лей)?|\d+[\s,.]*\d*\s*/\s*(?:шт|кг|г|л|мл))", re.I)
NOISY_DESCRIPTION_RE = re.compile(
    r"(?:cookie|каталог каталог|контакты контакты|новости новости|войти зарегистрироваться|личный кабинет корзина)",
    re.I,
)


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
    if address and (PRICE_RE.search(address) or not ADDRESS_RE.search(address)):
        reject("address", "значение не похоже на почтовый адрес")
    elif address:
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

