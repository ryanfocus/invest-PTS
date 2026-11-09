"""寫交易日劇本的小工具：給 `FakeBroker(events=...)` 用的行情事件。

劇本就是 broker 依序送來的事件。這裡只負責把「08:45:03 成交 45000」寫得短一點，
不替測試決定任何行為——事件的先後就是 list 的先後。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from broker import Clock, MinuteBar, ProductTrade, TxTrade, TxTradeCounts

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


# ── 進場劇本 ────────────────────────────────────────────────────────────

PREVIOUS_DAY = date(2026, 11, 6)     # DAY 的前一個交易日（週五）


def history(closes: list[float], day: date = PREVIOUS_DAY) -> list[MinuteBar]:
    """前一個交易日的大台日盤 1 分 K（`FakeBroker(minute_bars={day: ...})` 用）：從 08:45 起
    每分鐘一根，開高低收都是同一個價。最後一根的收盤價就是昨收（GLOSSARY）。"""
    start = datetime.combine(day, datetime.strptime("08:45", "%H:%M").time())
    return [MinuteBar(start + timedelta(minutes=i), c, c, c, c) for i, c in enumerate(closes)]


def calm_history(close: float = 45000, day: date = PREVIOUS_DAY) -> list[MinuteBar]:
    """平淡的前一天：300 根在 close−10 與 close 之間來回，收在 `close`。漲跌各半，
    RSI 落在 50 附近——今天的走勢才是讓它越過 70／30 的原因。"""
    return history([close - 10 if i % 2 == 0 else close for i in range(300)], day)


def minute(hhmm: str, open_: float, close: float, *, bichai: int | None = None,
           product: float | None = None, day: date = DAY) -> list:
    """一分鐘的劇本：`:00` 本機時鐘與成交在開盤價、`:20` 累計筆數（給了筆差才有）、
    `:30` 成交在收盤價、`:40` 交易商品成交（沒給就是大台的收盤價）。

    時鐘和成交在同一秒：本機時鐘和交易所一致、成交即時收到。盤中程式拿最新的時鐘分辨
    即時的成交與回補的舊成交，沒有時鐘就不判斷。

    這根 K 棒要等**下一分鐘的第一筆成交**才結束，策略也在那時判斷——所以劇本最後要再接
    一分鐘（或一筆成交），最後那根才會被判斷到。
    """
    base = datetime.combine(day, datetime.strptime(hhmm, "%H:%M").time())
    events: list = [Clock(base), TxTrade(base, float(open_))]
    if bichai is not None:
        buy = 20000
        events.append(TxTradeCounts(base + timedelta(seconds=20), buy_count=buy,
                                    sell_count=buy + bichai))
    events.append(TxTrade(base + timedelta(seconds=30), float(close)))
    events.append(ProductTrade(base + timedelta(seconds=40),
                               float(close if product is None else product)))
    return events


def rally(start: str, n: int, *, open_: float = 45100, step: float = 10, bichai: int = 1500,
          day: date = DAY) -> list:
    """從 `start` 起連續 `n` 分鐘的強勢：每根開盤比上一根高 `step`（紅K）、收在開盤 + `step`、
    筆差 `bichai`。交易商品成交在大台的收盤價。"""
    t0 = datetime.combine(day, datetime.strptime(start, "%H:%M").time())
    events: list = []
    for i in range(n):
        o = open_ + i * step
        events += minute(f"{t0 + timedelta(minutes=i):%H:%M}", o, o + step, bichai=bichai, day=day)
    return events


def replayed(events: list) -> list:
    """群益第一次訂閱時回補的當天成交：只有成交（大台與交易商品），帶的是交易所當時的時間；
    沒有時鐘，也沒有累計筆數（筆數沒有歷史，只有現在的累計值）。晚啟動或重啟時，
    這一段會在第一個時鐘之後一口氣送來。"""
    return [e for e in events if isinstance(e, (TxTrade, ProductTrade))]
