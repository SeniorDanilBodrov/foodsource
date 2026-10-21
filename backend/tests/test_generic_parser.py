import unittest

from backend.parsers.generic import parse_supplier_document


class GenericParserTests(unittest.TestCase):
    def test_product_price_after_offices_heading_is_not_an_address(self):
        html = """
        <html><head>
          <title>Лента PRO – товары для бизнеса</title>
          <meta name="description" content="Лента PRO — выгодные поставки товаров для вашего бизнеса">
        </head><body>
          <nav>Скидки Как заказать Акции Контакты Войти О нас</nav>
          <div>«Лента PRO» — надёжный поставщик товаров для вашего бизнеса</div>
          <div>HoReCa</div>
          <div>Сливки ультрапастеризованные, 1 кг</div>
          <div>Кофе зерновой, 900 г</div>
          <div>Офисы и предприятия</div>
          <div>325,00 ₽/шт.</div>
          <div>Жидкое мыло, 5 л</div>
          <footer class="contacts">Или выберите город и спросите персонального менеджера.
          Контакты менеджеров maria.shalina@lenta.com 8 800 700 41 13</footer>
        </body></html>
        """
        item = parse_supplier_document(html, "https://b2b.lenta.com/")
        self.assertEqual(item["address"], "")
        self.assertEqual(item["description"], "Лента PRO — выгодные поставки товаров для вашего бизнеса")
        self.assertIn("Продукты для HoReCa", item["categories"])
        self.assertIn("Молочные продукты", item["products"])
        self.assertIn("Напитки", item["products"])

    def test_explicit_street_address_is_preserved(self):
        html = """
        <html><head><title>Поставщик</title></head><body>
          <div class="contacts">Адрес: г. Екатеринбург, ул. Примерная, д. 12</div>
          <p>Оптовые поставки продуктов питания для ресторанов и кафе.</p>
        </body></html>
        """
        item = parse_supplier_document(html, "https://supplier.example/")
        self.assertIn("ул. Примерная", item["address"])


if __name__ == "__main__":
    unittest.main()
