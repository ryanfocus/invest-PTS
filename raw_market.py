"""原始行情記錄：每個交易日一個 CSV，存大台的成交與累計成交筆數（規格第 58 條）。

它有三個用途，所以格式要穩定、人和程式都讀得懂：

  1. ticket 03 用一週的紀錄回答筆差的事實問題（正負方向、從什麼時候開始累計）。
  2. 盤中重啟時，重啟前每根 K 棒結束時的筆差**只能從這裡讀回**：群益的筆差沒有歷史回補
     （ticket 09）。照檔案裡的順序重播一次 `indicators`，就得到和當時一樣的筆差。
  3. 作者日後可以拿它做 tick 等級的回測。

格式（`marketdata/TX-YYYYMMDD.csv`，UTF-8，第一行是欄名）：

    time,price,buy_count,sell_count
    2026-11-09T08:44:50.000,,100,100        ← 累計筆數（price 空白）
    2026-11-09T08:45:00.000,45000,,         ← 成交（筆數空白）

  * 每一行是一個事件，**依收到的順序**。
  * 成交的 `time` 是交易所的成交時間；累計筆數的 `time` 是本機收到的時刻
    （群益的筆數事件不帶時間）。
  * **沒有任何帳號資訊**，所以不在 30 天清除範圍內，長期保留（`paths.market_data_dir`）。

同一天重新啟動時接在原檔後面寫（ticket 09）。
"""

from __future__ import annotations

import csv
import os
from datetime import date
from typing import IO

import paths
from broker import TxTrade, TxTradeCounts

HEADER = ("time", "price", "buy_count", "sell_count")


def path_for(day: date) -> str:
    """那個交易日的原始行情檔。"""
    return os.path.join(paths.market_data_dir(), f"TX-{day:%Y%m%d}.csv")


class RawMarketWriter:
    """寫一個交易日的原始行情。**確認報價屬於今天之前（`confirm()`）只暫存、不建檔。**

    臨時休市那天收到的是前一個交易日的報價，那一天一行都不能寫；但開盤前（08:40 起）
    的累計筆數又要留下來，所以先暫存，確認之後一次補寫。

    ⚠️ 每一行寫完就落到磁碟（行緩衝）：程式當掉時，已經收到的行情要留在檔案裡，
       重啟後才讀得回重啟前的筆差。
    """

    def __init__(self, day: date) -> None:
        self._day = day
        self._pending: list[tuple] | None = []     # confirm() 之後是 None
        self._fh: IO[str] | None = None
        self._csv = None

    def trade(self, event: TxTrade) -> None:
        self._write((_time(event), _price(event.price), "", ""))

    def counts(self, event: TxTradeCounts) -> None:
        self._write((_time(event), "", event.buy_count, event.sell_count))

    def confirm(self) -> None:
        """報價屬於今天：建檔（已存在就接在後面），補寫暫存的事件，之後每個事件直接寫。"""
        if self._pending is None:
            return
        path = path_for(self._day)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        is_new = not os.path.exists(path) or os.path.getsize(path) == 0
        self._fh = open(path, "a", encoding="utf-8", newline="", buffering=1)
        self._csv = csv.writer(self._fh, lineterminator="\n")
        if is_new:
            self._csv.writerow(HEADER)
        self._csv.writerows(self._pending)
        self._pending = None

    def close(self) -> None:
        """沒確認過就關掉的話，暫存的事件直接丟掉——那一天沒有屬於它的行情。"""
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        self._pending = None

    def _write(self, row: tuple) -> None:
        if self._pending is not None:
            self._pending.append(row)
        elif self._csv is not None:
            self._csv.writerow(row)


def _time(event: TxTrade | TxTradeCounts) -> str:
    return event.time.isoformat(timespec="milliseconds")


def _price(price: float) -> str:
    """大台的價格是整數點，寫成 `45000` 而不是 `45000.0`；有小數時照實寫。"""
    return str(int(price)) if price.is_integer() else repr(price)
