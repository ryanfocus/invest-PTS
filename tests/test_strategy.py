"""策略判斷的表格式測試（規格 Testing Decisions「策略判斷」）。

每一條都從一個「五項全部成立」的基準情境出發，只改一項，看它是不是剛好在邊界上翻轉。
期望值是 GLOSSARY 的定義：筆差 > 1000、RSI > 70、損益 ≥ 0 都是嚴格／不嚴格各有規定，
恰好等於門檻的那一格最容易寫錯。

RSI 恰好 70／30 的情境用 6 根收盤價（RSI(5) 的最少根數）手算：變動 +7、−3、0、0、0
→ 平均漲幅 7/5、平均跌幅 3/5 → RSI = 100 × 1.4 ÷ 2.0 = 70。
"""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from broker import MinuteBar
from indicators import ClosedBar
import strategy
from strategy import LONG, SHORT, Condition, Enter, Position, Situation

DAY = datetime(2026, 11, 9)
YESTERDAY_CLOSE = 45000.0


def _bars(start: str, opens: list[float], closes: list[float], bichai: int | None) -> tuple:
    """從 `start` 起每分鐘一根；最後一根結束時的筆差是 `bichai`，前面幾根不重要。"""
    t0 = datetime.combine(DAY.date(), datetime.strptime(start, "%H:%M").time())
    bars = [ClosedBar(MinuteBar(t0 + timedelta(minutes=i), o, max(o, c), min(o, c), c), 0)
            for i, (o, c) in enumerate(zip(opens, closes))]
    bars[-1] = replace(bars[-1], bichai=bichai)
    return tuple(bars)


def _after(bars: tuple, seconds: int = 0) -> datetime:
    """最後一根結束的那一刻（下一分鐘的第 `seconds` 秒）。"""
    return bars[-1].bar.start + timedelta(minutes=1, seconds=seconds)


def _long(start: str = "08:45", **overrides) -> Situation:
    """條件 1 全部成立：開高 45100 > 45000、每根紅K、筆差 1500、收盤一路漲（RSI 100）、沒有部位。"""
    bars = _bars(start, [45100, 45110, 45120, 45130, 45140, 45150],
                 [45110, 45120, 45130, 45140, 45150, 45160], 1500)
    s = Situation(bars=bars, today_open=45100.0, yesterday_close=YESTERDAY_CLOSE,
                  now=_after(bars), position=None, product_price=45160.0,
                  lots_per_entry=1, max_position=20)
    return replace(s, **overrides)


def _short(start: str = "08:45", **overrides) -> Situation:
    """條件 2 全部成立：開低 44900 < 45000、每根黑K、筆差 −1500、收盤一路跌（RSI 0）、沒有部位。"""
    bars = _bars(start, [44900, 44890, 44880, 44870, 44860, 44850],
                 [44890, 44880, 44870, 44860, 44850, 44840], -1500)
    s = Situation(bars=bars, today_open=44900.0, yesterday_close=YESTERDAY_CLOSE,
                  now=_after(bars), position=None, product_price=44840.0,
                  lots_per_entry=1, max_position=20)
    return replace(s, **overrides)


def _with_last_bichai(s: Situation, bichai: int | None) -> Situation:
    return replace(s, bars=s.bars[:-1] + (replace(s.bars[-1], bichai=bichai),))


def _with_closes(s: Situation, closes: list[float]) -> Situation:
    bars = tuple(replace(b, bar=replace(b.bar, close=c)) for b, c in zip(s.bars, closes))
    return replace(s, bars=bars)


ENTER_LONG = Enter(LONG, Condition.EARLY_LONG, 1)
ENTER_SHORT = Enter(SHORT, Condition.EARLY_SHORT, 1)


def test_all_five_items_of_condition_1_enter_one_lot_long():
    assert strategy.decide(_long()) == ENTER_LONG


def test_all_five_items_of_condition_2_enter_one_lot_short():
    assert strategy.decide(_short()) == ENTER_SHORT


# --- 筆差（規格第 11、12 條）---


@pytest.mark.parametrize("bichai, expected", [
    (1000, None), (1001, ENTER_LONG), (None, None)])
def test_condition_1_needs_bichai_above_1000(bichai, expected):
    """恰好 1000 不算；None 是「還沒收到筆數」，不是 0，更不是成立。"""
    assert strategy.decide(_with_last_bichai(_long(), bichai)) == expected


@pytest.mark.parametrize("bichai, expected", [(-1000, None), (-1001, ENTER_SHORT)])
def test_condition_2_needs_bichai_below_minus_1000(bichai, expected):
    assert strategy.decide(_with_last_bichai(_short(), bichai)) == expected


# --- RSI ---


@pytest.mark.parametrize("closes, expected", [
    ([45000, 45007, 45004, 45004, 45004, 45004], None),         # 恰好 70
    ([45000, 45008, 45005, 45005, 45005, 45005], ENTER_LONG),   # 72.7
])
def test_condition_1_needs_rsi_above_70(closes, expected):
    assert strategy.decide(_with_closes(_long(), closes)) == expected


@pytest.mark.parametrize("closes, expected", [
    ([45000, 44993, 44996, 44996, 44996, 44996], None),         # 恰好 30
    ([45000, 44992, 44995, 44995, 44995, 44995], ENTER_SHORT),  # 27.3
])
def test_condition_2_needs_rsi_below_30(closes, expected):
    assert strategy.decide(_with_closes(_short(), closes)) == expected


def test_without_enough_closes_for_rsi_5_nothing_is_entered():
    """5 根收盤價算不出 RSI(5)。實際上暖身會接前一個交易日（規格第 4 條），不會走到這裡。"""
    s = _long()
    assert strategy.decide(replace(s, bars=s.bars[1:])) is None


def test_the_rsi_counts_the_warmup_bars_before_today():
    """暖身的 K 棒接在今天前面一起算（規格第 4 條）。只看今天的 6 根，收盤一路漲、RSI 100；
    前面接上前一天尾盤的連續暴跌，跌幅還留在 Wilder 的平滑裡，RSI 就過不了 70。"""
    s = _long()
    warmup = tuple(ClosedBar(MinuteBar(datetime(2026, 11, 6, 13, 35) + timedelta(minutes=i),
                                       c, c, c, c), None)
                   for i, c in enumerate([46000, 45000, 44000, 43000, 42000, 41000, 40000,
                                          39000, 38000, 45100]))
    assert strategy.decide(replace(s, bars=warmup + s.bars)) is None


# --- 開高／開低（規格第 17 條）---


@pytest.mark.parametrize("make", [_long, _short], ids=["做多的情境", "做空的情境"])
def test_an_open_equal_to_yesterdays_close_enters_neither_way(make):
    assert strategy.decide(make(today_open=YESTERDAY_CLOSE)) is None


def test_a_day_that_opened_high_never_goes_short():
    assert strategy.decide(_short(today_open=45001.0)) is None


def test_a_day_that_opened_low_never_goes_long():
    assert strategy.decide(_long(today_open=44999.0)) is None


def test_without_todays_0845_open_nothing_is_entered():
    """沒有今天 08:45 那根（開盤時報價斷了），就不知道今天是開高還是開低。"""
    assert strategy.decide(_long(today_open=None)) is None


# --- 紅K／黑K（規格第 6 條）---


def _with_last_open(s: Situation, open_: float) -> Situation:
    last = s.bars[-1]
    return replace(s, bars=s.bars[:-1] + (replace(last, bar=replace(last.bar, open=open_)),))


def test_red_k_is_an_open_above_the_previous_bars_open_not_a_close_above_its_own_open():
    """作者的定義：這根的開盤價高於**上一根的開盤價**。最後一根開 45150、收 45160 是一般說的
    陽線，但開盤價和上一根（45140）比——相等就不是紅K。"""
    s = _long()
    assert strategy.decide(_with_last_open(s, 45140)) is None
    assert strategy.decide(_with_last_open(s, 45141)) == ENTER_LONG


def test_a_bar_closing_below_its_own_open_is_still_red_if_it_opened_above_the_previous():
    s = _with_last_open(_long(), 45170)       # 開 45170、收 45160：一般說的陰線
    assert strategy.decide(s) == ENTER_LONG


def test_black_k_is_an_open_below_the_previous_bars_open():
    s = _short()
    assert strategy.decide(_with_last_open(s, 44860)) is None
    assert strategy.decide(_with_last_open(s, 44859)) == ENTER_SHORT


def test_the_previous_bar_must_be_the_minute_right_before():
    """少了一分鐘（那一分鐘報價斷了）時，「上一根」不是上一分鐘——不拿兩分鐘前的比。
    08:50 那根沒有：08:51 那根的開盤價高於 08:49 那根，但那不是紅K的定義。"""
    bars = _bars("08:45", [45100, 45110, 45120, 45130, 45140, 45150, 45160],
                 [45110, 45120, 45130, 45140, 45150, 45160, 45170], 1500)
    with_gap = bars[:5] + bars[6:]
    s = _long(bars=with_gap, now=_after(with_gap))
    assert strategy.decide(s) is None
    assert strategy.decide(replace(s, bars=bars)) == ENTER_LONG, "沒有缺的時候要成立"


# --- 部位不虧（規格第 13、14 條）---


@pytest.mark.parametrize("price, expected", [(45160.0, ENTER_LONG), (45159.0, None)])
def test_holding_long_needs_the_product_price_at_or_above_the_average(price, expected):
    """損益恰好 0 算不虧。用的是交易商品的價格與均價，不是大台的。"""
    s = _long(position=Position(LONG, 2, 45160.0), product_price=price)
    assert strategy.decide(s) == expected


@pytest.mark.parametrize("price, expected", [(44850.0, ENTER_SHORT), (44851.0, None)])
def test_holding_short_needs_the_product_price_at_or_below_the_average(price, expected):
    s = _short(position=Position(SHORT, 2, 44850.0), product_price=price)
    assert strategy.decide(s) == expected


def test_holding_without_a_product_price_is_not_taken_as_not_losing():
    s = _long(position=Position(LONG, 1, 45160.0), product_price=None)
    assert strategy.decide(s) is None


# --- 時間（08:50 開始，09:45（含）截止）---


@pytest.mark.parametrize("start, expected", [
    ("08:43", None),             # 最後一根 08:48，08:49 判斷
    ("08:44", ENTER_LONG),       # 最後一根 08:49，08:50 判斷
    ("09:39", ENTER_LONG),       # 最後一根 09:44，09:45 判斷
    ("09:40", None),             # 最後一根 09:45，09:46 判斷
])
def test_conditions_1_and_2_are_judged_from_0850_through_0945(start, expected):
    assert strategy.decide(_long(start)) == expected


def test_0945_includes_the_whole_minute():
    """讓 09:44 那根結束的成交不一定剛好在 09:45:00。"""
    s = _long("09:39")
    assert strategy.decide(replace(s, now=_after(s.bars, seconds=59))) == ENTER_LONG


def test_a_bar_that_ended_more_than_a_minute_ago_is_not_judged():
    """K 棒要等下一筆成交才結束。中間整整一分鐘沒成交（報價中斷）的話，拿那根舊的 K 棒在
    現在的價格進場，和回測不是同一件事（規格第 53 條：報價中斷時不進場）。"""
    s = _long()
    assert strategy.decide(replace(s, now=_after(s.bars) + timedelta(minutes=1))) is None


# --- 加碼上限（規格第 25 條）---


def test_at_the_position_cap_nothing_more_is_entered():
    s = _long(position=Position(LONG, 20, 45000.0))
    assert strategy.decide(s) is None


def test_an_entry_is_cut_to_what_is_left_under_the_cap():
    """每次進 3 口、上限 20、已有 18 口：只剩 2 口的空間。"""
    s = _long(position=Position(LONG, 18, 45000.0), lots_per_entry=3)
    assert strategy.decide(s) == Enter(LONG, Condition.EARLY_LONG, 2)


def test_each_entry_is_the_configured_number_of_lots():
    assert strategy.decide(_long(lots_per_entry=3)) == Enter(LONG, Condition.EARLY_LONG, 3)
