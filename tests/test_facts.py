"""Fact-card grounding: nothing the model says survives unless the document's text supports it."""
import json

from paperless_ask.facts import EXTRACTOR_VERSION, FactStore, ground

TEXT = ("Example Mutual Insurance Automobile Insurance Identification Card Policy Number: 1234-5678-9 "
        "Effective Date: 04/22/2026 Expiration Date: Oct 22, 2026 Premium $ 1,234.50 Insured: JOHN DOE")


def raw(**kw):
    base = {"kind": "insurance", "category": "insurance", "vendor": "Example Mutual", "summary": "Card.",
            "total": None, "dates": [], "ids": []}
    return {**base, **kw}


def test_keeps_grounded_values_in_any_written_form():
    c = ground(raw(total="1234.50", dates=[{"label": "expires", "date": "2026-10-22"},
                                           {"label": "effective", "date": "2026-04-22"}],
                   ids=[{"label": "Policy Number", "value": "123456789"}]), TEXT)
    assert c["total"] == 1234.5
    assert c["dates"] == [{"label": "expires", "date": "2026-10-22"}, {"label": "effective", "date": "2026-04-22"}]
    assert c["ids"] == [{"label": "Policy Number", "value": "123456789"}]  # matched ignoring the dashes
    assert c["dropped"] == []


def test_drops_what_the_text_does_not_support():
    c = ground(raw(total="999.99", vendor="Acme Insurance Group",
                   dates=[{"label": "due", "date": "2026-11-01"}, {"label": "due", "date": "not a date"}],
                   ids=[{"label": "member", "value": "JOHN DOE"}, {"label": "x", "value": "12"},
                        {"label": "claim", "value": "777777"}]), TEXT, correspondent="Example Mutual")
    assert c["total"] is None and c["dates"] == [] and c["ids"] == []
    assert c["vendor"] == "Example Mutual"  # fell back to the Paperless correspondent
    assert len(c["dropped"]) == 7


def test_null_string_total_and_unknown_enums():
    c = ground(raw(total="null", kind="spaceship", category="??"), TEXT)
    assert c["total"] is None and c["kind"] == "other" and c["category"] == "other" and c["dropped"] == []


def test_duplicate_dates_and_ids_collapse():
    c = ground(raw(dates=[{"label": "expires", "date": "2026-10-22"}] * 2,
                   ids=[{"label": "Policy", "value": "123456789"}, {"label": "Policy no", "value": "1234-5678-9"}]), TEXT)
    assert len(c["dates"]) == 1 and len(c["ids"]) == 1


def test_store_round_trip_and_staleness(tmp_path):
    s = FactStore(tmp_path / "facts.json")
    assert s.stale(1, "m1", "model")
    s.put(1, {"kind": "receipt"}, "m1", "model")
    s2 = FactStore(tmp_path / "facts.json")
    assert s2.get(1)["kind"] == "receipt" and s2.get(1)["version"] == EXTRACTOR_VERSION
    assert not s2.stale(1, "m1", "model")
    assert s2.stale(1, "m2", "model") and s2.stale(1, "m1", "other-model")
    s2.prune([2])
    assert json.loads((tmp_path / "facts.json").read_text())["cards"] == {}


def test_store_survives_a_damaged_file(tmp_path):
    (tmp_path / "facts.json").write_text("{not json")
    assert FactStore(tmp_path / "facts.json").cards == {}


def test_ids_must_be_in_the_text_not_just_the_title():
    c = ground(raw(ids=[{"label": "Policy", "value": "MX-M264N"}, {"label": "request", "value": "(A9307)"}]),
               "Request A9307) received", title="MX-M264N_20151116_112053")
    assert c["ids"] == [{"label": "request", "value": "A9307"}]
