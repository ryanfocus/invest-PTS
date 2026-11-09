"""台灣期貨的日曆：哪些日子是交易日（從 invest-os 的同名模組搬來），以及日盤的時段。

非交易日盤中程式要完全不動作：不登入、不發訊息（規格第 40 條）。判斷錯了有兩種後果：
把交易日當成假日 → 整天沒有進場；把假日當成交易日 → 登入後拿到的是舊資料。

⚠️ **颱風假這類臨時休市，這裡不知道。** `holidays` 只認得國定假日。臨時休市那天
   程式會照常登入，拿到的是前一個交易日的報價，由盤中流程的報價交易日檢查擋下來
   （規格第 10 條）。invest-os 原本有手動覆寫的參數，2026-09-03 拿掉了：
   從沒用過，而「要記得事先填」本身就是最容易漏的那種步驟。
   反過來的情況（臨時開市而 `holidays` 不知道）那天會被靜默跳過，這是刻意接受的取捨。

⚠️ **這裡沒有結算日判定。** 結算日由商品清單查到的最後交易日決定，不用「第三個週三」
   推算——遇假日順延時算式會錯（規格第 24 條）。
"""

from __future__ import annotations

from datetime import date, time

import holidays

_TW_HOLIDAYS = holidays.country_holidays("TW")

# 大台日盤（一般交易時段）08:45–13:45。作者的回測只用日盤 1 分 K，不含夜盤（規格第 2 條）。
# 組 K 棒（`indicators`）、解析群益的 1 分 K（`broker.capital_wire`）、出場程式送單的期限
# （`exit_program`）都看這同一個時段：交易所哪天調整時段，只改這裡。
DAY_SESSION_OPEN = time(8, 45)
DAY_SESSION_CLOSE = time(13, 45)


def is_trading_day(day: date) -> bool:
    """`day` 是不是台灣期貨的交易日：平日，而且不是國定假日。"""
    return day.weekday() < 5 and day not in _TW_HOLIDAYS
