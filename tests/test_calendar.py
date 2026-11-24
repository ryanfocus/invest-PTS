"""交易日判定（從 invest-os 搬來）。

非交易日盤中程式要完全不動作（規格第 40 條）。判斷錯了有兩種後果：把交易日當成假日
→ 整天沒有進場；把假日當成交易日 → 登入後拿到的是舊資料（再由報價交易日檢查擋下）。

⚠️ **期望值的來源**：只斷言**固定日期的法定假日**（1/1、5/1、12/25）——日期與假日身分
都是不必查套件就知道的常識。農曆假日的西曆日期只能從 `holidays` 套件查到，拿它當期望值
就是用程式自己的來源驗證程式，不會失敗也不證明任何事，所以不寫。
"""

from datetime import date

import pytest

from calendar_tw import is_trading_day


@pytest.mark.parametrize("day", [date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 13)])
def test_ordinary_weekdays_are_trading_days(day):
    assert is_trading_day(day) is True


@pytest.mark.parametrize("day", [date(2026, 11, 7), date(2026, 11, 8)])
def test_weekends_are_not_trading_days(day):
    assert is_trading_day(day) is False


@pytest.mark.parametrize("day, name", [
    (date(2026, 1, 1), "中華民國開國紀念日"),
    (date(2026, 5, 1), "勞動節"),
    (date(2026, 12, 25), "行憲紀念日"),
])
def test_fixed_date_statutory_holidays_are_not_trading_days(day, name):
    assert is_trading_day(day) is False, f"{day} 是{name}"


def test_the_first_weekday_after_new_year_is_a_trading_day():
    assert is_trading_day(date(2026, 1, 2)) is True


# 證交所公告的「市場無交易，僅辦理結算交割作業」日：農曆春節前兩個平日，期交所同步不交易。
# 來源是證交所各年的「市場開休市日期」（https://www.twse.com.tw/zh/trading/holiday.html，
# 2026-11-24 取得），不是 `holidays` 套件——套件不知道這兩天，程式原本把它們當成交易日。
_SETTLEMENT_ONLY = [
    date(2023, 1, 18), date(2023, 1, 19),
    date(2024, 2, 6), date(2024, 2, 7),
    date(2025, 1, 23), date(2025, 1, 24),
    date(2026, 2, 12), date(2026, 2, 13),
    date(2027, 2, 2), date(2027, 2, 3),
]
# 同一份公告的「農曆春節前最後交易日」
_LAST_BEFORE_LUNAR_NEW_YEAR = [date(2023, 1, 17), date(2024, 2, 5), date(2025, 1, 22),
                               date(2026, 2, 11), date(2027, 2, 1)]


@pytest.mark.parametrize("day", _SETTLEMENT_ONLY)
def test_the_settlement_only_days_before_lunar_new_year_are_not_trading_days(day):
    """當成交易日的話，盤中程式那天會登入、整天收不到今天的成交，08:48 發一則報價中斷告警——
    市場本來就沒開，是假告警。"""
    assert is_trading_day(day) is False


@pytest.mark.parametrize("day", _LAST_BEFORE_LUNAR_NEW_YEAR)
def test_the_last_trading_day_before_lunar_new_year_still_trades(day):
    assert is_trading_day(day) is True
