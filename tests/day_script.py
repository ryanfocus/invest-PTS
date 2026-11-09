"""寫交易日劇本的小工具：給 `FakeBroker(events=...)` 用的行情事件。

劇本就是 broker 依序送來的事件。這裡只負責把「08:45:03 成交 45000」寫得短一點，
不替測試決定任何行為——事件的先後就是 list 的先後。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from broker import Clock, ProductTrade, TxTrade, TxTradeCounts

DAY = date(2026, 11, 9)              # 週一，一般交易日


def at(hhmmss: str, day: date = DAY) -> datetime:
    """`at("08:45:03")` → 那一天的 08:45:03；`at("08:45:03.250")` 帶毫秒。"""
    fmt = "%H:%M:%S.%f" if "." in hhmmss else "%H:%M:%S"
    return datetime.combine(day, datetime.strptime(hhmmss, fmt).time())


def clock(t: datetime) -> Clock:
    return Clock(t)


def trade(t: datetime, price: float) -> TxTrade:
    return TxTrade(t, float(price))


def counts(t: datetime, buy: int, sell: int) -> TxTradeCounts:
    return TxTradeCounts(t, buy_count=buy, sell_count=sell)


def product_trade(t: datetime, price: float) -> ProductTrade:
    return ProductTrade(t, float(price))


def whole_morning(day: date = DAY) -> list:
    """一個完整上午：08:40:00–13:20:00 每秒一個時鐘；08:45–13:19 每 10 秒一筆大台成交，
    每筆之後 5 秒一筆交易商品的成交；每 30 秒一次累計筆數。

    價格寫成「45000 ＋ 開盤後第幾分鐘 ＋ 這一分鐘裡第幾筆」，所以任何一根 K 棒的
    開高低收都能心算：第 n 根（08:45 是第 0 根）是 (45000+n, 45005+n, 45000+n, 45005+n)。
    交易商品的成交固定比同一分鐘的大台開盤價低 100 點，混進大台的 K 棒一眼就看得出來。
    第 k 次筆數是買 k、賣 2k。
    """
    events: list = []
    open_at = datetime.combine(day, datetime.strptime("08:45", "%H:%M").time())
    end_at = datetime.combine(day, datetime.strptime("13:20", "%H:%M").time())
    t = datetime.combine(day, datetime.strptime("08:40", "%H:%M").time())
    k = 0
    while t <= end_at:
        events.append(Clock(t))
        if open_at <= t < end_at and t.second % 10 == 0:
            minute_index = int((t - open_at).total_seconds()) // 60
            events.append(TxTrade(t, 45000.0 + minute_index + t.second // 10))
        if open_at <= t < end_at and t.second % 10 == 5:
            minute_index = int((t - open_at).total_seconds()) // 60
            events.append(ProductTrade(t, 44900.0 + minute_index))
        if t < end_at and t.second % 30 == 0:
            k += 1
            events.append(TxTradeCounts(t, buy_count=k, sell_count=2 * k))
        t += timedelta(seconds=1)
    return events
