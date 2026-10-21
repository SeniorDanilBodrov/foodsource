"""Explainable hybrid search for FoodSource MVP.

Business filters and exact signals remain deterministic.  When the optional
local rubert-tiny2 model is available, semantic similarity expands and reranks
the safe candidate set; it never creates supplier records or replaces filters.
"""
import re

from .query_understanding import analyze_query, normalized_text
from .semantic import semantic_engine

CATEGORY_RULES = [
    # The order expresses intent.  "Упаковка для персиков" is a packaging
    # request, while "персики" alone is a produce request.
    ("Упаковка", r"упаков|контейнер|короб|бокс|пакет|тара|лоток|пл[её]нк|бутыл|крышк|стакан|этикет|однораз|посуд|прибор"),
    ("Ингредиенты для выпечки", r"выпеч|мук|шоколад|кондитер|ингредиент|сироп|начинк|топпинг|пюре|концентрат|ароматизатор|дрожж"),
    ("Молочная продукция", r"молок|молоч|сыр|сливк|йогурт|творог|кефир|сметан|масло сливоч"),
    ("Бакалея", r"бакале|круп|кофе|чай|масл|спец|макарон|консерв|соус|напит|сок|сахар|соль"),
    ("Мясо и птица", r"мяс|куриц|курин|птиц|говядин|свинин|индейк|утк|кролик|баран|колбас|фарш"),
    ("Рыба и морепродукты", r"рыб|лосос|форел|с[её]мг|морепродукт|кревет|икр|сельд|тунец|кальмар|мид"),
    ("Овощи и фрукты", r"овощ|фрукт|яблок|груш|банан|цитрус|апельсин|мандарин|лимон|лайм|персик|нектарин|абрикос|слив|виноград|киви|ананас|манго|авокадо|гранат|хурм|фейхоа|ягод|клубник|малин|черник|смородин|арбуз|дын|зелень|картоф|томат|помидор|огурц|капуст|морков|св[её]кл|лук|чеснок|перец|баклаж|кабач|тыкв|редис|гриб"),
    ("Продукты для HoReCa", r"horeca|ресторан|кафе|общепит|столов|готов\w*\s+продук"),
]
PRODUCT_SYNONYMS = [
    (r"яблок", ["яблок"]),
    (r"груш", ["груш"]),
    (r"банан", ["банан"]),
    (r"персик", ["персик"]),
    (r"нектарин", ["нектарин"]),
    (r"абрикос", ["абрикос"]),
    (r"апельсин", ["апельсин"]),
    (r"мандарин", ["мандарин"]),
    (r"цитрус", ["цитрус"]),
    (r"манго", ["манго"]),
    (r"авокадо", ["авокадо"]),
    (r"ананас", ["ананас"]),
    (r"киви", ["киви"]),
    (r"гранат", ["гранат"]),
    (r"хурм", ["хурм"]),
    (r"фейхоа", ["фейхоа"]),
    (r"ягод", ["ягод"]),
    (r"клубник", ["клубник"]),
    (r"малин", ["малин"]),
    (r"черник", ["черник"]),
    (r"смородин", ["смородин"]),
    # Frozen convenience foods are often described by suppliers only as
    # "полуфабрикаты".  Cover common dishes and the frequent "варенники" typo.
    (r"пельмен|варен+ик|мант(?:ы|ов)|хинкал", ["полуфабрикат"]),
    (r"курин|куриц", ["курин", "куриц"]),
    (r"индейк", ["индейк"]),
    (r"утк", ["утк"]),
    (r"говядин", ["говядин"]),
    (r"свинин", ["свинин"]),
    (r"лосос", ["лосос"]),
    (r"с[её]мг", ["семг", "сёмг"]),
    (r"форел", ["форел"]),
    (r"сыр", ["сыр"]),
    (r"творог", ["творог"]),
    (r"кефир", ["кефир"]),
    (r"йогурт", ["йогурт"]),
    (r"мук", ["мук"]),
    (r"шоколад", ["шоколад"]),
    (r"сироп", ["сироп"]),
    (r"начинк", ["начинк"]),
    (r"пюре", ["пюре"]),
    (r"топпинг", ["топпинг"]),
    (r"концентрат", ["концентрат"]),
    (r"ароматизатор", ["ароматизатор"]),
    (r"дрожж", ["дрожж"]),
    (r"упаков", ["упаков"]),
    (r"контейнер", ["контейнер"]),
    (r"короб|бокс", ["короб", "бокс"]),
    (r"пакет", ["пакет"]),
    (r"лоток", ["лоток"]),
    (r"стакан", ["стакан"]),
    (r"посуд", ["посуд"]),
    (r"бутыл", ["бутыл"]),
    (r"крышк", ["крышк"]),
    (r"пл[её]нк", ["пленк", "плёнк"]),
    (r"этикет", ["этикет"]),
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
SEMANTIC_FLOOR = 0.80
SEMANTIC_TOP_WINDOW = 0.025


def is_known(value: object) -> bool:
    if isinstance(value, list):
        return bool(value)
    return str(value or "").strip().lower() not in UNKNOWN


def normalize_query(text: str) -> dict:
    text = text.strip()
    understanding = analyze_query(text)
    analysis_text = " ".join([
        understanding["corrected_text"], *understanding["lemmas"]
    ])
    category = understanding["category"] or next(
        (name for name, pattern in CATEGORY_RULES if re.search(pattern, analysis_text, re.I)), ""
    )
    region = next((name for name, pattern in REGION_RULES if re.search(pattern, analysis_text, re.I)), "")
    minimum = re.search(r"(?:от|минимум)\s*(\d+)\s*(кг|шт|руб|₽)", text, re.I)
    lexical_terms = understanding["lexical_terms"]
    product_terms = list(understanding["product_terms"])
    for pattern, terms in PRODUCT_SYNONYMS:
        if re.search(pattern, analysis_text, re.I):
            product_terms.extend(terms)
    if category and not product_terms:
        product_terms = lexical_terms
    excludes_delivery = bool(re.search(r"\bбез\s+достав", analysis_text, re.I))
    return {
        "text": text,
        "corrected_text": understanding["corrected_text"],
        "category": category,
        "region": region,
        "delivery": bool(re.search(r"достав|привез|логист", analysis_text, re.I)) and not excludes_delivery,
        "excludes_delivery": excludes_delivery,
        "min_order": f"{minimum.group(1)} {minimum.group(2)}" if minimum else "",
        "product_terms": list(dict.fromkeys(product_terms)),
        "lexical_terms": lexical_terms,
        "concepts": understanding["concepts"],
        "corrections": understanding["corrections"],
    }


def supplier_matches_category(supplier: dict, category: str) -> bool:
    if not category:
        return False
    if category in supplier.get("categories", []):
        return True
    pattern = next((rule for name, rule in CATEGORY_RULES if name == category), "")
    category_text = " ".join(supplier.get("categories", [])).lower()
    if pattern and re.search(pattern, category_text, re.I):
        return True
    # If a card already has a concrete category, product phrases from another
    # domain must not override it (for example "fruit filling" is not a fruit
    # wholesaler, and "packaging for meat" is not a meat supplier).
    classified_categories = {
        name for name, rule in CATEGORY_RULES if re.search(rule, category_text, re.I)
    }
    if classified_categories and category not in classified_categories:
        return False
    return bool(pattern and re.search(pattern, supplier_product_haystack(supplier), re.I))


def supplier_declares_category(supplier: dict, category: str) -> bool:
    """True when the card category itself confirms the requested domain."""
    if not category:
        return False
    category_text = " ".join(supplier.get("categories", [])).lower()
    if category in supplier.get("categories", []):
        return True
    pattern = next((rule for name, rule in CATEGORY_RULES if name == category), "")
    return bool(pattern and re.search(pattern, category_text, re.I))


def semantic_query(normalized: dict) -> str:
    """Keep product intent, removing constraints already handled by Python."""
    text = (normalized.get("corrected_text") or normalized["text"]).lower()
    for _, pattern in REGION_RULES:
        text = re.sub(pattern, " ", text, flags=re.I)
    text = re.sub(r"(?:от|минимум)\s*\d+\s*(?:кг|шт|руб|₽)", " ", text, flags=re.I)
    text = re.sub(r"\b(?:с\s+доставкой|доставка|доставить|привезти|логистика)\b", " ", text, flags=re.I)
    text = re.sub(r"\b(?:нужен|нужна|нужны|нужно|ищу|найди|поставщик|поставщики|оптом|закупка)\b", " ", text, flags=re.I)
    text = " ".join(text.split()).strip(" ,.-")
    return text or normalized.get("category", "")


def semantic_percent(similarity: float) -> int:
    """Calibrate cosine similarity to a readable 0-99 relevance component."""
    return min(99, max(0, round((similarity - 0.55) / 0.40 * 100)))


def score_supplier(
    supplier: dict, normalized: dict, similarity: float | None = None
) -> tuple[int, list[str], dict]:
    # With an empty search box the user is browsing a catalogue, not asking for
    # a relevance calculation. Returning zero lets the interface omit the
    # percentage instead of showing a misleading base score.
    if not normalized["text"]:
        return 0, [], {"business": 0, "semantic": 0, "semantic_raw": None}
    score, reasons = 0, []
    name_terms = normalized.get("lexical_terms", [])
    supplier_name = normalized_text(supplier.get("name", ""))
    if name_terms and all(term in supplier_name for term in name_terms):
        # A direct supplier-name lookup must stay above a semantically similar
        # company (for example "Лента PRO" above "Концерн Протэк").
        score += 60
        reasons.append("название поставщика")
    category_match = bool(
        normalized["category"]
        and supplier_matches_category(supplier, normalized["category"])
    )
    if category_match:
        score += 35; reasons.append("категория")
    if normalized["region"] and normalized["region"] in supplier.get("regions", []):
        score += 25; reasons.append("регион")
    if normalized["delivery"] and is_known(supplier.get("delivery")):
        score += 15; reasons.append("доставка")
    if normalized["min_order"] and is_known(supplier.get("minimumOrder")):
        score += 10; reasons.append("минимальная партия указана")
    terms = normalized.get("product_terms") or name_terms
    haystack = supplier_haystack(supplier)
    structured = supplier_product_haystack(supplier)
    structured_matches = sum(term in structured for term in terms)
    if structured_matches:
        # A match in products/categories is stronger than a word found in
        # noisy website text (navigation, news, SEO snippets).
        score += min(20, 15 + 5 * (structured_matches - 1)); reasons.append("совпадение по товару")
    elif any(term in haystack for term in terms):
        score += 5; reasons.append("упоминание в описании")
    elif normalized.get("product_terms") and category_match:
        # The supplier works with the requested category, but the exact SKU is
        # not listed in public structured data.  Keep the lead and say so.
        reasons.append("точный товар уточнить")
    # Completeness can be a small tie-breaker only after an actual match. It is
    # not a signal of similarity to the user's request on its own.
    if score and supplier.get("completeness", 0) >= 85:
        score += 5; reasons.append("полные данные")
    if normalized.get("corrections") and score:
        reasons.append("опечатка исправлена")
    business_score = min(99, score)
    semantic_score = semantic_percent(similarity) if similarity is not None else 0
    if semantic_score:
        # Exact/business evidence remains the main part of the score.  A purely
        # semantic candidate receives a deliberately lower confidence.
        score = (
            round(0.60 * business_score + 0.40 * semantic_score)
            if business_score
            else round(0.75 * semantic_score)
        )
        if similarity >= SEMANTIC_FLOOR:
            reasons.append("смысл запроса")
    return min(99, score), reasons[:4], {
        "business": business_score,
        "semantic": semantic_score,
        "semantic_raw": round(similarity, 4) if similarity is not None else None,
    }


def supplier_haystack(supplier: dict) -> str:
    return normalized_text(" ".join([supplier.get("name", ""), supplier.get("description", ""), *supplier.get("categories", []), *supplier.get("products", [])]))


def supplier_product_haystack(supplier: dict) -> str:
    """Structured product fields only; descriptions often contain navigation noise."""
    return normalized_text(" ".join([supplier.get("name", ""), *supplier.get("categories", []), *supplier.get("products", [])]))


def missing_fields(supplier: dict) -> list[str]:
    return [label for field, label in REQUIRED_FIELDS.items() if not is_known(supplier.get(field))]


def search(items: list[dict], query: str, filters: dict) -> tuple[dict, list[dict]]:
    normalized = normalize_query(query)
    candidates = []
    for supplier in items:
        if filters.get("category") and filters["category"] not in supplier.get("categories", []): continue
        if filters.get("region") and filters["region"] not in supplier.get("regions", []): continue
        if filters.get("supplier_type") and filters["supplier_type"] != supplier.get("type"): continue
        if filters.get("has_delivery") and not is_known(supplier.get("delivery")): continue
        if filters.get("has_certificates") and not is_known(supplier.get("certificates")): continue
        if filters.get("has_minimum_order") and not is_known(supplier.get("minimumOrder")): continue
        if filters.get("has_contact") and not is_known(supplier.get("contact")): continue
        candidates.append(supplier)

    # Never let embeddings jump across an explicit business category.  This is
    # the central guard against results such as packaging for a fruit query.
    semantic_pool = [
        supplier for supplier in candidates
        if not normalized.get("category")
        or supplier_matches_category(supplier, normalized["category"])
    ]
    search_terms = normalized.get("product_terms") or normalized.get("lexical_terms", [])
    has_exact_candidate = any(
        any(term in supplier_product_haystack(supplier) for term in search_terms)
        for supplier in semantic_pool
    )
    # A tiny embedding model is useful for reranking a known neighbourhood, but
    # is not a safe ontology on its own.  Unknown, unsupported wording returns
    # no result instead of inventing a category from a high cosine score.
    allow_semantic_expansion = bool(normalized.get("category") or has_exact_candidate)
    semantic_scores = (
        semantic_engine.score(semantic_query(normalized), semantic_pool)
        if normalized["text"] and allow_semantic_expansion else {}
    )
    semantic_top = max(semantic_scores.values(), default=0.0)
    semantic_leaders = {
        supplier_id
        for supplier_id, similarity in sorted(semantic_scores.items(), key=lambda item: item[1], reverse=True)[:3]
        if similarity >= SEMANTIC_FLOOR and similarity >= semantic_top - SEMANTIC_TOP_WINDOW
    }
    top_semantic_id = max(semantic_scores, key=semantic_scores.get) if semantic_scores else ""
    top_semantic_supplier = next(
        (item for item in candidates if str(item.get("id", "")) == top_semantic_id), {}
    )
    top_semantic_categories = set(top_semantic_supplier.get("categories", []))
    result = []
    for supplier in candidates:
        structured = supplier_product_haystack(supplier)
        exact_product = any(term in structured for term in search_terms)
        similarity = semantic_scores.get(str(supplier.get("id", "")))
        semantic_candidate = str(supplier.get("id", "")) in semantic_leaders
        if semantic_candidate and not normalized.get("category") and top_semantic_categories:
            semantic_candidate = bool(top_semantic_categories.intersection(supplier.get("categories", [])))
        # A recognized category is a safe boundary for semantic reranking.  It
        # prevents a fluent but unrelated description from entering the result.
        category_match = bool(
            normalized.get("category")
            and supplier_matches_category(supplier, normalized["category"])
        )
        declared_category = bool(
            normalized.get("category")
            and supplier_declares_category(supplier, normalized["category"])
        )
        if normalized.get("category") and not category_match:
            continue
        # A concrete product still requires structured or strong semantic
        # evidence; matching only the city/delivery is never enough.
        if normalized.get("product_terms") and not exact_product:
            # For a concrete SKU ("персики", "форель") a generic HoReCa card
            # is too weak.  Category fallbacks are allowed only when the card
            # itself declares the domain; otherwise we need an exact product.
            if not declared_category:
                continue
        score, reasons, breakdown = score_supplier(supplier, normalized, similarity)
        # A non-empty query is a search, not a catalogue browse: hide cards
        # without any query signal so the count stays truthful.
        if normalized["text"] and score == 0 and not semantic_candidate:
            continue
        # For unknown phrasing semantic retrieval is limited to the strongest
        # neighbourhood rather than returning the whole catalogue.
        if normalized["text"] and not normalized.get("category") and not exact_product and not semantic_candidate:
            continue
        result.append({
            **supplier,
            "score": score,
            "reasons": reasons,
            "score_breakdown": breakdown,
            "missing_fields": missing_fields(supplier),
        })
    sort = filters.get("sort", "match")
    if sort == "completeness": result.sort(key=lambda item: item.get("completeness", 0), reverse=True)
    elif sort == "name": result.sort(key=lambda item: item.get("name", "").lower())
    elif sort == "region": result.sort(key=lambda item: item.get("city", "").lower())
    elif sort == "contact": result.sort(key=lambda item: not is_known(item.get("contact")))
    else: result.sort(key=lambda item: item["score"], reverse=True)
    normalized["semantic_query"] = semantic_query(normalized) if normalized["text"] else ""
    normalized["search_mode"] = (
        semantic_engine.status()["mode"] if normalized["text"] else "deterministic"
    )
    return normalized, result
