"""條件 3、4：追價加碼與突破進場（模擬模式）—— 從最上層打進去。

餵一個交易日的劇本給 `intraday.run`，斷言發了哪些訊息、下單函式有沒有被呼叫。
期望值是照 GLOSSARY 的定義手算的字面值。

劇本的慣例同 test_early_entry：一根 K 棒在下一分鐘的第一筆成交時結束，策略在那時判斷。
`rally` 的第 i 根開 45100 + 10i、收（也是最高）45110 + 10i，交易商品成交在收盤價；
所以 08:49 那根收 45150，08:50 判斷時在 45150 進第一口。
"""

from datetime import date

import pytest

from broker import TX_CODE, ProductListUnavailable
from day_script import (SETTLEMENT_DAY, at, clock, contracts, make_config, minute, product_trade,
                        rally, run_day, trade, trading_tx)


def _run(events, *, config=None, last_trading_day: date = SETTLEMENT_DAY):
    """跑 DAY（週一）這一天（`run_day`），回傳 (broker, 發出的訊息)。商品清單的最後交易日
    預設在下週三，今天不在結算週。"""
    broker, sent, _ = run_day(events, config=config, contracts=contracts(last_trading_day))
    return broker, sent


# 每一天的劇本都從 08:50 條件 1 在 45150 進的第一口開始：已經持有多單，條件 3 才有意義。
FIRST_LOT = ("【模擬】PTS 做多：買進微台 1 口 @ 45150\n"
             "觸發：條件 1（早盤做多）\n"
             "累計：多單 1 口，均價 45150\n"
             "2026/11/09 08:50:00")


def _jump_before_0945() -> list:
    """08:50 那根筆差從 1500 跳到 1600（恰好 +100），交易商品卻跌到 45149。"""
    return (rally("08:45", 5)
            + minute("08:50", 45150, 45160, bichai=1600, product=45149)
            + [trade(at("08:51:00"), 45170)])


def test_a_bichai_jump_before_0945_adds_a_lot_at_once_even_while_losing():
    """08:51 判斷 08:50 那根：部位在虧（現價 45149 < 均價 45150），條件 1 不成立；
    條件 3 不要求部位不虧（規格第 22 條），立刻市價加一口。均價 (45150 + 45149) ÷ 2 = 45149.5。"""
    broker, sent = _run(_jump_before_0945())
    assert sent == [
        FIRST_LOT,
        "【模擬】PTS 做多：買進微台 1 口 @ 45149\n"
        "觸發：條件 3（追多加碼）\n"
        "累計：多單 2 口，均價 45149.5\n"
        "2026/11/09 08:51:00",
    ]
    assert broker.orders == []


# --- 突破進場（規格第 19–21 條）---


def _quiet_until_1000() -> list:
    """08:50 條件 1 在 45150 進一口，之後到 09:59 平盤（45150）、筆差 500：什麼都不成立。"""
    return rally("08:45", 5) + rally("08:50", 70, open_=45150, step=0, bichai=500)


def _breakout_day(*, waiting_bichai: int = 1600) -> list:
    """10:00 那根筆差從 500 跳到 1600、最高 45180；10:01 判斷時設突破進場，觸發價是
    09:56–10:00 五根的最高點 45180。10:01 那根在 45170–45175 之間沒碰到（筆差
    `waiting_bichai`）；10:02:30 大台成交在 45180，碰到了。"""
    return (_quiet_until_1000()
            + minute("10:00", 45150, 45180, bichai=1600)
            + minute("10:01", 45170, 45175, bichai=waiting_bichai)
            + minute("10:02", 45175, 45180, bichai=waiting_bichai)
            + [trade(at("10:03:00"), 45185)])


# 碰到觸發價的那一刻（10:02:30），交易商品最新的成交是 10:01:40 的 45175：模擬成交在它。
BREAKOUT_LOT = ("【模擬】PTS 做多：買進微台 1 口 @ 45175\n"
                "觸發：條件 3（追多加碼），突破進場（觸發價 45180）\n"
                "累計：多單 2 口，均價 45162.5\n"
                "2026/11/09 10:02:30")


def test_after_0945_a_bichai_jump_waits_for_the_price_to_touch_the_high_of_the_last_5_bars():
    broker, sent = _run(_breakout_day())
    assert sent == [FIRST_LOT, BREAKOUT_LOT]
    assert broker.orders == []


def test_a_waiting_breakout_still_enters_after_bichai_falls_back_within_the_threshold():
    """觸價單的語意：設定之後只看價格（規格第 21 條）。10:01 那根的筆差掉到 800，
    碰到觸發價照樣進場。"""
    _, sent = _run(_breakout_day(waiting_bichai=800))
    assert sent == [FIRST_LOT, BREAKOUT_LOT]


def test_condition_4_waits_for_the_price_to_touch_the_low_of_the_last_5_bars():
    """條件 3 的鏡像：開低、08:50 條件 2 在 44850 放空一口；10:00 那根筆差從 −500 掉到 −1600、
    最低 44820，10:01 設突破進場，觸發價是 09:56–10:00 五根的最低點 44820。10:01 那根在
    44825 以上沒碰到；10:02:30 大台成交在 44820，碰到了，成交在交易商品最新的 44825。"""
    events = (rally("08:45", 5, open_=44900, step=-10, bichai=-1500)
              + rally("08:50", 70, open_=44850, step=0, bichai=-500)
              + minute("10:00", 44850, 44820, bichai=-1600)
              + minute("10:01", 44830, 44825, bichai=-1600)
              + minute("10:02", 44825, 44820, bichai=-1600)
              + [trade(at("10:03:00"), 44815)])
    broker, sent = _run(events)
    assert sent == [
        "【模擬】PTS 做空：賣出微台 1 口 @ 44850\n"
        "觸發：條件 2（早盤做空）\n"
        "累計：空單 1 口，均價 44850\n"
        "2026/11/09 08:50:00",
        "【模擬】PTS 做空：賣出微台 1 口 @ 44825\n"
        "觸發：條件 4（追空加碼），突破進場（觸發價 44820）\n"
        "累計：空單 2 口，均價 44837.5\n"
        "2026/11/09 10:02:30",
    ]
    assert broker.orders == []


def test_trading_tx_fills_at_the_very_trade_that_triggered_the_entry():
    """交易大台時，大台的成交就是交易商品的成交：群益對同一筆先送大台成交、再送交易商品成交。
    進場要成交在**觸發的那一筆**，不是它前一筆——否則同一個商品會出現「觸發在 45180、
    成交在 45175」這種不可能的結果，模擬週核對的均價也一路偏向有利的一方。

    08:50:00 那筆 45155 讓 08:49 那根結束、條件 1 成立：成交在 45155（前一筆是 45150）。
    08:50 起筆差 500、什麼都不成立；10:00 那根筆差 500 → 1600、最高 45180，10:01 設突破進場，
    觸發價 45180。10:02:30 那筆 45180 碰到觸發價：成交在 45180（前一筆是 45175）。
    均價 (45155 + 45180) ÷ 2 = 45167.5。"""
    events = trading_tx(rally("08:45", 5)
                        + minute("08:50", 45155, 45150, bichai=500)
                        + rally("08:51", 69, open_=45150, step=0, bichai=500)
                        + minute("10:00", 45150, 45180, bichai=1600)
                        + minute("10:01", 45170, 45175, bichai=1600)
                        + minute("10:02", 45175, 45180, bichai=1600)
                        + [trade(at("10:03:00"), 45185)])
    broker, sent = _run(events, config=make_config(product=TX_CODE))
    assert sent == [
        "【模擬】PTS 做多：買進大台 1 口 @ 45155\n"
        "觸發：條件 1（早盤做多）\n"
        "累計：多單 1 口，均價 45155\n"
        "2026/11/09 08:50:00",
        "【模擬】PTS 做多：買進大台 1 口 @ 45180\n"
        "觸發：條件 3（追多加碼），突破進場（觸發價 45180）\n"
        "累計：多單 2 口，均價 45167.5\n"
        "2026/11/09 10:02:30",
    ]
    assert broker.orders == []


def test_a_trade_delivered_late_does_not_break_out():
    """10:01 設下觸發價 45180。10:02:20 那筆 45185 的成交晚了 40 秒才送到（本機已經 10:03:00）：
    價格那時就碰到了，但現在的價格已經不是那個價，拿它進場和回測不是同一件事
    （同 K 棒判斷的「回補的舊成交不判斷」，規格第 53 條）。即時收到的 10:03:05 只有 45170；
    10:04:00 即時的 45180 才進場，成交在交易商品最新的 45172。"""
    events = (_quiet_until_1000()
              + minute("10:00", 45150, 45180, bichai=1600)
              + minute("10:01", 45170, 45175, bichai=1600)
              + [clock(at("10:03:00")), trade(at("10:02:20"), 45185),
                 trade(at("10:03:05"), 45170), product_trade(at("10:03:06"), 45172),
                 clock(at("10:04:00")), trade(at("10:04:00"), 45180)])
    _, sent = _run(events)
    assert sent == [
        FIRST_LOT,
        "【模擬】PTS 做多：買進微台 1 口 @ 45172\n"
        "觸發：條件 3（追多加碼），突破進場（觸發價 45180）\n"
        "累計：多單 2 口，均價 45161\n"
        "2026/11/09 10:04:00",
    ]


def test_a_jump_while_waiting_keeps_the_original_trigger_and_a_new_one_follows_the_fill():
    """10:01 設下觸發價 45180。之後五根都在 45175 以下，10:05 那根又筆差跳增（1600 → 1700）：
    最近 5 根（10:01–10:05）的最高點這時只剩 45175，但等待中不設新的，維持 45180
    （規格第 21 條）。所以 10:06:30 的 45176 不算突破，10:07:30 的 45185 才碰到。
    原本那筆成交之後，10:07 那根再跳增（1700 → 1800），10:08:00 設新的突破進場，觸發價是
    10:03–10:07 的最高點 45185；讓那根結束的成交 45190 本身就碰到了，馬上進場。"""
    events = (_quiet_until_1000()
              + minute("10:00", 45150, 45180, bichai=1600)
              + minute("10:01", 45170, 45175, bichai=1600)
              + minute("10:02", 45170, 45175, bichai=1600)
              + minute("10:03", 45170, 45175, bichai=1600)
              + minute("10:04", 45170, 45175, bichai=1600)
              + minute("10:05", 45170, 45175, bichai=1700)
              + minute("10:06", 45170, 45176, bichai=1700)
              + minute("10:07", 45170, 45185, bichai=1800)
              + [trade(at("10:08:00"), 45190)])
    _, sent = _run(events)
    assert sent == [
        FIRST_LOT,
        "【模擬】PTS 做多：買進微台 1 口 @ 45176\n"
        "觸發：條件 3（追多加碼），突破進場（觸發價 45180）\n"
        "累計：多單 2 口，均價 45163\n"
        "2026/11/09 10:07:30",
        "【模擬】PTS 做多：買進微台 1 口 @ 45185\n"
        "觸發：條件 3（追多加碼），突破進場（觸發價 45185）\n"
        "累計：多單 3 口，均價 45170.33\n"
        "2026/11/09 10:08:00",
    ]


def test_a_breakout_that_never_breaks_is_cancelled_at_1320():
    """10:01 設下觸發價 45180，之後一整天平盤在 45150。13:20 進場截止，等待中的突破進場
    一併取消（規格第 26 條）：13:20 之後大台再碰到 45180 也不進場。只有早上那一口的訊息。"""
    events = (_quiet_until_1000()
              + minute("10:00", 45150, 45180, bichai=1600)
              + rally("10:01", 199, open_=45150, step=0, bichai=1600)
              + [clock(at("13:20:00")), trade(at("13:20:00"), 45200),
                 clock(at("13:20:01")), trade(at("13:20:01"), 45210)])
    broker, sent = _run(events)
    assert sent == [FIRST_LOT]
    assert broker.orders == []


def _times(sent: list[str]) -> list[str]:
    return [text.splitlines()[-1][-8:] for text in sent]


def test_a_strong_day_adds_all_the_way_to_the_cap():
    """08:45 起一路上漲，每一根都筆差跳增（+100）。08:50–09:45 條件 1 和條件 3 每分鐘都
    同時成立，每分鐘仍然只進一口（規格第 16 條），共 56 口，觸發都寫條件 1。09:46 起改成
    突破進場：觸發價是上一根的最高點（收盤價），下一根一開盤就碰到，09:46、09:47、09:48、
    09:49 各進一口，到加碼上限 60 口為止；之後到 10:00 一路照樣強勢，不再進場。"""
    broker, sent = _run(rally("08:45", 76, bichai_step=100) + [trade(at("10:01:00"), 45860)],
                        config=make_config(max_position=60))
    times = _times(sent)
    assert len(sent) == 60
    assert (times[0], times[55], times[56], times[-1]) == ("08:50:00", "09:45:00", "09:46:00",
                                                          "09:49:00")
    assert all("條件 1（早盤做多）\n" in text for text in sent[:56])
    # 09:49 進場的觸發價是 09:48 那根的最高點（收盤價）45740。60 口依序成交在 45150、
    # 45160、…、45740（每口比上一口高 10），均價是頭尾的平均 (45150 + 45740) ÷ 2 = 45445。
    assert sent[-1].splitlines() == [
        "【模擬】PTS 做多：買進微台 1 口 @ 45740",
        "觸發：條件 3（追多加碼），突破進場（觸發價 45740）",
        "累計：多單 60 口，均價 45445",
        "2026/11/09 09:49:00",
    ]
    assert broker.orders == []


def test_a_breakout_fills_only_what_was_left_under_the_cap_when_it_was_set():
    """每次進 3 口、加碼上限 20。08:50–08:55 條件 1 連續成立六次，進到 18 口（成交在
    08:49–08:54 那幾根的收盤價 45150、45160、…、45200）；之後筆差 500，什麼都不成立。
    10:00 那根筆差 500 → 1600、最高 45230，10:01 設突破進場：只剩 2 口的空間，所以是 2 口
    （規格第 25 條）。10:02:30 碰到 45230，成交在交易商品最新的 45225——進 2 口、累計剛好
    20 口，不是照每次 3 口進到 21 口。
    均價 (3 × (45150 + 45160 + … + 45200) + 2 × 45225) ÷ 20 = (813150 + 90450) ÷ 20 = 45180。"""
    events = (rally("08:45", 10)
              + rally("08:55", 65, open_=45200, step=0, bichai=500)
              + minute("10:00", 45200, 45230, bichai=1600)
              + minute("10:01", 45220, 45225, bichai=1600)
              + minute("10:02", 45225, 45230, bichai=1600)
              + [trade(at("10:03:00"), 45235)])
    broker, sent = _run(events, config=make_config(lots_per_entry=3, max_position=20))
    assert _times(sent) == ["08:50:00", "08:51:00", "08:52:00", "08:53:00", "08:54:00",
                            "08:55:00", "10:02:30"]
    assert sent[-1] == ("【模擬】PTS 做多：買進微台 2 口 @ 45225\n"
                        "觸發：條件 3（追多加碼），突破進場（觸發價 45230）\n"
                        "累計：多單 20 口，均價 45180\n"
                        "2026/11/09 10:02:30")
    assert broker.orders == []


# --- 結算週（規格第 23、24 條）---


@pytest.mark.parametrize("last_trading_day", [date(2026, 11, 11), date(2026, 11, 12)],
                         ids=["週三結算", "順延到週四"])
@pytest.mark.parametrize("events", [_jump_before_0945, _breakout_day],
                         ids=["立刻加碼", "突破進場"])
def test_in_the_settlement_week_only_conditions_1_and_2_enter(events, last_trading_day):
    """今天是週一。商品清單說近月的最後交易日是這週三（或順延到週四）：今天在結算週，
    條件 3、4 停用，只剩早上條件 1 那一口。"""
    broker, sent = _run(events(), last_trading_day=last_trading_day)
    assert sent == [FIRST_LOT]
    assert broker.orders == []


def test_without_the_product_list_conditions_3_and_4_are_off_but_1_and_2_are_not():
    """商品清單查不到，就不知道今天是不是結算週：條件 3、4 停用（寧可少做），條件 1、2 照常。
    每分鐘再查一次；到 08:50 還沒有，講一則今天哪些條件判斷不了。"""
    _, sent, _ = run_day(_breakout_day(),
                         contracts_error=ProductListUnavailable("10 秒內未取得完整商品清單"))
    assert sent == [
        "【要你處理】PTS 到 08:50 還有資料查不到，今天的進場判斷不完整\n"
        "查不到商品清單裡大台近月的結算日：不知道今天是不是結算週，條件 3、4 停用。\n"
        "盤中程式每分鐘再查一次，查到之後就照常判斷，不另外通知。原始行情照常記錄，"
        "已經有的部位照常了結。\n"
        "通常不必處理（多半是群益主機一時查不到）；想知道原因請看 logs/intraday-20261109.log。\n"
        "2026/11/09 08:50:00",
        FIRST_LOT,
    ]


def test_a_product_list_that_failed_at_first_is_fetched_again_a_minute_later():
    """08:40 查商品清單失敗，下一分鐘再查就有了：條件 3、4 照常，突破進場照樣觸發。"""
    _, sent, _ = run_day(_breakout_day(),
                         contracts_error=[ProductListUnavailable("10 秒內未取得完整商品清單")])
    assert sent[0] == FIRST_LOT
    assert [m.splitlines()[1] for m in sent[1:]] == [
        "觸發：條件 3（追多加碼），突破進場（觸發價 45180）"]
