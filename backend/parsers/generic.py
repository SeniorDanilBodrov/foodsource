"""A conservative parser for public supplier pages.

It extracts visible metadata and always keeps the source URL. The parser does not
try to bypass robots.txt, login walls, CAPTCHAs or anti-bot protection.
"""
import asyncio
import ipaddress
import json
import re
import socket
from datetime import date
from hashlib import sha1
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

USER_AGENT = "FoodSourceResearchBot/0.1 (+portfolio demo; respectful public-page collector)"

PRICE_OR_PRODUCT_RE = re.compile(
    r"(?:₽|руб(?:\.|лей)?|\d+[\s,.]*\d*\s*/\s*(?:шт|кг|г|л|мл)|sku|артикул)", re.I
)
ADDRESS_MARKER_RE = re.compile(
    r"(?:\b(?:ул(?:ица)?|проспект|пр-т|шоссе|переулок|пер\.|проезд|тракт|"
    r"набережная|наб\.|площадь|пл\.|бульвар|бул\.)\s+[А-ЯЁа-яё0-9]|"
    r"\b(?:дом|д\.|строение|стр\.|корпус|корп\.)\s*\d)", re.I
)
PRODUCT_HINTS = (
    ("Молочные продукты", r"\b(?:молок|сливк|сыр|йогурт|кефир|творог|морожен)"),
    ("Напитки", r"\b(?:вода|напиток|сок|кофе|чай)"),
    ("Бакалея", r"\b(?:круп|макарон|мук|сахар|консерв|соус|специ)"),
    ("Мясо и птица", r"\b(?:мяс|говядин|свинин|куриц|птиц|колбас|сосиск)"),
    ("Рыба и морепродукты", r"\b(?:рыб|лосос|форел|семг|морепродукт|кревет)"),
    ("Овощи и фрукты", r"\b(?:овощ|фрукт|яблок|груш|банан|томат|огур|картоф|зелень)"),
    ("Упаковка", r"\b(?:упаков|тара|короб|контейнер|пакет|стакан)"),
)


async def ensure_public_url(url: str) -> None:
    """Block SSRF targets before the collector performs a network request."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Нужна публичная ссылка http:// или https://")
    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("Локальные адреса запрещены")
    try:
        addresses = await asyncio.get_running_loop().run_in_executor(None, socket.getaddrinfo, host, None)
        ips = {record[4][0] for record in addresses}
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise ValueError("Адрес не является публичным")
    except socket.gaierror as exc:
        raise ValueError("Не удалось разрешить адрес источника") from exc


async def robots_allowed(client: httpx.AsyncClient, url: str) -> bool:
    """Respect robots.txt. A missing robots.txt means no published restriction."""
    parsed = urlparse(url)
    robots_url = urljoin(f"{parsed.scheme}://{parsed.netloc}", "/robots.txt")
    try:
        response = await client.get(robots_url)
        if response.status_code == 404:
            return True
        if response.status_code >= 400:
            return False
        disallowed = False
        applies = False
        for raw in response.text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                applies = value in {"*", "FoodSourceResearchBot"}
            if applies and key == "disallow" and value and parsed.path.startswith(value):
                disallowed = True
        return not disallowed
    except httpx.HTTPError:
        # Fail closed when the policy cannot be checked.
        return False


def _first(text: str, patterns: list[str], fallback: str = "") -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I | re.S)
        if match:
            return " ".join(match.group(1).split())[:240]
    return fallback


def _meta_content(soup: BeautifulSoup, *keys: str) -> str:
    for key in keys:
        node = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
        if node and node.get("content"):
            return " ".join(node["content"].split())[:240]
    return ""


def _plausible_address(value: str) -> bool:
    candidate = " ".join(value.split()).strip(" —-:;,.")
    if not 8 <= len(candidate) <= 220:
        return False
    if PRICE_OR_PRODUCT_RE.search(candidate):
        return False
    return bool(ADDRESS_MARKER_RE.search(candidate))


def _address_from_json(value) -> str:
    if isinstance(value, dict):
        if value.get("@type") == "PostalAddress":
            parts = [
                value.get("addressLocality"), value.get("streetAddress"),
                value.get("postalCode"), value.get("addressRegion"),
            ]
            candidate = ", ".join(str(part).strip() for part in parts if part)
            if _plausible_address(candidate):
                return candidate
        for nested in value.values():
            found = _address_from_json(nested)
            if found:
                return found
    if isinstance(value, list):
        for nested in value:
            found = _address_from_json(nested)
            if found:
                return found
    return ""


def _extract_address(soup: BeautifulSoup, text: str) -> str:
    # Structured metadata and explicitly marked address blocks are safer than
    # scanning the whole page. The latter confused "Офисы и предприятия" with
    # an address and captured the next product price on b2b.lenta.com.
    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            found = _address_from_json(json.loads(node.string or node.get_text()))
        except (json.JSONDecodeError, TypeError):
            found = ""
        if found:
            return found
    selectors = (
        '[itemprop="address"]', '[class*="address"]', '[id*="address"]',
        '[class*="contacts"]', '[id*="contacts"]',
    )
    for selector in selectors:
        for node in soup.select(selector):
            candidate = " ".join(node.get_text(" ", strip=True).split())[:220]
            if _plausible_address(candidate):
                return candidate
    for match in re.finditer(r"\bадрес\s*[:—-]\s*([^|.;\n]{8,220})", text, flags=re.I):
        candidate = " ".join(match.group(1).split())
        if _plausible_address(candidate):
            return candidate
    return ""


def _infer_categories(text: str) -> list[str]:
    categories = []
    if re.search(r"\bhoreca\b|поставк\w*\s+товар\w*\s+для\s+бизнес", text, re.I):
        categories.append("Продукты для HoReCa")
    for label, pattern in PRODUCT_HINTS:
        if re.search(pattern, text, re.I):
            categories.append(label)
    return list(dict.fromkeys(categories))[:4]


def parse_supplier_document(
    html: str,
    source_url: str,
    category: str | None = None,
    region: str | None = None,
    identity_url: str | None = None,
) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    address = _extract_address(soup, " ".join(soup.stripped_strings))
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    text = " ".join(soup.stripped_strings)
    title = soup.title.get_text(" ", strip=True) if soup.title else "Поставщик без названия"
    heading = soup.find("h1")
    meta_title = _meta_content(soup, "og:title")
    name = (
        heading.get_text(" ", strip=True) if heading else ""
    ) or meta_title or title
    name = " ".join(name.split())[:120]
    meta_description = _meta_content(soup, "description", "og:description")
    description = meta_description
    if not description:
        description = _first(text, [r"(?:о компании|описание|о нас)\s*[:—-]?\s*(.{40,240})"])
    if not description:
        description = next((" ".join(p.get_text(" ", strip=True).split())[:240] for p in soup.find_all("p") if 40 <= len(p.get_text(" ", strip=True)) <= 320), text[:240])
    phone = _first(text, [r"(\+?\d[\d\s()\-]{8,}\d)"])
    email = _first(text, [r"([\w.+-]+@[\w.-]+\.[a-z]{2,})"])
    inferred_categories = [category] if category else _infer_categories(text)
    products = [label for label, pattern in PRODUCT_HINTS if re.search(pattern, text, re.I)]
    field_confidence = {
        "name": "high" if heading or meta_title else "medium",
        "description": "high" if meta_description else "medium",
        "categories": "high" if category else ("medium" if inferred_categories else "low"),
        "products": "medium" if products else "low",
        "regions": "high" if region else "low",
        "phone": "high" if phone else "low",
        "email": "high" if email else "low",
        "address": "high" if address else "low",
    }
    completeness = sum(bool(x) for x in (name, description, phone or email, products, inferred_categories, region, source_url))
    stable_url = identity_url or source_url
    return {
        "id": "web-" + sha1(stable_url.encode()).hexdigest()[:10],
        "name": name,
        "type": "Поставщик",
        "categories": inferred_categories,
        "products": products[:12],
        "regions": [region] if region else [],
        "city": region or "Не указано",
        "description": description,
        "minimumOrder": "Не указано",
        "priceRange": "По запросу",
        "delivery": "Уточнить у поставщика",
        "certificates": [],
        "contact": " · ".join(x for x in (phone, email) if x) or "Не найдено",
        "phone": phone,
        "email": email,
        "address": address,
        "website": source_url,
        "source": source_url,
        "updated": date.today().isoformat(),
        "completeness": min(95, 40 + completeness * 7),
        "collection_mode": "automatic",
        "field_confidence": field_confidence,
    }


async def parse_supplier(url: str, category: str | None = None, region: str | None = None) -> dict:
    await ensure_public_url(url)
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        if not await robots_allowed(client, url):
            raise ValueError("robots.txt запрещает автоматический сбор с этой страницы")
        response = None
        for attempt in range(3):
            try:
                response = await client.get(url)
                response.raise_for_status()
                break
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
                if attempt == 2:
                    raise
                await asyncio.sleep(1.5 * (attempt + 1))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500 or attempt == 2:
                    raise
                await asyncio.sleep(1.5 * (attempt + 1))
        if response is None:
            raise RuntimeError("Источник не вернул ответ")
    return parse_supplier_document(
        response.text,
        str(response.url),
        category=category,
        region=region,
        identity_url=url,
    )
