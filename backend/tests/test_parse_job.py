import unittest
from unittest.mock import AsyncMock, patch

from backend import main
from backend.models import ParseRequest


class ParseJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_parse_returns_immediately_and_result_is_polled(self):
        item = {"id": "web-test", "name": "Тестовый поставщик"}
        stored = {"item": item, "review_required": False}
        request = ParseRequest(url="https://supplier.example")

        with patch.object(main, "parse_supplier", new=AsyncMock(return_value=item)), patch.object(
            main, "safe_upsert", return_value=stored
        ):
            started = await main.parse(request)
            # A real network fetch remains running; the mocked fetch may finish
            # within the one event-loop turn intentionally yielded by the route.
            self.assertIn(started["status"], {"queued", "running", "completed"})
            await main.parse_tasks[started["id"]]
            finished = main.parse_job_status(started["id"])

        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["result"], stored)


if __name__ == "__main__":
    unittest.main()
