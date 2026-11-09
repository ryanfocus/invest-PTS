"""出場程式（13:25）—— 從最上層打進去（ticket 08）。

部位檔是今天稍早盤中程式寫下的（這裡直接放進 conftest 導開的暫存目錄），出場程式只看它。
斷言三種外部效果：**送出了哪些委託**（假 broker 的紀錄）、**發了哪些訊息**（記錄器）、
**部位檔寫了什麼**（暫存目錄裡的真實檔案）。

出場程式在啟動時讀一次系統時鐘；之後只有重試前的「等一分鐘」會讓時間往前走。
測試用 `_Time` 同時扮演時鐘與睡眠：睡多久，時鐘就往前走多久，所以「13:25 送出、
13:26 重試」是真的發生在劇本的時間上。
"""

import json
import os
from datetime import date, datetime, timedelta

import pytest

from broker import BUY, SELL, TMF_CODE, FillQuery, FillUnknown, LoginFailed, OrderFailed
from broker.fake import FakeBroker
from recording import RecordingNotifier
import exit_program
import position_file
import settings

DAY = date(2026, 11, 9)          # 週一，一般交易日；近月 11/18 結算


def _config(**overrides) -> settings.Config:
    values = dict(auto_order_enabled=True, product=TMF_CODE, lots_per_entry=1,
                  max_position=20, discord_enabled=True)
    values.update(overrides)
    return settings.Config(**values)


class _Time:
    """出場程式的時鐘與睡眠。睡多久，時鐘就往前走多久；每一次睡了多久都記下來。"""

    def __init__(self, start: datetime):
        self.now = start
        self.slept: list[float] = []

    def clock(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


def _at(hhmm: str, day: date = DAY) -> datetime:
    return datetime.combine(day, datetime.strptime(hhmm, "%H:%M").time())


def _run(*, at: datetime | None = None, config=None, **broker_options):
    """13:25（或 `at`）叫醒出場程式，回傳 (broker, 發出的訊息, 時間)。成交價預設 45200。"""
    broker_options.setdefault("fill_price", 45200.0)
    broker = FakeBroker(**broker_options)
    notifier = RecordingNotifier()
    time = _Time(at or _at("13:25"))
    exit_program.run(broker, config or _config(), notify=notifier, clock=time.clock,
                     sleep=time.sleep)
    return broker, notifier.sent, time


def _on_file(day: date = DAY, **overrides) -> dict:
    """盤中程式今天寫下的部位檔：微台多單 3 口（兩筆委託：2 口 @ 45100、1 口 @ 45150）。"""
    content = {
        "trading_day": f"{day:%Y-%m-%d}",
        "product": "TM0000AM",
        "order_code": "TM2611",
        "contract_month": "202611",
        "direction": "LONG",
        "last_trading_day": "2026-11-18",
        "status": "CONFIRMED",
        "entries": [{"order_seq": "2315609990001", "lots": 2, "price": 45100.0},
                    {"order_seq": "2315609990002", "lots": 1, "price": 45150.0}],
        "exits": [],
        "uncertain": None,
    }
    content.update(overrides)
    path = position_file.path_for(day)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(content, fh, ensure_ascii=False)
    return content


def _file(day: date = DAY) -> dict:
    with open(position_file.path_for(day), encoding="utf-8") as fh:
        return json.load(fh)


def _orders(broker: FakeBroker) -> list[tuple]:
    return [(o.contract.order_code, o.side, o.lots, o.day_trade, o.trading_day)
            for o in broker.orders]


# --- 微台：平掉 PTS 自己的全部口數（規格第 31、32 條）---


def test_at_1325_the_micro_position_on_file_is_closed_with_one_market_order():
    """部位檔記著微台多單 3 口（兩筆進場）。13:25 送出一筆賣 3 口的市價單——只平 PTS
    開的這 3 口，不看帳戶裡別的部位（使用者自己的、OS 的）。微台不能標當沖。
    成交寫進部位檔的出場明細，平倉口數等於進場口數，部位就了結了。例行出場不發 Discord。"""
    content = _on_file()
    broker, sent, _ = _run()

    assert _orders(broker) == [("TM2611", SELL, 3, False, DAY)]
    assert sent == []
    assert _file() == {**content, "exits": [
        {"order_seq": "2315610000001", "lots": 3, "price": 45200.0}]}


def _lots(*sizes: int) -> list[dict]:
    """進場明細：每一筆委託成交的口數，價格不重要。"""
    return [{"order_seq": f"23156099900{i:02d}", "lots": n, "price": 45100.0}
            for i, n in enumerate(sizes, 1)]


def test_more_than_ten_lots_are_closed_in_orders_of_at_most_ten():
    """期交所每筆市價委託最多 10 口（規格第 33 條）：空單 23 口拆成買 10、10、3。
    每一筆成交都記進出場明細，各自帶自己的委託序號。"""
    _on_file(direction="SHORT", entries=_lots(*[1] * 20, 3))
    broker, sent, _ = _run()

    assert [(o.side, o.lots) for o in broker.orders] == [(BUY, 10), (BUY, 10), (BUY, 3)]
    assert sent == []
    assert _file()["exits"] == [
        {"order_seq": "2315610000001", "lots": 10, "price": 45200.0},
        {"order_seq": "2315610000002", "lots": 10, "price": 45200.0},
        {"order_seq": "2315610000003", "lots": 3, "price": 45200.0},
    ]


def test_lots_already_closed_today_are_not_closed_again():
    """部位檔裡已經有今天的出場明細（例如出場程式中途被關掉、又被叫起來）：只平剩下的。
    照進場口數再平一次的話，多平的就是反向新倉。"""
    _on_file(exits=[{"order_seq": "2315609990009", "lots": 2, "price": 45180.0}])
    broker, _, _ = _run()
    assert [(o.side, o.lots) for o in broker.orders] == [(SELL, 1)]
    assert [e["lots"] for e in _file()["exits"]] == [2, 1]


def test_a_position_already_closed_sends_nothing():
    """出場明細已經等於進場：部位了結了，再叫醒一次也什麼都不做。"""
    content = _on_file(exits=[{"order_seq": "2315609990009", "lots": 3, "price": 45180.0}])
    broker, sent, _ = _run()
    assert broker.orders == []
    assert sent == []
    assert _file() == content


# --- 大台、小台：交給券商 13:30 當沖強平（規格第 30、35 條，ADR-0002）---


@pytest.mark.parametrize("product, order_code", [("TX00AM", "TX11"), ("MTX00AM", "MTX11")],
                         ids=["大台", "小台"])
@pytest.mark.parametrize("last_trading_day", ["2026-11-18", "2026-11-09"],
                         ids=["一般交易日", "結算日"])
def test_tx_and_mtx_positions_are_left_to_the_brokers_day_trade_close(
        product, order_code, last_trading_day):
    """大台、小台進場時標了當沖，券商 13:30 會強平；出場程式再送一筆平倉單，兩邊同時平，
    多出來的就是反向新倉。結算日也一樣不送，由交易所以最後結算價了結。
    不登入、不送單、不發訊息，部位檔原封不動。"""
    content = _on_file(product=product, order_code=order_code,
                       last_trading_day=last_trading_day,
                       contract_month=last_trading_day[:7].replace("-", ""))
    broker, sent, _ = _run(login_error=AssertionError("大台、小台不該登入"))
    assert broker.orders == []
    assert sent == []
    assert _file() == content


# --- 交易商品以部位檔為準，不看設定檔（規格第 43 條）---


def test_the_settings_say_tx_but_the_file_says_micro_so_micro_is_closed():
    """使用者盤中把設定改成大台。今天的部位是微台，照部位檔平微台。
    照設定檔的話，微台什麼都不平就過夜了。"""
    _on_file()
    broker, _, _ = _run(config=_config(product="TX00AM"))
    assert _orders(broker) == [("TM2611", SELL, 3, False, DAY)]


def test_the_settings_say_micro_but_the_file_says_mtx_so_nothing_is_sent():
    """反過來：設定改成微台，今天的部位是小台。照設定檔的話，會對一個沒有部位的微台送
    賣單——開出一口空單過夜；同時小台又被券商強平。"""
    content = _on_file(product="MTX00AM", order_code="MTX11")
    broker, sent, _ = _run(config=_config(product=TMF_CODE))
    assert broker.orders == []
    assert sent == []
    assert _file() == content


# --- 盤中程式沒有跑、或當掉：出場照常（規格第 49 條）---


def test_no_position_file_means_no_position_and_nothing_to_do():
    """今天沒有進場（或模擬模式）：沒有部位檔就是確定沒有部位。不登入、不發訊息。"""
    broker, sent, _ = _run(login_error=AssertionError("沒有部位不該登入"))
    assert broker.orders == []
    assert sent == []
    assert not os.path.exists(position_file.path_for(DAY))


def test_a_position_left_by_an_intraday_program_that_crashed_is_still_closed():
    """盤中程式 09:00 進場後當掉，再也沒有起來。出場程式不問它還在不在，只看部位檔。"""
    _on_file(entries=_lots(1))
    broker, _, _ = _run()
    assert [(o.side, o.lots) for o in broker.orders] == [(SELL, 1)]


# --- 非交易日：完全安靜（規格第 40 條）---


def test_on_a_non_trading_day_it_does_not_log_in_and_does_nothing():
    """週六。排程每天都會叫醒出場程式，由它自己判斷今天是不是交易日。"""
    saturday = date(2026, 11, 14)
    broker, sent, _ = _run(at=_at("13:25", saturday),
                           login_error=AssertionError("非交易日不該登入"))
    assert broker.orders == []
    assert sent == []


# --- 部位檔讀不懂：不送單，告警（規格第 38 條）---


def _damaged(text: str) -> None:
    path = position_file.path_for(DAY)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


@pytest.mark.parametrize("text", [
    '{"trading_day": "2026-11-09", "product": "TM00',
    "",
    '{"trading_day": "2026-11-09"}',
], ids=["寫到一半", "空檔", "少了欄位"])
def test_a_position_file_that_cannot_be_read_sends_nothing_and_says_so(text):
    """讀不懂就不知道帳上有沒有部位、有幾口、是哪個商品。照猜的口數平倉，平多了就是反向
    新倉；當成沒有部位，微台就過夜。所以一張單都不送，請人看帳戶；檔案原封不動。"""
    _damaged(text)
    broker, sent, _ = _run(login_error=AssertionError("讀不懂部位檔不該登入"))
    assert broker.orders == []
    assert len(sent) == 1
    lines = sent[0].splitlines()
    assert lines[0] == "【要你處理】PTS 部位檔讀不懂，出場程式沒有平倉"
    assert lines[1].startswith("原因：")
    assert lines[2:] == [
        "PTS 今天可能還有部位：請到券商 APP 確認帳戶。"
        "微台要自行平掉（日盤 13:45 收盤）；大台、小台標了當沖，券商 13:30 會強平。",
        "2026/11/09 13:25:00",
    ]
    with open(position_file.path_for(DAY), encoding="utf-8") as fh:
        assert fh.read() == text


# --- 部位不確定：不送單，告警（規格第 50 條）---


def _uncertain(stage: str, *, seq="2315609990003", requested=1, known=0) -> dict:
    return {"stage": stage, "order_seq": seq, "requested_lots": requested,
            "known_filled": known}


def test_an_uncertain_entry_on_file_sends_nothing_and_says_what_is_known():
    """今天稍早有一筆進場單送出去了、卻不知道成交幾口（盤中程式記下「不確定」）。帳上可能是
    3 口，也可能是 4 口：照 3 口平，可能留 1 口過夜；照 4 口平，可能開出 1 口空單。
    不猜，一張單都不送，把知道的都講出來。檔案原封不動。"""
    content = _on_file(status="UNCERTAIN", uncertain=_uncertain("ENTRY"))
    broker, sent, _ = _run(login_error=AssertionError("不確定時不該登入"))
    assert broker.orders == []
    assert sent == [
        "【要你處理】PTS 委託送出去了，但不知道成交幾口，出場程式不再送單\n"
        "不確定的委託：進場買進微台 1 口（委託序號 2315609990003），已知至少成交 0 口\n"
        "部位檔記到的部位（不含這一筆）：多單 3 口\n"
        "請到券商 APP 確認帳戶，自行平掉 PTS 的微台部位（日盤 13:45 收盤）。\n"
        "2026/11/09 13:25:00",
    ]
    assert _file() == content


def test_an_uncertain_exit_on_file_is_never_sent_again():
    """出場單不確定（出場程式稍早寫下的，之後又被叫起來一次）：**絕不再送**。它很可能已經
    成交了，再送就是反向新倉。"""
    content = _on_file(status="UNCERTAIN",
                       uncertain=_uncertain("EXIT", requested=3, known=1))
    broker, sent, _ = _run()
    assert broker.orders == []
    assert sent[0].splitlines()[1:3] == [
        "不確定的委託：平倉賣出微台 3 口（委託序號 2315609990003），已知至少成交 1 口",
        "部位檔記到的部位（不含這一筆）：多單 3 口",
    ]
    assert _file() == content


def test_an_uncertain_tx_position_is_reported_with_the_brokers_close_in_mind():
    """大台本來就不送出場單，但不確定一樣要講：券商會強平標了當沖的部位，使用者要確認的是
    13:30 之後帳上已經沒有 PTS 的部位。叫他自己平的話，會和券商的強平撞在一起。"""
    _on_file(product="TX00AM", order_code="TX11", status="UNCERTAIN",
             uncertain=_uncertain("ENTRY"))
    broker, sent, _ = _run()
    assert broker.orders == []
    assert sent[0].splitlines()[1] == (
        "不確定的委託：進場買進大台 1 口（委託序號 2315609990003），已知至少成交 0 口")
    assert sent[0].splitlines()[3] == (
        "大台、小台標了當沖，券商 13:30 會強平：13:30 之後請到券商 APP 確認帳戶裡"
        "已經沒有 PTS 的部位。")


def test_an_uncertain_first_entry_has_nothing_on_file_but_is_still_reported():
    """第一筆進場就不確定：部位檔裡一筆成交都沒有，只有那一筆不確定的委託。這不是「沒有部位」——
    當成沒有部位安靜結束的話，可能成交了的那口微台就過夜。"""
    _on_file(entries=[], status="UNCERTAIN", uncertain=_uncertain("ENTRY"))
    broker, sent, _ = _run()
    assert broker.orders == []
    assert sent[0].splitlines()[2] == "部位檔記到的部位（不含這一筆）：多單 0 口"


# --- 平倉單沒送出去：每分鐘重試、最多 3 次（規格第 34 條）---


def test_an_exit_order_that_was_not_sent_is_retried_a_minute_later():
    """「委託沒有送出去」確定沒有成交，再送是安全的。13:25、13:26 都被拒，13:27 成交：
    同一筆 3 口送了三次，中間各隔一分鐘；平倉完成就不打擾人。"""
    _on_file()
    broker, sent, time = _run(order_errors=[OrderFailed("保證金不足"),
                                            OrderFailed("回報連線中斷，代碼 3002"), None])
    assert [(o.side, o.lots) for o in broker.orders] == [(SELL, 3)] * 3
    assert time.slept == [60, 60]
    assert sent == []
    assert _file()["exits"] == [{"order_seq": "2315610000003", "lots": 3, "price": 45200.0}]


def test_after_three_retries_the_lots_still_open_are_reported():
    """13:25 送出、13:26／13:27／13:28 重試三次，全部被拒：還剩 3 口。13:30 之前告警，
    寫明還剩幾口、要送什麼單，以及最後一次被拒的原因。部位檔沒有任何出場。"""
    content = _on_file()
    broker, sent, time = _run(order_errors=[OrderFailed("保證金不足")] * 4)
    assert len(broker.orders) == 4
    assert time.slept == [60, 60, 60]
    assert sent == [
        "【要你處理】PTS 部位還在：微台平倉沒有完成，還剩多單 3 口\n"
        "原因：委託沒有送出去，重試 3 次仍然失敗（最後一次：保證金不足）\n"
        "請自行賣出微台 3 口（TM2611），日盤 13:45 收盤。\n"
        "2026/11/09 13:28:00",
    ]
    assert _file() == content


# --- 只成交一部分：剩下的口數照同樣方式重試（規格第 34 條）---


def test_the_lots_left_by_a_partial_fill_are_retried_a_minute_later():
    """市價 IOC 只成交 2 口（當下對手價不夠）：一分鐘後再送剩下的 1 口。"""
    _on_file()
    broker, sent, time = _run(fills=[2, 1])
    assert [o.lots for o in broker.orders] == [3, 1]
    assert time.slept == [60]
    assert sent == []
    assert [e["lots"] for e in _file()["exits"]] == [2, 1]


def test_lots_still_open_after_partial_fills_and_three_retries_are_reported():
    """3 口 → 成交 1 → 剩 2 口成交 1 → 剩 1 口兩次都 0 口：還剩 1 口。成交了的 2 口記在
    出場明細裡，告警只叫人平還沒平的那 1 口——叫他平 3 口的話，多平的 2 口就是反向新倉。"""
    _on_file()
    broker, sent, _ = _run(fills=[1, 1, 0, 0])
    assert [o.lots for o in broker.orders] == [3, 2, 1, 1]
    assert [e["lots"] for e in _file()["exits"]] == [1, 1]
    assert sent == [
        "【要你處理】PTS 部位還在：微台平倉沒有完成，還剩多單 1 口\n"
        "原因：市價 IOC 沒有全部成交，重試 3 次仍有剩餘\n"
        "請自行賣出微台 1 口（TM2611），日盤 13:45 收盤。\n"
        "2026/11/09 13:28:00",
    ]


# --- 13:45 之後才補跑：不送單，告警請使用者在夜盤處理（規格第 55 條）---


def test_run_after_the_day_session_has_closed_it_sends_nothing_and_says_use_the_night_session():
    """排程錯過 13:25，13:50 才補跑。日盤已經收盤，送出去的單不會在日盤成交；
    不送，請使用者在夜盤自己平。"""
    content = _on_file()
    broker, sent, _ = _run(at=_at("13:50"), login_error=AssertionError("收盤後不該登入"))
    assert broker.orders == []
    assert sent == [
        "【要你處理】PTS 部位還在：微台平倉沒有完成，還剩多單 3 口\n"
        "原因：出場程式 13:50 才執行，日盤 13:45 已經收盤，沒有送出平倉單\n"
        "請在夜盤自行賣出微台 3 口（TM2611）。\n"
        "2026/11/09 13:50:00",
    ]
    assert _file() == content


def test_retries_stop_when_the_day_session_closes():
    """13:44 才跑、第一次被拒：一分鐘後已經 13:45，不再重試，改請使用者在夜盤處理。"""
    _on_file()
    broker, sent, _ = _run(at=_at("13:44"), order_errors=[OrderFailed("保證金不足")] * 4)
    assert len(broker.orders) == 1
    assert sent[0].splitlines()[1:3] == [
        "原因：委託沒有送出去（最後一次：保證金不足），重試到日盤 13:45 收盤仍未完成",
        "請在夜盤自行賣出微台 3 口（TM2611）。",
    ]


@pytest.mark.parametrize("product, order_code", [("TX00AM", "TX11"), ("MTX00AM", "MTX11")],
                         ids=["大台", "小台"])
def test_tx_and_mtx_are_quiet_even_after_the_close(product, order_code):
    """大台、小台在 13:30 已經被券商強平了，補跑時也沒有事情要講。"""
    _on_file(product=product, order_code=order_code)
    broker, sent, _ = _run(at=_at("13:50"))
    assert broker.orders == []
    assert sent == []


# --- 結算日的微台：近月 13:30 停止交易，之後由交易所以最後結算價了結 ---

SETTLEMENT = date(2026, 11, 18)


def test_on_the_settlement_day_the_micro_position_is_closed_before_1330():
    _on_file(SETTLEMENT, last_trading_day="2026-11-18")
    broker, sent, _ = _run(at=_at("13:25", SETTLEMENT))
    assert _orders(broker) == [("TM2611", SELL, 3, False, SETTLEMENT)]
    assert sent == []


def test_on_the_settlement_day_after_1330_the_exchange_settles_what_is_left():
    """結算日 13:30 之後近月已經停止交易，送出去只會被拒；留著的口數由交易所以最後結算價
    現金了結，不會過夜，也不需要人做什麼。不送單、不告警。"""
    content = _on_file(SETTLEMENT, last_trading_day="2026-11-18")
    broker, sent, _ = _run(at=_at("13:35", SETTLEMENT))
    assert broker.orders == []
    assert sent == []
    assert _file(SETTLEMENT) == content


def test_on_the_settlement_day_retries_stop_at_1330():
    """13:29 第一次被拒，一分鐘後已經 13:30：近月停止交易，剩下的由交易所結算，不再重試。"""
    _on_file(SETTLEMENT, last_trading_day="2026-11-18")
    broker, sent, _ = _run(at=_at("13:29", SETTLEMENT),
                           order_errors=[OrderFailed("保證金不足")] * 4)
    assert len(broker.orders) == 1
    assert sent == []


# --- 出場單送出去但不知道成交：絕不重送，用委託序號查（規格第 50 條後半）---

_SENT_BUT_UNKNOWN = FillUnknown("30 秒內沒等到委託 2315610000001 結束",
                                order_seq="2315610000001")


def test_an_exit_order_with_no_reply_is_looked_up_and_counted_when_fully_filled():
    """平倉單送出去了，回報卻沒回來。**不再送一次**——它很可能已經成交。改用這張單的委託序號
    向券商查成交紀錄：3 口全部成交 @ 45210，就當作已經平倉，記進出場明細，不打擾人。"""
    content = _on_file()
    broker, sent, _ = _run(order_errors=[_SENT_BUT_UNKNOWN],
                           fill_queries=[FillQuery(lots=3, avg_price=45210.0)])
    assert [(o.side, o.lots) for o in broker.orders] == [(SELL, 3)]
    assert broker.queries == [("2315610000001", DAY, 3)]
    assert sent == []
    assert _file() == {**content, "exits": [
        {"order_seq": "2315610000001", "lots": 3, "price": 45210.0}]}


QUERY_ANSWERS = {
    "只成交一部分": (FillQuery(lots=2, avg_price=45210.0), "成交 2 口（委託 3 口）"),
    "查不到": (FillQuery(lots=None), "查不到這筆委託的成交"),
    "查詢失敗": (FillQuery(lots=None, failure="查詢錯誤（M999）"), "查詢失敗：查詢錯誤（M999）"),
}


@pytest.mark.parametrize("answer, said", QUERY_ANSWERS.values(), ids=QUERY_ANSWERS.keys())
def test_an_exit_order_not_confirmed_by_the_lookup_is_never_sent_again(answer, said):
    """查到的不是「全部成交」：可能還有部位、也可能已經平掉，程式分不出來。一張單都不再送，
    告警附上券商紀錄顯示的口數。部位檔記下這筆出場單不確定（重跑時也不會再送）。"""
    _on_file()
    broker, sent, _ = _run(order_errors=[_SENT_BUT_UNKNOWN], fill_queries=[answer])
    assert len(broker.orders) == 1
    known = answer.lots or 0
    assert sent == [
        "【要你處理】PTS 委託送出去了，但不知道成交幾口，出場程式不再送單\n"
        "不確定的委託：平倉賣出微台 3 口（委託序號 2315610000001），"
        f"已知至少成交 {known} 口\n"
        f"券商的成交紀錄：{said}\n"
        "部位檔記到的部位（不含這一筆）：多單 3 口\n"
        "請到券商 APP 確認帳戶，自行平掉 PTS 的微台部位（日盤 13:45 收盤）。\n"
        "2026/11/09 13:25:00",
    ]
    assert (_file()["status"], _file()["exits"]) == ("UNCERTAIN", [])
    assert _file()["uncertain"] == {"stage": "EXIT", "order_seq": "2315610000001",
                                    "requested_lots": 3, "known_filled": known}


def test_an_exit_order_without_a_sequence_cannot_be_looked_up():
    """送出成功卻沒拿到委託序號：連查都沒辦法查。不查、不重送，直接告警。"""
    _on_file()
    broker, sent, _ = _run(order_errors=[FillUnknown("委託已送出但沒有拿到委託序號")])
    assert len(broker.orders) == 1
    assert broker.queries == []
    assert sent[0].splitlines()[1:3] == [
        "不確定的委託：平倉賣出微台 3 口（委託序號 沒有拿到），已知至少成交 0 口",
        "券商的成交紀錄：沒有委託序號，無法查詢",
    ]


def test_after_a_lookup_confirms_the_fill_the_remaining_orders_are_sent():
    """23 口拆成 10、10、3。第一筆不確定、查到 10 口全部成交：確定了，照送後面兩筆。"""
    _on_file(entries=_lots(*[1] * 20, 3))
    broker, sent, _ = _run(order_errors=[_SENT_BUT_UNKNOWN],
                           fill_queries=[FillQuery(lots=10, avg_price=45210.0)])
    assert [o.lots for o in broker.orders] == [10, 10, 3]
    assert sent == []
    assert [e["lots"] for e in _file()["exits"]] == [10, 10, 3]
    assert _file()["status"] == "CONFIRMED"


def test_once_an_exit_order_is_uncertain_nothing_more_is_sent():
    """23 口：第一筆 10 口成交，第二筆不確定、查不到。第三筆 3 口也不送了——帳上剩幾口已經
    不知道。部位檔記著第一筆的出場與第二筆的不確定；告警裡「不含這一筆」的 13 口，包括
    第三筆沒送出去的 3 口。"""
    _on_file(entries=_lots(*[1] * 20, 3))
    unknown = FillUnknown("沒等到回報", order_seq="2315610000002")
    broker, sent, _ = _run(order_errors=[None, unknown], fill_queries=[FillQuery(lots=None)])
    assert [o.lots for o in broker.orders] == [10, 10]
    assert sent[0].splitlines()[3] == "部位檔記到的部位（不含這一筆）：多單 13 口"
    assert [e["lots"] for e in _file()["exits"]] == [10]
    assert _file()["uncertain"]["order_seq"] == "2315610000002"


# --- 自動下單關閉：完全不呼叫下單 API（規格第 42 條）---


def test_with_auto_order_off_a_micro_position_on_file_is_reported_not_closed():
    """模擬模式不寫部位檔，所以「開關關著卻有部位檔」代表使用者今天進場之後把它關掉了。
    關掉就是不要程式碰下單 API：不登入、不送單。但微台沒人平就過夜，一定要講。"""
    content = _on_file()
    broker, sent, _ = _run(config=_config(auto_order_enabled=False),
                           login_error=AssertionError("自動下單關閉時不該登入"))
    assert broker.orders == []
    assert sent == [
        "【要你處理】PTS 部位還在：微台平倉沒有完成，還剩多單 3 口\n"
        "原因：自動下單已關閉（order.auto_enabled: false），出場程式不送單\n"
        "請自行賣出微台 3 口（TM2611），日盤 13:45 收盤。\n"
        "2026/11/09 13:25:00",
    ]
    assert _file() == content


def test_with_auto_order_off_and_no_position_file_it_stays_quiet():
    """模擬模式的平常一天。"""
    broker, sent, _ = _run(config=_config(auto_order_enabled=False))
    assert broker.orders == []
    assert sent == []


# --- 登入失敗：重試無用，立刻告警 ---


def test_a_failed_login_is_reported_at_once_with_the_lots_to_close():
    """密碼、憑證這類問題，一分鐘後再試也一樣。13:45 收盤前還有時間讓人自己平。"""
    _on_file()
    broker, sent, time = _run(login_error=LoginFailed("群益登入失敗，代碼 1001：密碼錯誤"))
    assert broker.orders == []
    assert time.slept == []
    assert sent[0].splitlines()[1:3] == [
        "原因：群益登入失敗，代碼 1001：密碼錯誤",
        "請自行賣出微台 3 口（TM2611），日盤 13:45 收盤。",
    ]


# --- 13:20 之前被叫起來：盤中程式可能還在進場 ---


def test_run_before_the_entry_cutoff_it_does_nothing():
    """有人在盤中手動執行了出場程式。這時盤中程式還在進場：平掉的口數它不知道，下一筆成交
    就會用自己記憶體裡的部位覆寫部位檔，出場明細從此不見。13:20 進場截止之前什麼都不做。"""
    content = _on_file()
    broker, sent, _ = _run(at=_at("10:00"), login_error=AssertionError("13:20 前不該登入"))
    assert broker.orders == []
    assert sent == []
    assert _file() == content


# --- Discord 關閉（規格第 45 條）---


def test_with_discord_off_the_position_is_still_closed_and_nothing_is_sent():
    _on_file()
    broker, sent, _ = _run(config=_config(discord_enabled=False),
                           order_errors=[OrderFailed("保證金不足")] * 4)
    assert len(broker.orders) == 4
    assert sent == []


# --- 平倉成交了，部位檔卻寫不進去 ---


def test_a_close_that_cannot_be_written_still_closes_everything_and_says_so(monkeypatch):
    """23 口：寫檔一直失敗（防毒鎖住檔案）。平倉照送完——微台不能過夜，記憶體裡的口數是對的；
    最後告警：部位檔還記著 23 口沒平，**不要再執行出場程式**，它會照檔再平一次。"""
    content = _on_file(entries=_lots(*[1] * 20, 3))

    def denied(src, dst):
        raise PermissionError(13, "存取被拒")
    monkeypatch.setattr(os, "replace", denied)
    broker, sent, _ = _run()
    assert [o.lots for o in broker.orders] == [10, 10, 3]
    assert sent == [
        "【要你處理】PTS 部位檔沒記到出場程式這次的平倉\n"
        "實際：平倉 23 口，還剩多單 0 口；部位檔記的是平倉 0 口\n"
        "原因：PermissionError: [Errno 13] 存取被拒\n"
        "不要再執行出場程式：它只認部位檔，會把已經平掉的口數再平一次，開出反向新倉。\n"
        "2026/11/09 13:25:00",
    ]
    assert _file() == content


def test_an_uncertain_exit_that_cannot_be_written_warns_against_running_again(monkeypatch):
    """最危險的一種：平倉單不確定、查不到，而「不確定」這件事也寫不進部位檔。檔裡看起來是
    「3 口還沒平」，再執行一次出場程式就會再送一筆——那筆很可能是反向新倉。兩則告警：
    不確定那一筆，以及不要重跑。"""
    _on_file()

    def denied(src, dst):
        raise PermissionError(13, "存取被拒")
    monkeypatch.setattr(os, "replace", denied)
    broker, sent, _ = _run(order_errors=[_SENT_BUT_UNKNOWN], fill_queries=[FillQuery(lots=None)])
    assert len(broker.orders) == 1
    assert len(sent) == 2
    assert sent[0].startswith("【要你處理】PTS 委託送出去了，但不知道成交幾口")
    assert sent[1].splitlines()[:3] == [
        "【要你處理】PTS 部位檔沒記到出場程式這次的平倉",
        "實際：平倉 0 口，還剩多單 3 口；部位檔記的是平倉 0 口",
        "部位檔也沒記到那筆不確定的平倉委託（委託序號 2315610000001）",
    ]
    assert _file()["status"] == "CONFIRMED"


# --- 排程的進入點：程式本身出錯時也要講 ---


def test_when_the_program_itself_breaks_main_still_raises_the_alarm(tmp_path, monkeypatch):
    """出場程式是微台唯一的出場。它自己出錯中止的話，部位可能就這樣過夜，而平常沒有例行
    訊息能讓人發現——所以 `main()` 盡量發一則告警，並回 1 讓排程記錄留下痕跡。"""
    import paths
    from notifiers import discord

    env = tmp_path / ".env"
    env.write_text("CAPITAL_USER_ID=A100000000\nCAPITAL_PASSWORD=pw\n"
                   "DISCORD_WEBHOOK_URL=https://discord.invalid/api/webhooks/1/x\n",
                   encoding="utf-8")
    monkeypatch.setattr(paths, "env_path", lambda: str(env))
    monkeypatch.setattr(exit_program, "_setup_logging", lambda today: None)

    def broken(*args, **kwargs):
        raise RuntimeError("意料之外的錯誤")
    monkeypatch.setattr(exit_program, "run", broken)
    posted = []
    monkeypatch.setattr(discord, "send", lambda text, webhook: posted.append((text, webhook)))

    assert exit_program.main() == 1
    assert len(posted) == 1
    text, webhook = posted[0]
    assert webhook == "https://discord.invalid/api/webhooks/1/x"
    assert text.splitlines()[:2] == ["【要你處理】PTS 出場程式意外中止",
                                     "原因：RuntimeError: 意料之外的錯誤"]
