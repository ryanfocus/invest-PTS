"""寫交易日劇本的小工具：給 `FakeBroker(events=...)` 用的行情事件，以及各測試檔共用的
設定、「跑一天」、部位檔的寫法。

劇本就是 broker 依序送來的事件。這裡只負責把「08:45:03 成交 45000」寫得短一點，
不替測試決定任何行為——事件的先後就是 list 的先後。

共用的東西集中在這裡，理由同 invest-os 的 `make_config`：設定多一個鍵、部位檔的格式改一次，
只有一個地方要改，不是每個測試檔各自炸掉一次。
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

from broker import (BUY, MTX_CODE, TMF_CODE, TX_CODE, Clock, ContractInfo, MinuteBar,
                    OrderRequest, ProductTrade, TxTrade, TxTradeCounts)
from broker.fake import FakeBroker
from recording import RecordingNotifier
import intraday
import position_file
import settings

DAY = date(2026, 11, 9)              # 週一，一般交易日
SETTLEMENT_DAY = date(2026, 11, 18)  # 2026 年 11 月的第三個週三：DAY 那一週不是結算週


def contracts(last_trading_day: date = SETTLEMENT_DAY) -> dict[str, ContractInfo]:
    """商品清單（`FakeBroker(contracts=...)` 用）：三個商品的近月，最後交易日相同。"""
    month = f"{last_trading_day:%m}"
    return {TX_CODE: ContractInfo(TX_CODE, last_trading_day, f"TX{month}"),
            MTX_CODE: ContractInfo(MTX_CODE, last_trading_day, f"MTX{month}"),
            TMF_CODE: ContractInfo(TMF_CODE, last_trading_day, f"TM{last_trading_day:%y%m}")}


def order_request(code: str = TMF_CODE, *, side: str = BUY, lots: int = 1,
                  day: date = DAY) -> OrderRequest:
    """一筆委託（`contracts()` 的近月）。broker 的測試直接拿它送單。"""
    return OrderRequest(contract=contracts()[code], side=side, lots=lots, trading_day=day)


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
          bichai_step: int = 0, day: date = DAY) -> list:
    """從 `start` 起連續 `n` 分鐘的強勢：每根開盤比上一根高 `step`（紅K）、收在開盤 + `step`、
    筆差從 `bichai` 起每根多 `bichai_step`（給 100 就是每一根都筆差跳增）。
    交易商品成交在大台的收盤價。"""
    t0 = datetime.combine(day, datetime.strptime(start, "%H:%M").time())
    events: list = []
    for i in range(n):
        o = open_ + i * step
        events += minute(f"{t0 + timedelta(minutes=i):%H:%M}", o, o + step,
                         bichai=bichai + i * bichai_step, day=day)
    return events


def trading_tx(events: list) -> list:
    """交易商品就是大台時 broker 送來的樣子：大台的每一筆成交同時也是交易商品的成交，
    群益先送 TxTrade、緊接著送一筆同時同價的 ProductTrade（broker/capital.py 的逐筆成交）。
    原劇本裡另外寫的交易商品成交全部拿掉——大台不會有不屬於大台成交的「交易商品成交」。"""
    out: list = []
    for event in events:
        if isinstance(event, ProductTrade):
            continue
        out.append(event)
        if isinstance(event, TxTrade):
            out.append(ProductTrade(event.time, event.price))
    return out


def replayed(events: list) -> list:
    """群益第一次訂閱時回補的當天成交：只有成交（大台與交易商品），帶的是交易所當時的時間；
    沒有時鐘，也沒有累計筆數（筆數沒有歷史，只有現在的累計值）。晚啟動或重啟時，
    這一段會在第一個時鐘之後一口氣送來。"""
    return [e for e in events if isinstance(e, (TxTrade, ProductTrade))]


# ── 當掉與重啟（ticket 09）────────────────────────────────────────────────


def until(events: list, t: datetime) -> list:
    """劇本到 `t` 為止：盤中程式在那裡停了（當掉、被關掉），之後的事件它收不到。"""
    return [e for e in events if e.time <= t]


def restarted_at(events: list, t: datetime) -> list:
    """盤中程式在 `t` 重啟之後，broker 送來的樣子：先一個本機時鐘，接著群益第一次訂閱時
    回補的今天的成交（到 `t` 為止，只有成交），然後才是 `t` 之後的即時行情。"""
    return [Clock(t)] + replayed(until(events, t)) + [e for e in events if e.time > t]


# ── 設定與「跑一天」───────────────────────────────────────────────────────


def make_config(**overrides) -> settings.Config:
    """使用者的設定。**預設不下單**（模擬模式）：會下單的測試自己寫明
    `auto_order_enabled=True`，哪些測試會碰下單 API 在測試碼裡看得見（invest-os 的前例）。"""
    values = dict(auto_order_enabled=False, product=TMF_CODE, lots_per_entry=1,
                  max_position=20, discord_enabled=True)
    values.update(overrides)
    return settings.Config(**values)


def run_day(events, *, config: settings.Config | None = None, today: date = DAY,
            **broker_options) -> tuple[FakeBroker, list[str], intraday.SessionOutcome]:
    """盤中程式跑 `today` 一輪：假 broker 照 `events` 回放，通知換成記錄器。
    回傳 (broker, 發出的訊息, 結果)。

    假 broker 預設：前一個交易日（`PREVIOUS_DAY`）平淡、收 45000；商品清單是 `contracts()`。
    其餘的（成交、失敗、登入）照 `broker_options` 交給 `FakeBroker`。"""
    broker_options.setdefault("minute_bars", {PREVIOUS_DAY: calm_history(45000)})
    broker_options.setdefault("contracts", contracts())
    broker = FakeBroker(events=events, **broker_options)
    notifier = RecordingNotifier()
    outcome = intraday.run(broker, config or make_config(), today=today, notify=notifier)
    return broker, notifier.sent, outcome


# ── 部位檔（兩支程式之間的約定，測試裡逐字寫出來）────────────────────────────

# `position_content(某欄位=DROP)`：那一欄不寫（「少了欄位」的部位檔）
DROP = object()


def position_content(day: date = DAY, **overrides) -> dict:
    """部位檔的原始內容：預設是盤中程式今天寫下的微台多單 1 口 @ 45100。
    `overrides` 換掉其中幾欄；值是 `DROP` 的那一欄拿掉。"""
    content = {
        "trading_day": f"{day:%Y-%m-%d}",
        "product": "TM0000AM",
        "order_code": "TM2611",
        "contract_month": "202611",
        "direction": "LONG",
        "last_trading_day": "2026-11-18",
        "status": "CONFIRMED",
        "entries": [{"order_seq": "2315609990001", "lots": 1, "price": 45100.0}],
        "exits": [],
        "uncertain": None,
    }
    for key, value in overrides.items():
        if value is DROP:
            del content[key]
        else:
            content[key] = value
    return content


def write_position_file(content: dict | str, day: date = DAY) -> None:
    """把部位檔放進 conftest 導開的 `state/`：dict 寫成 JSON，字串原封不動寫進去
    （寫壞的、寫到一半的檔）。"""
    path = position_file.path_for(day)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        if isinstance(content, str):
            fh.write(content)
        else:
            json.dump(content, fh, ensure_ascii=False)


def position_on_file(day: date = DAY) -> dict:
    """部位檔現在的原始內容。格式是和出場程式、和人之間的約定，所以逐字斷言。"""
    with open(position_file.path_for(day), encoding="utf-8") as fh:
        return json.load(fh)


# 送單前記下的「不確定」：這一筆還沒有序號、已知成交 0 口（`intraday._Orders._mark_sending`）
SENDING_MARKER = {"stage": "ENTRY", "order_seq": "", "requested_lots": 1, "known_filled": 0}


def _denied(*args) -> None:
    raise PermissionError(13, "存取被拒")


def lock_position_file(m) -> None:
    """部位檔置換的那一步失敗：Windows 上防毒或索引程式剛好開著正式檔時，`os.replace` 就是
    這樣。`m` 是 `monkeypatch`，或只管一段劇本的 `monkeypatch.context()`。"""
    m.setattr(os, "replace", _denied)


def lock_position_file_once_sent(m) -> None:
    """委託送出去之後，部位檔才被鎖住（等成交回報的那段時間，防毒或索引程式剛好開著它）：
    送單前記下的「不確定」寫進去了，之後的每一次都寫不進去。

    只鎖一輪（之後重啟時已經不鎖）的話，用 `with monkeypatch.context() as m:` 包住那一輪：
    離開時只還原 `m` 做過的事。⚠️ 不要用 `monkeypatch.undo()`——它連 conftest 導開的資料夾
    也一起還原，之後的檔案就寫進正式的 `state/`。"""
    place_order = FakeBroker.place_order

    def locking(self, request):
        lock_position_file(m)
        return place_order(self, request)
    m.setattr(FakeBroker, "place_order", locking)
