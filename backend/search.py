"""Deterministic, explainable search for FoodSource MVP."""
import re

CATEGORY_RULES = [
    ("Мясо и птица", r"мяс|куриц|курин|птиц|говядин|свинин|мясн|индейк"),
    ("Овощи и фрукты", r"овощ|фрукт|яблок|груш|банан|цитрус|зелень|картоф|томат|огурц"),
    ("Упаковка", r"упаков|контейнер|короб|пакет|тара|пл[её]нк|стакан|этикет|однораз"),
    ("Молочная продукция", r"молок|сыр|сливк|йогурт|творог|кефир|сметан"),
    ("Рыба и морепродукты", r"рыб|лосос|форел|семг|морепродукт|кревет|икр"),
    ("Ингредиенты для выпечки", r"выпеч|мук|шоколад|кондитер|ингредиент|сироп|кофе|чай"),
    ("Бакалея", r"бакале|круп|кофе|чай|масл|спец"),
]
PRODUCT_SYNONYMS = [
    (r"яблок", ["яблок", "фрукт"]),
    (r"груш", ["груш", "фрукт"]),
    (r"банан", ["банан", "фрукт"]),
    (r"курин|куриц|индейк", ["курин", "куриц", "птиц", "мяс"]),
    (r"говядин", ["говядин", "мяс"]),
    (r"свинин", ["свинин", "мяс"]),
    (r"лосос|семг|форел", ["лосос", "семг", "рыб"]),
    (r"сыр|творог|кефир|йогурт", ["сыр", "молоч"]),
    (r"мук|шоколад|сироп", ["мук", "шоколад", "ингредиент"]),
    (r"упаков|контейнер|короб|пакет|стакан", ["упаков", "контейнер", "короб", "пакет", "тара"]),
    (r"помидор|томат", ["помидор", "томат", "овощ"]),
    (r"огурц", ["огурц", "овощ"]),
]
REGION_RULES = [
    ("Екатеринбург", r"екатеринбург|екб|свердловск"),
    ("Москва", r"москв"),
    ("Санкт-Петербург", r"петербург|спб"),
    ("Казань", r"казан"),
    ("Новосибирск", r"новосибирск"),
]
REQUIRED_FIELDS = {
    "products": "товары", "regions": "регионы", "contact": "контакт",
    "minimumOrder": "минимальный заказ", "priceRange": "цена",
    "delivery": "доставка", "certificates": "документы",
}
UNKNOWN = {"", "не указано", "по запросу", "уточнить", "уточнить у поставщика", "не найдено"}


def is_known(value: object) -> bool:
    if isinstance(value, list):
        return bool(value)
    return str(value or "").strip().lower() not in UNKNOWN


def normalize_query(text: str) -> dict:
    text = text.strip()
    category = next((name for name, pattern in CATEGORY_RULES if re.search(pattern, text, re.I)), "")
    region = next((name for name, pattern in REGION_RULES if re.search(pattern, text, re.I)), "")
    minimum = re.search(r"(?:от|минимум)\s*(\d+)\s*(кг|шт|руб|₽)", text, re.I)
    return {
        "text": text,
        "category": category,
        "region": region,
        "delivery": bool(re.search(r"достав|привез|логист", text, re.I)),
        "min_order": f"{minimum.group(1)} {minimum.group(2)}" if minimum else "",
        "product_terms": next((terms for pattern, terms in PRODUCT_SYNONYMS if re.search(pattern, text, re.I)), []),
    }


def score_supplier(supplier: dict, normalized: dict) -> tuple[int, list[str]]:
    # With an empty search box the user is browsing a catalogue, not asking for
    # a relevance calculation. Returning zero lets the interface omit the
    # percentage instead of showing a misleading base score.
    if not normalized["text"]:
        return 0, []
    score, reasons = 0, []
    if normalized["category"] and normalized["category"] in supplier.get("categories", []):
        score += 35; reasons.append("категория")
    if normalized["region"] and normalized["region"] in supplier.get("regions", []):
        score += 25; reasons.append("регион")
    if normalized["delivery"] and is_known(supplier.get("delivery")):
        score += 15; reasons.append("доставка")
    if normalized["min_order"] and is_known(supplier.get("minimumOrder")):
        score += 10; reasons.append("минимальная партия указана")
    terms = normalized.get("product_terms") or [term for term in normalized["text"].lower().split() if len(term) > 3]
    haystack = supplier_haystack(supplier)
    structured = supplier_product_haystack(supplier)
    structured_matches = sum(term in structured for term in terms)
    if structured_matches:
        # A match in products/categories is stronger than a word found in
        # noisy website text (navigation, news, SEO snippets).
        score += min(20, 15 + 5 * (structured_matches - 1)); reasons.append("совпадение по товару")
    elif any(term in haystack for term in terms):
        score += 5; reasons.append("упоминание в описании")
    # Completeness can be a small tie-breaker only after an actual match. It is
    # not a signal of similarity to the user's request on its own.
    if score and supplier.get("completeness", 0) >= 85:
        score += 5; reasons.append("полные данные")
    return min(99, score), reasons[:3]


def supplier_haystack(supplier: dict) -> str:
    return " ".join([supplier.get("name", ""), supplier.get("description", ""), *supplier.get("categories", []), *supplier.get("products", [])]).lower()


def supplier_product_haystack(supplier: dict) -> str:
    """Structured product fields only; descriptions often contain navigation noise."""
    return " ".join([supplier.get("name", ""), *supplier.get("categories", []), *supplier.get("products", [])]).lower()


def missing_fields(supplier: dict) -> list[str]:
    return [label for field, label in REQUIRED_FIELDS.items() if not is_known(supplier.get(field))]


def search(items: list[dict], query: str, filters: dict) -> tuple[dict, list[dict]]:
    normalized = normalize_query(query)
    result = []
    for supplier in items:
        if filters.get("category") and filters["category"] not in supplier.get("categories", []): continue
        if filters.get("region") and filters["region"] not in supplier.get("regions", []): continue
        if filters.get("supplier_type") and filters["supplier_type"] != supplier.get("type"): continue
        if filters.get("has_delivery") and not is_known(supplier.get("delivery")): continue
        if filters.get("has_certificates") and not is_known(supplier.get("certificates")): continue
        if filters.get("has_minimum_order") and not is_known(supplier.get("minimumOrder")): continue
        if filters.get("has_contact") and not is_known(supplier.get("contact")): continue
        # If the query contains a concrete product, a match on only the city
        # or delivery conditions is not enough to call a supplier relevant.
        if normalized.get("product_terms") and not any(term in supplier_product_haystack(supplier) for term in normalized["product_terms"]):
            continue
        score, reasons = score_supplier(supplier, normalized)
        # A non-empty query is a search, not a catalogue browse: hide cards
        # without any query signal so the count stays truthful.
        if normalized["text"] and score == 0:
            continue
        result.append({**supplier, "score": score, "reasons": reasons, "missing_fields": missing_fields(supplier)})
    sort = filters.get("sort", "match")
    if sort == "completeness": result.sort(key=lambda item: item.get("completeness", 0), reverse=True)
    elif sort == "name": result.sort(key=lambda item: item.get("name", "").lower())
    elif sort == "region": result.sort(key=lambda item: item.get("city", "").lower())
    elif sort == "contact": result.sort(key=lambda item: not is_known(item.get("contact")))
    else: result.sort(key=lambda item: item["score"], reverse=True)
    return normalized, result
