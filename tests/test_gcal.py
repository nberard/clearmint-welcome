import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gcal  # noqa: E402

PARIS = ZoneInfo("Europe/Paris")


class Config(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.patch = mock.patch.multiple(gcal, CONFIG_DIR=self.dir,
                                         CLIENT_FILE=os.path.join(self.dir, "client.json"),
                                         TOKEN_FILE=os.path.join(self.dir, "token.json"))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        with open(gcal.CLIENT_FILE, "w") as f:
            json.dump({"installed": {"client_id": "ID.apps.googleusercontent.com", "client_secret": "S"}}, f)


class OAuth(Config):
    def test_pkce(self):
        verifier, challenge = gcal.pkce_pair()
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(challenge, expected)
        self.assertGreaterEqual(len(verifier), 43)

    def test_missing_client_and_token(self):
        os.remove(gcal.CLIENT_FILE)
        with self.assertRaises(gcal.NotConfigured):
            gcal.load_client()
        with self.assertRaises(gcal.NotConfigured):
            gcal.load_token()

    def test_login_flow_and_token_file_private(self):
        """Simule Google : le « navigateur » revient sur 127.0.0.1 avec le code."""
        def fake_browser(url):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            self.assertEqual(q["code_challenge_method"], ["S256"])
            self.assertIn("calendar.readonly", q["scope"][0])
            back = f"{q['redirect_uri'][0]}/?state={q['state'][0]}&code=CODE123"
            threading.Thread(target=lambda: urllib.request.build_opener(
                urllib.request.ProxyHandler({})).open(back).read(), daemon=True).start()
            return True

        posted = {}

        def fake_post(url, fields):
            posted.update(fields)
            return {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}

        with mock.patch("webbrowser.open", fake_browser), mock.patch.object(gcal, "_post", fake_post), \
                mock.patch("builtins.print"):
            gcal.login()
        self.assertEqual(posted["code"], "CODE123")
        self.assertIn("code_verifier", posted)
        tok = gcal.load_token()
        self.assertEqual((tok["access_token"], tok["refresh_token"]), ("AT", "RT"))
        self.assertEqual(os.stat(gcal.TOKEN_FILE).st_mode & 0o777, 0o600)

    def test_refresh_when_expired(self):
        gcal.save_token({"access_token": "OLD", "refresh_token": "RT", "expiry": 0})
        with mock.patch.object(gcal, "_post", return_value={"access_token": "NEW", "expires_in": 3600}) as p:
            self.assertEqual(gcal.access_token(), "NEW")
            self.assertEqual(p.call_args[0][1]["grant_type"], "refresh_token")
            self.assertEqual(gcal.access_token(), "NEW")      # encore valide : pas de 2e appel
            self.assertEqual(p.call_count, 1)


class Expired(Config):
    def test_invalid_grant_gives_clear_message(self):
        gcal.save_token({"access_token": "OLD", "refresh_token": "RT", "expiry": 0})
        err = RuntimeError('Google a refusé (400) : {"error": "invalid_grant"}')
        with mock.patch.object(gcal, "_post", side_effect=err):
            with self.assertRaises(gcal.NotConfigured) as ctx:
                gcal.access_token()
        self.assertIn("gcal.py login", str(ctx.exception))


class Convert(unittest.TestCase):
    def test_timed_event_with_own_colour(self):
        e = gcal.convert_event({"summary": "Réunion", "colorId": "6", "location": "Nîmes",
                                "start": {"dateTime": "2026-11-16T09:30:00+01:00"},
                                "end": {"dateTime": "2026-11-16T10:00:00+01:00"}}, "#039be5", tz=PARIS)
        self.assertEqual(e["start"], "2026-11-16T09:30:00+01:00")
        self.assertEqual(e["color"], "#f4511e")
        self.assertFalse(e["all_day"])

    def test_all_day_uses_calendar_colour(self):
        e = gcal.convert_event({"summary": "Vacances", "start": {"date": "2026-11-21"},
                                "end": {"date": "2026-11-23"}}, "#33b679")
        self.assertEqual((e["start"], e["end"], e["color"], e["all_day"]), ("2026-11-21", "2026-11-23", "#33b679", True))

    def test_utc_converted_and_cancelled_skipped(self):
        e = gcal.convert_event({"start": {"dateTime": "2026-11-16T17:00:00Z"},
                                "end": {"dateTime": "2026-11-16T18:00:00Z"}}, "#000000", tz=PARIS)
        self.assertTrue(e["start"].startswith("2026-11-16T18:00"))
        self.assertIsNone(gcal.convert_event({"status": "cancelled"}, "#000000"))

    def test_task(self):
        t = gcal.convert_task({"title": "Payer le loyer", "due": "2026-11-18T00:00:00.000Z", "status": "needsAction"})
        self.assertEqual((t["start"], t["end"], t["task"], t["done"]), ("2026-11-18", "2026-11-19", True, False))
        self.assertTrue(gcal.convert_task({"title": "x", "due": "2026-11-18T00:00:00Z", "status": "completed"})["done"])
        self.assertIsNone(gcal.convert_task({"title": "sans échéance"}))


class Fetch(Config):
    def test_events_and_tasks_merged(self):
        responses = {
            "/users/me/calendarList": {"items": [
                {"id": "me@gmail.com", "summary": "Moi", "backgroundColor": "#039be5", "selected": True},
                {"id": "hidden", "summary": "Masqué", "selected": False}]},
            "/calendars/me%40gmail.com/events": {"items": [
                {"summary": "Sport", "start": {"dateTime": "2026-11-17T18:00:00+01:00"},
                 "end": {"dateTime": "2026-11-17T19:00:00+01:00"}}]},
            "/users/@me/lists": {"items": [{"id": "L1"}]},
            "/lists/L1/tasks": {"items": [{"title": "Courses", "due": "2026-11-16T00:00:00Z"}]},
        }

        def fake_get(url, params=None):
            path = urllib.parse.urlsplit(url).path
            for k, v in responses.items():
                if path.endswith(k):
                    return v
            self.fail(f"appel inattendu : {url}")

        with mock.patch.object(gcal, "api_get", fake_get):
            start = datetime(2026, 11, 16, tzinfo=PARIS)
            evs, errors = gcal.events_between(start, datetime(2026, 11, 23, tzinfo=PARIS))
        self.assertEqual(errors, [])
        self.assertEqual([e["title"] for e in evs], ["Courses", "Sport"])   # tâche du lundi, puis mardi
        self.assertTrue(evs[0]["task"])
        self.assertEqual(evs[1]["calendar"], "Moi")
        with mock.patch.object(gcal, "api_get", fake_get):
            evs, _ = gcal.events_between(start, datetime(2026, 11, 23, tzinfo=PARIS), hide=["moi"])
        self.assertEqual([e["title"] for e in evs], ["Courses"])   # agenda « Moi » masqué


if __name__ == "__main__":
    unittest.main()
