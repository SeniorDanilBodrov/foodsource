"""A conservative parser for public supplier pages.

It extracts visible metadata and always keeps the source URL. The parser does not
try to bypass robots.txt, login walls, CAPTCHAs or anti-bot protection.
"""
import asyncio
import ipaddress
import re
import socket
from datetime import date
from hashlib import sha1
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

USER_AGENT = "FoodSourceResearchBot/0.1 (+portfolio demo; respectful public-page collector)"


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


async def parse_supplier(url: str, category: str | None = None, region: str | None = None) -> dict:
    await ensure_public_url(url)
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        if not await robots_allowed(client, url):
            raise ValueError("robots.txt запрещает автоматический сбор с этой страницы")
        response = await client.get(url)
        response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    text = " ".join(soup.stripped_strings)
    title = soup.title.get_text(" ", strip=True) if soup.title else "Поставщик без названия"
    name = _first(text, [r"(?:название|компания|бренд)\s*[:—-]\s*([^|.;]{3,100})"], title)
    description = _first(text, [r"(?:о компании|описание|о нас)\s*[:—-]?\s*(.{40,240})"], text[:240])
    phone = _first(text, [r"(\+?\d[\d\s()\-]{8,}\d)"])
    email = _first(text, [r"([\w.+-]+@[\w.-]+\.[a-z]{2,})"])
    address = _first(text, [r"(?:адрес|офис)\s*[:—-]?\s*([^|.;]{8,180})"])
    products = []
    for heading in soup.find_all(["h1", "h2", "h3", "li"]):
        value = heading.get_text(" ", strip=True)
        if 3 < len(value) < 80 and value.lower() not in {name.lower(), "главная", "контакты"}:
            products.append(value)
    products = list(dict.fromkeys(products))[:12]
    completeness = sum(bool(x) for x in (name, description, phone or email, products, category, region, url))
    return {
        "id": "web-" + sha1(url.encode()).hexdigest()[:10],
        "name": name,
        "type": "Поставщик",
        "categories": [category] if category else [],
        "products": products,
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
        "website": str(response.url),
        "source": str(response.url),
        "updated": date.today().isoformat(),
        "completeness": min(95, 40 + completeness * 7),
    }
