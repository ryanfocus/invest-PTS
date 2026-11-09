"""策略判斷的表格式測試（規格 Testing Decisions「策略判斷」）。

每一條都從一個「五項全部成立」的基準情境出發，只改一項，看它是不是剛好在邊界上翻轉。
期望值是 GLOSSARY 的定義：筆差 > 1000、RSI > 70、損益 ≥ 0 都是嚴格／不嚴格各有規定，
恰好等於門檻的那一格最容易寫錯。

RSI 恰好 70／30 的情境用 6 根收盤價（RSI(5) 的最少根數）手算：變動 +7、−3、0、0、0
→ 平均漲幅 7/5、平均跌幅 3/5 → RSI = 100 × 1.4 ÷ 2.0 = 70。
"""

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from broker import MinuteBar
from indicators import ClosedBar
import strategy
from strategy import LONG, SHORT, Breakout, Condition, Enter, Position, Situation

DAY = datetime(2026, 11, 9)                 # 週一
YESTERDAY_CLOSE = 45000.0
SETTLEMENT_DAY = date(2026, 11, 18)         # 下週三：DAY 不在結算週


def _bars(start: str, opens: list[float], closes: list[float], bichai: int | None) -> tuple:
    """從 `start` 起每分鐘一根，每一根結束時的筆差都是 `bichai`——沒有筆差跳增，
    要測條件 3、4 的情境自己改最後一根。"""
    t0 = datetime.combine(DAY.date(), datetime.strptime(start, "%H:%M").time())
    return tuple(ClosedBar(MinuteBar(t0 + timedelta(minutes=i), o, max(o, c), min(o, c), c),
                           bichai)
                 for i, (o, c) in enumerate(zip(opens, closes)))


def _after(bars: tuple, seconds: int = 0) -> datetime:
    """最後一根結束的那一刻（下一分鐘的第 `seconds` 秒）。"""
    return bars[-1].bar.start + timedelta(minutes=1, seconds=seconds)


def _long(start: str = "08:45", **overrides) -> Situation:
    """條件 1 全部成立：開高 45100 > 45000、每根紅K、筆差 1500、收盤一路漲（RSI 100）、沒有部位。"""
    bars = _bars(start, [45100, 45110, 45120, 45130, 45140, 45150],
                 [45110, 45120, 45130, 45140, 45150, 45160], 1500)
    s = Situation(bars=bars, today_open=45100.0, yesterday_close=YESTERDAY_CLOSE,
                  now=_after(bars), position=None, product_price=45160.0,
                  lots_per_entry=1, max_position=20, breakout=None,
                  settlement_day=SETTLEMENT_DAY)
    return replace(s, **overrides)


def _short(start: str = "08:45", **overrides) -> Situation:
    """條件 2 全部成立：開低 44900 < 45000、每根黑K、筆差 −1500、收盤一路跌（RSI 0）、沒有部位。"""
    bars = _bars(start, [44900, 44890, 44880, 44870, 44860, 44850],
                 [44890, 44880, 44870, 44860, 44850, 44840], -1500)
    s = Situation(bars=bars, today_open=44900.0, yesterday_close=YESTERDAY_CLOSE,
                  now=_after(bars), position=None, product_price=44840.0,
                  lots_per_entry=1, max_position=20, breakout=None,
                  settlement_day=SETTLEMENT_DAY)
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


# --- 條件 3、4：追價加碼（規格第 7、18、20、22 條）---


def _with_bichai(s: Situation, previous: int | None, last: int | None) -> Situation:
    """最後兩根結束時的筆差：上一根 `previous`、這一根 `last`。"""
    bars = s.bars[:-2] + (replace(s.bars[-2], bichai=previous), replace(s.bars[-1], bichai=last))
    return replace(s, bars=bars)


def _chase_long(start: str = "08:45", **overrides) -> Situation:
    """條件 3 成立、條件 1 不成立：持有 2 口多單而且在虧（均價 45200，現價 45160），
    筆差 1500 → 1600，恰好跳增 100。"""
    s = _with_bichai(_long(start, position=Position(LONG, 2, 45200.0)), 1500, 1600)
    return replace(s, **overrides)


def _chase_short(start: str = "08:45", **overrides) -> Situation:
    """條件 4 成立、條件 2 不成立：持有 2 口空單而且在虧（均價 44800，現價 44840），
    筆差 −1500 → −1600。"""
    s = _with_bichai(_short(start, position=Position(SHORT, 2, 44800.0)), -1500, -1600)
    return replace(s, **overrides)


CHASE_LONG = Enter(LONG, Condition.CHASE_LONG, 1)
CHASE_SHORT = Enter(SHORT, Condition.CHASE_SHORT, 1)


def test_condition_3_adds_a_lot_even_while_the_position_is_losing():
    assert strategy.decide(_chase_long()) == CHASE_LONG


def test_condition_4_adds_a_lot_even_while_the_position_is_losing():
    assert strategy.decide(_chase_short()) == CHASE_SHORT


@pytest.mark.parametrize("previous, last, expected", [
    (1500, 1599, None),           # 跳增 99
    (1500, 1600, CHASE_LONG),     # 恰好 100
    (900, 1000, None),            # 跳增 100，但筆差恰好 1000
    (901, 1001, CHASE_LONG),
    (None, 1600, None),           # 上一根的筆差不知道：算不出跳增，不是當成 0
])
def test_condition_3_needs_a_jump_of_at_least_100_and_bichai_above_1000(previous, last, expected):
    assert strategy.decide(_with_bichai(_chase_long(), previous, last)) == expected


@pytest.mark.parametrize("previous, last, expected", [
    (-1500, -1599, None), (-1500, -1600, CHASE_SHORT), (-900, -1000, None),
    (-901, -1001, CHASE_SHORT)])
def test_condition_4_needs_a_drop_of_at_least_100_and_bichai_below_minus_1000(
        previous, last, expected):
    assert strategy.decide(_with_bichai(_chase_short(), previous, last)) == expected


def test_condition_3_needs_a_long_position_already_held():
    """沒有部位時條件 3 不成立（開盤價不知道，條件 1 也不成立）；持有空單時（在虧，
    條件 1 也不成立），多方的筆差跳增不會讓它加碼做多。"""
    assert strategy.decide(_chase_long(position=None, today_open=None)) is None
    assert strategy.decide(_chase_long(position=Position(SHORT, 2, 45100.0))) is None


def test_condition_4_needs_a_short_position_already_held():
    assert strategy.decide(_chase_short(position=Position(LONG, 2, 44900.0))) is None


@pytest.mark.parametrize("make, expected", [
    (lambda: _with_bichai(_long(position=Position(LONG, 1, 45160.0)), 1500, 1600), ENTER_LONG),
    (lambda: _with_bichai(_short(position=Position(SHORT, 1, 44840.0)), -1500, -1600),
     ENTER_SHORT),
], ids=["條件 1 和 3", "條件 2 和 4"])
def test_when_two_conditions_hold_in_the_same_minute_only_one_lot_is_entered(make, expected):
    """同一分鐘條件 1 和條件 3 都成立，只進一口（規格第 16 條）。觸發條件寫編號小的那個。"""
    assert strategy.decide(make()) == expected


def test_condition_3_adds_at_once_through_0945():
    """09:44 那根在 09:45 判斷，仍然立刻加碼（09:45 含）。"""
    assert strategy.decide(_chase_long("09:39")) == CHASE_LONG


# --- 突破進場（規格第 19、20、21、25、26 條）---


def test_after_0945_condition_3_sets_a_breakout_at_the_highest_high_of_the_last_5_bars():
    """09:45 那根在 09:46 判斷：不再立刻加碼，改設突破進場。`_long` 每根的最高是收盤價，
    最近 5 根（09:41–09:45）最高 45160。"""
    assert strategy.decide(_chase_long("09:40")) == Breakout(LONG, Condition.CHASE_LONG, 45160, 1)


def test_after_0945_condition_4_sets_a_breakout_at_the_lowest_low_of_the_last_5_bars():
    assert strategy.decide(_chase_short("09:40")) == Breakout(SHORT, Condition.CHASE_SHORT,
                                                              44840, 1)


def _with_extremes(s: Situation, index: int, *, high: float | None = None,
                   low: float | None = None) -> Situation:
    bars = list(s.bars)
    bar = bars[index].bar
    bars[index] = replace(bars[index], bar=replace(bar, high=bar.high if high is None else high,
                                                   low=bar.low if low is None else low))
    return replace(s, bars=tuple(bars))


def test_the_breakout_looks_at_exactly_the_last_5_bars():
    """第 6 根之前的高點再高也不算；最近 5 根裡的最高點不一定是最後一根。"""
    s = _with_extremes(_with_extremes(_chase_long("09:40"), 0, high=45999), 2, high=45170)
    assert strategy.decide(s) == Breakout(LONG, Condition.CHASE_LONG, 45170, 1)
    s = _with_extremes(_with_extremes(_chase_short("09:40"), 0, low=44001), 2, low=44830)
    assert strategy.decide(s) == Breakout(SHORT, Condition.CHASE_SHORT, 44830, 1)


@pytest.mark.parametrize("start, expected", [
    ("13:13", Breakout(LONG, Condition.CHASE_LONG, 45160, 1)),   # 13:18 那根，13:19 判斷
    ("13:14", None),                                              # 13:19 那根，13:20 判斷
])
def test_no_breakout_is_set_from_1320(start, expected):
    """13:20 進場截止（規格第 26 條）：那時設下的突破進場也只會馬上被取消。"""
    assert strategy.decide(_chase_long(start)) == expected


def test_a_breakout_breaks_by_touching_the_trigger():
    """「碰到」最高點就算，不必超過；空方碰到最低點。"""
    long = Breakout(LONG, Condition.CHASE_LONG, 45160, 1)
    short = Breakout(SHORT, Condition.CHASE_SHORT, 44840, 1)
    assert [long.touched_by(p) for p in (45159, 45160, 45161)] == [False, True, True]
    assert [short.touched_by(p) for p in (44841, 44840, 44839)] == [False, True, True]


def test_a_jump_while_a_breakout_is_waiting_keeps_the_original_trigger():
    """等待中又出現筆差跳增：不設新的（規格第 21 條）。原本那筆成交之後（等待中的沒了），
    再出現筆差跳增就設新的。"""
    waiting = Breakout(LONG, Condition.CHASE_LONG, 45130, 1)
    s = _chase_long("09:40", position=Position(LONG, 3, 45200.0))
    assert strategy.decide(replace(s, breakout=waiting)) is None
    assert strategy.decide(s) == Breakout(LONG, Condition.CHASE_LONG, 45160, 1)


@pytest.mark.parametrize("held, lots_per_entry, expected", [
    (18, 3, Breakout(LONG, Condition.CHASE_LONG, 45160, 2)),
    (20, 1, None),
])
def test_a_breakout_is_cut_to_what_is_left_under_the_cap(held, lots_per_entry, expected):
    s = _chase_long("09:40", position=Position(LONG, held, 45200.0), lots_per_entry=lots_per_entry)
    assert strategy.decide(s) == expected


@pytest.mark.parametrize("waiting_lots, expected", [
    (2, None), (1, Enter(LONG, Condition.EARLY_LONG, 1))])
def test_the_cap_counts_a_waiting_breakout(waiting_lots, expected):
    """加碼上限 20、已有 18 口：等待中的突破進場佔 2 口就滿了（規格第 25 條）。"""
    waiting = Breakout(LONG, Condition.CHASE_LONG, 45300, waiting_lots)
    s = _long(position=Position(LONG, 18, 45000.0), breakout=waiting)
    assert strategy.decide(s) == expected


# --- 結算週（規格第 23、24 條）---


def _moved_to(s: Situation, day: date) -> Situation:
    """同一個情境，搬到 `day` 那一天的同一時刻。"""
    shift = datetime.combine(day, datetime.min.time()) - DAY
    bars = tuple(replace(b, bar=replace(b.bar, start=b.bar.start + shift)) for b in s.bars)
    return replace(s, bars=bars, now=s.now + shift)


MON, WED, THU, FRI = date(2026, 11, 9), date(2026, 11, 11), date(2026, 11, 12), date(2026, 11, 13)
LAST_FRI = date(2026, 11, 6)


@pytest.mark.parametrize("today, settlement_day, enabled", [
    (LAST_FRI, WED, True),        # 上週五：不在結算週
    (MON, WED, False),            # 週一開始停用
    (WED, WED, False),            # 結算日當天（含）
    (THU, WED, True),             # 結算日之後
    (MON, THU, False),            # 順延到週四：停用範圍跟著延長
    (WED, THU, False),
    (THU, THU, False),
    (FRI, THU, True),
], ids=["上週五", "週一", "週三結算日", "週三結算後的週四", "順延週四的週一", "順延週四的週三",
        "順延的週四結算日", "順延週四後的週五"])
def test_conditions_3_and_4_are_off_from_monday_through_the_settlement_day(
        today, settlement_day, enabled):
    """結算日取自商品清單的最後交易日（規格第 24 條），所以颱風假順延時不必自己推算。
    條件 3 的立刻加碼、條件 3 的突破進場、條件 4 都一樣。"""
    cases = [(_chase_long(), CHASE_LONG),
             (_chase_long("09:40"), Breakout(LONG, Condition.CHASE_LONG, 45160, 1)),
             (_chase_short(), CHASE_SHORT)]
    for s, entered in cases:
        s = replace(_moved_to(s, today), settlement_day=settlement_day)
        assert strategy.decide(s) == (entered if enabled else None)


def test_conditions_1_and_2_still_enter_in_the_settlement_week():
    assert strategy.decide(_long(settlement_day=WED)) == ENTER_LONG
    assert strategy.decide(_short(settlement_day=WED)) == ENTER_SHORT


def test_without_the_settlement_day_conditions_3_and_4_are_off():
    """商品清單查不到，就不知道今天是不是結算週。寧可少做：條件 3、4 停用。"""
    assert strategy.decide(_chase_long(settlement_day=None)) is None
    assert strategy.decide(_chase_short(settlement_day=None)) is None
    assert strategy.decide(_long(settlement_day=None)) == ENTER_LONG
