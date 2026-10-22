import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend import database
from backend.quality import assess_supplier, calculate_completeness


class QualityAssessmentTests(unittest.TestCase):
    def test_completeness_is_derived_from_actual_fields(self):
        sparse = {"name": "Поставщик", "categories": ["Бакалея"], "minimumOrder": "Не указано"}
        detailed = {
            **sparse, "description": "Оптовая поставка продуктов", "products": ["Мука"],
            "regions": ["Россия"], "phone": "+7 999 000-00-00", "delivery": "По России",
        }

        self.assertEqual(calculate_completeness(sparse), 20)
        self.assertGreater(calculate_completeness(detailed), calculate_completeness(sparse))

    def test_price_fragment_cannot_be_published_as_address(self):
        safe, report = assess_supplier({
            "name": "Лента PRO",
            "address": "ы и предприятия 325,00 ₽/шт",
            "phone": "8 800 700 4 113",
        })

        self.assertEqual(safe["address"], "")
        self.assertEqual(report["status"], "review")
        self.assertTrue(any(item["field"] == "address" for item in report["warnings"]))

    def test_navigation_noise_is_not_used_as_description(self):
        safe, report = assess_supplier({
            "name": "Поставщик",
            "description": "Каталог Каталог Контакты Контакты Личный кабинет Корзина",
        })

        self.assertEqual(safe["description"], "")
        self.assertEqual(report["status"], "review")

    def test_phone_is_trimmed_from_a_valid_street_address(self):
        safe, report = assess_supplier({
            "name": "Поставщик",
            "address": "Московская область, территория Трио-Инвест, стр. 3 +7 (495) 651-92-52",
        })

        self.assertEqual(safe["address"], "Московская область, территория Трио-Инвест, стр. 3")
        self.assertEqual(report["status"], "accepted")

    def test_common_russian_street_abbreviation_is_accepted(self):
        safe, report = assess_supplier({
            "name": "Поставщик",
            "address": "Екатеринбург, ул. Старых Большевиков, 2А/2",
        })

        self.assertEqual(safe["address"], "Екатеринбург, ул. Старых Большевиков, 2А/2")
        self.assertEqual(report["status"], "accepted")


class SafeUpsertTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "quality.sqlite3"
        db = sqlite3.connect(database.DB_PATH)
        try:
            db.execute("""CREATE TABLE suppliers (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, payload TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE pending_supplier_updates (
              id INTEGER PRIMARY KEY AUTOINCREMENT, supplier_id TEXT NOT NULL,
              source_url TEXT NOT NULL DEFAULT '', current_payload TEXT NOT NULL DEFAULT '{}',
              candidate_payload TEXT NOT NULL, reasons TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              created_at TEXT NOT NULL DEFAULT (datetime('now')), reviewed_at TEXT
            )""")
            db.execute("""CREATE TABLE archived_suppliers (
              id TEXT PRIMARY KEY, payload TEXT NOT NULL, state_payload TEXT NOT NULL DEFAULT '{}',
              archived_at TEXT NOT NULL DEFAULT (datetime('now'))
            )""")
            db.commit()
        finally:
            db.close()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    @staticmethod
    def supplier(description: str, confidence: str) -> dict:
        return {
            "id": "web-test", "name": "Тестовый поставщик", "source": "https://supplier.example",
            "website": "https://supplier.example", "description": description,
            "categories": ["Овощи и фрукты"], "products": ["Персики"],
            "regions": ["Россия"], "city": "Россия", "phone": "+7 999 000-00-00",
            "email": "hello@supplier.example", "address": "ул. Ленина, д. 1",
            "collection_mode": "operator_import",
            "field_confidence": {"description": confidence, "categories": "high", "products": "high"},
        }

    def test_automatic_medium_confidence_change_waits_for_review(self):
        database.upsert_many([self.supplier("Проверенное описание", "high")])
        candidate = self.supplier("Новое автоматически извлечённое описание", "medium")

        result = database.safe_upsert(candidate, source_mode="automatic")
        stored = database.list_suppliers()[0]
        pending = database.list_pending_updates()

        self.assertTrue(result["review_required"])
        self.assertEqual(stored["description"], "Проверенное описание")
        self.assertEqual(stored["field_confidence"]["description"], "high")
        self.assertEqual(len(pending), 1)
        self.assertIn("description", " ".join(pending[0]["reasons"]))

    def test_operator_can_approve_or_reject_pending_change(self):
        database.upsert_many([self.supplier("Старое описание", "high")])
        first = database.safe_upsert(
            self.supplier("Описание после ручной проверки", "medium"), source_mode="automatic"
        )
        approved = database.resolve_pending_update(first["review_id"], approve=True)

        self.assertEqual(approved["status"], "approved")
        self.assertEqual(database.list_suppliers()[0]["description"], "Описание после ручной проверки")

        second = database.safe_upsert(
            self.supplier("Ещё одно сомнительное описание", "medium"), source_mode="automatic"
        )
        rejected = database.resolve_pending_update(second["review_id"], approve=False)

        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(database.list_suppliers()[0]["description"], "Описание после ручной проверки")
        self.assertEqual(database.list_pending_updates(), [])


class SupplierDeduplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "dedup.sqlite3"
        db = sqlite3.connect(database.DB_PATH)
        try:
            db.execute("""CREATE TABLE suppliers (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, payload TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )""")
            db.commit()
        finally:
            db.close()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    @staticmethod
    def supplier(identifier: str, name: str, source: str, phone: str) -> dict:
        return {
            "id": identifier, "name": name, "source": source, "website": source,
            "categories": ["Бакалея"], "products": ["Мука"], "regions": ["Россия"],
            "phone": phone, "contact": phone,
        }

    def test_same_domain_updates_existing_card(self):
        database.upsert_many([self.supplier("first", "Поставщик", "https://www.example.ru/catalog", "+7 900 000-00-01")])
        database.upsert_many([self.supplier("second", "Новое название", "https://example.ru/about", "+7 900 000-00-02")])

        items = database.list_suppliers()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "first")
        self.assertEqual(items[0]["name"], "Новое название")

    def test_same_phone_does_not_create_duplicate(self):
        database.upsert_many([self.supplier("first", "Компания Альфа", "https://alpha.ru", "+7 (900) 111-22-33")])
        database.upsert_many([self.supplier("second", "Компания Альфа Опт", "https://alpha-opt.ru", "8 900 111 22 33")])

        self.assertEqual(len(database.list_suppliers()), 1)

    def test_same_normalized_name_does_not_create_duplicate(self):
        database.upsert_many([self.supplier("first", "Фуд-Сервис", "https://food-one.ru", "+7 900 000-00-01")])
        database.upsert_many([self.supplier("second", "Фуд Сервис", "https://food-two.ru", "+7 900 000-00-02")])

        self.assertEqual(len(database.list_suppliers()), 1)


class ArchiveSupplierTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "archive.sqlite3"
        db = sqlite3.connect(database.DB_PATH)
        try:
            db.execute("""CREATE TABLE suppliers (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, payload TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE supplier_states (
              supplier_id TEXT PRIMARY KEY, favourite INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'new', note TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )""")
            db.execute("""CREATE TABLE pending_supplier_updates (
              id INTEGER PRIMARY KEY AUTOINCREMENT, supplier_id TEXT NOT NULL,
              source_url TEXT NOT NULL DEFAULT '', current_payload TEXT NOT NULL DEFAULT '{}',
              candidate_payload TEXT NOT NULL, reasons TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              created_at TEXT NOT NULL DEFAULT (datetime('now')), reviewed_at TEXT
            )""")
            db.execute("""CREATE TABLE archived_suppliers (
              id TEXT PRIMARY KEY, payload TEXT NOT NULL, state_payload TEXT NOT NULL DEFAULT '{}',
              archived_at TEXT NOT NULL DEFAULT (datetime('now'))
            )""")
            db.commit()
        finally:
            db.close()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    @staticmethod
    def supplier(mode: str = "manual_parse") -> dict:
        return {
            "id": "web-archive", "name": "Архивируемый поставщик",
            "categories": ["Бакалея"], "products": ["Мука"], "regions": ["Россия"],
            "source": "https://supplier.example", "website": "https://supplier.example",
            "collection_mode": mode,
        }

    def test_archive_and_restore_preserve_supplier(self):
        database.upsert_many([self.supplier()])
        database.update_state("web-archive", {"favourite": True, "status": "check", "note": "Позвонить"})

        archived = database.archive_supplier("web-archive")
        self.assertEqual(database.list_suppliers(), [])
        self.assertEqual(len(database.list_archived_suppliers()), 1)
        self.assertTrue(archived["can_delete"])

        restored = database.restore_archived_supplier("web-archive")
        self.assertEqual(restored["name"], "Архивируемый поставщик")
        self.assertEqual(database.get_state("web-archive")["note"], "Позвонить")
        self.assertEqual(database.list_archived_suppliers(), [])

    def test_catalog_card_cannot_be_permanently_deleted(self):
        database.upsert_many([self.supplier("automatic")])
        database.archive_supplier("web-archive")

        with self.assertRaises(ValueError):
            database.delete_archived_supplier("web-archive")

        self.assertEqual(len(database.list_archived_suppliers()), 1)


if __name__ == "__main__":
    unittest.main()
