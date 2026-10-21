"""Deterministic Russian query understanding for the FoodSource domain.

The module deliberately keeps typo correction and ontology lookup separate from
semantic ranking.  A corrected token may help retrieve a known food concept,
but it can never turn an out-of-domain query into an arbitrary supplier match.
"""
from __future__ import annotations

import re
from functools import lru_cache

from pymorphy3 import MorphAnalyzer
from rapidfuzz import fuzz, process


TOKEN_RE = re.compile(r"[a-zа-яё]+", re.I)
STOPWORDS = {
    "без", "для", "или", "как", "мне", "нам", "нужен", "нужна", "нужны",
    "нужно", "ищу", "найди", "поставщик", "поставщики", "оптом", "закупка",
    "товар", "товары", "продукт", "продукты", "доставка", "город", "регион",
    "екатеринбург", "москва", "петербург", "казань", "новосибирск",
}

# Ordered by intent priority.  The order resolves compound requests such as
# "упаковка для курицы" and "персиковая начинка" before the raw ingredient.
CONCEPTS = (
    {
        "name": "food_packaging", "category": "Упаковка",
        "aliases": ("упаковка", "пищевая упаковка", "контейнер", "коробка", "бокс", "пакет", "тара", "лоток", "пленка", "плёнка", "бутылка", "крышка", "стакан", "этикетка", "одноразовая посуда"),
        "terms": ("упаков", "контейнер", "короб", "бокс", "пакет", "тара", "лоток", "пленк", "плёнк", "бутыл", "крышк", "стакан", "этикет", "посуд"),
    },
    {
        "name": "bakery_ingredients", "category": "Ингредиенты для выпечки",
        "aliases": ("начинка", "сироп", "топпинг", "пюре", "концентрат", "ароматизатор", "дрожжи", "шоколад", "кондитерский ингредиент", "ингредиент для выпечки"),
        "terms": ("начинк", "сироп", "топпинг", "пюре", "концентрат", "ароматизатор", "дрожж", "шоколад", "ингредиент"),
    },
    {
        "name": "dairy", "category": "Молочная продукция",
        "aliases": ("молочка", "молоко", "сыр", "сливки", "йогурт", "творог", "творожок", "кефир", "сметана", "сливочное масло"),
        "terms": ("молоч", "сыр", "сливк", "йогурт", "творог", "кефир", "сметан"),
    },
    {
        "name": "grocery", "category": "Бакалея",
        "aliases": ("бакалея", "крупа", "макароны", "консервы", "консервация", "консервированный", "соус", "напиток", "сок", "сахар", "соль", "растительное масло", "чай", "кофе"),
        "terms": ("бакале", "круп", "макарон", "консерв", "соус", "напит", "сок", "сахар", "соль", "масл", "чай", "кофе"),
    },
    {
        "name": "meat", "category": "Мясо и птица",
        "aliases": ("мясо", "курица", "куриное филе", "птица", "говядина", "свинина", "индейка", "утка", "кролик", "баранина", "колбаса", "сосиска", "сосиски", "фарш"),
        "terms": ("мяс", "курин", "куриц", "птиц", "говядин", "свинин", "индейк", "утк", "кролик", "баран", "колбас", "сосиск", "фарш"),
    },
    {
        "name": "fish", "category": "Рыба и морепродукты",
        "aliases": ("рыба", "лосось", "форель", "семга", "сёмга", "морепродукт", "креветка", "икра", "сельдь", "тунец", "кальмар", "мидия"),
        "terms": ("рыб", "лосос", "форел", "семг", "сёмг", "морепродукт", "кревет", "икр", "сельд", "тунец", "кальмар", "мид"),
    },
    {
        "name": "produce", "category": "Овощи и фрукты",
        "aliases": ("овощ", "фрукт", "яблоко", "груша", "банан", "цитрус", "апельсин", "мандарин", "лимон", "персик", "нектарин", "абрикос", "виноград", "киви", "ананас", "манго", "авокадо", "гранат", "хурма", "ягода", "зелень", "картофель", "помидор", "томат", "огурец", "капуста", "морковь", "свекла", "свёкла", "лук", "чеснок", "перец", "баклажан", "кабачок", "тыква", "гриб"),
        "terms": ("овощ", "фрукт", "яблок", "груш", "банан", "цитрус", "апельсин", "мандарин", "лимон", "персик", "нектарин", "абрикос", "виноград", "киви", "ананас", "манго", "авокадо", "гранат", "хурм", "ягод", "зелен", "картоф", "помидор", "томат", "огур", "капуст", "морков", "свекл", "свёкл", "чеснок", "перец", "баклаж", "кабач", "тыкв", "гриб"),
    },
    {
        "name": "horeca", "category": "Продукты для HoReCa",
        "aliases": ("horeca", "ресторан", "кафе", "общепит", "столовая", "готовая продукция"),
        "terms": ("horeca", "ресторан", "кафе", "общепит", "столов", "готов"),
    },
    {
        # These products cross several catalogue categories, so no hard
        # category is assigned.  Retrieval is anchored to "полуфабрикат".
        "name": "frozen_convenience", "category": "",
        "aliases": ("полуфабрикат", "заморозка", "пельмени", "вареники", "манты", "хинкали", "котлеты", "блины"),
        "terms": ("полуфабрикат",),
    },
)

REGION_ALIASES = {
    "екб": "екатеринбург", "спб": "петербург",
}

_morph = MorphAnalyzer()


@lru_cache(maxsize=4096)
def lemma(token: str) -> str:
    token = token.lower().replace("ё", "е")
    if not re.search(r"[а-я]", token):
        return token
    return _morph.parse(token)[0].normal_form


def _alias_tokens() -> set[str]:
    result = set(REGION_ALIASES) | set(REGION_ALIASES.values())
    for concept in CONCEPTS:
        for alias in concept["aliases"]:
            result.update(TOKEN_RE.findall(alias.lower().replace("ё", "е")))
    return {token for token in result if len(token) >= 4}


DOMAIN_VOCABULARY = tuple(sorted(_alias_tokens()))
DOMAIN_LEMMAS = {lemma(token) for token in DOMAIN_VOCABULARY}


def _correct_token(token: str) -> tuple[str, int]:
    plain = token.lower().replace("ё", "е")
    if len(plain) < 5 or plain in DOMAIN_VOCABULARY or lemma(plain) in DOMAIN_LEMMAS:
        return plain, 100
    match = process.extractOne(plain, DOMAIN_VOCABULARY, scorer=fuzz.ratio, score_cutoff=80)
    if not match:
        return plain, 0
    candidate, score, _ = match
    if abs(len(candidate) - len(plain)) > 2:
        return plain, 0
    return candidate, round(score)


def _contains_alias(text: str, lemmas: set[str], alias: str) -> bool:
    alias_plain = alias.lower().replace("ё", "е")
    if " " in alias_plain:
        return alias_plain in text
    return alias_plain in text or lemma(alias_plain) in lemmas


def analyze_query(text: str) -> dict:
    raw = " ".join(str(text or "").strip().split())
    original_tokens = TOKEN_RE.findall(raw.lower())
    corrected_tokens: list[str] = []
    corrections: list[dict] = []
    for token in original_tokens:
        corrected, score = _correct_token(token)
        corrected_tokens.append(corrected)
        if corrected != token.replace("ё", "е"):
            corrections.append({"from": token, "to": corrected, "score": score})

    corrected_text = " ".join(corrected_tokens)
    lemmas = [lemma(REGION_ALIASES.get(token, token)) for token in corrected_tokens]
    lemma_set = set(lemmas)
    matching_text = f"{corrected_text} {' '.join(lemmas)}"
    matched = [
        (concept, [alias for alias in concept["aliases"] if _contains_alias(matching_text, lemma_set, alias)])
        for concept in CONCEPTS
    ]
    matched = [(concept, aliases) for concept, aliases in matched if aliases]
    concepts = [concept for concept, _ in matched]
    category = next((concept["category"] for concept in concepts if concept["category"]), "")
    product_terms: list[str] = []
    for concept, aliases in matched:
        if not concept["category"]:
            product_terms.extend(concept["terms"])
            continue
        for alias in aliases:
            product_terms.extend(
                lemma(token) for token in TOKEN_RE.findall(alias) if len(token) >= 4
            )
    product_terms = list(dict.fromkeys(product_terms))
    lexical_terms = list(dict.fromkeys(
        token for token in lemmas if len(token) >= 4 and token not in STOPWORDS
    ))
    return {
        "corrected_text": corrected_text,
        "lemmas": lemmas,
        "lexical_terms": lexical_terms,
        "product_terms": product_terms,
        "category": category,
        "concepts": [concept["name"] for concept in concepts],
        "corrections": corrections,
    }


@lru_cache(maxsize=1024)
def normalized_text(text: str) -> str:
    """Return both raw text and lemmas for morphology-aware substring search."""
    raw = " ".join(TOKEN_RE.findall(str(text or "").lower().replace("ё", "е")))
    return f"{raw} {' '.join(lemma(token) for token in raw.split())}".strip()
