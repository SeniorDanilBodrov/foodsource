import unittest

from backend.database import DatabaseSession


class RecordingConnection:
    def __init__(self):
        self.calls = []

    def execute(self, statement, parameters=()):
        self.calls.append((statement, parameters))
        return self


class DatabaseCompatibilityTests(unittest.TestCase):
    def test_postgres_like_pattern_is_passed_as_parameter(self):
        connection = RecordingConnection()
        session = DatabaseSession(connection, postgres=True)

        session.execute(
            "SELECT id FROM suppliers WHERE id LIKE ?", ("reviewed-%",)
        )

        self.assertEqual(
            connection.calls,
            [("SELECT id FROM suppliers WHERE id LIKE %s", ("reviewed-%",))],
        )


if __name__ == "__main__":
    unittest.main()
