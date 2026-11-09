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
一分鐘只判斷一次，一次最多進一筆，「同一分鐘只進一口」（規格第 16 條）因此由結構保證。

目前只有條件 1、2（ticket 04）。條件 3、4 與突破進場在 ticket 05 加進來。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Sequence

import indicators

# ── 策略常數（只在這裡）──────────────────────────────────────────────────

BICHAI_THRESHOLD = 1000            # 條件 1：筆差 > 1000；條件 2：筆差 < −1000
RSI_PERIOD = 5                     # RSI(5)，用最近 5 根 1 分 K 的收盤價（規格第 3 條）
RSI_LONG = 70                      # 條件 1：RSI > 70（動能確認，不是過熱）
RSI_SHORT = 30                     # 條件 2：RSI < 30
EARLY_START = time(8, 50)          # 條件 1、2 從 08:50 開始判斷
EARLY_END = time(9, 45)            # 到 09:45（含）
ENTRY_CUTOFF = time(13, 20)        # 進場截止：之後不再有任何新進場，盤中程式收工

LONG = "LONG"
SHORT = "SHORT"

_MINUTE = timedelta(minutes=1)


class Condition(enum.Enum):
    """觸發條件。值是 GLOSSARY 的名字，訊息裡直接用它。"""

    EARLY_LONG = "條件 1（早盤做多）"
    EARLY_SHORT = "條件 2（早盤做空）"


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


@dataclass(frozen=True)
class Enter:
    """市價進場。`lots` 已經扣掉加碼上限剩下的空間。"""

    direction: str
    condition: Condition
    lots: int


def decide(s: Situation) -> Enter | None:
    """這一分鐘要不要進場。None 是不動作。"""
    if not s.bars or not _fresh(s):
        return None
    minute = s.now.time().replace(second=0, microsecond=0)
    if not EARLY_START <= minute <= EARLY_END:
        return None
    lots = min(s.lots_per_entry, s.max_position - (s.position.lots if s.position else 0))
    if lots <= 0:
        return None

    last = s.bars[-1]
    previous = s.bars[-2] if len(s.bars) >= 2 else None
    if previous is None or previous.bar.start + _MINUTE != last.bar.start:
        return None                 # 「上一根」必須是上一分鐘那根
    value = rsi(s.bars)
    if s.today_open is None or value is None or last.bichai is None or not _not_losing(s):
        return None
    rising = last.bar.open > previous.bar.open        # 紅K（作者的定義，規格第 6 條）
    falling = last.bar.open < previous.bar.open       # 黑K
    if (s.today_open > s.yesterday_close and last.bichai > BICHAI_THRESHOLD
            and rising and value > RSI_LONG):
        return Enter(LONG, Condition.EARLY_LONG, lots)
    if (s.today_open < s.yesterday_close and last.bichai < -BICHAI_THRESHOLD
            and falling and value < RSI_SHORT):
        return Enter(SHORT, Condition.EARLY_SHORT, lots)
    return None


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
