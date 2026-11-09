"""群益 broker 的大台 1 分 K 歷史 —— 從 `minute_bars()` 打進去，底下的 COM 換成 `FakeSkcom`。

PTS 用它取前一個交易日的日盤 1 分 K：最後一根的收盤價是昨收，整天的收盤價是 RSI 的暖身
（規格第 4、5 條）。格式照官方文件 4-4-24／4-4-b／4-4-t（notes/capital-api.md 第 3 節）。

⚠️ 文件的新版 1 分線範例本身就不一致（`2021/8/2 09:01 12.95,12.95,12.95.12.95,5`：日期與
   時間之間是空白、月日沒補零、有一個逗號打成句點）。這裡的字串照文件的欄位順序寫，
   **第一次實機收到的真實字串要回來換成 fixture**（ticket 04 的 Comments）。
"""

from datetime import date, datetime, timedelta

import pytest

from broker import MinuteBar, QuoteUnavailable
from broker.capital import CapitalBroker
from fake_skcom import FakeSkcom

FRIDAY = date(2026, 11, 6)


def _broker(skcom: FakeSkcom) -> CapitalBroker:
    broker = CapitalBroker("A100000000", "pw", "F0000000000000", com=skcom, settle_seconds=0,
                           connect_timeout=0.2, pump_interval=0)
    broker.login()
    return broker


def _day(labels: list[str], day: str = "2026/11/06") -> list[str]:
    """一天的 K 線字串：第 i 根開 45000+i、高 +5、低 −5、收 +1，量 100+i。"""
    return [f"{day} {hhmm}, {45000 + i}, {45005 + i}, {44995 + i}, {45001 + i}, {100 + i}"
            for i, hhmm in enumerate(labels)]


def _labels(first: str, n: int) -> list[str]:
    t0 = datetime.strptime(first, "%H:%M")
    return [f"{t0 + timedelta(minutes=i):%H:%M}" for i in range(n)]


def test_it_asks_for_one_minute_bars_of_the_day_session_of_that_day():
    """`RequestKLineAMByDate(代碼, 0 分線, 1 新版格式, 1 AM 盤, 起日, 迄日, 1 分)`。
    盤別是用 `sTradeSession=1` 選的（文件 4-4-24），所以代碼是不帶 AM 的 `TX00`。"""
    skcom = FakeSkcom()
    _broker(skcom).minute_bars(FRIDAY)
    calls = [args for _, name, args in skcom.calls if name == "SKQuoteLib_RequestKLineAMByDate"]
    assert calls == [("TX00", 0, 1, 1, "20261106", "20261106", 1)]


def test_bars_labelled_by_their_end_minute_become_bars_starting_a_minute_earlier():
    """日盤 08:45–13:45 標終點的話是 08:46…13:45，共 300 根。PTS 的 K 棒一律標起點
    （`MinuteBar.start`），所以 08:46 那根是 08:45 開始的那一根。新版格式的價格已經處理過
    小數點，不用再除 100。"""
    skcom = FakeSkcom(klines={"20261106": _day(_labels("08:46", 300))})
    bars = _broker(skcom).minute_bars(FRIDAY)
    assert len(bars) == 300
    assert bars[0] == MinuteBar(datetime(2026, 11, 6, 8, 45), 45000.0, 45005.0, 44995.0, 45001.0)
    assert bars[-1] == MinuteBar(datetime(2026, 11, 6, 13, 44), 45299.0, 45304.0, 45294.0, 45300.0)


def test_bars_labelled_by_their_start_minute_are_kept_as_they_are():
    """標起點的話會有 08:45 那根（終點標記不會有：08:45 之前沒有日盤）。"""
    skcom = FakeSkcom(klines={"20261106": _day(_labels("08:45", 300))})
    bars = _broker(skcom).minute_bars(FRIDAY)
    assert (bars[0].start, bars[-1].start) == (datetime(2026, 11, 6, 8, 45),
                                               datetime(2026, 11, 6, 13, 44))
    assert bars[-1].close == 45300.0


def test_bars_labelled_both_0845_and_1345_are_quote_unavailable_rather_than_a_dropped_close():
    """301 根、兩頭都有：標起點加上 13:45:00 的成交，或標終點加上 08:45:00 的開盤集合競價
    自成一根，都會這樣。不管是哪一種，13:45 那根的收盤價才是日盤的收盤價（昨收）；
    分不出是哪一種就不能挑一根丟掉，丟了昨收就悄悄變成倒數第二根。"""
    skcom = FakeSkcom(klines={"20261106": _day(_labels("08:45", 301))})
    with pytest.raises(QuoteUnavailable, match="08:45.*13:45"):
        _broker(skcom).minute_bars(FRIDAY)


def test_the_documented_format_variations_are_read():
    """文件裡出現過的寫法：月日不補零、日期與時間之間空白或逗號、逗號後有沒有空白。"""
    rows = ["2026/1/5 08:46 45000,45005,44995,45001,10",
            "2026/01/05, 08:47, 45001, 45006, 44996, 45002, 11"]
    skcom = FakeSkcom(klines={"20260105": rows})
    bars = _broker(skcom).minute_bars(date(2026, 1, 5))
    assert [(b.start, b.close) for b in bars] == [(datetime(2026, 1, 5, 8, 45), 45001.0),
                                                 (datetime(2026, 1, 5, 8, 46), 45002.0)]


def test_bars_outside_that_days_day_session_are_left_out():
    """只要那一天的日盤。文件的全盤範例會帶前一晚的夜盤；就算群益沒照 AM 盤給，
    混進來的夜盤 K 棒也不能變成昨收或暖身。"""
    rows = (_day(["23:59"], day="2026/11/05") + _day(["04:59"]) + _day(_labels("08:46", 3))
            + _day(["15:01"]))
    skcom = FakeSkcom(klines={"20261106": rows})
    bars = _broker(skcom).minute_bars(FRIDAY)
    assert [f"{b.start:%H:%M}" for b in bars] == ["08:45", "08:46", "08:47"]


def test_a_day_without_bars_is_an_empty_list():
    """臨時休市那天什麼都沒有，只有結束通知。呼叫端會再往前找一天。"""
    assert _broker(FakeSkcom()).minute_bars(FRIDAY) == []


def test_the_end_of_the_data_can_also_come_as_a_row_starting_with_hashes():
    """文件 4-4-t 的備註：「將回傳一筆以『##』開頭的內容，表示查詢結束」——沒說是哪個事件。"""
    skcom = FakeSkcom(klines={"20261106": _day(_labels("08:46", 2)) + ["##"]},
                      kline_completes=False)
    assert len(_broker(skcom).minute_bars(FRIDAY)) == 2


def test_no_end_of_data_in_time_is_quote_unavailable():
    """沒收到結束通知就不知道收齊了沒有：少了尾盤幾根，昨收就是錯的。"""
    skcom = FakeSkcom(klines={"20261106": _day(_labels("08:46", 2))}, kline_completes=False)
    with pytest.raises(QuoteUnavailable, match="1 分 K"):
        _broker(skcom).minute_bars(FRIDAY)


def test_a_refused_request_is_quote_unavailable_with_the_brokers_message():
    skcom = FakeSkcom(quote_codes={"SKQuoteLib_RequestKLineAMByDate": 3022})
    with pytest.raises(QuoteUnavailable, match="群益訊息3022"):
        _broker(skcom).minute_bars(FRIDAY)


def test_a_row_that_cannot_be_read_is_quote_unavailable_rather_than_a_skipped_bar():
    """跳過看不懂的那一根，昨收或暖身就悄悄少了一塊。格式和預期不同時要大聲講，
    並且把那一列寫出來（K 線字串沒有帳號資訊），才能照它修。"""
    rows = _day(_labels("08:46", 2)) + ["2026/11/06 08:48 45000,45005,44995.45001,10"]
    skcom = FakeSkcom(klines={"20261106": rows})
    with pytest.raises(QuoteUnavailable, match="44995.45001"):
        _broker(skcom).minute_bars(FRIDAY)


def test_minute_bars_before_logging_in_is_quote_unavailable():
    with pytest.raises(QuoteUnavailable, match="尚未登入"):
        CapitalBroker("A100000000", "pw", com=FakeSkcom()).minute_bars(FRIDAY)


def test_an_unexpected_component_error_is_quote_unavailable():
    """broker 對外只拋自己定義的例外。元件自己的錯誤漏出去的話，盤中程式在開始收行情之前
    就中止，那天連原始行情都沒有（ticket 03 要一週的資料）。"""
    skcom = FakeSkcom(kline_error=OSError("COM 物件已中斷連線"))
    with pytest.raises(QuoteUnavailable, match="OSError"):
        _broker(skcom).minute_bars(FRIDAY)
