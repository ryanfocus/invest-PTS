"""筆差週報 `tools/bichai_report.py`：從原始行情記錄算 ticket 03 的筆差統計。

原始行情檔用盤中程式同一個 `raw_market.RawMarketWriter` 寫（`market_dir` fixture 把它導到暫存目錄），
再從工具的 `main` 讀出來：測的是「盤中程式記下來的東西，工具讀得懂、算得對」這整條路。
"""

import io
import os
import sys
from datetime import date, datetime, time, timedelta

import raw_market
from broker import TxTrade, TxTradeCounts
from conftest import REPO_ROOT

sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))

import bichai_report  # noqa: E402

DAY = date(2026, 11, 17)
OPEN = datetime.combine(DAY, time(8, 45))


def _record(day, minutes, *, before_open=(), skip_counts=()):
    """從 08:45 起每分鐘一根：(價格, 買進累計, 賣出累計)。成交在第 1 秒，累計筆數在第 50 秒收到。"""
    writer = raw_market.RawMarketWriter(day)
    start = datetime.combine(day, time(8, 45))
    for k, (buy, sell) in enumerate(before_open):          # 08:40 起每分鐘一次
        writer.counts(TxTradeCounts(datetime.combine(day, time(8, 40 + k)), buy, sell))
    for i, (price, buy, sell) in enumerate(minutes):
        t = start + timedelta(minutes=i)
        writer.trade(TxTrade(t + timedelta(seconds=1), price))
        writer.confirm()
        if i not in skip_counts:
            writer.counts(TxTradeCounts(t + timedelta(seconds=50), buy, sell))
    writer.close()


def _bichai_day(bichai, *, prices=None, base=10_000):
    """每根收盤時的筆差照 `bichai`（賣出 − 買進）；買進筆數每分鐘多 1000。"""
    prices = prices or [45_000] * len(bichai)
    return [(p, base + i * 1000, base + i * 1000 + b) for i, (p, b) in enumerate(zip(prices, bichai))]


def _run(*days):
    out = io.StringIO()
    code = bichai_report.main([f"{d:%Y%m%d}" for d in days], out=out)
    return code, out.getvalue()


# ── 累計起點 ──


def test_counts_that_start_from_zero_at_the_open_read_as_starting_at_0845():
    _record(DAY, [(45_000, 10 + i * 500, 10 + i * 500) for i in range(20)])
    _, text = _run(DAY)
    assert "看起來從 08:45 起算" in text
    assert "開盤前（08:45 之前）沒有收到累計筆數" in text


def test_counts_reset_at_the_open_read_as_starting_at_0845():
    _record(DAY, [(45_000, 10 + i * 500, 10 + i * 500) for i in range(20)],
            before_open=[(30_000, 30_500), (30_100, 30_600)])
    _, text = _run(DAY)
    assert "開盤時歸零重算：從 08:45 起算" in text
    assert "開盤前最後一次（08:41:00）：買進 30,100、賣出 30,600" in text


def test_counts_already_large_at_the_open_read_as_including_the_night_session():
    _record(DAY, [(45_000, 30_000 + i * 100, 30_500 + i * 100) for i in range(20)],
            before_open=[(30_000, 30_500)])
    _, text = _run(DAY)
    assert "看起來含夜盤（或更早）" in text


# ── ±1000 的分鐘數與筆差跳增 ──


def test_minutes_beyond_the_threshold_and_jumps_follow_the_strategy_definitions():
    #         跳增：  +1000  +50  +150  -100  -200  -1900  -1  -199
    bichai = [0, 1000, 1050, 1200, 1100, 900, -1000, -1001, -1200]
    _record(DAY, _bichai_day(bichai))
    _, text = _run(DAY)
    # 恰好 ±1000 不算過門檻（> 1000、< −1000）
    assert "筆差 > +1,000：3 分鐘；< -1,000：2 分鐘" in text
    # 恰好 −100 算跳增（≤ −100）；+1000 那次跳到恰好 1000、−1900 那次跳到恰好 −1000，
    # 都不算條件 3、4 的筆差部分
    assert ("筆差跳增 ≥ +100：2 次（其中筆差 > +1,000 時 1 次）；"
            "≤ -100：4 次（其中筆差 < -1,000 時 1 次）") in text


def test_a_minute_without_counts_is_unknown_and_not_used_for_jumps():
    bichai = [0, 100, 200, 5000, 5100, 5200]
    _record(DAY, _bichai_day(bichai), skip_counts={3})
    _, text = _run(DAY)
    assert "1 分 K：6 根，筆差未知 1 根" in text
    # 第 3 根沒有筆數；第 4 根接在未知後面，5100 − 200 這種跨過缺口的差不算跳增
    assert "筆差跳增 ≥ +100：3 次" in text
    assert "累計筆數推送：日盤裡 5 次，間隔中位數 60 秒，最長 120 秒（08:47:50 起）" in text


# ── 方向 ──

_STEPS = [5, 20, 3, 15, 8, 30, 2, 12, 7, 25, 4, 9, 6, 1]
_PRICES = [45_000 + sum(_STEPS[:i]) for i in range(len(_STEPS) + 1)]


def test_bichai_rising_with_the_price_reads_as_the_expected_sign():
    _record(DAY, _bichai_day([(p - 45_000) * 10 for p in _PRICES], prices=_PRICES))
    _, text = _run(DAY)
    assert "相關係數 +1.00，價漲時筆差多半往上" in text
    assert "漲最多的 10 分鐘 08:46～08:56 漲 127 點，筆差 +0 → +1,270" in text
    assert "沒有下跌的 10 分鐘" in text


def test_bichai_falling_as_the_price_rises_is_flagged_as_a_possibly_reversed_sign():
    _record(DAY, _bichai_day([(45_000 - p) * 10 for p in _PRICES], prices=_PRICES))
    _, text = _run(DAY)
    assert "相關係數 -1.00" in text and "筆差的正負可能定義反了" in text
    assert "筆差 +0 → -1,270" in text


# ── 主程式 ──


def test_without_arguments_every_recorded_day_is_reported_with_a_total():
    second = DAY + timedelta(days=1)
    _record(DAY, _bichai_day([0, 1500, 1700]))
    _record(second, _bichai_day([0, -1500, -1400]))
    out = io.StringIO()
    assert bichai_report.main([], out=out) == 0
    text = out.getvalue()
    assert "2026-11-17（二）" in text and "2026-11-18（三）" in text
    assert "合計 2 天" in text
    assert "筆差 > +1,000：2 分鐘（平均每天 1.0）；< -1,000：2 分鐘（平均每天 1.0）" in text
    # 第一天 0→1500→1700 兩次都是過 +1000 又跳增；第二天 -1500→-1400 是 +100 但筆差在 -1000 以下
    assert "多方 2 次、空方 1 次" in text


def test_a_day_without_a_record_is_said_so():
    code, text = _run(DAY)
    assert code == 1
    assert "2026-11-17：沒有原始行情記錄" in text


def test_nothing_recorded_yet_is_said_so():
    out = io.StringIO()
    assert bichai_report.main([], out=out) == 1
    assert "還沒有任何原始行情記錄" in out.getvalue()


def test_the_tool_never_reads_logs_or_the_env_file():
    """原始行情記錄沒有帳號資訊；log 與 `.env` 有。這支工具的輸出要能直接貼給別人看。"""
    with open(bichai_report.__file__, encoding="utf-8") as fh:
        source = fh.read()
    code = source.split('"""', 2)[2]          # 說明文字可以提到它們，程式碼不行
    for forbidden in ("logs_dir", "daily_log", "env_path", "settings", "broker.capital", "dotenv"):
        assert forbidden not in code, forbidden
