import json
from datetime import datetime, timedelta, timezone

import main
import opening_strength as study
import paper_trading
from early_runner import has_current_premarket_evidence
from market_calendar import market_session_status

OPEN = datetime(2026, 11, 19, 14, 30, tzinfo=timezone.utc)
SIGNAL = OPEN + timedelta(minutes=15, seconds=5)
SYMBOLS = [f"S{i:02}" for i in range(12)]


def bar(stamp, opening, close):
    return dict(
        t=stamp.isoformat(), o=opening, c=close, h=max(opening, close), l=min(opening, close), v=1000
    )


def quote(stamp, bid=10, ask=10.01):
    return dict(t=stamp.isoformat(), bp=bid, ap=ask, bs=1, **{"as": 1})


class Tape:
    def __init__(self):
        self.calls = []

    def read(self, kind, symbols, start, end):
        self.calls.append((kind, start))
        if start == OPEN + 3 * study.MINUTE:
            return {
                s: [bar(start + i * study.MINUTE, 10, 10 + i / (j + 1)) for i in range(3)]
                for j, s in enumerate(symbols)
                if s != "S11"
            }
        if start == SIGNAL:
            return {
                s: [quote(start), quote(start + timedelta(seconds=5), ask=10.1)]
                for s in symbols
                if s != "S01"
            }
        if kind == "bars":
            # The entry minute crosses both levels and must be excluded.
            first = SIGNAL.replace(second=0)
            return {s: [bar(first, 5, 20), bar(first + study.MINUTE, 10.1, 11.2)] for s in symbols}
        return {s: [quote(start + timedelta(seconds=1), 10.9, 11.0)] for s in symbols}


def test_forward_session_roundtrip_and_paper_only_reporting(tmp_path, monkeypatch):
    tape = Tape()
    monkeypatch.setattr(study, "OpeningMarketData", lambda *args: tape)
    book = {}
    study.advance_opening_test(book, SYMBOLS, OPEN + 7 * study.MINUTE, None, lambda: SIGNAL)
    assert not tape.calls
    study.advance_opening_test(book, SYMBOLS, SIGNAL, None, lambda: SIGNAL)
    session = book["sessions"]["2026-11-19"]
    assert session["signal_at"] == SIGNAL.isoformat()
    assert [s for s, r in session["rows"].items() if r["selected"]] == ["S00", "S01"]
    assert session["rows"]["S11"] == {"selected": False}
    later = SIGNAL + 2 * study.MINUTE
    study.advance_opening_test(book, SYMBOLS, later, None)
    assert session["rows"]["S00"]["ask"] == 10.1
    assert session["rows"]["S01"]["outcome"] == "no_entry_quote"
    assert session["phase"] == "entered"
    later += 65 * study.MINUTE
    study.advance_opening_test(book, SYMBOLS, later, None)
    calls = len(tape.calls)
    study.advance_opening_test(book, SYMBOLS, later, None)
    assert len(tape.calls) == calls
    assert session["rows"]["S00"]["outcome"] == "target"
    assert session["rows"]["S00"]["net_return_pct"] < (10.9 / 10.1 - 1) * 100
    monkeypatch.setattr(paper_trading, "STATE_PATH", tmp_path / "paper.json")
    account = paper_trading.update_account([], [], later, opening_strength_book=book)
    assert account["cash"] == 250 and account["positions"] == []
    saved = paper_trading.load_account()
    monkeypatch.setattr(
        main,
        "latest_history_state",
        lambda: {"paper_trading": saved, "paper_tracker": {}, "lifetime": {}},
    )
    paper = main.paper_trading_data()["summary"]["opening_strength"]
    assert "opening_strength" not in json.loads(main.market_overview_data().body)
    assert paper["target_first"] == paper["evaluated"] == paper["net_count"] == 1
    assert paper["missed_runners"] == 10 and paper["completed"] == 1
    assert "Opening strength paper test" in main.paper_trading_page().body.decode()


def test_no_lookahead_or_stale_entry_and_stop_first_on_ambiguous_bar():
    assert (
        study.first_quote(
            [
                quote(SIGNAL),
                quote(SIGNAL + timedelta(seconds=31)),
                quote(SIGNAL + timedelta(seconds=1), 11, 10),
            ],
            SIGNAL,
        )
        is None
    )
    at = SIGNAL.replace(second=0) + study.MINUTE
    r = dict(entry_at=SIGNAL.isoformat(), ask=10)
    assert study.exit_trigger([bar(at, 9, 11)], r) == ("stop", at + study.MINUTE)
    assert study.exit_trigger([bar(at, 10, 10)], r) == ("unknown_path", None)
    assert study.exit_trigger([bar(at + 59 * study.MINUTE, 10, 10)], r) == (
        "horizon",
        at + 60 * study.MINUTE,
    )
    start = OPEN + 3 * study.MINUTE
    rows = study.rank_session(
        ["X"],
        {
            "X": [
                bar(start, 10, 11),
                bar(start, 10, 11),
                bar(start + study.MINUTE, 10, 10),
                bar(start + 5 * study.MINUTE, 10, 90),
            ]
        },
        start,
    )
    assert rows == {"X": {"selected": False}}


def test_late_scan_and_weekend_never_create_a_retrospective_signal():
    book = {}
    study.advance_opening_test(book, SYMBOLS, OPEN + 40 * study.MINUTE, None)
    study.advance_opening_test(book, SYMBOLS, OPEN + timedelta(days=2, minutes=10), None)
    assert book["sessions"] == {"2026-11-19": {"phase": "missed_window", "rows": {}}}


def test_premarket_requires_current_volume_current_verified_quote_and_session():
    now = OPEN - study.MINUTE
    session = market_session_status(now)
    item = dict(
        price=10,
        price_as_of=now.isoformat(),
        premarket_volume=42,
        premarket_volume_as_of=(now - study.MINUTE).isoformat(),
        price_validation_passed=True,
        data_quality={"verification_status": "verified"},
    )
    assert has_current_premarket_evidence(item, now, session)
    for field, value in [
        ("price_as_of", (now - timedelta(days=1)).isoformat()),
        ("premarket_volume_as_of", (now - timedelta(days=1)).isoformat()),
        ("premarket_volume", 0),
        ("price_validation_passed", False),
    ]:
        assert not has_current_premarket_evidence({**item, field: value}, now, session)
    assert not has_current_premarket_evidence(item, OPEN, market_session_status(OPEN))


def test_exit_data_failure_resumes_without_repricing_entries(monkeypatch):
    tape = Tape()
    original = tape.read
    fail = True

    def read(kind, symbols, start, end):
        if kind == "quotes" and start > SIGNAL and fail:
            raise OSError("feed temporarily unavailable")
        return original(kind, symbols, start, end)

    tape.read = read
    monkeypatch.setattr(study, "OpeningMarketData", lambda *args: tape)
    book = {}
    study.advance_opening_test(book, SYMBOLS, SIGNAL, None, lambda: SIGNAL)
    later = SIGNAL + 70 * study.MINUTE
    study.advance_opening_test(book, SYMBOLS, later, None)
    session = book["sessions"]["2026-11-19"]
    assert session["phase"] == "exits_pending" and session["data_error"] == "OSError"
    fail = False
    study.advance_opening_test(book, SYMBOLS, later + study.MINUTE, None)
    assert session["phase"] == "complete" and "data_error" not in session
    assert tape.calls.count(("quotes", SIGNAL)) == 1
    assert tape.calls.count(("bars", SIGNAL.replace(second=0))) == 1


def test_paged_market_data_maps_provider_symbols_and_preserves_empty_controls(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setenv("ALPACA_API_KEY", "fixture")
    monkeypatch.setenv("ALPACA_API_SECRET", "fixture")
    http = Mock()

    def response(status, payload, text=""):
        result = Mock(status_code=status, text=text)
        result.json.return_value = payload
        return result

    http.get.side_effect = [
        response(400, {}, "invalid symbol"),
        response(400, {}, "invalid symbol"),
        response(200, {"bars": {"LEN.B": [{"o": 1}]}, "next_page_token": "next"}),
        response(200, {"bars": {"LEN.B": [{"o": 2}]}}),
    ]
    actual = study.OpeningMarketData(http, "iex").read("bars", ["BAD", "LEN-B"], OPEN, SIGNAL)
    assert dict(actual) == {"LEN-B": [{"o": 1}, {"o": 2}]}
    assert http.get.call_args.kwargs["params"]["page_token"] == "next"
