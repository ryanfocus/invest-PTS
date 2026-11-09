"""指標（純函式）：由大台的成交組成日盤 1 分 K，並在每根結束時記下筆差。

規格要求指標是純函式：同樣的事件序列，永遠得到同樣的 K 棒。這讓盤中重啟時可以把
原始行情檔照順序重播一次，得到和當時一樣的筆差（ticket 09；群益的筆差沒有歷史回補）。
狀態是一個不可變的 `BarState`，每個事件回傳新的狀態，沒有 I/O。

RSI 也在這裡：期數由策略判斷給（策略常數只出現在那一處）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from typing import Sequence

from broker import MinuteBar, TxTrade, TxTradeCounts

# 大台日盤（一般交易時段）。作者的回測只用日盤 1 分 K，不含夜盤（規格第 2 條）。
DAY_SESSION_OPEN = time(8, 45)
DAY_SESSION_CLOSE = time(13, 45)

_MINUTE = timedelta(minutes=1)

# K 棒結束時拿來當筆差的累計筆數，最晚可以是那一分鐘結束後多久收到的。讓 K 棒結束的是下一分鐘
# 的第一筆成交，累計筆數可能比它先到一點；晚得比這還多，就是那根很晚才結束（重啟後回補的
# 舊成交），手上的筆數已經是好幾分鐘之後的累計了。
_COUNTS_AFTER_BAR = timedelta(seconds=30)

# 相鄰兩根 K 棒的筆數，取到的位置（這一根開始之後第幾秒收到的）最多可以差多少。筆差跳增是
# 兩根筆差相減，代表的是兩次取到之間的累積：位置一樣就正好一分鐘；上一根早了 X 秒取到，
# 就變成一分鐘又 X 秒的累積，跳增被放大 X/60。這個寬容度就是可以接受的放大上限（約 17%）。
# 群益的筆數照固定間隔推來的話，每一根取到的位置都一樣；推得很密的話都在結尾附近。差得比
# 這還多，就是筆數停過（報價中斷、只有筆數停住）。
_COUNTS_DRIFT = timedelta(seconds=10)


@dataclass(frozen=True)
class ClosedBar:
    """一根結束了的 1 分 K，以及**它結束那一刻**的筆差（成交賣出筆數 − 成交買進筆數）。

    `bichai` 是 None 代表不知道——是「不知道」，不是 0。筆差 0 是「買賣筆數相等」，
    一個會被拿去判斷的值。不知道的情況見 `_bichai_at_close`。
    """

    bar: MinuteBar
    bichai: int | None


@dataclass(frozen=True)
class BarState:
    """組 K 棒到目前為止的狀態。`forming` 是還沒結束的那一根；`counts` 是最新的累計筆數；
    `counted_at` 是上一根結束時用的筆數，在那一根開始之後第幾秒收到（沒取到是 None）。"""

    forming: MinuteBar | None = None
    counts: TxTradeCounts | None = None
    counted_at: timedelta | None = None


def in_day_session(t: datetime) -> bool:
    """`t` 的時刻在不在日盤（08:45–13:45）。

    ⚠️ 看時刻，不只看日期：夜盤的成交算在次日，清晨 05:00 以前的夜盤成交帶的是今天的日期。
    """
    return DAY_SESSION_OPEN <= t.time() < DAY_SESSION_CLOSE


def add_counts(state: BarState, counts: TxTradeCounts) -> BarState:
    """記下最新的累計筆數。它不會讓 K 棒結束：它的時間是本機收到的時刻，不是交易所的。"""
    return replace(state, counts=counts)


def add_trade(state: BarState, trade: TxTrade) -> tuple[BarState, ClosedBar | None]:
    """加入一筆大台成交。這筆成交屬於新的一分鐘時，上一根就此結束並交出來。

    K 棒的分界**只看成交的時間**（交易所時間），理由見 `intraday` 的模組說明。代價是
    整整一分鐘沒有成交時，那一根要等到下一筆成交才結束；大台日盤實際上不會發生。
    那樣的分鐘也不會有一根 K 棒（不自己造一根平的出來）。

    不收的成交：日盤以外的，以及屬於**已經結束的那一分鐘**的（遲到的成交）——K 棒一交出去
    就被拿去判斷了，不能回頭改，也不能為它再開一根舊的。
    """
    if not in_day_session(trade.time):
        return state, None
    minute = trade.time.replace(second=0, microsecond=0)
    price = trade.price
    forming = state.forming
    if forming is not None and minute < forming.start:
        return state, None
    if forming is not None and minute == forming.start:
        return replace(state, forming=replace(
            forming, high=max(forming.high, price), low=min(forming.low, price),
            close=price)), None
    if forming is None:
        return replace(state, forming=MinuteBar(minute, price, price, price, price)), None
    state, closed = _close(state)
    return replace(state, forming=MinuteBar(minute, price, price, price, price)), closed


def close_forming(state: BarState) -> tuple[BarState, ClosedBar | None]:
    """不等下一筆成交，直接結束正在組的那一根（13:20 收工時用）。"""
    if state.forming is None:
        return state, None
    state, closed = _close(state)
    return replace(state, forming=None), closed


def _close(state: BarState) -> tuple[BarState, ClosedBar]:
    """結束正在組的那一根：交出它與結束這一刻的筆差，並記下這次的筆數在它開始之後第幾秒收到。

    筆差 = 最新的成交賣出筆數 − 成交買進筆數（GLOSSARY），正值解讀為大戶在買。
    **手上的累計筆數不是「這一根結束時」的值，筆差就是不知道**（None）。兩道關：

      * **要在這一根的時間裡收到**：這一根開始之後、結束後 `_COUNTS_AFTER_BAR` 之內。
        太早是當機、報價中斷**之前**的值；太晚是晚啟動、重啟後群益回補的舊成交，K 棒結束得
        很晚，手上的是**之後**好幾分鐘的值。
      * **要和上一根在差不多的位置取到**（`_COUNTS_DRIFT`）。筆數停了一陣子（報價中斷，或只有
        筆數停住、成交照常）時，停住前那一根拿到的是開頭附近的值，恢復後那一根拿到的是新的：
        兩者相減是一分多鐘的累積，不是一分鐘的——中斷前後的差距就被誤判成「突然大量買進」，
        條件 3、4 送出追價單（ticket 09）。前後兩根都當成不知道：停住前那一根比它的上一根早取到，
        恢復後那一根比停住前那一根晚取到。

    這兩條只看事件自己的時間，不看程式在不在、成交是不是即時的：重啟時把原始行情檔照順序
    重播一次，得到的就是當時的筆差（群益的筆差沒有歷史回補，只能從那裡讀回）。
    累計筆數的時間是本機收到的時刻，K 棒的分界是交易所時間，兩者差個一兩秒無妨。

    ⚠️ 前提是群益的累計筆數照固定間隔推（不超過一分鐘一次），或推得很密。推送頻率還沒實機確認
       （ticket 03）；忽密忽疏的話很多 K 棒的筆差都會是不知道，條件就很少成立——log 裡每根
       K 棒的「筆差 未知」看得出來。
    """
    counts, bar = state.counts, state.forming
    if counts is None or not bar.start <= counts.time < bar.start + _MINUTE + _COUNTS_AFTER_BAR:
        return replace(state, counted_at=None), ClosedBar(bar, None)
    counted_at = counts.time - bar.start
    bichai: int | None = counts.sell_count - counts.buy_count
    if state.counted_at is not None and abs(counted_at - state.counted_at) > _COUNTS_DRIFT:
        bichai = None
    return replace(state, counted_at=counted_at), ClosedBar(bar, bichai)


# ── RSI ─────────────────────────────────────────────────────────────────


def rsi(closes: Sequence[float], period: int) -> float | None:
    """以 `closes` 最後一筆為準的 Wilder RSI（0–100）。不到 `period + 1` 筆時是 None。

    移植 invest-WOIC `indicators/rsi.py` 的 wilder 算法（已對照 StockCharts 範例驗證，
    同一份 fixture 在 tests/test_rsi.py）：第一筆平均 = 前 N 期總和 ÷ N；之後 =
    (前值 × (N−1) + 當期) ÷ N。平均跌幅 0 → 100、平均漲幅 0 → 0（StockCharts 的定義）。

    ⚠️ Wilder 的遞推受起點影響，所以呼叫端要接上暖身資料（規格第 4 條）。PTS 用前一個
       交易日的整個日盤（約 300 根）：起點的權重剩 (4/5)^300，08:50 一開始就是收斂後的值。
    期數由呼叫端給：RSI 期數是策略常數，只出現在策略判斷一處。
    """
    changes = [b - a for a, b in zip(closes, closes[1:])]
    if len(changes) < period:
        return None
    gains = [max(c, 0.0) for c in changes]
    losses = [max(-c, 0.0) for c in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 * avg_gain / (avg_gain + avg_loss)
