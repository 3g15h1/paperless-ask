"""'Coming up': pattern dates, paid/renewed filtering, alerts, iCal."""
from datetime import date

from paperless_ask.reminders import ics, pattern_dates, upcoming


class D:
    def __init__(self, id, content, title="Doc", correspondent=None, tags=()):
        self.id, self.content, self.title, self.correspondent, self.tags = id, content, title, correspondent, list(tags)


TODAY = date(2026, 10, 10)


def test_pattern_dates():
    text = """Policy Number: 123 Effective Date: 04/22/2026 Expiration Date: 10/22/2026
    Minimum Payment Due by Oct 24, 2026. Payment due date
    11/05/26 Renewal date 2027-01-15 Statement date 10/01/2026"""
    assert pattern_dates(text) == {("expires", date(2026, 10, 22)), ("due", date(2026, 10, 24)),
                                   ("due", date(2026, 11, 5)), ("expires", date(2027, 1, 15))}


def test_upcoming_filters_paid_renewed_and_out_of_window():
    docs = [
        D(1, "Expiration Date: 10/22/2026", correspondent="Example Mutual"),
        D(2, "Expiration Date: 10/22/2025", correspondent="Example Mutual"),     # past
        D(3, "Due by 10/13/2026 Amount due $ 61.20", tags=["Paid"]),             # paid
        D(4, "Payment due date: 10/24/2026", correspondent="Northwind"),
        D(5, "Expires 12/31/2026", correspondent="Gym"),                         # beyond 60 days
        D(6, "Expiration Date: 10/12/2026", correspondent="Example Mutual"),     # renewed by doc 1
    ]
    cards = {4: {"vendor": "Northwind Card", "total": 250.0, "kind": "bill", "dates": [{"label": "due", "date": "2026-10-24"}]}}
    r = upcoming(docs, cards, TODAY, 60, link=lambda i: f"http://pl/documents/{i}/details", alert_days=(14, 3))
    assert [(i["id"], i["label"], i["days_left"]) for i in r["items"]] == [(1, "expires", 12), (4, "due", 14)]
    assert r["items"][1]["text"] == "Northwind Card: $250.00 due in 14 days (Sat Oct 24)"
    assert [i["id"] for i in r["alerts"]] == [4]
    assert upcoming(docs, {}, TODAY, 90)["items"][-1]["id"] == 5


def test_ics_feed():
    r = upcoming([D(4, "Payment due date: 10/24/2026", correspondent="Northwind, Inc; East")], {}, TODAY, 60,
                 link=lambda i: f"http://pl/documents/{i}/details")
    body = ics(r["items"], (14, 0), host="ask.example")
    assert body.startswith("BEGIN:VCALENDAR\r\n") and body.endswith("END:VCALENDAR\r\n")
    assert "UID:4-due-20261024@ask.example" in body
    assert "DTSTART;VALUE=DATE:20261024" in body and "DTEND;VALUE=DATE:20261025" in body
    assert "SUMMARY:Due: Northwind\\, Inc\\; East" in body
    assert "TRIGGER:-PT19620M" in body and "TRIGGER:PT540M" in body  # 14 days before 09:00, and 09:00 on the day
    assert all(len(line.encode()) <= 75 for line in body.split("\r\n"))
