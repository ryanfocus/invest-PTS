"""真實下單：送單、部位檔、成交通知 —— 從最上層打進去（ticket 06）。

自動下單打開時，餵一個交易日的劇本給 `intraday.run`，斷言三種外部效果：
**送出了哪些委託**（假 broker 的紀錄）、**發了哪些訊息**（記錄器）、**部位檔寫了什麼**
（暫存資料夾裡的真實檔案，conftest 已經把部位檔的目錄導過去）。

劇本的慣例同 test_early_entry：一根 K 棒在下一分鐘的第一筆成交時結束，策略在那時判斷；
假 broker 預設全部成交在交易商品最後一筆成交的價格。
"""

import json
import os
from datetime import date, timedelta

import pytest

from broker import BUY, MTX_CODE, SELL, TMF_CODE, TX_CODE, ContractInfo
from broker.capital import CapitalBroker
from broker.fake import FakeBroker
from day_script import (DAY, PREVIOUS_DAY, at, calm_history, clock, minute, rally,
                        restarted_at, trade, until)
from fake_skcom import TMF_INDEX, TX_INDEX, FakeSkcom, reply_row
from recording import RecordingNotifier
import intraday
import position_file
import settings

CONTRACTS = {
    TX_CODE: ContractInfo(TX_CODE, date(2026, 11, 18), "TX11"),
    MTX_CODE: ContractInfo(MTX_CODE, date(2026, 11, 18), "MTX11"),
    TMF_CODE: ContractInfo(TMF_CODE, date(2026, 11, 18), "TM2611"),
}


def _config(**overrides) -> settings.Config:
    values = dict(auto_order_enabled=True, product=TMF_CODE, lots_per_entry=1,
                  max_position=20, discord_enabled=True)
    values.update(overrides)
    return settings.Config(**values)


def _run(events, *, config=None, contracts=CONTRACTS, **broker_options):
    """跑一天，回傳 (broker, 發出的訊息)。前一個交易日是平淡的一天、收 45000。"""
    broker = FakeBroker(contracts=contracts, events=events,
                        minute_bars={PREVIOUS_DAY: calm_history(45000)}, **broker_options)
    notifier = RecordingNotifier()
    intraday.run(broker, config or _config(), today=DAY, notify=notifier)
    return broker, notifier.sent


def _file(day: date = DAY) -> dict:
    """部位檔的原始內容。格式是和出場程式、和人之間的約定，所以逐字斷言。"""
    with open(position_file.path_for(day), encoding="utf-8") as fh:
        return json.load(fh)


def test_a_strong_open_sends_a_market_order_writes_the_fill_and_reports_it():
    """開高、紅K、筆差 1500、RSI 被跳空推過 70：08:50 判斷 08:49 那根，條件 1 成立。
    送出微台近月的買單 1 口（微台不標當沖），成交在 45150；部位檔記下這筆委託的序號、
    口數、價格；Discord 是真的成交訊息，沒有「模擬」。"""
    broker, sent = _run(rally("08:45", 5) + [trade(at("08:50:00"), 45150)])

    assert [(o.contract.order_code, o.side, o.lots, o.day_trade, o.trading_day)
            for o in broker.orders] == [("TM2611", BUY, 1, False, DAY)]
    assert sent == [
        "PTS 做多：買進微台 1 口 @ 45150\n"
        "觸發：條件 1（早盤做多）\n"
        "累計：多單 1 口，均價 45150\n"
        "2026/11/09 08:50:00",
    ]
    assert _file() == {
        "trading_day": "2026-11-09",
        "product": "TM0000AM",
        "order_code": "TM2611",
        "contract_month": "202611",
        "direction": "LONG",
        "last_trading_day": "2026-11-18",
        "status": "CONFIRMED",
        "entries": [{"order_seq": "2315610000001", "lots": 1, "price": 45150.0}],
        "exits": [],
        "uncertain": None,
    }


# --- 自動下單關閉：零呼叫（規格第 42 條）---


def test_with_auto_order_off_the_same_day_sends_nothing_and_writes_no_position_file():
    """同一個會進場的早上，開關關著：下單函式一次都沒被呼叫，部位檔不存在——出場程式
    看的是部位檔，模擬的口數寫進去，13:25 它就會去平一個不存在的部位。只有「模擬」訊息。"""
    broker, sent = _run(rally("08:45", 5) + [trade(at("08:50:00"), 45150)],
                        config=_config(auto_order_enabled=False))
    assert broker.orders == []
    assert not os.path.exists(position_file.path_for(DAY))
    assert sent[0].splitlines()[0] == "【模擬】PTS 做多：買進微台 1 口 @ 45150"


# --- 委託參數：市價 IOC、倉別自動、當沖註記依商品（規格第 27、28 條）---


@pytest.mark.parametrize("product, order_code, day_trade, name", [
    (TX_CODE, "TX11", True, "大台"),
    (MTX_CODE, "MTX11", True, "小台"),
    (TMF_CODE, "TM2611", False, "微台"),
])
def test_tx_and_mtx_entries_are_marked_day_trade_and_tmf_entries_are_not(
        product, order_code, day_trade, name):
    """大台、小台標當沖（保證金減半才放得下 20 口，由券商 13:30 強平）；微台不能標，
    由出場程式 13:25 平倉（ADR-0002）。下單代碼是商品清單裡指名月份的那個。"""
    broker, sent = _run(rally("08:45", 5) + [trade(at("08:50:00"), 45150)],
                        config=_config(product=product))
    assert [(o.contract.order_code, o.day_trade) for o in broker.orders] == [(order_code, day_trade)]
    assert sent[0].splitlines()[0] == f"PTS 做多：買進{name} 1 口 @ 45150"
    assert (_file()["product"], _file()["order_code"]) == (product, order_code)


def test_a_weak_open_sells_and_the_file_says_short():
    events = rally("08:45", 5, open_=44900, step=-10, bichai=-1500) + [trade(at("08:50:00"), 44840)]
    broker, sent = _run(events)
    assert [(o.side, o.lots) for o in broker.orders] == [(SELL, 1)]
    assert sent[0].splitlines()[:3] == ["PTS 做空：賣出微台 1 口 @ 44850",
                                        "觸發：條件 2（早盤做空）",
                                        "累計：空單 1 口，均價 44850"]
    assert _file()["direction"] == "SHORT"


# --- 近月；結算日交易當天到期的合約（規格第 9 條）---


def test_on_the_settlement_day_it_trades_the_contract_expiring_that_day():
    """結算日當天，近月仍是當天到期的合約，13:30 才停止交易。商品清單的近月就是它，
    照樣送單；部位檔記下它的最後交易日，出場程式靠它認出今天是結算日。"""
    expiring = {TMF_CODE: ContractInfo(TMF_CODE, DAY, "TM2611")}
    broker, sent = _run(rally("08:45", 5) + [trade(at("08:50:00"), 45150)], contracts=expiring)
    assert [o.contract.order_code for o in broker.orders] == ["TM2611"]
    assert len(sent) == 1
    assert (_file()["contract_month"], _file()["last_trading_day"]) == ("202611", "2026-11-09")


# --- 記實際成交（規格第 29 條）---


def test_a_partial_fill_is_recorded_and_reported_as_the_lots_actually_filled():
    """每次 3 口，IOC 只成交 1 口：部位檔、訊息、之後的加碼上限都照 1 口算。"""
    broker, sent = _run(rally("08:45", 5) + [trade(at("08:50:00"), 45150)],
                        config=_config(lots_per_entry=3), fills=[1])
    assert [o.lots for o in broker.orders] == [3]
    assert _file()["entries"] == [{"order_seq": "2315610000001", "lots": 1, "price": 45150.0}]
    assert sent[0].splitlines()[0] == "PTS 做多：買進微台 1 口 @ 45150"
    assert sent[0].splitlines()[2] == "累計：多單 1 口，均價 45150"


def test_an_order_that_fills_nothing_changes_nothing_and_the_next_minute_tries_again():
    """市價 IOC 當下沒有對手價：0 口，委託結束。沒有成交就沒有訊息、不寫部位檔；
    條件下一分鐘仍然成立就再送一次。"""
    broker, sent = _run(rally("08:45", 6) + [trade(at("08:51:00"), 45160)], fills=[0, 1])
    assert len(broker.orders) == 2
    assert sent == [
        "PTS 做多：買進微台 1 口 @ 45160\n"
        "觸發：條件 1（早盤做多）\n"
        "累計：多單 1 口，均價 45160\n"
        "2026/11/09 08:51:00",
    ]
    assert _file()["entries"] == [{"order_seq": "2315610000002", "lots": 1, "price": 45160.0}]


def test_an_order_that_fills_nothing_on_the_only_chance_leaves_no_position_file():
    """「確定沒有部位」就是沒有部位檔（規格「部位檔」一節）。"""
    broker, sent = _run(rally("08:45", 5) + [trade(at("08:50:00"), 45150)], fills=[0])
    assert len(broker.orders) == 1
    assert sent == []
    assert not os.path.exists(position_file.path_for(DAY))


def test_every_order_is_kept_with_its_own_sequence_and_the_average_comes_from_them():
    """兩口成交在 45150、45160（08:51 那一筆只成交 2 口裡的 1 口）。均價由明細算：
    (45150 × 2 + 45160 × 1) ÷ 3 = 45153.33。每一則訊息都帶累計，看最新的一則就知道部位。"""
    broker, sent = _run(rally("08:45", 6) + [trade(at("08:51:00"), 45160)],
                        config=_config(lots_per_entry=2), fills=[2, 1])
    assert _file()["entries"] == [
        {"order_seq": "2315610000001", "lots": 2, "price": 45150.0},
        {"order_seq": "2315610000002", "lots": 1, "price": 45160.0},
    ]
    assert sent[-1] == ("PTS 做多：買進微台 1 口 @ 45160\n"
                        "觸發：條件 1（早盤做多）\n"
                        "累計：多單 3 口，均價 45153.33\n"
                        "2026/11/09 08:51:00")


def test_the_cap_counts_the_lots_actually_filled():
    """加碼上限 3、每次 2 口。08:50 只成交 1 口 → 08:51 還有 2 口的空間，送 2 口；
    滿了之後就不再送。照委託口數算的話，08:51 只會送 1 口。"""
    broker, _ = _run(rally("08:45", 8) + [trade(at("08:53:00"), 45180)],
                     config=_config(lots_per_entry=2, max_position=3), fills=[1, 2])
    assert [o.lots for o in broker.orders] == [2, 2]
    assert sum(e["lots"] for e in _file()["entries"]) == 3


def test_a_touched_breakout_sends_a_real_order_like_any_other_entry():
    """突破進場被觸發時，和立刻進場走同一條路：送出市價單、寫進部位檔、發真的成交訊息
    （帶觸發價、沒有「模擬」）。設定突破進場本身不送單。

    劇本同 test_chase_entry 的突破進場：08:50 條件 1 在 45150 進一口；10:00 那根筆差
    500 → 1600、最高 45180，10:01 設突破進場（觸發價 45180），10:01 那根沒碰到；
    10:02:30 大台成交在 45180 碰到了，成交在交易商品最新的 45175。"""
    events = (rally("08:45", 5) + rally("08:50", 70, open_=45150, step=0, bichai=500)
              + minute("10:00", 45150, 45180, bichai=1600)
              + minute("10:01", 45170, 45175, bichai=1600)
              + minute("10:02", 45175, 45180, bichai=1600)
              + [trade(at("10:03:00"), 45185)])
    broker, sent = _run(events)

    assert [(o.side, o.lots) for o in broker.orders] == [(BUY, 1), (BUY, 1)]
    assert [(e["lots"], e["price"]) for e in _file()["entries"]] == [(1, 45150.0), (1, 45175.0)]
    assert sent[1:] == ["PTS 做多：買進微台 1 口 @ 45175\n"
                        "觸發：條件 3（追多加碼），突破進場（觸發價 45180）\n"
                        "累計：多單 2 口，均價 45162.5\n"
                        "2026/11/09 10:02:30"]


# --- 成交之後的下一件事就是寫檔（規格第 52 條）---


class _SnapshotNotifier(RecordingNotifier):
    """收到訊息的那一刻，順便看一眼部位檔裡有幾口。"""

    def __init__(self):
        super().__init__()
        self.lots_on_file: list[int] = []

    def __call__(self, text: str) -> bool:
        self.lots_on_file.append(sum(e["lots"] for e in _file()["entries"]))
        return super().__call__(text)


def test_the_fill_is_on_file_before_the_message_goes_out():
    """發 Discord 會失敗、會卡住，程式也可能在那時被關掉。成交訊息送出時，那一筆成交
    必須已經在部位檔裡——出場程式不看 Discord。"""
    broker = FakeBroker(contracts=CONTRACTS,
                        events=rally("08:45", 6) + [trade(at("08:51:00"), 45160)],
                        minute_bars={PREVIOUS_DAY: calm_history(45000)})
    notifier = _SnapshotNotifier()
    intraday.run(broker, _config(), today=DAY, notify=notifier)
    assert notifier.lots_on_file == [1, 2]


def _locked(monkeypatch) -> None:
    """置換那一步失敗：Windows 上防毒或索引程式剛好開著正式檔時，`os.replace` 就是這樣。"""
    def denied(src, dst):
        raise PermissionError(13, "存取被拒")
    monkeypatch.setattr(os, "replace", denied)


def test_a_fill_that_cannot_be_written_to_the_position_file_is_reported_and_ends_entries(
        monkeypatch):
    """單已經成交，寫部位檔卻失敗。部位檔是出場程式唯一看的東西：沒有這一口的話，13:25
    什麼都不平，微台就帶著部位過夜。所以立刻發「部位還在」的告警，講明這次成交了什麼、
    要人自己平掉；之後的部位檔和帳上對不上，加碼上限與部位不虧都算不準，今天不再進場
    （08:51 條件仍然成立，也不送）。盤中程式不因此中止：原始行情照樣記到收工。"""
    _locked(monkeypatch)
    broker = FakeBroker(contracts=CONTRACTS,
                        events=rally("08:45", 6) + [trade(at("08:51:00"), 45160)],
                        minute_bars={PREVIOUS_DAY: calm_history(45000)})
    notifier = RecordingNotifier()
    outcome = intraday.run(broker, _config(), today=DAY, notify=notifier)

    assert [(o.side, o.lots) for o in broker.orders] == [(BUY, 1)]
    assert notifier.sent == [
        "【要你處理】PTS 部位還在，部位檔卻沒記到這次成交\n"
        "這次：買進微台 1 口 @ 45150（委託序號 2315610000001）\n"
        "帳上 PTS 共多單 1 口，部位檔只記到 0 口\n"
        "原因：PermissionError: [Errno 13] 存取被拒\n"
        "出場程式和盤中程式都只認部位檔，不知道這 1 口：請到券商 APP 確認帳戶，"
        "自行平掉這 1 口。盤中程式今天不再進場。\n"
        "2026/11/09 08:50:00",
    ]
    assert outcome.ending is intraday.Ending.STREAM_ENDED
    assert not os.path.exists(position_file.path_for(DAY))


def test_when_the_write_fails_after_earlier_fills_the_alert_says_how_many_lots_are_on_file(
        monkeypatch):
    """部位檔已經有今天稍早的 2 口：出場程式會平那 2 口，要人自己平的只有寫不進去的這 1 口。
    檔案維持寫壞之前的樣子。"""
    content = _on_file()
    _locked(monkeypatch)
    broker, sent = _run(rally("08:45", 6) + [trade(at("08:51:00"), 45160)])
    assert len(broker.orders) == 1
    assert sent[0].splitlines()[1:3] == ["這次：買進小台 1 口 @ 45150（委託序號 2315610000001）",
                                         "帳上 PTS 共多單 3 口，部位檔只記到 2 口"]
    assert _file() == content


def test_after_a_fill_that_could_not_be_written_a_restart_enters_nothing(monkeypatch):
    """成交的 1 口寫不進部位檔，檔裡仍是稍早的 2 口；之後盤中程式重啟。重啟後只看得到
    檔裡的 2 口，照常加碼的話，加碼上限少算 1 口、部位不虧也算在錯的均價上——而告警
    已經說了「盤中程式今天不再進場」。"""
    content = _on_file()
    replace = os.replace
    _locked(monkeypatch)
    morning = rally("08:45", 8) + [trade(at("08:53:00"), 45180)]   # 條件 1 每分鐘成立
    _run(until(morning, at("08:50:40")))
    # 重啟時檔案已經不鎖了。⚠️ 不用 monkeypatch.undo()：它連 conftest 導開的資料夾也還原
    monkeypatch.setattr(os, "replace", replace)

    broker, sent = _run(restarted_at(morning, at("08:51:10")))

    assert broker.orders == []
    assert sent == []
    assert _file() == content


def test_the_fill_is_on_file_even_with_discord_switched_off():
    _, sent = _run(rally("08:45", 5) + [trade(at("08:50:00"), 45150)],
                   config=_config(discord_enabled=False))
    assert sent == []
    assert len(_file()["entries"]) == 1


# --- 沒有停損（規格第 36 條）---


def test_a_position_deep_in_the_red_is_never_closed_by_the_intraday_program():
    """08:50 做多一口之後大台一路崩到收工。PTS 沒有停損，出場只由出場程式負責：
    盤中程式送出的委託從頭到尾只有那一筆買單。"""
    crash = [e for i in range(20)
             for e in minute(f"09:{i:02d}", 45000 - 50 * i, 44950 - 50 * i, bichai=-3000)]
    broker, _ = _run(rally("08:45", 5) + [trade(at("08:50:00"), 45150)] + crash
                     + [clock(at("13:20:00"))])
    assert [(o.side, o.lots) for o in broker.orders] == [(BUY, 1)]
    assert len(_file()["entries"]) == 1


# --- 當天已經有部位：交易商品以部位檔為準（規格第 43 條）---


def _on_file(**overrides) -> dict:
    """今天稍早（例如重啟之前）寫下的部位檔：小台多單 2 口 @ 45100。"""
    content = {
        "trading_day": "2026-11-09",
        "product": "MTX00AM",
        "order_code": "MTX11",
        "contract_month": "202611",
        "direction": "LONG",
        "last_trading_day": "2026-11-18",
        "status": "CONFIRMED",
        "entries": [{"order_seq": "2315619999999", "lots": 2, "price": 45100.0}],
        "exits": [],
        "uncertain": None,
    }
    content.update(overrides)
    os.makedirs(os.path.dirname(position_file.path_for(DAY)), exist_ok=True)
    with open(position_file.path_for(DAY), "w", encoding="utf-8") as fh:
        json.dump(content, fh, ensure_ascii=False)
    return content


def test_a_position_already_on_file_today_decides_the_product_not_the_settings():
    """使用者盤中把設定改成微台，盤中程式重啟。今天已經有小台的部位，所以仍然看小台的
    行情、下小台的單；新的一筆接在原來的明細後面，累計與均價算進原來的 2 口：
    (45100 × 2 + 45150) ÷ 3 = 45116.67。設定從下一個交易日起才生效。"""
    _on_file()
    broker, sent = _run(rally("08:45", 5) + [trade(at("08:50:00"), 45150)],
                        config=_config(product=TMF_CODE))
    assert broker.streamed == [MTX_CODE]
    assert [(o.contract.order_code, o.day_trade) for o in broker.orders] == [("MTX11", True)]
    assert sent[0].splitlines()[0] == "PTS 做多：買進小台 1 口 @ 45150"
    assert sent[0].splitlines()[2] == "累計：多單 3 口，均價 45116.67"
    assert _file()["entries"] == [
        {"order_seq": "2315619999999", "lots": 2, "price": 45100.0},
        {"order_seq": "2315610000001", "lots": 1, "price": 45150.0},
    ]
    assert _file()["product"] == MTX_CODE


def test_the_lots_on_file_count_toward_the_cap():
    """加碼上限 3、每次 2 口、部位檔已有 2 口：今天只剩 1 口的空間，第一筆就只送 1 口。
    重啟時沒把檔裡的口數算進去的話，第一筆會送 2 口、衝過上限。"""
    _on_file()
    broker, _ = _run(rally("08:45", 7) + [trade(at("08:52:00"), 45170)],
                     config=_config(lots_per_entry=2, max_position=3))
    assert [o.lots for o in broker.orders] == [1]


def test_a_position_on_file_that_is_losing_blocks_conditions_1_and_2():
    """部位檔記著多單 2 口 @ 46000，小台現價只有 45100 上下：部位在虧，條件 1、2 要求的
    「部位不虧」不成立，整個早上一筆都不送。重啟時沒把檔裡的部位恢復的話，會被當成
    沒有部位（「部位不虧」直接成立）而照樣加碼。"""
    _on_file(entries=[{"order_seq": "2315619999999", "lots": 2, "price": 46000.0}])
    broker, sent = _run(rally("08:45", 8) + [trade(at("08:53:00"), 45180)])
    assert broker.orders == []
    assert sent == []


@pytest.mark.parametrize("auto", [True, False], ids=["自動下單", "模擬模式"])
def test_a_signal_against_the_direction_on_file_is_never_entered(auto):
    """一天只做一個方向（規格第 17 條）。部位檔記著今天稍早的多單 2 口 @ 44000，重啟後
    這一次卻判斷成開低（例如前一交易日的 1 分 K 這次查不到，往前多退一天，昨收變了），
    條件 2 每分鐘成立。賣單一送出，倉別「自動」會把它變成平倉：帳上剩 1 口，部位檔卻記成
    多單 3 口，13:25 出場程式賣 3 口，就開出空單 2 口帶過夜。所以一筆都不送、不寫、
    不發成交訊息；模擬模式也不把反向的口數累計進部位。"""
    content = _on_file(entries=[{"order_seq": "2315619999999", "lots": 2, "price": 44000.0}])
    events = (rally("08:45", 6, open_=44900, step=-10, bichai=-1500)
              + [trade(at("08:51:00"), 44830)])
    broker, sent = _run(events, config=_config(auto_order_enabled=auto))
    assert broker.orders == []
    assert sent == []
    assert _file() == content


def test_a_position_file_left_from_another_day_is_not_todays_position():
    """一個交易日一個檔。前一天的檔不會被當成今天的部位，也不會被今天的成交覆蓋——
    那一天沒了結的話，出場程式的告警與人工處理都要靠它。"""
    friday = position_file.path_for(PREVIOUS_DAY)
    os.makedirs(os.path.dirname(friday), exist_ok=True)
    with open(friday, "w", encoding="utf-8") as fh:
        fh.write("前一天的檔，內容不重要")
    broker, sent = _run(rally("08:45", 5) + [trade(at("08:50:00"), 45150)])
    assert [o.contract.order_code for o in broker.orders] == ["TM2611"]
    assert sent[0].splitlines()[2] == "累計：多單 1 口，均價 45150"
    with open(friday, encoding="utf-8") as fh:
        assert fh.read() == "前一天的檔，內容不重要"


# --- 部位檔讀不懂：大聲失敗，不能當成「沒有部位」---


def _damaged(text: str) -> None:
    os.makedirs(os.path.dirname(position_file.path_for(DAY)), exist_ok=True)
    with open(position_file.path_for(DAY), "w", encoding="utf-8") as fh:
        fh.write(text)


def _with(**changes) -> str:
    content = {
        "trading_day": "2026-11-09", "product": "MTX00AM", "order_code": "MTX11",
        "contract_month": "202611", "direction": "LONG", "last_trading_day": "2026-11-18",
        "status": "CONFIRMED",
        "entries": [{"order_seq": "2315619999999", "lots": 2, "price": 45100.0}],
        "exits": [], "uncertain": None,
    }
    for key, value in changes.items():
        if value is _DROP:
            del content[key]
        else:
            content[key] = value
    return json.dumps(content, ensure_ascii=False)


_DROP = object()

DAMAGE = {
    "寫到一半": '{"trading_day": "2026-11-09", "product": "MTX0',
    "不是 JSON 物件": "[]",
    "空檔": "",
    "少了欄位": _with(order_code=_DROP),
    "多了不認得的欄位": _with(stop_loss=44000),
    "別天的內容": _with(trading_day="2026-11-06"),
    "日期寫錯": _with(last_trading_day="2026/11/18"),
    "不認得的商品": _with(product="TMF"),
    "不認得的方向": _with(direction="BUY"),
    "不認得的狀態": _with(status="OK"),
    "口數是 0": _with(entries=[{"order_seq": "2315619999999", "lots": 0, "price": 45100.0}]),
    "口數是 true": _with(entries=[{"order_seq": "2315619999999", "lots": True, "price": 45100.0}]),
    "口數是字串": _with(entries=[{"order_seq": "2315619999999", "lots": "2", "price": 45100.0}]),
    "價格不是數字": _with(entries=[{"order_seq": "2315619999999", "lots": 2, "price": "45100"}]),
    "成交少了序號": _with(entries=[{"lots": 2, "price": 45100.0}]),
    "平倉比進場多": _with(exits=[{"order_seq": "2315619999998", "lots": 3, "price": 45200.0}]),
    "不確定卻沒說哪一筆": _with(status="UNCERTAIN"),
    "已確認卻掛著不確定的委託": _with(uncertain={"stage": "ENTRY", "order_seq": "x",
                                                  "requested_lots": 1, "known_filled": 0}),
    "契約月份和最後交易日對不上": _with(contract_month="202612"),
}


@pytest.mark.parametrize("text", DAMAGE.values(), ids=DAMAGE.keys())
def test_a_position_file_that_cannot_be_read_stops_the_day_and_says_so(text):
    """讀不懂的部位檔可能記著今天已經成交的口數。當成「沒有部位」照常進場的話，
    口數、均價、加碼上限、甚至商品都會錯；這一天不進場，發 Discord 請人看帳戶。
    檔案原封不動——那是人工處理時唯一的線索。"""
    _damaged(text)
    broker, sent = _run(rally("08:45", 6) + [trade(at("08:51:00"), 45160)])
    assert broker.orders == []
    assert len(sent) == 1
    assert sent[0].startswith("【要你處理】PTS 部位檔讀不懂，盤中程式今天不進場\n")
    with open(position_file.path_for(DAY), encoding="utf-8") as fh:
        assert fh.read() == text


# --- 部位不確定：當天不再進場（規格第 50 條）---


def test_an_uncertain_position_on_file_means_no_more_entries_today():
    """今天稍早有一筆進場單送出去了、卻不知道成交幾口（ticket 07 寫下「不確定」），之後
    盤中程式重啟。帳上的口數不知道，部位不虧、加碼上限都算不了：這一天不再進場。
    告警在當時已經發過，重啟不再重發；檔案原封不動，留給出場程式與人工處理。"""
    content = _on_file(status="UNCERTAIN", uncertain={
        "stage": "ENTRY", "order_seq": "2315619999998", "requested_lots": 1, "known_filled": 0})
    broker, sent = _run(rally("08:45", 8) + [trade(at("08:53:00"), 45180)])
    assert broker.orders == []
    assert sent == []
    assert _file() == content


# --- 只認自己委託序號的成交回報（規格第 56 條）：從群益 broker 一路打到部位檔 ---


class _Ticking:
    """本機時鐘的替身：每被問一次就往前走 `step`。"""

    def __init__(self, start, step):
        self._t, self._step = start, step

    def __call__(self):
        t, self._t = self._t, self._t + self._step
        return t


def _tick(index: int, t, price: float) -> tuple:
    """`OnNotifyTicksLONG` 的參數（文件 4-4-m）：價格放大 100 倍、時間是整數 hhmmss。"""
    hhmmss = t.hour * 10000 + t.minute * 100 + t.second
    return "OnNotifyTicksLONG", (2, index, 0, 20261109, hhmmss, 0, 0, 0, int(price * 100), 1, 0)


def _deal_counts(buy: int, sell: int) -> tuple:
    """`OnNotifyFutureTradeInfoLONG` 的參數（文件 4-4-q）：最後兩個是成交買進／賣出筆數。"""
    return "OnNotifyFutureTradeInfoLONG", ("TX00AM", 2, TX_INDEX, 0, 0, 0, 0, buy, sell)


def test_fills_of_other_orders_on_the_shared_reply_line_never_become_ptss_position():
    """回報頻道是共用的：OS 的單、使用者手動下的單、連線時補送的舊回報，全部走同一條線進來。
    這裡不用假 broker，換掉的是群益元件本身（`FakeSkcom`），從盤中程式一路打到部位檔：
    PTS 的委託成交 1 口 @ 45152；同一段時間 OS 成交 2 口、手動成交 3 口，連上回報主機時
    還補送了 OS 前一天的 1 口——部位檔與訊息只有 PTS 自己那 1 口。"""
    market = []
    for i in range(5):                       # 08:45–08:49 一路強勢，同 rally("08:45", 5)
        start = at(f"08:4{5 + i}:00")
        market += [_tick(TX_INDEX, start, 45100 + 10 * i),
                   _deal_counts(20000, 21500),
                   _tick(TX_INDEX, start + timedelta(seconds=30), 45110 + 10 * i),
                   _tick(TMF_INDEX, start + timedelta(seconds=40), 45110 + 10 * i)]
    market.append(_tick(TX_INDEX, at("08:50:00"), 45150))
    calm = [f"2026/11/06 {b.start:%H:%M}, {b.open:g}, {b.high:g}, {b.low:g}, {b.close:g}, 100"
            for b in calm_history(45000)]

    def crowded(order, seq):
        os_seq, manual_seq = "2315620000001", "2315630000001"
        return seq, 0, [reply_row("N", seq=os_seq, qty=2),
                        reply_row("N", seq=seq, qty=order.nQty),
                        reply_row("D", seq=os_seq, qty=2, price=45140.0),
                        reply_row("D", seq=manual_seq, qty=3, price=45145.0),
                        reply_row("D", seq=seq, qty=1, price=45152.0)]

    old = "2315600000007"
    skcom = FakeSkcom(market=market, klines={"20261106": calm}, on_send=crowded,
                      replayed_rows=(reply_row("N", seq=old, qty=1),
                                     reply_row("D", seq=old, qty=1, price=44000.0)))
    broker = CapitalBroker("A100000000", "pw", "F0000000000000", com=skcom, settle_seconds=0,
                           pump_interval=0, connect_timeout=1, fill_timeout=1,
                           now=_Ticking(at("08:50:00"), timedelta(minutes=10)))
    notifier = RecordingNotifier()
    intraday.run(broker, _config(), today=DAY, notify=notifier)

    # 市價（M）、IOC（1）、倉別自動（2）、微台不標當沖（0）、買進（0）——送到群益的就是這些
    assert [(o.bstrStockNo, o.sBuySell, o.nQty, o.bstrPrice, o.sTradeType, o.sNewClose,
             o.sDayTrade) for o in skcom.sent] == [("TM2611", 0, 1, "M", 1, 2, 0)]
    assert _file()["entries"] == [{"order_seq": "2315610000001", "lots": 1, "price": 45152.0}]
    # 08:50 之後劇本沒有行情，本機時鐘一次跳 10 分鐘直到 13:20：那是一次報價中斷，
    # 後面跟著一則中斷告警（ticket 09，test_quote_outage）。這裡只看成交訊息。
    assert notifier.sent[0] == ("PTS 做多：買進微台 1 口 @ 45152\n"
                                "觸發：條件 1（早盤做多）\n"
                                "累計：多單 1 口，均價 45152\n"
                                "2026/11/09 08:50:00")
    assert [m.splitlines()[0] for m in notifier.sent[1:]] == [
        "【要你處理】PTS 盤中報價中斷超過 3 分鐘"]
