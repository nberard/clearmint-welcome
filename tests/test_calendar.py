import os
import sys
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import calendar_ics as C  # noqa: E402

PARIS = ZoneInfo("Europe/Paris")

ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:standup
DTSTART;TZID=Europe/Paris:20261005T093000
DTEND;TZID=Europe/Paris:20261005T094500
RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR
EXDATE;TZID=Europe/Paris:20261118T093000
SUMMARY:Stand-up
END:VEVENT
BEGIN:VEVENT
UID:standup
RECURRENCE-ID;TZID=Europe/Paris:20261120T093000
DTSTART;TZID=Europe/Paris:20261120T110000
DTEND;TZID=Europe/Paris:20261120T111500
SUMMARY:Stand-up (décalé)
END:VEVENT
BEGIN:VEVENT
UID:birthday
DTSTART;VALUE=DATE:19900617
DTEND;VALUE=DATE:19900618
RRULE:FREQ=YEARLY
SUMMARY:Anniversaire Léa
END:VEVENT
BEGIN:VEVENT
UID:trip
DTSTART;VALUE=DATE:20261121
DTEND;VALUE=DATE:20261123
SUMMARY:Week-end à Montpellier
LOCATION:Montpellier\\, France
END:VEVENT
BEGIN:VEVENT
UID:utc
DTSTART:20261117T170000Z
DTEND:20261117T180000Z
SUMMARY:Appel
 avec une ligne coupée
END:VEVENT
BEGIN:VEVENT
UID:cancelled
DTSTART;TZID=Europe/Paris:20261119T140000
DTEND;TZID=Europe/Paris:20261119T150000
STATUS:CANCELLED
SUMMARY:Annulé
END:VEVENT
BEGIN:VEVENT
UID:limited
DTSTART;TZID=Europe/Paris:20261110T080000
DURATION:PT30M
RRULE:FREQ=DAILY;COUNT=8
SUMMARY:Sport
END:VEVENT
BEGIN:VEVENT
UID:monthly
DTSTART;TZID=Europe/Paris:20260101T190000
DTEND;TZID=Europe/Paris:20260101T200000
RRULE:FREQ=MONTHLY;BYDAY=3TH
SUMMARY:Club
END:VEVENT
END:VCALENDAR
"""


def week():
    start = datetime(2026, 11, 16, tzinfo=PARIS)
    return start, start + timedelta(days=7)


class Week(unittest.TestCase):
    def setUp(self):
        self.events = C.events_between(ICS, *week(), tz=PARIS)
        self.titles = [(e["start"][:16], e["title"]) for e in self.events]

    def test_weekly_with_exception_and_override(self):
        standups = [t for t in self.titles if t[1].startswith("Stand-up")]
        self.assertEqual(standups, [("2026-11-16T09:30", "Stand-up"),
                                    ("2026-11-20T11:00", "Stand-up (décalé)")])

    def test_yearly_not_in_week_and_all_day_multi_day(self):
        self.assertNotIn("Anniversaire Léa", [t[1] for t in self.titles])
        trip = next(e for e in self.events if e["title"].startswith("Week-end"))
        self.assertTrue(trip["all_day"])
        self.assertEqual((trip["start"], trip["end"]), ("2026-11-21", "2026-11-23"))
        self.assertEqual(trip["location"], "Montpellier, France")

    def test_utc_converted_and_folded_line(self):
        call = next(e for e in self.events if e["title"].startswith("Appel"))
        self.assertEqual(call["title"], "Appelavec une ligne coupée")
        self.assertTrue(call["start"].startswith("2026-11-17T18:00"))  # 17:00 UTC = 18:00 Paris

    def test_cancelled_skipped(self):
        self.assertNotIn("Annulé", [t[1] for t in self.titles])

    def test_count_limit(self):
        sport = [t for t in self.titles if t[1] == "Sport"]
        self.assertEqual([t[0][:10] for t in sport], ["2026-11-16", "2026-11-17"])  # 10..17 = 8

    def test_monthly_nth_weekday(self):
        club = [t for t in self.titles if t[1] == "Club"]
        self.assertEqual(club, [("2026-11-19T19:00", "Club")])  # 3e jeudi de novembre 2026

    def test_sorted_all_day_first(self):
        days = [e["start"][:10] for e in self.events]
        self.assertEqual(days, sorted(days))
        sat = [e for e in self.events if e["start"].startswith("2026-11-21")]
        self.assertTrue(sat[0]["all_day"])


class Birthday(unittest.TestCase):
    def test_yearly_all_day(self):
        start = datetime(2026, 6, 15, tzinfo=PARIS)
        ev = C.events_between(ICS, start, start + timedelta(days=7), tz=PARIS)
        bday = [e for e in ev if e["title"] == "Anniversaire Léa"]
        self.assertEqual(len(bday), 1)
        self.assertEqual(bday[0]["start"], "2026-06-17")


class Helpers(unittest.TestCase):
    def test_week_bounds_monday(self):
        start, end = C.week_bounds(date(2026, 11, 14))
        self.assertEqual(start.date(), date(2026, 11, 9))
        self.assertEqual((end - start).days, 7)

    def test_duration(self):
        self.assertEqual(C.parse_duration("PT1H30M"), timedelta(hours=1, minutes=30))
        self.assertEqual(C.parse_duration("P1D"), timedelta(days=1))

    def test_bad_lines_ignored(self):
        bad = "BEGIN:VEVENT\nDTSTART:garbage\nSUMMARY:x\nEND:VEVENT\n" + ICS
        self.assertTrue(C.events_between(bad, *week(), tz=PARIS))


if __name__ == "__main__":
    unittest.main()
