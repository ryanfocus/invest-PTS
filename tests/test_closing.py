"""平倉（`closing.close`）：出場程式與收工指令共用的那一段，從它自己的介面打進去。

送單、重試、查詢的細節在兩支程式的測試裡從最上層各測過（test_exit_program、test_stop_today）；
這裡測的是只有這個介面看得到的東西：沒有要平的就**不建立連線**、結果分成哪幾種、這一次平掉
幾口、部位檔有沒有跟上。部位檔是 conftest 導開的暫存目錄裡的真實檔案。
"""

import os
from datetime import date, datetime, timedelta

import pytest

from broker import FillQuery, FillUnknown, LoginFailed, LoginUnavailable
from broker.fake import FakeBroker
from closing import Result, Short
from day_script import DAY
from position_file import (CONFIRMED, ENTRY, EXIT, UNCERTAIN, Fill, PositionRecord,
                           UncertainOrder)
import closing
import position_file
from strategy import LONG


class _Time:
    def __init__(self, start: datetime):
        self.now = start

    def clock(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _record(*, uncertain: UncertainOrder | None = None, exits: tuple[Fill, ...] = (),
            lots: int = 3) -> PositionRecord:
    record = PositionRecord(
        trading_day=DAY, product="TM0000AM", order_code="TM2611", contract_month="202611",
        direction=LONG, last_trading_day=date(2026, 11, 18),
        status=CONFIRMED if uncertain is None else UNCERTAIN,
        entries=(Fill("2315609990001", lots, 45100.0),), exits=exits, uncertain=uncertain)
    position_file.write(record)
    return record


def _close(record: PositionRecord, broker=None, *, at: str = "13:25", until: str = "13:45"):
    time = _Time(datetime.combine(DAY, datetime.strptime(at, "%H:%M").time()))
    deadline = datetime.combine(DAY, datetime.strptime(until, "%H:%M").time())
    return closing.close(record, broker=lambda: broker, until=deadline, clock=time.clock,
                         sleep=time.sleep)


def _no_broker():
    raise AssertionError("沒有要平的，不該建立連線")


def test_nothing_open_needs_no_connection():
    """收工指令讀不到帳密時也講得清楚：沒有要平的就不必登入。"""
    record = _record(exits=(Fill("2315610000001", 3, 45200.0),))
    closed = closing.close(record, broker=_no_broker,
                           until=datetime.combine(DAY, datetime.max.time()),
                           clock=datetime.now, sleep=lambda s: None)
    assert closed.result is Result.NOTHING_OPEN
    assert closed.closed_lots == 0


@pytest.mark.parametrize("uncertain", [
    UncertainOrder(EXIT, "2315610000001", requested_lots=3, known_filled=0),
    UncertainOrder(ENTRY, "", requested_lots=1, known_filled=0),
], ids=["平倉不確定", "沒有序號的進場不確定"])
def test_an_uncertain_order_that_cannot_be_looked_up_sends_nothing_and_needs_no_connection(
        uncertain):
    closed = closing.close(_record(uncertain=uncertain), broker=_no_broker,
                           until=datetime.combine(DAY, datetime.max.time()),
                           clock=datetime.now, sleep=lambda s: None)
    assert closed.result is Result.ON_FILE_UNCERTAIN
    assert not closed.file_behind


def test_closed_lots_count_only_this_closing_even_when_an_entry_lookup_adds_lots():
    """盤中留下的進場不確定查到成交 2 口：進場口數先變多（3 → 5），再平掉 5 口。這一次平掉的是
    5 口——拿「還沒平的口數」前後相減會算成 −2（e3479a8）。"""
    record = _record(uncertain=UncertainOrder(ENTRY, "2315609990003", requested_lots=2,
                                              known_filled=0))
    broker = FakeBroker(fill_price=45200.0, fill_queries=[FillQuery(lots=2, avg_price=45150.0)])
    closed = _close(record, broker)
    assert closed.result is Result.CLOSED
    assert closed.closed_lots == 5
    assert position_file.read(DAY).open_lots == 0


def test_an_entry_lookup_that_finds_part_of_it_sends_nothing():
    record = _record(uncertain=UncertainOrder(ENTRY, "2315609990003", requested_lots=2,
                                              known_filled=0))
    broker = FakeBroker(fill_price=45200.0, fill_queries=[FillQuery(lots=1, avg_price=45150.0)])
    closed = _close(record, broker)
    assert closed.result is Result.ENTRY_UNRESOLVED
    assert closed.lookup == "成交 1 口（委託 2 口）"
    assert broker.orders == []


def test_a_close_order_with_an_unknown_fill_is_never_sent_again():
    broker = FakeBroker(fill_price=45200.0,
                        order_errors=[FillUnknown("沒等到回報", order_seq="2315610000001")],
                        fill_queries=[FillQuery(lots=None)])
    closed = _close(_record(), broker)
    assert closed.result is Result.EXIT_UNCERTAIN
    assert closed.lookup == "查不到這筆委託的成交"
    assert len(broker.orders) == 1
    assert closed.record.uncertain.stage == EXIT


SHORTS = {
    "帳密錯了：不再試": (dict(login_error=LoginFailed("代碼 300 帳號密碼錯誤")), "13:25",
                    Short.LOGIN_FAILED, 0, "代碼 300 帳號密碼錯誤"),
    "IOC 一直沒有成交：重試 3 次": (dict(fills=[0, 0, 0, 0]), "13:25", Short.RETRIES_USED, 4, ""),
    "網路一直沒回來：到期限為止": (dict(login_error=LoginUnavailable("與主機尚未連線")), "13:43",
                         Short.OUT_OF_TIME, 2, "與主機尚未連線"),
}


@pytest.mark.parametrize("options, at, short, rounds, problem", SHORTS.values(), ids=SHORTS.keys())
def test_why_lots_are_left(options, at, short, rounds, problem):
    """還剩口數時說得出為什麼（各程式照它講原因）。"""
    closed = _close(_record(), FakeBroker(fill_price=45200.0, **options), at=at)
    assert closed.result is Result.LEFT
    assert (closed.short, closed.rounds, closed.problem) == (short, rounds, problem)
    assert closed.record.open_lots == 3


def test_a_close_the_file_cannot_take_is_reported_with_both_counts(monkeypatch):
    def denied(src, dst):
        raise PermissionError(13, "存取被拒")
    record = _record()
    monkeypatch.setattr(os, "replace", denied)
    closed = _close(record, FakeBroker(fill_price=45200.0))
    assert closed.result is Result.CLOSED
    assert closed.file_behind
    assert (closed.record.open_lots, closed.on_file.open_lots) == (0, 3)
    assert closed.file_error.startswith("PermissionError")
