"""了結交接（`handoff`）：PTS 還沒了結的口數由誰平、幾點前、平幾口。

盤中程式的告警、收工指令印的、13:25 出場程式的告警都問這裡，出場程式也照它決定送不送單、送到
幾點。所以規則本身在這裡用表格測：部位檔記著什麼 × 說的這一刻 × 出場程式處理過了沒有 → 由誰平、
過了期限沒有。各則告警怎麼把答案講出來，在各程式的測試裡從最上層打進去。
"""

from datetime import date, datetime, time

import pytest

import handoff
import messages
from broker import TMF_CODE, TX_CODE
from handoff import Who
from position_file import (CONFIRMED, ENTRY, EXIT, UNCERTAIN, Fill, PositionRecord,
                           UncertainOrder)
from strategy import LONG

DAY = date(2026, 11, 9)                 # 一般日：近月 11/18 才結算
SETTLEMENT_DAY = date(2026, 11, 18)


def _record(*, day: date = DAY, product: str = TMF_CODE,
            uncertain: UncertainOrder | None = None, exits: tuple[Fill, ...] = ()) -> PositionRecord:
    """部位檔：多單 3 口，`uncertain` 是不確定的那一筆。"""
    return PositionRecord(
        trading_day=day, product=product, order_code="TM2611", contract_month="202611",
        direction=LONG, last_trading_day=SETTLEMENT_DAY,
        status=CONFIRMED if uncertain is None else UNCERTAIN,
        entries=(Fill("2315609990001", 3, 45100.0),), exits=exits, uncertain=uncertain)


ENTRY_WITH_SEQ = UncertainOrder(ENTRY, "2315609990003", requested_lots=2, known_filled=1)
ENTRY_NO_SEQ = UncertainOrder(ENTRY, "", requested_lots=1, known_filled=0)
EXIT_ORDER = UncertainOrder(EXIT, "2315610000001", requested_lots=3, known_filled=0)


def _at(hhmm: str, day: date = DAY) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm))


#            部位檔                                 這一刻              出場程式處理過了  → 誰、過了期限
WHO = {
    # 盤中程式、收工指令講的時候：出場程式還會照部位檔做一次
    "微台、確定：13:25 平":        (_record(),                         _at("10:00"), False, Who.EXIT_PROGRAM, False),
    "微台、有序號的進場不確定：13:25 先查":
                                   (_record(uncertain=ENTRY_WITH_SEQ), _at("10:00"), False, Who.EXIT_LOOKS_UP, False),
    "微台、沒有序號的進場不確定：查不了，你平":
                                   (_record(uncertain=ENTRY_NO_SEQ),   _at("10:00"), False, Who.USER, False),
    "微台、平倉不確定：你平":       (_record(uncertain=EXIT_ORDER),     _at("10:00"), False, Who.USER, False),
    "大台：券商當沖強平":           (_record(product=TX_CODE),          _at("10:00"), False, Who.BROKER, False),
    "大台、進場不確定：照樣券商強平":
                                   (_record(product=TX_CODE, uncertain=ENTRY_WITH_SEQ),
                                                                       _at("10:00"), False, Who.BROKER, False),
    # 出場程式自己講（平不完、查不到）：之後不會再送單
    "出場程式平不完：你平":         (_record(),                         _at("13:28"), True, Who.USER, False),
    "出場程式查不到進場：你平":     (_record(uncertain=ENTRY_WITH_SEQ), _at("13:25"), True, Who.USER, False),
    "13:45 之後：夜盤自己平":       (_record(),                         _at("13:50"), True, Who.USER, True),
    "結算日 13:30 前：你平":        (_record(day=SETTLEMENT_DAY),       _at("13:28", SETTLEMENT_DAY), True, Who.USER, False),
    "結算日 13:30 之後：交易所了結":(_record(day=SETTLEMENT_DAY),       _at("13:30", SETTLEMENT_DAY), True, Who.EXCHANGE, True),
    "大台 13:30 之後：已經由券商強平":
                                   (_record(product=TX_CODE),          _at("13:35"), True, Who.BROKER, True),
}


@pytest.mark.parametrize("on_file, at, exit_done, who, passed", WHO.values(), ids=WHO.keys())
def test_who_closes_what_is_left(on_file, at, exit_done, who, passed):
    answer = handoff.decide(on_file, at=at, exit_done=exit_done)
    assert (answer.who, answer.passed) == (who, passed)


def test_the_micro_deadline_is_the_day_session_close_but_1330_on_the_settlement_day():
    """出場程式停止送單、告警叫人在幾點前平，看的是同一個時刻。"""
    assert handoff.deadline(_record()) == _at("13:45")
    assert handoff.deadline(_record(day=SETTLEMENT_DAY)) == _at("13:30", SETTLEMENT_DAY)


def test_from_1325_the_exit_program_has_started():
    """收工指令 13:25 之後講到微台：不能再保證「13:25 會平掉」，它可能正等著、也可能已經告警了。"""
    assert not handoff.decide(_record(), at=_at("13:24"), exit_done=False).exit_started
    assert handoff.decide(_record(), at=_at("13:25"), exit_done=False).exit_started


LOTS = {
    "確定：就是還沒平的":            (_record(),                                          (3, 3, 3)),
    "已經平了一部分":                 (_record(exits=(Fill("2315610000001", 1, 45200.0),)), (2, 2, 2)),
    "進場不確定：成交幾口就多平幾口": (_record(uncertain=ENTRY_WITH_SEQ),                 (3, 4, 5)),
    "平倉不確定：成交幾口就少平幾口": (_record(uncertain=EXIT_ORDER),                     (3, 0, 3)),
}


@pytest.mark.parametrize("record, held_low_high", LOTS.values(), ids=LOTS.keys())
def test_how_many_lots_are_left_to_close(record, held_low_high):
    lots = handoff.lots_to_close(record)
    assert (lots.held, lots.low, lots.high) == held_low_high


def test_an_entry_the_file_could_not_take_is_counted_the_same_way():
    """盤中送出的進場單不知道成交幾口、而且 broker 給的數字組不成一筆記錄時，口數照同一條算式。"""
    lots = handoff.entry_lots(3, requested=2, known_filled=1)
    assert (lots.held, lots.low, lots.high) == (3, 4, 5)


def test_every_log_that_holds_pts_fills_is_listed():
    """少列收工指令的 log，算出來就多了它平掉的口數（5dac30b）。"""
    logs = handoff.fill_logs(DAY)
    assert logs.entry.endswith("intraday-20261109.log")
    assert [log.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for log in logs.closes] == [
        "exit-20261109.log", "stop-20261109.log"]


# --- 說法也照這個答案：13:25 之後不叫人等 13:25，結算日 13:30 之後不叫人在 13:30 前平 ---


def test_an_intraday_alert_after_1325_does_not_say_wait_for_1325():
    """盤中程式 13:25 之後才重啟，替上一輪補發「委託送出去了，但…」（`report_uncertain_entry_left_by_last_run`）：
    出場程式已經起來了，它要是查不到已經發了 Discord。"""
    on_file = _record(uncertain=ENTRY_WITH_SEQ)
    text = messages.fill_unknown(at=_at("13:40"), on_file=on_file, lots=2,
                                 order_seq=ENTRY_WITH_SEQ.order_seq, known_filled=1,
                                 reason="重啟", file_error=None)
    assert "不必現在處理" not in text
    assert "13:25 起由出場程式用委託序號查這筆、查到全部成交就平倉。" in text


def test_an_intraday_alert_after_1330_on_the_settlement_day_says_the_exchange_settles_it():
    on_file = _record(day=SETTLEMENT_DAY, uncertain=ENTRY_NO_SEQ)
    text = messages.fill_unknown(at=_at("13:35", SETTLEMENT_DAY), on_file=on_file, lots=1,
                                 order_seq="", known_filled=0, reason="重啟", file_error=None)
    assert "13:30 前" not in text
    assert "近月 13:30 已經停止交易，不能再平" in text
