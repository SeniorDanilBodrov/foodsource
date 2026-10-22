import json
import unittest
from pathlib import Path

from backend.search import normalize_query, search, supplier_declares_category, supplier_matches_category
from backend.semantic import semantic_engine


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class HybridSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.items = json.loads(
            (PROJECT_ROOT / "data" / "suppliers.seed.json").read_text(encoding="utf-8")
        )

    def test_local_model_is_available(self) -> None:
        status = semantic_engine.status(load=True)
        self.assertTrue(status["available"], status)
        self.assertEqual(status["mode"], "hybrid")

    def test_prewarm_reuses_supplier_embeddings(self) -> None:
        warmed = semantic_engine.prewarm(self.items)
        cached = warmed["cached_documents"]
        self.assertGreater(cached, len(self.items))

        semantic_engine.score("пластиковые боксы для еды", self.items[:6])
        self.assertEqual(semantic_engine.status()["cached_documents"], cached)

    def test_empty_query_is_catalogue_without_fake_percentage(self) -> None:
        normalized, items = search(self.items, "", {})
        self.assertEqual(len(items), 60)
        self.assertEqual(normalized["search_mode"], "deterministic")
        self.assertTrue(all(item["score"] == 0 for item in items))

    def test_name_sort_uses_natural_russian_catalog_order(self) -> None:
        samples = [
            {**self.items[0], "id": "latin", "name": "EvroFresh"},
            {**self.items[0], "id": "cyr-b", "name": "Восток-Запад"},
            {**self.items[0], "id": "number", "name": "4 Овощебаза"},
            {**self.items[0], "id": "cyr-a", "name": "Агросервис"},
        ]
        _, items = search(samples, "", {"sort": "name"})

        self.assertEqual(
            [item["name"] for item in items],
            ["4 Овощебаза", "Агросервис", "Восток-Запад", "EvroFresh"],
        )

    def test_semantic_packaging_query_finds_packaging_supplier(self) -> None:
        _, items = search(self.items, "пластиковые боксы для еды", {})
        self.assertGreaterEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "ПО-Картон")
        self.assertIn("смысл запроса", items[0]["reasons"])

    def test_nonsense_does_not_return_the_catalogue(self) -> None:
        _, items = search(self.items, "абракадабра", {})
        self.assertEqual(items, [])

    def test_packaging_company_is_not_a_chicken_supplier(self) -> None:
        _, items = search(self.items, "куриная грудка", {})
        self.assertNotIn("Концерн Протэк", {item["name"] for item in items})

    def test_exact_dishware_query_keeps_real_match_first(self) -> None:
        _, items = search(self.items, "одноразовая посуда", {})
        self.assertEqual(items[0]["name"], "Концерн Протэк")
        self.assertGreater(items[0]["score_breakdown"]["semantic"], 0)

    def test_direct_supplier_name_stays_above_semantic_neighbours(self) -> None:
        lenta = {
            "id": "lenta-pro", "name": "Лента PRO – товары для бизнеса",
            "type": "Поставщик", "categories": ["Продукты для HoReCa"],
            "products": ["Молочные продукты", "Напитки", "Бакалея"],
            "regions": [], "city": "Не указано",
            "description": "Надёжный поставщик товаров для бизнеса",
            "minimumOrder": "Не указано", "priceRange": "По запросу",
            "delivery": "Уточнить у поставщика", "certificates": [],
            "contact": "8 800 700 41 13", "completeness": 82,
        }
        _, items = search([*self.items, lenta], "Лента PRO", {})
        self.assertEqual(items[0]["name"], lenta["name"])
        self.assertIn("название поставщика", items[0]["reasons"])

    def test_peaches_never_return_packaging_suppliers(self) -> None:
        normalized, items = search(self.items, "персики", {})
        self.assertEqual(normalized["category"], "Овощи и фрукты")
        self.assertGreaterEqual(len(items), 1)
        self.assertTrue(all(supplier_declares_category(item, "Овощи и фрукты") for item in items))
        self.assertTrue(all("совпадение по товару" in item["reasons"] for item in items))

    def test_packaging_for_peaches_respects_packaging_intent(self) -> None:
        normalized, items = search(self.items, "упаковка для персиков", {})
        self.assertEqual(normalized["category"], "Упаковка")
        self.assertGreaterEqual(len(items), 1)
        self.assertTrue(all(supplier_declares_category(item, "Упаковка") for item in items))

    def test_processed_peach_query_is_not_fresh_produce(self) -> None:
        normalized, items = search(self.items, "персиковая начинка", {})
        self.assertEqual(normalized["category"], "Ингредиенты для выпечки")
        self.assertGreaterEqual(len(items), 1)
        self.assertNotIn("Овощи и фрукты", items[0]["categories"])

    def test_unrelated_unknown_product_has_no_semantic_hallucination(self) -> None:
        _, items = search(self.items, "офисная мебель", {})
        self.assertEqual(items, [])

    def test_dumplings_and_common_vareniki_typo_find_semi_finished_foods(self) -> None:
        expected = {"ИнтерФудГруп", "Мираторг HoReCa"}
        for query in ["пельмени", "вареники", "варенники", "пельмени и варенники"]:
            with self.subTest(query=query):
                _, items = search(self.items, query, {})
                names = {item["name"] for item in items}
                self.assertTrue(expected.intersection(names), names)
                self.assertTrue(all("полуфабрикат" in " ".join(item["products"]).lower() for item in items))

    def test_rapidfuzz_correction_is_explainable_and_safe(self) -> None:
        cases = {
            "пилмени": "пельмени",
            "упоковка для курицы": "упаковка",
            "пищивая упаковка": "пищевая",
        }
        for query, corrected_word in cases.items():
            with self.subTest(query=query):
                normalized, items = search(self.items, query, {})
                self.assertTrue(items)
                self.assertIn(corrected_word, normalized["corrected_text"])
                self.assertTrue(normalized["corrections"])
                self.assertTrue(any("опечатка исправлена" in item["reasons"] for item in items))

    def test_morphology_normalizes_inflected_products(self) -> None:
        normalized, items = search(self.items, "яблоками оптом", {})
        self.assertEqual(normalized["category"], "Овощи и фрукты")
        self.assertIn("яблоко", normalized["lexical_terms"])
        self.assertTrue(items)
        self.assertTrue(all(supplier_matches_category(item, "Овощи и фрукты") for item in items))

    def test_negative_delivery_phrase_is_not_a_delivery_requirement(self) -> None:
        normalized = normalize_query("пельмени без доставки")
        self.assertFalse(normalized["delivery"])
        self.assertTrue(normalized["excludes_delivery"])

    def test_query_intent_matrix_stays_inside_business_domain(self) -> None:
        cases = {
            "манго": "Овощи и фрукты",
            "морковь": "Овощи и фрукты",
            "коробки для яблок": "Упаковка",
            "бутылки для сока": "Упаковка",
            "упаковка для курицы": "Упаковка",
            "фруктовое пюре": "Ингредиенты для выпечки",
            "сироп манго": "Ингредиенты для выпечки",
            "персиковый сок": "Бакалея",
            "консервированные персики": "Бакалея",
            "куриная грудка": "Мясо и птица",
            "продукты для ресторана": "Продукты для HoReCa",
        }
        for query, expected_category in cases.items():
            with self.subTest(query=query):
                normalized, items = search(self.items, query, {})
                self.assertEqual(normalized["category"], expected_category)
                self.assertGreaterEqual(len(items), 1)
                self.assertTrue(
                    all(supplier_matches_category(item, expected_category) for item in items)
                )

    def test_out_of_domain_matrix_returns_nothing(self) -> None:
        for query in [
            "офисная мебель", "ноутбуки", "ноутбуки оптом", "смартфоны",
            "серверные стойки", "принтеры", "телевизоры", "автозапчасти",
        ]:
            with self.subTest(query=query):
                _, items = search(self.items, query, {})
                self.assertEqual(items, [])


if __name__ == "__main__":
    unittest.main()
