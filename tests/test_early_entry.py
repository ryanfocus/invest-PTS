"""條件 1、2：早盤進場（模擬模式）—— 從最上層打進去。

餵一個交易日的劇本給 `intraday.run`（假 broker 照劇本回放，前一個交易日的 1 分 K 也由它給），
斷言三件外部的事：發了哪些訊息、下單函式有沒有被呼叫、有沒有寫出部位檔。
期望值是照 GLOSSARY 的定義手算的字面值。

劇本的慣例（見 `day_script.minute`）：一根 K 棒在下一分鐘的第一筆成交時結束，策略在那時
判斷——08:49 那根在 08:50:00 判斷，這就是「08:50 開始」的第一次判斷。
"""

from datetime import date

import pytest

from broker import MTX_CODE, TMF_CODE, TX_CODE, MinuteBar, ProductTrade, QuoteUnavailable
from broker.fake import FakeBroker
from day_script import (DAY, PREVIOUS_DAY, at, calm_history, history as history_bars, minute,
                        rally, trade, whole_morning)
from recording import RecordingNotifier
import intraday
import paths
import settings

SATURDAY = date(2026, 11, 7)


def _config(**overrides) -> settings.Config:
    values = dict(auto_order_enabled=False, product=TMF_CODE, lots_per_entry=1,
                  max_position=20, discord_enabled=True)
    values.update(overrides)
    return settings.Config(**values)


def _run(events, *, history=None, config=None, today: date = DAY):
    """跑一天，回傳 (broker, 發出的訊息)。前一個交易日預設是平淡的一天、收 45000。"""
    broker = FakeBroker(events=events,
                        minute_bars={PREVIOUS_DAY: calm_history(45000) if history is None
                                     else history})
    notifier = RecordingNotifier()
    intraday.run(broker, config or _config(), today=today, notify=notifier)
    return broker, notifier.sent


def test_a_strong_open_enters_long_once_a_minute_from_0850_as_simulation():
    """開高（45100 > 昨收 45000）、每根紅K、筆差 1500、RSI 被跳空推過 70。
    08:50 判斷 08:49 那根，第一口進場；08:51 部位不虧（現價 45160 ≥ 均價 45150），再一口。
    模擬成交在交易商品當下的價格，下單函式一次都沒被呼叫。"""
    broker, sent = _run(rally("08:45", 6) + [trade(at("08:51:00"), 45160)])

    assert sent == [
        "【模擬】PTS 做多：買進微台 1 口 @ 45150\n"
        "觸發：條件 1（早盤做多）\n"
        "累計：多單 1 口，均價 45150\n"
        "2026/11/09 08:50:00",
        "【模擬】PTS 做多：買進微台 1 口 @ 45160\n"
        "觸發：條件 1（早盤做多）\n"
        "累計：多單 2 口，均價 45155\n"
        "2026/11/09 08:51:00",
    ]
    assert broker.orders == []


def _times(sent: list[str]) -> list[str]:
    """每則訊息最後一行的時刻（HH:MM:SS）。"""
    return [text.splitlines()[-1][-8:] for text in sent]


def test_while_the_conditions_hold_it_enters_once_a_minute_until_0945():
    """08:45 到 09:50 一路強勢。08:50（判斷 08:49 那根）到 09:45（判斷 09:44 那根）每分鐘一口，
    共 56 口；09:46 之後條件 1 不再判斷。加碼上限調高到 100，才看得到 09:45 這條線。"""
    broker, sent = _run(rally("08:45", 66) + [trade(at("09:51:00"), 45760)],
                        config=_config(max_position=100))
    times = _times(sent)
    assert len(sent) == 56
    assert (times[0], times[1], times[-1]) == ("08:50:00", "08:51:00", "09:45:00")
    assert sent[-1].splitlines()[2].startswith("累計：多單 56 口")
    assert broker.orders == []


def test_entries_stop_at_the_position_cap():
    """加碼上限 3：08:50、08:51、08:52 各一口，之後條件仍然成立也不再進。"""
    _, sent = _run(rally("08:45", 10) + [trade(at("08:55:00"), 45200)],
                   config=_config(max_position=3))
    assert _times(sent) == ["08:50:00", "08:51:00", "08:52:00"]
    assert sent[-1].splitlines()[2] == "累計：多單 3 口，均價 45160"


def test_the_average_is_weighted_by_lots_and_shown_to_two_decimals():
    """三口成交在 45150、45160、45171：均價 135481 ÷ 3 = 45160.333…，訊息寫 45160.33。"""
    events = (rally("08:45", 6)
              + minute("08:51", 45160, 45170, bichai=1500, product=45171)
              + [trade(at("08:52:00"), 45180)])
    _, sent = _run(events)
    assert sent[-1].splitlines()[:3] == ["【模擬】PTS 做多：買進微台 1 口 @ 45171",
                                         "觸發：條件 1（早盤做多）",
                                         "累計：多單 3 口，均價 45160.33"]


def test_a_losing_position_in_the_trading_product_skips_that_minute():
    """部位不虧看的是**交易商品**的價格（規格第 14 條）。08:50 在 45150 進一口；08:50 那根裡
    交易商品成交在 45149（大台照樣強勢），08:51 就不加碼；08:51 那根回到 45170，08:52 再加。"""
    events = (rally("08:45", 5)
              + minute("08:50", 45150, 45160, bichai=1500, product=45149)
              + minute("08:51", 45160, 45170, bichai=1500)
              + [trade(at("08:52:00"), 45180)])
    _, sent = _run(events)
    assert _times(sent) == ["08:50:00", "08:52:00"]
    assert sent[-1].splitlines()[0] == "【模擬】PTS 做多：買進微台 1 口 @ 45170"


def test_a_weak_open_enters_short_with_condition_2():
    """開低（44900 < 昨收 45000）、每根黑K、筆差 −1500、RSI 被跳空壓到 30 以下。"""
    events = rally("08:45", 5, open_=44900, step=-10, bichai=-1500) + [trade(at("08:50:00"), 44840)]
    broker, sent = _run(events)
    assert sent == [
        "【模擬】PTS 做空：賣出微台 1 口 @ 44850\n"
        "觸發：條件 2（早盤做空）\n"
        "累計：空單 1 口，均價 44850\n"
        "2026/11/09 08:50:00",
    ]
    assert broker.orders == []


# --- 不發訊息的時候（規格第 39、40、45 條）---


def test_with_discord_switched_off_nothing_is_sent_even_when_entering():
    _, sent = _run(rally("08:45", 6) + [trade(at("08:51:00"), 45160)],
                   config=_config(discord_enabled=False))
    assert sent == []


def test_a_day_without_entries_sends_nothing():
    """一整個平淡的上午：筆差從沒超過 1000。"""
    broker, sent = _run(whole_morning())
    assert sent == []
    assert broker.orders == []


def test_a_non_trading_day_sends_nothing():
    broker, sent = _run(rally("08:45", 6, day=SATURDAY), today=SATURDAY)
    assert sent == []


# --- 模擬部位不寫進真實的部位檔（規格第 42 條）---


def test_simulated_entries_leave_no_file_in_the_program_directory(tmp_path, monkeypatch):
    """出場程式看的是部位檔。模擬的口數寫進去的話，13:25 它會去平一個不存在的部位。
    程式目錄底下（部位檔、設定、log 都從這裡推導）一個檔都不能多；原始行情與 log
    已經由 conftest 導到別處。"""
    app = tmp_path / "app"
    app.mkdir()
    monkeypatch.setattr(paths, "app_root", lambda: str(app))
    _, sent = _run(rally("08:45", 6) + [trade(at("08:51:00"), 45160)])
    assert len(sent) == 2
    assert list(app.rglob("*")) == []


# --- 指標一律看大台（規格第 8 條）---


@pytest.mark.parametrize("product, name", [(TX_CODE, "大台"), (MTX_CODE, "小台"),
                                           (TMF_CODE, "微台")])
def test_the_trading_product_does_not_change_when_it_enters(product, name):
    """交易商品的成交價和大台差很多，判斷照樣只看大台：三種商品在同樣的時刻進場，
    只有訊息裡的商品名稱和成交價（交易商品自己的價格）不同。"""
    events = []
    for event in rally("08:45", 6) + [trade(at("08:51:00"), 45160)]:
        if isinstance(event, ProductTrade):
            event = ProductTrade(event.time, 22000.0)      # 和大台完全不像的價格
        events.append(event)
    _, sent = _run(events, config=_config(product=product))
    assert _times(sent) == ["08:50:00", "08:51:00"]
    assert sent[0].splitlines()[0] == f"【模擬】PTS 做多：買進{name} 1 口 @ 22000"


# --- 昨收與暖身（規格第 4、5 條）---


@pytest.mark.parametrize("last_close, entries", [(45100, 0), (45099, 2)])
def test_yesterdays_close_is_the_last_bar_of_the_previous_day_session(last_close, entries):
    """今天開 45100。前一天最後一根收 45100 → 開盤價等於昨收，不進場；收 45099 → 開高。
    前面幾根收在哪裡都不算（昨收不是結算價、不是參考價，是日盤最後一根的收盤價）。"""
    history = calm_history(45000)[:-1] + history_bars([last_close])[:0] + \
        [MinuteBar(at("13:44:00", PREVIOUS_DAY), last_close, last_close, last_close, last_close)]
    _, sent = _run(rally("08:45", 6) + [trade(at("08:51:00"), 45160)], history=history)
    assert len(sent) == entries


def test_a_trading_day_without_bars_is_skipped_when_looking_for_yesterday():
    """週五臨時休市（颱風假），日曆不知道，查不到 K 棒；昨收要往前找到週四。
    今天開 45100：週四收 45099 → 開高、進場。拿錯成週三（收 45100，開盤價等於昨收）
    或因為週五是空的就放棄，都不會進場。"""
    thursday, wednesday = date(2026, 11, 5), date(2026, 11, 4)
    broker = FakeBroker(events=rally("08:45", 6) + [trade(at("08:51:00"), 45160)],
                        minute_bars={PREVIOUS_DAY: [], thursday: calm_history(45099, thursday),
                                     wednesday: calm_history(45100, wednesday)})
    notifier = RecordingNotifier()
    intraday.run(broker, _config(), today=DAY, notify=notifier)
    assert _times(notifier.sent) == ["08:50:00", "08:51:00"]


def test_the_rsi_is_carried_over_from_the_previous_day_session():
    """前一天整個日盤每分鐘跌 20，今天開高後一路漲。只看今天的話，08:51 有 6 根收盤價、
    RSI 是 100；接上前一天，跌勢還留在 Wilder 的平滑裡（手算見下），08:50、08:51 都不到 70。

    平均跌幅從 20 開始，今天的變動 +20、+10、+10、+10、+10、+10：
    漲 4／5.2／6.16／6.928／7.5424／8.03392，跌 16／12.8／10.24／8.192／6.5536／5.24288
    → 08:50 的 RSI 53.5、08:51 的 RSI 60.5。
    """
    falling = history_bars([45000 + 20 * (299 - i) for i in range(300)])
    events = (minute("08:45", 45010, 45020, bichai=1500)
              + rally("08:46", 5, open_=45020, step=10)
              + [trade(at("08:51:00"), 45080)])
    _, sent = _run(events, history=falling)
    assert sent == []


def test_without_bars_of_the_previous_day_it_records_the_market_but_never_enters(market_dir):
    """查不到昨收與暖身，就判斷不了開高開低與 RSI：今天不進場。但原始行情照樣要記
    （ticket 03 要一週的資料），盤中程式不能因此中止。"""
    broker = FakeBroker(events=rally("08:45", 6) + [trade(at("08:51:00"), 45160)],
                        minute_bars_error=QuoteUnavailable("群益訊息3022"))
    notifier = RecordingNotifier()
    outcome = intraday.run(broker, _config(), today=DAY, notify=notifier)
    assert notifier.sent == []
    assert len(outcome.bars) == 6
    assert (market_dir / "TX-20261109.csv").exists()


def test_with_auto_order_switched_on_it_refuses_to_start_until_real_orders_exist():
    """真實下單是 ticket 06。在那之前打開自動下單，程式若照樣跑，使用者會以為條件成立時
    真的下了單（或者收到「模擬」訊息卻以為那是真的）。一啟動就講清楚，不登入、不送單。"""
    broker = FakeBroker(events=rally("08:45", 6), login_error=AssertionError("不該登入"),
                        minute_bars={PREVIOUS_DAY: calm_history()})
    with pytest.raises(NotImplementedError, match="自動下單"):
        intraday.run(broker, _config(auto_order_enabled=True), today=DAY,
                     notify=RecordingNotifier())
    assert broker.orders == []
