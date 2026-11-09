"""指標（純函式）：由大台的成交組成日盤 1 分 K，並在每根結束時記下筆差。

規格要求指標是純函式：同樣的事件序列，永遠得到同樣的 K 棒。這讓盤中重啟時可以把
原始行情檔照順序重播一次，得到和當時一樣的筆差（ticket 09；群益的筆差沒有歷史回補）。
狀態是一個不可變的 `BarState`，每個事件回傳新的狀態，沒有 I/O。

ticket 04 的 RSI(5) 也放在這裡。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time

from broker import MinuteBar, TxTrade, TxTradeCounts

# 大台日盤（一般交易時段）。作者的回測只用日盤 1 分 K，不含夜盤（規格第 2 條）。
DAY_SESSION_OPEN = time(8, 45)
DAY_SESSION_CLOSE = time(13, 45)


@dataclass(frozen=True)
class ClosedBar:
    """一根結束了的 1 分 K，以及**它結束那一刻**的筆差（成交賣出筆數 − 成交買進筆數）。

    `bichai` 是 None 代表那時還沒收到任何累計筆數——是「不知道」，不是 0。
    筆差 0 是「買賣筆數相等」，一個會被拿去判斷的值。
    """

    bar: MinuteBar
    bichai: int | None


@dataclass(frozen=True)
class BarState:
    """組 K 棒到目前為止的狀態。`forming` 是還沒結束的那一根；`counts` 是最新的累計筆數。"""

    forming: MinuteBar | None = None
    counts: TxTradeCounts | None = None


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
    closed = ClosedBar(forming, _bichai(state.counts)) if forming is not None else None
    return replace(state, forming=MinuteBar(minute, price, price, price, price)), closed


def close_forming(state: BarState) -> tuple[BarState, ClosedBar | None]:
    """不等下一筆成交，直接結束正在組的那一根（13:20 收工時用）。"""
    if state.forming is None:
        return state, None
    return replace(state, forming=None), ClosedBar(state.forming, _bichai(state.counts))


def _bichai(counts: TxTradeCounts | None) -> int | None:
    """筆差 = 成交賣出筆數 − 成交買進筆數（GLOSSARY）。正值解讀為大戶在買。"""
    return None if counts is None else counts.sell_count - counts.buy_count
