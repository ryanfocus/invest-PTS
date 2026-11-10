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
from day_script import (DAY, SETTLEMENT_DAY, WEBHOOK, main_env, make_config, position_content,
                        position_on_file as _file, settings_yaml, write_position_file)
from recording import RecordingNotifier
import exit_program
import heartbeat
import position_file


def _intraday_ran(day: date) -> None:
    """盤中程式那一天照常跑完了：心跳記錄寫著那一天、13:20 收工。"""
    with open(heartbeat.path(), "w", encoding="utf-8") as fh:
        json.dump({"trading_day": f"{day:%Y-%m-%d}", "last_alive": f"{day:%Y-%m-%d}T13:20:00",
                   "finished": True}, fh)


@pytest.fixture(autouse=True)
def intraday_ran_today(state_dir):
    """這裡的每一個出場情境，盤中程式今天（`DAY`）都照常跑完了。盤中程式沒跑的告警（ticket 09）
    在 test_heartbeat 測；不放這一份的話，每個情境都會多一則。別天的情境自己再寫一次。"""
    _intraday_ran(DAY)


# 今天進場用的設定：自動下單打開（部位檔只有真實下單才會寫）
LIVE = make_config(auto_order_enabled=True)


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
    exit_program.run(broker, config or LIVE, notify=notifier, clock=time.clock,
                     sleep=time.sleep)
    return broker, notifier.sent, time


def _on_file(day: date = DAY, **overrides) -> dict:
    """盤中程式今天寫下的部位檔：微台多單 3 口（兩筆委託：2 口 @ 45100、1 口 @ 45150），
    換掉 `overrides` 那幾欄。回傳內容。"""
    content = position_content(day, **{
        "entries": [{"order_seq": "2315609990001", "lots": 2, "price": 45100.0},
                    {"order_seq": "2315609990002", "lots": 1, "price": 45150.0}],
        **overrides})
    write_position_file(content, day)
    return content


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
    broker, _, _ = _run(config=make_config(auto_order_enabled=True, product="TX00AM"))
    assert _orders(broker) == [("TM2611", SELL, 3, False, DAY)]


def test_the_settings_say_micro_but_the_file_says_mtx_so_nothing_is_sent():
    """反過來：設定改成微台，今天的部位是小台。照設定檔的話，會對一個沒有部位的微台送
    賣單——開出一口空單過夜；同時小台又被券商強平。"""
    content = _on_file(product="MTX00AM", order_code="MTX11")
    broker, sent, _ = _run(config=make_config(auto_order_enabled=True, product=TMF_CODE))
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


@pytest.mark.parametrize("text", [
    '{"trading_day": "2026-11-09", "product": "TM00',
    "",
    '{"trading_day": "2026-11-09"}',
], ids=["寫到一半", "空檔", "少了欄位"])
def test_a_position_file_that_cannot_be_read_sends_nothing_and_says_so(text):
    """讀不懂就不知道帳上有沒有部位、有幾口、是哪個商品。照猜的口數平倉，平多了就是反向
    新倉；當成沒有部位，微台就過夜。所以一張單都不送，請人看帳戶；檔案原封不動。"""
    write_position_file(text)
    broker, sent, _ = _run(login_error=AssertionError("讀不懂部位檔不該登入"))
    assert broker.orders == []
    assert len(sent) == 1
    lines = sent[0].splitlines()
    assert lines[0] == "【要你處理】PTS 部位檔讀不懂，出場程式沒有平倉"
    assert lines[1].startswith("原因：")
    assert lines[2:] == [
        "PTS 今天可能還有部位：請到券商 APP 確認帳戶。"
        "微台要自行平掉（日盤 13:45 收盤；今天如果是結算日，到期的近月 13:30 就停止交易，"
        "之後由交易所以最後結算價現金了結）；大台、小台標了當沖，券商 13:30 會強平。",
        "2026/11/09 13:25:00",
    ]
    with open(position_file.path_for(DAY), encoding="utf-8") as fh:
        assert fh.read() == text


def test_a_position_file_that_cannot_be_read_after_the_close_points_to_the_night_session():
    """13:50 才補跑、部位檔又讀不懂：日盤已經收盤，要人做的事是夜盤（規格第 55 條）。
    讀不懂就不知道今天是不是結算日，兩種都講。"""
    write_position_file("")
    broker, sent, _ = _run(at=_at("13:50"))
    assert broker.orders == []
    assert sent[0].splitlines()[2] == (
        "PTS 今天可能還有部位：請到券商 APP 確認帳戶。"
        "日盤 13:45 已經收盤，微台請在夜盤自行平掉（今天如果是結算日，到期的近月已經由交易所"
        "以最後結算價現金了結，不必平）；大台、小台標了當沖，由券商 13:30 強平。")


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


UNCERTAIN_DEADLINES = {
    "一般日收盤後": (DAY, "13:50", "2026-11-18",
                "請到券商 APP 確認帳戶，在夜盤自行平掉 PTS 的微台部位（日盤 13:45 已經收盤）。"),
    "結算日 13:30 前": (date(2026, 11, 18), "13:25", "2026-11-18",
                    "請到券商 APP 確認帳戶，在 13:30 前自行平掉 PTS 的微台部位：今天是結算日，"
                    "近月 13:30 停止交易，之後還在的口數由交易所以最後結算價現金了結。"),
    "結算日 13:30 後": (date(2026, 11, 18), "13:35", "2026-11-18",
                    "今天是結算日，近月 13:30 已經停止交易，不能再平：PTS 還在的微台部位由交易所"
                    "以最後結算價現金了結。結算後請到券商 APP 確認帳戶。"),
}


@pytest.mark.parametrize("day, hhmm, last_trading_day, todo", UNCERTAIN_DEADLINES.values(),
                         ids=UNCERTAIN_DEADLINES.keys())
def test_an_uncertain_micro_position_is_reported_with_the_deadline_that_applies(
        day, hhmm, last_trading_day, todo):
    """「不確定」時要人自己平微台，期限要講對（規格第 41、55 條）：13:45 之後補跑就是夜盤；
    結算日的近月 13:30 就停止交易，13:30 之後已經不能平，由交易所了結。"""
    _intraday_ran(day)
    _on_file(day, last_trading_day=last_trading_day, status="UNCERTAIN",
             uncertain=_uncertain("ENTRY"))
    broker, sent, _ = _run(at=_at(hhmm, day))
    assert broker.orders == []
    assert sent[0].splitlines()[3] == todo


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
    寫明還剩幾口、要送什麼單，以及最後一次被拒的原因。部位檔沒有任何出場。

    部位檔還是「3 口沒平」：使用者自己賣掉之後，出場程式只要再被執行一次（手動、排程補跑），
    就照檔再賣 3 口，開出反向新倉過夜。告警要把這件事講出來。"""
    content = _on_file()
    broker, sent, time = _run(order_errors=[OrderFailed("保證金不足")] * 4)
    assert len(broker.orders) == 4
    assert time.slept == [60, 60, 60]
    assert sent == [
        "【要你處理】PTS 部位還在：微台平倉沒有完成，還剩多單 3 口\n"
        "原因：委託沒有送出去，重試 3 次仍然失敗（最後一次：保證金不足）\n"
        "請自行賣出微台 3 口（TM2611），日盤 13:45 收盤。\n"
        "自己平掉之後，不要再執行出場程式：它只認部位檔，會把這 3 口再賣出一次，開出反向新倉。\n"
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
        "自己平掉之後，不要再執行出場程式：它只認部位檔，會把這 1 口再賣出一次，開出反向新倉。\n"
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

def test_on_the_settlement_day_the_micro_position_is_closed_before_1330():
    _intraday_ran(SETTLEMENT_DAY)
    _on_file(SETTLEMENT_DAY, last_trading_day="2026-11-18")
    broker, sent, _ = _run(at=_at("13:25", SETTLEMENT_DAY))
    assert _orders(broker) == [("TM2611", SELL, 3, False, SETTLEMENT_DAY)]
    assert sent == []


def test_on_the_settlement_day_the_alert_gives_1330_as_the_deadline():
    """結算日 13:28 重試用完。近月 13:30 就停止交易：告警照一般日寫「日盤 13:45 收盤」的話，
    使用者以為還有 17 分鐘，其實只剩 2 分鐘，13:30 之後 TM2611 就不能交易了。"""
    _intraday_ran(SETTLEMENT_DAY)
    _on_file(SETTLEMENT_DAY, last_trading_day="2026-11-18")
    broker, sent, _ = _run(at=_at("13:25", SETTLEMENT_DAY),
                           order_errors=[OrderFailed("保證金不足")] * 4)
    assert len(broker.orders) == 4
    assert sent[0].splitlines()[2] == (
        "請在 13:30 前自行賣出微台 3 口（TM2611）：今天是結算日，近月 13:30 停止交易，"
        "之後還在的口數由交易所以最後結算價現金了結。")


_SETTLED_BY_EXCHANGE = (
    "TM2611 今天結算，13:30 已經停止交易，不能再平：這 3 口由交易所以最後結算價現金了結，"
    "不會留到明天。結算後請到券商 APP 確認帳戶裡已經沒有這 3 口。")


def test_on_the_settlement_day_after_1330_the_exchange_settles_what_is_left():
    """結算日 13:30 之後近月已經停止交易，送出去只會被拒，所以不送。PTS 的微台還是沒有平掉
    （規格第 34、38 條：最後仍有剩餘就告警）：講明還剩幾口、為什麼不必也不能自己平，
    以及結算後要確認什麼——損益照最後結算價算，和回測有落差。部位檔原封不動。"""
    _intraday_ran(SETTLEMENT_DAY)
    content = _on_file(SETTLEMENT_DAY, last_trading_day="2026-11-18")
    broker, sent, _ = _run(at=_at("13:35", SETTLEMENT_DAY),
                           login_error=AssertionError("近月停止交易後不該登入"))
    assert broker.orders == []
    assert sent == [
        "【要你處理】PTS 部位還在：微台平倉沒有完成，還剩多單 3 口\n"
        "原因：出場程式 13:35 才執行，今天結算的近月 13:30 已經停止交易，沒有送出平倉單\n"
        f"{_SETTLED_BY_EXCHANGE}\n"
        "2026/11/18 13:35:00",
    ]
    assert _file(SETTLEMENT_DAY) == content


def test_on_the_settlement_day_retries_stop_at_1330():
    """13:29 第一次被拒，一分鐘後已經 13:30：近月停止交易，不再重試。沒平掉的照樣告警——
    同一個失敗，13:25 開始的話 13:28 會告警，晚兩分鐘開始就不講，是說不通的。"""
    _intraday_ran(SETTLEMENT_DAY)
    _on_file(SETTLEMENT_DAY, last_trading_day="2026-11-18")
    broker, sent, _ = _run(at=_at("13:29", SETTLEMENT_DAY),
                           order_errors=[OrderFailed("保證金不足")] * 4)
    assert len(broker.orders) == 1
    assert sent[0].splitlines()[1:4] == [
        "原因：委託沒有送出去（最後一次：保證金不足），重試到近月 13:30 停止交易仍未完成",
        _SETTLED_BY_EXCHANGE,
        "2026/11/18 13:30:00",
    ]


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


class _Killed(BaseException):
    """程式在這一刻被關掉（工作管理員、關機、排程逾時）。不是 Exception：程式裡沒有人接得住。"""


class _KilledDuringLookup(FakeBroker):
    """成交查詢至少要 5.5 秒（兩段之間要等）。程式就在查詢途中被關掉。"""

    def query_fills(self, order_seq, trading_day, requested_lots):
        self.queries.append((order_seq, trading_day, requested_lots))
        raise _Killed()


def test_killed_while_looking_up_an_exit_order_it_is_never_sent_again():
    """平倉單不確定、正在查，程式被關掉，之後又被叫起來（手動或排程）。重跑時看到的必須是
    「這筆出場單不確定」——要是部位檔還是「3 口沒平」，它會再送一筆賣 3 口，而前一筆很可能
    已經成交，多賣的就是反向新倉（規格第 50 條）。所以「不確定」要在查詢**之前**就寫進檔。"""
    _on_file()
    first = _KilledDuringLookup(fill_price=45200.0, order_errors=[_SENT_BUT_UNKNOWN])
    with pytest.raises(_Killed):
        exit_program.run(first, LIVE, notify=RecordingNotifier(),
                         clock=lambda: _at("13:25"), sleep=lambda seconds: None)
    assert len(first.orders) == 1

    again, sent, _ = _run(at=_at("13:27"))
    assert again.orders == []
    assert sent[0].splitlines()[:2] == [
        "【要你處理】PTS 委託送出去了，但不知道成交幾口，出場程式不再送單",
        "不確定的委託：平倉賣出微台 3 口（委託序號 2315610000001），已知至少成交 0 口",
    ]


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
    broker, sent, _ = _run(config=make_config(),
                           login_error=AssertionError("自動下單關閉時不該登入"))
    assert broker.orders == []
    assert sent == [
        "【要你處理】PTS 部位還在：微台平倉沒有完成，還剩多單 3 口\n"
        "原因：自動下單已關閉（order.auto_enabled: false），出場程式不送單\n"
        "請自行賣出微台 3 口（TM2611），日盤 13:45 收盤。\n"
        "自己平掉之後，不要再執行出場程式：它只認部位檔，會把這 3 口再賣出一次，開出反向新倉。\n"
        "2026/11/09 13:25:00",
    ]
    assert _file() == content


def test_with_auto_order_off_and_no_position_file_it_stays_quiet():
    """模擬模式的平常一天。"""
    broker, sent, _ = _run(config=make_config())
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


# --- 出場程式那天沒有跑完，之後才被叫醒（規格第 31、55 條）---
#
# 13:25 電腦關機、睡著或出場程式中途停了。排程「錯過就補跑」，但補跑可能落在隔天早上、
# 隔天下午，或是週末——那時系統日期已經不是部位的那一天。微台過夜是這個策略唯一不能接受
# 的結果，而平常沒有例行訊息能讓人發現它。

TUESDAY = date(2026, 11, 10)
SATURDAY = date(2026, 11, 14)

LATER = {
    "隔天早上（13:20 之前）": datetime(2026, 11, 10, 8, 10),
    "隔天下午（13:45 之後）": datetime(2026, 11, 10, 14, 0),
    "週末": datetime(2026, 11, 14, 10, 0),
}


def _monday_missed(at: datetime) -> str:
    return (
        "【要你處理】PTS 部位還在：11/09 的微台沒有平倉，還剩多單 3 口\n"
        "原因：出場程式 11/09 沒有跑完（電腦當時關機、睡著，或出場程式中途停了），"
        "這一次執行才發現。那天的日盤已經收盤，不送單\n"
        "請先到券商 APP 確認帳戶（你可能已經自己平掉了），還在的話請自行賣出微台 3 口（TM2611）。"
        "TM2611 的最後交易日是 11/18：過了那天 13:30 還在的口數由交易所以最後結算價現金了結，"
        "不必再平。\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


@pytest.mark.parametrize("at", LATER.values(), ids=LATER.keys())
def test_a_micro_position_left_on_a_day_the_exit_program_missed_is_reported_later(at):
    """週一的部位檔記著微台多單 3 口、一口都沒平，週一的出場程式沒有跑。之後不管哪天、幾點
    被叫醒，都要講週一那 3 口——只看「今天」的部位檔的話，隔天早上是「進場截止之前」、
    週末是「非交易日」、隔天下午是「今天沒有部位」，三種都安靜地結束。
    那一天的日盤早就收了，不送單（規格第 55 條）。"""
    content = _on_file()
    broker, sent, _ = _run(at=at, login_error=AssertionError("前一天的部位不送單，不該登入"))
    assert broker.orders == []
    assert [m for m in sent if "11/09" in m] == [_monday_missed(at)]
    assert _file() == content


def test_a_missed_day_is_reported_once_not_on_every_later_run():
    """週二 08:10 補跑時講過週一那 3 口了。週二 13:25 照常叫醒、之後每天也都會叫醒：部位檔
    永遠記著那 3 口（使用者自己平掉的話，出場程式不會知道），每天講一次就是每天一則假警報。"""
    _on_file()
    _, first, _ = _run(at=datetime(2026, 11, 10, 8, 10))
    _intraday_ran(TUESDAY)
    _, later, _ = _run(at=_at("13:25", TUESDAY))
    _, after_that, _ = _run(at=_at("13:25", date(2026, 11, 11)))

    assert len([m for m in first if "11/09" in m]) == 1
    assert [m for m in later + after_that if "11/09" in m] == []


def test_a_day_whose_exit_program_already_raised_the_alarm_is_not_reported_again():
    """週一 13:25 出場程式照常跑了：平倉被拒、重試用完，已經告警「部位還在」。部位檔照樣記著
    那 3 口沒平——隔天再講一次，就是同一件事的第二則告警。"""
    _on_file()
    _, monday, _ = _run(order_errors=[OrderFailed("保證金不足")] * 4)
    assert len(monday) == 1
    _intraday_ran(TUESDAY)
    _, tuesday, _ = _run(at=_at("13:25", TUESDAY))
    assert tuesday == []


def test_a_missed_day_whose_position_file_cannot_be_read_is_reported_and_today_still_exits():
    """週一的部位檔讀不懂、週一的出場程式又沒跑：不知道那天有沒有微台，照實講（規格第 38 條）。
    今天的出場照常——一份舊檔讀不懂，不能讓今天的微台也跟著過夜。"""
    write_position_file("{寫到一半")
    _intraday_ran(TUESDAY)
    _on_file(TUESDAY, entries=_lots(1))

    broker, sent, _ = _run(at=_at("13:25", TUESDAY))

    assert [(o.side, o.lots, o.trading_day) for o in broker.orders] == [(SELL, 1, TUESDAY)]
    assert [m.splitlines()[0] for m in sent] == ["【要你處理】PTS 11/09 的部位檔讀不懂，出場程式沒有平倉"]
    assert sent[0].splitlines()[2] == (
        "PTS 那天可能留下微台部位：請到券商 APP 確認帳戶，還在的話請自行平掉。大台、小台標了當沖，"
        "那天 13:30 已經由券商強平。")


def test_an_uncertain_micro_position_on_a_missed_day_is_reported():
    """週一有一筆進場單送出去了、不知道成交幾口，週一的出場程式又沒跑。帳上可能比檔裡多一口
    （規格第 50 條：「委託送出去了，但…」），一樣要講。"""
    _on_file(status="UNCERTAIN", uncertain={"stage": "ENTRY", "order_seq": "2315609990003",
                                            "requested_lots": 1, "known_filled": 0})
    _, sent, _ = _run(at=datetime(2026, 11, 10, 8, 10))
    assert sent == [
        "【要你處理】PTS 委託送出去了，但不知道成交幾口：11/09 的微台沒有平倉\n"
        "不確定的委託：進場買進微台 1 口（委託序號 2315609990003），已知至少成交 0 口\n"
        "部位檔記到的部位（不含這一筆）：多單 3 口\n"
        "原因：出場程式 11/09 沒有跑完（電腦當時關機、睡著，或出場程式中途停了），"
        "這一次執行才發現。那天的日盤已經收盤，不送單\n"
        "請到券商 APP 確認帳戶，自行平掉 PTS 還在的微台部位。TM2611 的最後交易日是 11/18：過了那天 "
        "13:30 還在的口數由交易所以最後結算價現金了結，不必再平。\n"
        "2026/11/10 08:10:00",
    ]


@pytest.mark.parametrize("product, order_code", [("TX00AM", "TX11"), ("MTX00AM", "MTX11")],
                         ids=["大台", "小台"])
def test_tx_and_mtx_on_a_missed_day_were_closed_by_the_broker_and_need_no_word(product,
                                                                             order_code):
    """大台、小台標了當沖，那天 13:30 券商就強平了，跟出場程式有沒有跑無關（規格第 30 條）。"""
    _on_file(product=product, order_code=order_code)
    _, sent, _ = _run(at=datetime(2026, 11, 10, 8, 10))
    assert sent == []


# --- Discord 關閉（規格第 45 條）---


def test_with_discord_off_the_position_is_still_closed_and_nothing_is_sent():
    _on_file()
    broker, sent, _ = _run(config=make_config(auto_order_enabled=True, discord_enabled=False),
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


@pytest.mark.parametrize("answer, left", [(FillQuery(lots=None), "0～3"),
                                          (FillQuery(lots=2, avg_price=45210.0), "0～1")],
                         ids=["查不到", "查到成交 2 口"])
def test_an_uncertain_exit_that_cannot_be_written_warns_against_running_again(
        answer, left, monkeypatch):
    """最危險的一種：平倉單不確定、查不到，而「不確定」這件事也寫不進部位檔。檔裡看起來是
    「3 口還沒平」，再執行一次出場程式就會再送一筆——那筆很可能是反向新倉。兩則告警：
    不確定那一筆，以及不要重跑。

    第二則是最新的那一則，不能把不確定的平倉講成「確定平倉 0 口、還剩 3 口」：那張單可能
    已經全部成交，使用者照「還剩 3 口」去賣，就開出 3 口反向新倉。帳上剩幾口是一個範圍。"""
    _on_file()

    def denied(src, dst):
        raise PermissionError(13, "存取被拒")
    monkeypatch.setattr(os, "replace", denied)
    broker, sent, _ = _run(order_errors=[_SENT_BUT_UNKNOWN], fill_queries=[answer])
    assert len(broker.orders) == 1
    assert len(sent) == 2
    assert sent[0].startswith("【要你處理】PTS 委託送出去了，但不知道成交幾口")
    assert sent[1].splitlines()[:3] == [
        "【要你處理】PTS 部位檔沒記到出場程式這次的平倉",
        "實際：確定的平倉 0 口，另有一筆平倉賣出微台 3 口不知道成交幾口，"
        f"帳上還剩多單 {left} 口；部位檔記的是平倉 0 口",
        "部位檔也沒記到那筆不確定的平倉委託（委託序號 2315610000001）",
    ]
    assert _file()["status"] == "CONFIRMED"


# --- 排程的進入點：程式本身出錯時也要講 ---
#
# `main()` 從 `.env` 建立連線、讀設定檔；測試換掉的只有 broker（`connect`）與時間（`clock`、
# `sleep`），Discord 換成記錄器（`main_env`）。


class _BreaksAt(FakeBroker):
    """程式本身出錯：登入時拋出一個 broker 契約以外的例外。`time` 是出場程式的時鐘，出錯的那一刻
    它走到 `at`（例如登入卡了很久）。"""

    def __init__(self, time: "_Time", at: datetime, **options):
        super().__init__(**options)
        self._time, self._at = time, at

    def login(self) -> None:
        self._time.now = self._at
        raise RuntimeError("意料之外的錯誤")


def _main(*, at: datetime | None = None, broker: FakeBroker | None = None,
          time: "_Time | None" = None, **broker_options) -> tuple[int, list[FakeBroker]]:
    """排程叫醒出場程式（`main()`）：13:25（或 `at`），broker 是 `broker` 或照 `broker_options`
    新建的假 broker。回傳 (main 的結束代碼, main 建立的 broker)。"""
    time = time or _Time(at or _at("13:25"))
    broker_options.setdefault("fill_price", 45200.0)
    connected: list[FakeBroker] = []

    def connect(secrets) -> FakeBroker:
        connected.append(broker or FakeBroker(**broker_options))
        return connected[-1]
    return exit_program.main(connect=connect, clock=time.clock, sleep=time.sleep), connected


def test_when_the_program_itself_breaks_main_still_raises_the_alarm(tmp_path, monkeypatch):
    """出場程式是微台唯一的出場。它自己出錯中止的話，部位可能就這樣過夜，而平常沒有例行
    訊息能讓人發現——所以 `main()` 盡量發一則告警，並回 1 讓排程記錄留下痕跡。"""
    posted = main_env(tmp_path, monkeypatch, settings_text=_settings())
    _on_file()
    time = _Time(_at("13:25"))

    code, _ = _main(time=time, broker=_BreaksAt(time, _at("13:25")))

    assert code == 1
    assert posted.webhooks == [WEBHOOK]
    assert posted.sent[0].splitlines()[:2] == ["【要你處理】PTS 出場程式意外中止",
                                              "原因：RuntimeError: 意料之外的錯誤"]


CRASH_DEADLINES = {
    "日盤收盤前": ("13:26", [
        "今天 PTS 的微台部位可能沒有平完：請到券商 APP 確認帳戶。微台要自行平掉（日盤 13:45 "
        "收盤；今天如果是結算日，到期的近月 13:30 就停止交易，之後由交易所以最後結算價現金"
        "了結）。大台、小台由券商 13:30 當沖強平。",
        "自己平掉之後，不要再執行出場程式：它只認部位檔，會把你平掉的口數再平一次，開出反向新倉。",
    ]),
    "日盤收盤後": ("13:50", [
        "今天 PTS 的微台部位可能沒有平完：請到券商 APP 確認帳戶。日盤 13:45 已經收盤，"
        "微台請在夜盤自行平掉（今天如果是結算日，到期的近月已經由交易所以最後結算價現金"
        "了結，不必平）。大台、小台由券商 13:30 當沖強平。",
    ]),
}


@pytest.mark.parametrize("hhmm, todo", CRASH_DEADLINES.values(), ids=CRASH_DEADLINES.keys())
def test_when_the_program_breaks_the_alarm_gives_the_deadline_that_applies(
        hhmm, todo, tmp_path, monkeypatch):
    """出場程式當掉時不知道部位檔裡是什麼，也就不知道今天是不是結算日：兩種期限都講；
    13:45 之後才當掉，要人做的是夜盤（規格第 41、55 條）。收盤前還要講不要重跑：
    使用者自己平掉之後再執行一次，它會照部位檔再平一次。
    13:25 起跑，登入卡到 `hhmm` 才出錯。"""
    posted = main_env(tmp_path, monkeypatch, settings_text=_settings())
    _on_file()
    time = _Time(_at("13:25"))

    code, _ = _main(time=time, broker=_BreaksAt(time, _at(hhmm)))

    assert code == 1
    assert posted.sent[0].splitlines()[2:] == [*todo, f"2026/11/09 {hhmm}:00"]


# --- 設定檔寫錯：出場程式不送單，但不能安靜（規格第 43、47 條）---


def _settings(**values: str) -> str:
    """今天的設定檔：自動下單打開（今天進場了），換掉 `values` 那幾個值。"""
    return settings_yaml(**{"auto": "true", **values})


BROKEN_SETTINGS = {
    "商品打錯": (_settings(product="TMX"),
             "order.product 必須是 TX、MTX、TMF 其中之一，目前是 'TMX'"),
    "YAML 壞掉": (_settings() + "order: [\n", None),
}


@pytest.mark.parametrize("settings_text, error", BROKEN_SETTINGS.values(),
                         ids=BROKEN_SETTINGS.keys())
def test_settings_that_cannot_be_read_still_raise_the_alarm_for_the_micro_position(
        settings_text, error, tmp_path, monkeypatch):
    """使用者盤中改設定時打錯字（規格第 43 條預期盤中會改）。出場程式讀不懂設定，就不知道
    自動下單是不是開著，一張單都不送（規格第 47 條：一啟動就擋下來）——但微台沒人平就過夜，
    所以照樣讀部位檔，告警「部位還在」並講明原因。回 1，排程記錄留下痕跡。"""
    posted = main_env(tmp_path, monkeypatch, settings_text=settings_text)
    content = _on_file()

    code, brokers = _main(login_error=AssertionError("設定有錯不該登入"))

    assert code == 1
    assert brokers[0].orders == []
    lines = posted.sent[0].splitlines()
    assert lines[0] == "【要你處理】PTS 部位還在：微台平倉沒有完成，還剩多單 3 口"
    assert lines[1].startswith("原因：設定檔有錯，出場程式不送單：")
    if error:
        assert lines[1].endswith(error)
    assert lines[2] == "請自行賣出微台 3 口（TM2611），日盤 13:45 收盤。"
    assert _file() == content


def test_settings_that_cannot_be_read_without_a_position_stay_quiet(tmp_path, monkeypatch):
    """沒有部位的日子不發訊息（規格第 39 條）：設定錯了照樣回 1，留在排程記錄與 log。"""
    posted = main_env(tmp_path, monkeypatch, settings_text=_settings(product="TMX"))
    code, _ = _main(login_error=AssertionError("沒有部位不該登入"))
    assert code == 1
    assert posted.sent == []


def test_settings_that_cannot_be_read_but_say_discord_is_off_send_nothing(tmp_path, monkeypatch):
    """設定檔有錯，但 Discord 開關本身讀得到、寫的是 false：使用者明白關掉了，照樣不發
    （規格第 45 條）。開關本身讀不到時才當成開著——那時不講，微台就安靜地過夜。"""
    posted = main_env(tmp_path, monkeypatch,
                      settings_text=_settings(product="TMX", discord="false"))
    _on_file()
    code, brokers = _main(login_error=AssertionError("設定有錯不該登入"))
    assert code == 1
    assert brokers[0].orders == []
    assert posted.sent == []
