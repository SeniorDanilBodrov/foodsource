import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from backend.xlsx_export import export_suppliers_xlsx


class XlsxExportTests(unittest.TestCase):
    def test_export_creates_readable_workbook(self):
        supplier = {
            "name": "Тестовый поставщик",
            "type": "Производитель",
            "categories": ["Овощи"],
            "products": ["Персики"],
            "regions": ["Россия"],
            "city": "Екатеринбург",
            "completeness": 89,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "selection.xlsx"
            export_suppliers_xlsx([supplier], path)
            workbook = load_workbook(path, read_only=True)
            sheet = workbook["Поставщики"]

            self.assertEqual(sheet["A1"].value, "Выборка поставщиков FoodSource")
            self.assertEqual(sheet["A2"].value, "Поставщик")
            self.assertEqual(sheet["A3"].value, "Тестовый поставщик")
            self.assertEqual(sheet["R3"].value, 89)
            workbook.close()


if __name__ == "__main__":
    unittest.main()
