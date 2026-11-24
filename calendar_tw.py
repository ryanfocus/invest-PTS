"""台灣期貨的日曆：哪些日子是交易日（從 invest-os 的同名模組搬來），以及日盤的時段。

非交易日盤中程式要完全不動作：不登入、不發訊息（規格第 40 條）。判斷錯了有兩種後果：
把交易日當成假日 → 整天沒有進場；把假日當成交易日 → 登入後拿到的是舊資料。

⚠️ **颱風假這類臨時休市，這裡不知道。** `holidays` 只認得國定假日。臨時休市那天
   程式會照常登入，拿到的是前一個交易日的報價，由盤中流程的報價交易日檢查擋下來
   （規格第 10 條）。invest-os 原本有手動覆寫的參數，2026-09-03 拿掉了：
   從沒用過，而「要記得事先填」本身就是最容易漏的那種步驟。
   反過來的情況（臨時開市而 `holidays` 不知道）那天會被靜默跳過，這是刻意接受的取捨。

農曆春節前「市場無交易，僅辦理結算交割作業」的兩天，`holidays` 也不知道（不是國定假日）。
那兩天期交所不交易，盤中程式卻會登入、整天收不到今天的成交、08:48 發一則假的報價中斷告警。
證交所的慣例是春節連假（含除夕前的調整放假）開始前的最後兩個平日，2023～2027 的公告都是這樣
（tests/test_calendar.py 逐年列了公告的日期），這裡照這個慣例推算。哪一年證交所改了天數，那個測試
不會知道——每年 11 月左右證交所公告隔年的開休市日期時，對一次。

⚠️ **這裡沒有結算日判定。** 結算日由商品清單查到的最後交易日決定，不用「第三個週三」
   推算——遇假日順延時算式會錯（規格第 24 條）。
"""

from __future__ import annotations

import functools
from datetime import date, time, timedelta

import holidays

_TW_HOLIDAYS = holidays.country_holidays("TW")

# 大台日盤（一般交易時段）08:45–13:45。作者的回測只用日盤 1 分 K，不含夜盤（規格第 2 條）。
# 組 K 棒（`indicators`）、解析群益的 1 分 K（`broker.capital_wire`）、出場程式送單的期限
# （`exit_program`）都看這同一個時段：交易所哪天調整時段，只改這裡。
DAY_SESSION_OPEN = time(8, 45)
DAY_SESSION_CLOSE = time(13, 45)


def is_trading_day(day: date) -> bool:
    """`day` 是不是台灣期貨的交易日：平日、不是國定假日，也不是農曆春節前只辦交割的那兩天。"""
    return _weekday_not_holiday(day) and day not in _settlement_only_days(day.year)


def _weekday_not_holiday(day: date) -> bool:
    return day.weekday() < 5 and day not in _TW_HOLIDAYS


@functools.lru_cache(maxsize=None)
def _settlement_only_days(year: int) -> frozenset[date]:
    """那一年農曆春節前「市場無交易，僅辦理結算交割作業」的兩天：除夕之前的最後兩個平日（跳過週末與
    除夕前的調整放假）。`holidays` 查不到除夕（套件改版、改了名稱）就當作沒有，退回只看國定假日——
    寧可那兩天多一則告警，也不要猜錯把交易日當成休市。"""
    names = holidays.country_holidays("TW", years=year, language="zh_TW")
    eves = [d for d, name in names.items() if "除夕" in name]
    if not eves:
        return frozenset()
    day = min(eves)
    found: list[date] = []
    while len(found) < 2:
        day -= timedelta(days=1)
        if _weekday_not_holiday(day):
            found.append(day)
    return frozenset(found)
