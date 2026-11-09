"""策略判斷（純函式，整個策略的核心）。

給一個 `Situation`（到目前為止的大台 1 分 K、昨收、今日開盤價、現在時刻、目前部位…），
回答這一分鐘要不要進場。沒有 I/O、不讀時鐘、不碰 broker：同樣的輸入永遠得到同樣的答案，
所以每一項條件的邊界都能用表格測（tests/test_strategy.py）。

**所有策略常數只出現在這裡**（規格「策略判斷」一節）。改常數等於換了一個策略，所以它們
不在設定檔、也不散在流程裡；流程要用（例如 13:20 收工）就從這裡 import。

## 什麼時候判斷

一根 1 分 K 結束時判斷一次，用的是**剛結束的那一根**——和作者的 1 分 K 回測一致：
回測裡 08:50 能用的最新資料就是 08:49 那根。所以「08:50 開始」的第一次判斷是 08:49 那根
結束的時候，那時今天正好有 08:45–08:49 五根（規格第 4 條講的「當天那 5 根」）。
一分鐘只判斷一次，一次最多進一筆，「同一分鐘只進一口」（規格第 16 條）因此由結構保證：
條件 1 和條件 3 同時成立時回答的是條件 1（編號小的那個）。

## 三種回答

  * `Enter`：立刻市價進場。條件 1、2（08:50–09:45），以及條件 3、4 在 09:45（含）以前。
  * `Breakout`：設定突破進場。條件 3、4 在 09:45 之後到 13:20，觸發價是最近 5 根的最高點
    （空方為最低點）。盯價的是盤中流程（ADR-0001）：它把等待中的那一筆留著，下一次判斷時
    放回 `Situation.breakout`，每一筆大台成交問 `Breakout.touched_by`。
  * None：不動作。

突破進場同一時間最多一筆在等待；它佔的口數算進加碼上限（規格第 21、25 條）。
09:45 以前的進場和 09:46 以後設下的突破進場不會落在同一分鐘，所以突破進場也守得住
「同一分鐘只進一口」。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Sequence

import indicators

# ── 策略常數（只在這裡）──────────────────────────────────────────────────

BICHAI_THRESHOLD = 1000            # 條件 1、3：筆差 > 1000；條件 2、4：筆差 < −1000
BICHAI_JUMP = 100                  # 筆差跳增：這根收盤時的筆差減上一根 ≥ 100（空方 ≤ −100）
RSI_PERIOD = 5                     # RSI(5)，用最近 5 根 1 分 K 的收盤價（規格第 3 條）
RSI_LONG = 70                      # 條件 1：RSI > 70（動能確認，不是過熱）
RSI_SHORT = 30                     # 條件 2：RSI < 30
EARLY_START = time(8, 50)          # 條件 1、2 從 08:50 開始判斷
EARLY_END = time(9, 45)            # 到 09:45（含）；條件 3、4 在這之後改用突破進場
BREAKOUT_BARS = 5                  # 突破進場看最近 5 根 1 分 K 的最高點（空方為最低點）
ENTRY_CUTOFF = time(13, 20)        # 進場截止：之後不再有任何新進場，盤中程式收工

LONG = "LONG"
SHORT = "SHORT"

_MINUTE = timedelta(minutes=1)


class Condition(enum.Enum):
    """觸發條件。值是 GLOSSARY 的名字，訊息裡直接用它。"""

    EARLY_LONG = "條件 1（早盤做多）"
    EARLY_SHORT = "條件 2（早盤做空）"
    CHASE_LONG = "條件 3（追多加碼）"
    CHASE_SHORT = "條件 4（追空加碼）"


@dataclass(frozen=True)
class Position:
    """PTS 今天的部位。一天只會有一個方向（開高只做多、開低只做空）。"""

    direction: str          # LONG / SHORT
    lots: int
    avg_price: float        # **交易商品**的成交均價（規格第 14 條）


@dataclass(frozen=True)
class Situation:
    """策略判斷的全部輸入。

    `bars` 是暖身（前一個交易日的日盤）接上今天、依時間排序的大台 1 分 K；暖身的筆差是
    None（群益沒有筆差的歷史）。`now` 是判斷的這一刻：讓那一根結束的事件時間。
    """

    bars: Sequence[indicators.ClosedBar]
    today_open: float | None            # 今天 08:45 那根的開盤價；沒有那根就是不知道
    yesterday_close: float              # 前一交易日大台日盤最後一根的收盤價
    now: datetime
    position: Position | None
    product_price: float | None         # 交易商品的現價（最新一筆成交）
    lots_per_entry: int
    max_position: int                   # 加碼上限
    breakout: Breakout | None           # 等待中的突破進場
    # 近月合約的結算日（商品清單的最後交易日）。None 是查不到：不知道是不是結算週
    settlement_day: date | None


@dataclass(frozen=True)
class Enter:
    """市價進場。`lots` 已經扣掉加碼上限剩下的空間。"""

    direction: str
    condition: Condition
    lots: int


@dataclass(frozen=True)
class Breakout:
    """突破進場：大台價格碰到 `trigger` 時，市價進 `lots` 口（ADR-0001：由程式盯價，
    不在券商端掛單）。`decide` 回傳它代表「設定突破進場」；流程把它留著當作等待中的那一筆，
    每一筆大台成交都問 `touched_by`。

    `lots` 在設定的那一刻就從加碼上限裡扣掉了（規格第 25 條），所以成交時照它進，不再重算。
    """

    direction: str
    condition: Condition
    trigger: float
    lots: int

    def touched_by(self, price: float) -> bool:
        """大台價格碰到觸發價了沒：碰到就算，不必超過。"""
        return price >= self.trigger if self.direction == LONG else price <= self.trigger


def decide(s: Situation) -> Enter | Breakout | None:
    """這一分鐘要不要進場：立刻進場、設定突破進場，或 None（不動作）。"""
    if not s.bars or not _fresh(s):
        return None
    minute = s.now.time().replace(second=0, microsecond=0)
    if minute >= ENTRY_CUTOFF:
        return None
    held = s.position.lots if s.position else 0
    waiting = s.breakout.lots if s.breakout else 0
    lots = min(s.lots_per_entry, s.max_position - held - waiting)
    if lots <= 0:
        return None

    last = s.bars[-1]
    previous = s.bars[-2] if len(s.bars) >= 2 else None
    if previous is None or previous.bar.start + _MINUTE != last.bar.start:
        return None                 # 「上一根」必須是上一分鐘那根
    if EARLY_START <= minute <= EARLY_END:
        early = _early(s, last, previous)
        if early is not None:
            return Enter(*early, lots)
    chase = _chase(s, last, previous)
    if chase is None:
        return None
    if minute <= EARLY_END:
        return Enter(*chase, lots)
    if s.breakout is not None:
        # 前一筆還沒突破，可能是盤整或翻轉：不追更高（空方是更低）的價位（規格第 21 條）
        return None
    direction, condition = chase
    recent = [b.bar for b in s.bars[-BREAKOUT_BARS:]]
    trigger = (max(b.high for b in recent) if direction == LONG
               else min(b.low for b in recent))
    return Breakout(direction, condition, trigger, lots)


def _early(s: Situation, last: indicators.ClosedBar,
           previous: indicators.ClosedBar) -> tuple[str, Condition] | None:
    """條件 1、2：筆差、開高／開低、紅K／黑K、RSI、部位不虧，五項同時成立。"""
    value = rsi(s.bars)
    if s.today_open is None or value is None or last.bichai is None or not _not_losing(s):
        return None
    rising = last.bar.open > previous.bar.open        # 紅K（作者的定義，規格第 6 條）
    falling = last.bar.open < previous.bar.open       # 黑K
    if (s.today_open > s.yesterday_close and last.bichai > BICHAI_THRESHOLD
            and rising and value > RSI_LONG):
        return LONG, Condition.EARLY_LONG
    if (s.today_open < s.yesterday_close and last.bichai < -BICHAI_THRESHOLD
            and falling and value < RSI_SHORT):
        return SHORT, Condition.EARLY_SHORT
    return None


def _chase(s: Situation, last: indicators.ClosedBar,
           previous: indicators.ClosedBar) -> tuple[str, Condition] | None:
    """條件 3、4：已持有同方向的部位、筆差過門檻，而且出現筆差跳增。不要求部位不虧
    （規格第 22 條）。結算週停用。"""
    if s.position is None or last.bichai is None or previous.bichai is None:
        return None
    if s.settlement_day is None or in_settlement_week(s.now.date(), s.settlement_day):
        return None                 # 不知道結算日的時候寧可少做
    jump = last.bichai - previous.bichai
    if (s.position.direction == LONG and last.bichai > BICHAI_THRESHOLD
            and jump >= BICHAI_JUMP):
        return LONG, Condition.CHASE_LONG
    if (s.position.direction == SHORT and last.bichai < -BICHAI_THRESHOLD
            and jump <= -BICHAI_JUMP):
        return SHORT, Condition.CHASE_SHORT
    return None


def in_settlement_week(day: date, settlement_day: date) -> bool:
    """`day` 是不是落在結算週的停用期間：結算日那一週的週一到結算日（含）（規格第 23 條）。

    外資、內資從 12:30 起轉倉，近月的筆差會下滑，條件 3、4 會被它騙進場。結算日由商品清單
    給（規格第 24 條），不用「第三個週三」推算：颱風假順延到週四時，停用範圍跟著延長到週四。
    """
    monday = settlement_day - timedelta(days=settlement_day.weekday())
    return monday <= day <= settlement_day


def rsi(bars: Sequence[indicators.ClosedBar]) -> float | None:
    """策略用的 RSI(5)。流程也拿它寫 log，實機時要和看盤軟體核對（規格 Further Notes 2）。"""
    return indicators.rsi([b.bar.close for b in bars], RSI_PERIOD)


def _fresh(s: Situation) -> bool:
    """剛結束的那一根，是不是**現在的前一分鐘**。

    K 棒要等下一筆成交才結束；中間整整一分鐘沒有成交（實務上只有報價中斷），那根到現在
    才交出來。拿舊的 K 棒在現在的價格進場，和回測不是同一件事（規格第 53 條）。
    """
    return s.bars[-1].bar.start + _MINUTE == s.now.replace(second=0, microsecond=0)


def _not_losing(s: Situation) -> bool:
    """部位不虧：沒有部位算成立；有部位時，整個部位的未實現損益 ≥ 0（規格第 13、14 條）。"""
    if s.position is None:
        return True
    if s.product_price is None:
        return False
    sign = 1 if s.position.direction == LONG else -1
    return sign * (s.product_price - s.position.avg_price) * s.position.lots >= 0
