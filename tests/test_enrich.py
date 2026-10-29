import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import enrich  # noqa: E402


class FakeMembers:
    def __init__(self, members):
        self.members, self.calls = members, []

    def get(self, path):
        self.calls.append(path)
        member_id = path.rsplit("/", 1)[-1]
        if member_id not in self.members:
            raise RuntimeError(f"GET {path} -> HTTP 404")
        return self.members[member_id]


WORKBOOKS = [
    {"workbookId": "wb-1", "workbookUrlId": "slug1", "ownerId": "m1",
     "url": "https://app.example.invalid/northwind-demo/workbook/slug1"},
    {"workbookId": "wb-2", "workbookUrlId": "slug2", "ownerId": "m2",
     "url": "https://app.example.invalid/northwind-demo/workbook/slug2"},
]


class OwnerTests(unittest.TestCase):
    def test_owner_email_is_cached(self):
        client = FakeMembers({"m1": {"email": "analyst@example.com"}})
        cache, warnings = {}, []
        for _ in range(3):
            self.assertEqual(enrich.owner_email(client, "m1", cache, warnings),
                             "analyst@example.com")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(warnings, [])

    def test_lookup_failure_warns_once_and_is_cached(self):
        client = FakeMembers({})
        cache, warnings = {}, []
        self.assertIsNone(enrich.owner_email(client, "m9", cache, warnings))
        self.assertIsNone(enrich.owner_email(client, "m9", cache, warnings))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(warnings), 1)
        self.assertIsNone(enrich.owner_email(client, None, cache, warnings))

    def test_set_owner_sets_id_and_email(self):
        client = FakeMembers({"m1": {"email": "analyst@example.com"}})
        row = {}
        enrich.set_owner(row, WORKBOOKS[0], client, {}, [])
        self.assertEqual(row, {"OWNER_ID": "m1", "OWNER_EMAIL": "analyst@example.com"})

    def test_set_owner_keeps_id_when_email_unavailable(self):
        row, warnings = {}, []
        enrich.set_owner(row, WORKBOOKS[0], FakeMembers({}), {}, warnings)
        self.assertEqual(row, {"OWNER_ID": "m1"})
        self.assertEqual(len(warnings), 1)

    def test_fill_group_owners_by_object(self):
        client = FakeMembers({"m1": {"email": "analyst@example.com"},
                              "m2": {"email": "other@example.com"}})
        groups = [{"MEMBERS": [
            {"OBJECT": "/workbook/slug1", "ELEMENT": "e1"},
            {"OBJECT": "/workbook/slug2", "ELEMENT": "e1", "OWNER_EMAIL": "kept@example.com"},
            {"OBJECT": "/workbook/slug2", "ELEMENT": "e2"},
            {"OBJECT": "/data-model/dm1", "ELEMENT": "(workbook load)"},
            {"OBJECT": "/workbook/unknown", "ELEMENT": "e1"},
        ]}]
        enrich.fill_group_owners(groups, WORKBOOKS, client, {}, [])
        members = groups[0]["MEMBERS"]
        self.assertEqual(members[0]["OWNER_EMAIL"], "analyst@example.com")
        self.assertEqual(members[1]["OWNER_EMAIL"], "kept@example.com")
        self.assertEqual(members[2]["OWNER_EMAIL"], "other@example.com")
        self.assertNotIn("OWNER_EMAIL", members[3])
        self.assertNotIn("OWNER_EMAIL", members[4])

    def test_fill_group_owners_tolerates_missing_groups(self):
        enrich.fill_group_owners(None, WORKBOOKS, FakeMembers({}), {}, [])


class RetryTests(unittest.TestCase):
    ENV = {"SIGMA_BASE_URL": "https://api.example.invalid", "SIGMA_API_TOKEN": "test-token"}

    def http_error(self, code, headers=None):
        return urllib.error.HTTPError("u", code, "x", headers or {}, io.BytesIO(b"{}"))

    def response(self, body=b'{"ok": true}'):
        mock_response = mock.MagicMock()
        mock_response.__enter__.return_value = io.BytesIO(body)
        return mock_response

    def client(self):
        with mock.patch.dict("os.environ", self.ENV, clear=True):
            return enrich.SigmaClient()

    def test_429_is_retried_then_succeeds(self):
        client = self.client()
        sequence = [self.http_error(429, {"Retry-After": "1"}), self.response()]
        with mock.patch("urllib.request.urlopen", side_effect=sequence), \
                mock.patch("enrich.time.sleep") as sleep:
            self.assertEqual(client.get("/v2/x"), {"ok": True})
        sleep.assert_called_once()

    def test_429_gives_up_after_retries(self):
        client = self.client()
        with mock.patch("urllib.request.urlopen",
                        side_effect=lambda *a, **k: (_ for _ in ()).throw(self.http_error(429))), \
                mock.patch("enrich.time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                client.get("/v2/x", retries=2)
        self.assertIn("HTTP 429", str(ctx.exception))

    def test_other_errors_do_not_retry(self):
        client = self.client()
        with mock.patch("urllib.request.urlopen", side_effect=self.http_error(404)) as urlopen, \
                mock.patch("enrich.time.sleep") as sleep:
            with self.assertRaises(RuntimeError):
                client.get("/v2/x")
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
