"""RSI（Wilder）—— 移植 invest-WOIC 的 `indicators/rsi.py`，用同一份 StockCharts 範例證明公式。

期望值是 StockCharts 自己算出並公布的（外部獨立來源），不是本專案算的。
期數只是參數：14 期驗過，就驗證了 5 期走的同一段程式（PTS 用 RSI(5)，規格第 3 條）。
"""

import csv
import pathlib

import pytest

from indicators import rsi

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "stockcharts_rsi_example.csv"


def _stockcharts_example() -> tuple[list[float], list[float | None]]:
    with open(FIXTURE, encoding="utf-8") as fh:
        rows = list(csv.DictReader(line for line in fh if not line.startswith("#")))
    closes = [float(r["close"]) for r in rows]
    expected = [float(r["rsi_14"]) if r["rsi_14"] else None for r in rows]
    return closes, expected


def test_wilder_rsi_matches_the_stockcharts_worked_example():
    closes, expected = _stockcharts_example()
    checked = 0
    for i, want in enumerate(expected):
        if want is None:
            continue
        assert rsi(closes[: i + 1], period=14) == pytest.approx(want, abs=1e-4)
        checked += 1
    assert checked == 19, "範例的期望值不見了，這條檢查等於沒有在跑"


def test_all_gains_is_100_and_all_losses_is_0():
    """平均跌幅是 0 → 100；平均漲幅是 0 → 0（StockCharts 的定義，invest-WOIC 同一套）。"""
    assert rsi([100, 101, 102, 103, 104, 105], period=5) == 100
    assert rsi([105, 104, 103, 102, 101, 100], period=5) == 0


def test_without_period_plus_one_closes_there_is_no_rsi_yet():
    """少於 6 根收盤價就算不出 RSI(5)。不知道就是不知道，不猜一個數字給策略判斷。"""
    assert rsi([100, 101, 102, 103, 104], period=5) is None
