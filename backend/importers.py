"""Safe, dependency-free CSV/XLSX importer for operator-supplied suppliers."""
import base64
import binascii
import csv
import io
import json
import re
import zipfile
from datetime import date
from hashlib import sha1
from xml.etree import ElementTree as ET

from .search import missing_fields

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ROWS = 1_000

FIELD_ALIASES = {
    "name": {"поставщик", "название", "name"},
    "type": {"тип", "тип поставщика", "supplier type", "type"},
    "categories": {"категории", "категория", "categories", "category"},
    "products": {"товары", "товар", "products", "product"},
    "city": {"город", "city"},
    "regions": {"регионы", "регион", "regions", "region"},
    "minimumOrder": {"мин заказ", "мин. заказ", "минимальный заказ", "minimum order"},
    "priceRange": {"цена", "диапазон цен", "price", "price range"},
    "delivery": {"доставка", "условия доставки", "delivery"},
    "certificates": {"документы", "сертификаты", "certificates", "documents"},
    "phone": {"телефон", "phone"},
    "email": {"email", "e-mail", "почта"},
    "address": {"адрес", "address"},
    "contact": {"контакт", "contact"},
    "website": {"сайт", "website", "url"},
    "source": {"источник", "source", "source url"},
    "description": {"описание", "description"},
}


def _key(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower().replace("ё", "е"))


def _split(value: object) -> list[str]:
    return [part.strip() for part in re.split(r"[;,|]", str(value or "")) if part.strip()]


def _column_index(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference).group(0)
    value = 0
    for letter in letters:
        value = value * 26 + ord(letter) - 64
    return value - 1


def _xlsx_rows(raw: bytes) -> list[list[str]]:
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(node.itertext()) for node in root.findall(f"{namespace}si")]
        sheet_name = next((name for name in archive.namelist() if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")), "")
        if not sheet_name:
            raise ValueError("В XLSX не найден лист с данными")
        root = ET.fromstring(archive.read(sheet_name))
    rows = []
    for row in root.findall(f".//{namespace}sheetData/{namespace}row"):
        values: list[str] = []
        for cell in row.findall(f"{namespace}c"):
            index = _column_index(cell.attrib.get("r", "A1"))
            while len(values) <= index:
                values.append("")
            value = cell.findtext(f"{namespace}v", default="")
            if cell.attrib.get("t") == "s" and value:
                value = shared[int(value)]
            elif cell.attrib.get("t") == "inlineStr":
                value = "".join(cell.itertext())
            values[index] = value
        rows.append(values)
    return rows


def _csv_rows(raw: bytes) -> list[list[str]]:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            text = raw.decode(encoding)
            dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;")
            return list(csv.reader(io.StringIO(text), dialect))
        except (UnicodeDecodeError, csv.Error):
            continue
    raise ValueError("CSV должен быть в UTF-8 или Windows-1251 и содержать разделители , или ;")


def _map_headers(headers: list[str]) -> dict[int, str]:
    result: dict[int, str] = {}
    for index, header in enumerate(headers):
        normalized = _key(header)
        for field, aliases in FIELD_ALIASES.items():
            if normalized in aliases:
                result[index] = field
                break
    if "name" not in result.values():
        raise ValueError("Не найдена обязательная колонка «Поставщик» или «Название»")
    return result


def _supplier(values: dict[str, str], position: int) -> dict:
    name = values.get("name", "").strip()
    source = values.get("source", "").strip() or values.get("website", "").strip()
    item = {
        "id": "import-" + sha1(f"{source}|{name}|{values.get('city', '')}".encode()).hexdigest()[:12],
        "name": name,
        "type": values.get("type", "").strip() or "Поставщик",
        "categories": _split(values.get("categories")),
        "products": _split(values.get("products")),
        "regions": _split(values.get("regions")),
        "city": values.get("city", "").strip() or "Не указано",
        "description": values.get("description", "").strip() or "Добавлено оператором через импорт.",
        "minimumOrder": values.get("minimumOrder", "").strip() or "Уточнить у поставщика",
        "priceRange": values.get("priceRange", "").strip() or "По запросу",
        "delivery": values.get("delivery", "").strip() or "Уточнить у поставщика",
        "certificates": _split(values.get("certificates")),
        "phone": values.get("phone", "").strip(),
        "email": values.get("email", "").strip(),
        "address": values.get("address", "").strip(),
        "contact": values.get("contact", "").strip(),
        "website": values.get("website", "").strip(),
        "source": source,
        "source_url": source,
        "source_name": name,
        "updated": date.today().isoformat(),
        "collection_mode": "operator_import",
    }
    if not item["regions"] and item["city"] != "Не указано":
        item["regions"] = [item["city"]]
    item["completeness"] = max(35, 100 - len(missing_fields(item)) * 10)
    return item


def parse_import_file(filename: str, content_base64: str) -> tuple[list[dict], list[dict]]:
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Не удалось прочитать содержимое файла") from exc
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("Файл больше 5 МБ")
    lower_name = filename.lower()
    if lower_name.endswith(".csv"):
        rows = _csv_rows(raw)
    elif lower_name.endswith(".xlsx"):
        rows = _xlsx_rows(raw)
    else:
        raise ValueError("Поддерживаются только файлы CSV и XLSX")
    if len(rows) < 2:
        raise ValueError("Файл должен содержать заголовки и хотя бы одну строку")
    header_row = next((index for index, row in enumerate(rows) if any(_key(value) in FIELD_ALIASES["name"] for value in row)), None)
    if header_row is None:
        raise ValueError("Не найдена обязательная колонка «Поставщик» или «Название»")
    data_rows = rows[header_row + 1:]
    if len(data_rows) > MAX_ROWS:
        raise ValueError(f"В одном импорте можно обработать до {MAX_ROWS} строк")
    headers = _map_headers(rows[header_row])
    items, errors = [], []
    for row_number, row in enumerate(data_rows, start=header_row + 2):
        values = {field: (row[index].strip() if index < len(row) else "") for index, field in headers.items()}
        if not any(values.values()):
            continue
        if not values.get("name"):
            errors.append({"row": row_number, "error": "Не указано название поставщика"})
            continue
        items.append(_supplier(values, row_number))
    if not items:
        raise ValueError("Не найдено ни одной корректной строки для импорта")
    return items, errors
