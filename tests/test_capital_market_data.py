"""群益 broker 的行情事件串流 —— 從 `events()` 打進去，底下的 COM 換成 `FakeSkcom`。

斷言兩種東西：**向群益訂了什麼**，以及**群益送來的東西變成了哪些行情事件**。
事件的參數格式照官方文件 4-4-l／4-4-m／4-4-q 與 invest-os 的實測（`84500` 沒有前導零、
價格放大 100 倍、試撮 `nSimulate=1`），細節見 notes/capital-api.md 第 1、2、4 節。
"""

from datetime import datetime, timedelta
from itertools import islice

import pytest

from broker import (TMF_CODE, TX_CODE, Clock, ProductTrade, QuoteUnavailable, TxTrade,
                    TxTradeCounts)
from broker.capital import CapitalBroker
from fake_skcom import MTX_INDEX, TMF_INDEX, TX_INDEX, FakeSkcom


class _Ticking:
    """本機時鐘的替身：每被問一次就往前走 `step`。"""

    def __init__(self, start: datetime, step: timedelta):
        self._t, self._step = start, step

    def __call__(self) -> datetime:
        t, self._t = self._t, self._t + self._step
        return t


def _broker(skcom: FakeSkcom, now=None) -> CapitalBroker:
    broker = CapitalBroker("A100000000", "pw", "F0000000000000", com=skcom, settle_seconds=0,
                           pump_interval=0,
                           now=now or _Ticking(datetime(2026, 11, 9, 8, 40), timedelta(0.1)))
    broker.login()
    return broker


def _clocks(stream, n: int) -> list[datetime]:
    return [e.time for e in islice((e for e in stream if isinstance(e, Clock)), n)]


def _market(stream) -> list:
    """串流開頭的行情事件（時鐘以外）。串流不會自己結束，所以只看前 200 個事件——
    劇本裡的事件在第一次訊息幫浦就全部送達，200 個綽綽有餘。"""
    return [e for e in islice(stream, 200) if not isinstance(e, Clock)]


def _tick(index: int, ymd: int, hhmmss: int, close: int, *, simulate: int = 0,
          history: bool = False) -> tuple:
    """`OnNotifyTicksLONG`／`OnNotifyHistoryTicksLONG` 的參數（兩者完全相同，文件 4-4-l／m）。"""
    name = "OnNotifyHistoryTicksLONG" if history else "OnNotifyTicksLONG"
    #       sMarketNo nIndex nPtr nDate nTimehms nTimemillismicros nBid nAsk nClose nQty nSimulate
    return name, (2, index, 0, ymd, hhmmss, 0, close - 100, close + 100, close, 1, simulate)


def test_the_connection_is_kept_alive_by_asking_for_the_server_time_every_15_seconds():
    """文件 4-4-5：「為避免…無報價資料傳送，導致連線被防火牆切斷…請固定每十五秒呼叫此函式」。
    08:40 到 08:45 開盤前常常沒有行情，而盤中程式一連跑四個半小時。"""
    skcom = FakeSkcom()
    now = _Ticking(datetime(2026, 11, 9, 8, 40, 0), timedelta(seconds=1))
    _clocks(_broker(skcom, now=now).events(TMF_CODE), 46)          # 08:40:00–08:40:45
    assert len(_requests(skcom, "SKQuoteLib_RequestServerTime")) == 4   # :00 :15 :30 :45


# --- 大台成交 ---


def test_tx_ticks_become_trades_at_the_exchanges_time_and_real_price():
    """`nTimehms` 是整數 hhmmss，08:45:00 是 `84500`——前導零不見了，要用整數除法拆，
    不能切字串（官方 V2 範例那樣切，10 點以前會切成 84 時）。價格放大 100 倍。
    回補（History）與即時的格式相同，一樣是成交。"""
    skcom = FakeSkcom(market=[
        _tick(TX_INDEX, 20261109, 84500, 4500000, history=True),
        _tick(TX_INDEX, 20261109, 84501, 4501000),
        _tick(TX_INDEX, 20261109, 100000, 4500050),
    ])
    trades = [(e.time, e.price) for e in _market(_broker(skcom).events(TMF_CODE))]
    assert trades == [
        (datetime(2026, 11, 9, 8, 45, 0), 45000.0),
        (datetime(2026, 11, 9, 8, 45, 1), 45010.0),
        (datetime(2026, 11, 9, 10, 0, 0), 45000.5),
    ]


def test_trial_match_ticks_before_the_open_are_not_trades():
    """08:30 起就有成交明細，但那是試撮（`nSimulate=1`）。2026-08-10 實測：最早的試撮價
    44981，真正的開盤價 44985 在 08:45:00。拿試撮組 K 棒，開盤價就是錯的。"""
    skcom = FakeSkcom(market=[
        _tick(TX_INDEX, 20261109, 83000, 4498100, simulate=1, history=True),
        _tick(TX_INDEX, 20261109, 84500, 4498500, history=True),
    ])
    trades = [(e.time, e.price) for e in _market(_broker(skcom).events(TMF_CODE))]
    assert trades == [(datetime(2026, 11, 9, 8, 45, 0), 44985.0)]


# --- 每秒一次的時鐘 ---


def test_a_clock_event_comes_once_for_each_second_of_the_local_clock():
    """群益沒有每秒一次的事件（`OnTimer` 每分鐘一次），13:20 截止、報價中斷偵測卻需要
    沒有成交時也知道現在幾點——所以時鐘由 broker 用本機時間產生，每跨過一個整秒一次。"""
    start = datetime(2026, 11, 9, 8, 45, 0)
    broker = _broker(FakeSkcom(), now=_Ticking(start, timedelta(seconds=0.4)))
    assert _clocks(broker.events(TMF_CODE), 3) == [
        datetime(2026, 11, 9, 8, 45, 0, 0),
        datetime(2026, 11, 9, 8, 45, 1, 200000),
        datetime(2026, 11, 9, 8, 45, 2, 0),
    ]


# --- 交易商品成交 ---


def test_ticks_of_the_trading_product_become_product_trades():
    """「部位不虧」要用交易商品自己的現價（規格第 14 條）。逐筆成交事件只帶系統索引，
    哪個索引是哪個商品，由訂閱時查到的商品資料決定。"""
    skcom = FakeSkcom(market=[
        _tick(TX_INDEX, 20261109, 84500, 4500000),
        _tick(TMF_INDEX, 20261109, 84500, 4499000),
    ])
    events = _market(_broker(skcom).events(TMF_CODE))
    assert events == [TxTrade(datetime(2026, 11, 9, 8, 45), 45000.0),
                      ProductTrade(datetime(2026, 11, 9, 8, 45), 44990.0)]


def test_when_trading_tx_each_tx_tick_is_also_the_products_trade():
    skcom = FakeSkcom(market=[_tick(TX_INDEX, 20261109, 84500, 4500000)])
    events = _market(_broker(skcom).events(TX_CODE))
    assert events == [TxTrade(datetime(2026, 11, 9, 8, 45), 45000.0),
                      ProductTrade(datetime(2026, 11, 9, 8, 45), 45000.0)]


def test_ticks_under_an_index_we_did_not_look_up_are_dropped_loudly_once(caplog):
    """只訂了大台與交易商品，所以認不得的索引代表「訂閱時查到的索引」和事件帶來的不一樣
    （例如 `TX00AM` 查回的是全盤商品的索引）。那樣的話大台的成交會**全部**被丟掉——
    一週的實機紀錄（ticket 03）就白跑了。不能安靜，但也不必每一筆都講。"""
    skcom = FakeSkcom(market=[_tick(MTX_INDEX, 20261109, 84500, 4500000),
                              _tick(MTX_INDEX, 20261109, 84501, 4500100)])
    assert _market(_broker(skcom).events(TMF_CODE)) == []
    assert caplog.text.count(f"索引 {MTX_INDEX}") == 1


# --- 訂閱 ---


def _requests(skcom: FakeSkcom, method: str) -> list[tuple]:
    return [args for lib, name, args in skcom.calls if name == method]


def test_tx_and_the_trading_product_are_subscribed_on_their_own_pages():
    """一個 page 只能訂一檔（文件 4-4-3），從 0 開始。"""
    skcom = FakeSkcom()
    _market(_broker(skcom).events(TMF_CODE))
    assert _requests(skcom, "SKQuoteLib_RequestTicks") == [(0, TX_CODE), (1, TMF_CODE)]


def test_trading_tx_subscribes_its_ticks_only_once():
    skcom = FakeSkcom()
    _market(_broker(skcom).events(TX_CODE))
    assert _requests(skcom, "SKQuoteLib_RequestTicks") == [(0, TX_CODE)]


# --- 大台累計成交筆數 ---


def _trade_info(buy_deals: int, sell_deals: int, code: str = TX_CODE) -> tuple:
    """`OnNotifyFutureTradeInfoLONG` 的參數（文件 4-4-q）。前四個數字是**委託**的
    筆數與口數，筆差用不到；最後兩個才是**成交**買進／賣出筆數。"""
    #                               sMarketNo nStockidx 委買筆 委賣筆 委買口 委賣口 成交買筆 成交賣筆
    return "OnNotifyFutureTradeInfoLONG", (code, 2, TX_INDEX, 9000, 9100, 30000, 31000,
                                           buy_deals, sell_deals)


def test_tx_deal_counts_become_counts_stamped_with_the_time_they_arrived():
    """筆差 = 成交賣出筆數 − 成交買進筆數（GLOSSARY）。群益的這個事件沒有日期也沒有時間，
    所以時間是收到它的那一刻（本機時鐘）。"""
    skcom = FakeSkcom(market=[_trade_info(buy_deals=1500, sell_deals=1620)])
    now = _Ticking(datetime(2026, 11, 9, 8, 45, 10), timedelta(seconds=0.1))
    assert _market(_broker(skcom, now=now).events(TMF_CODE)) == [
        TxTradeCounts(datetime(2026, 11, 9, 8, 45, 10), buy_count=1500, sell_count=1620)]


def test_tx_deal_counts_are_subscribed_for_the_tx_day_session_code():
    """`psPageNo` 是 `[in] SHORT*`，comtypes 要的是 `ctypes.c_short`（官方範例的寫法），
    而且只回傳一個整數，不像 RequestTicks 回 `(page, 代碼)`。"""
    skcom = FakeSkcom()
    _market(_broker(skcom).events(TMF_CODE))
    [(page, code)] = _requests(skcom, "SKQuoteLib_RequestFutureTradeInfo")
    assert (page.value, code) == (0, TX_CODE)


# --- 訂閱失敗 ---


@pytest.mark.parametrize("method, code", [
    ("SKQuoteLib_EnterMonitorLONG", 3030),           # 行情連線超過限制
    ("SKQuoteLib_RequestTicks", 1004),
    ("SKQuoteLib_GetStockByNoLONG", 1004),
    ("SKQuoteLib_RequestFutureTradeInfo", 3031),     # 沒簽期貨 API 下單同意書
])
def test_a_subscription_the_broker_refuses_is_quote_unavailable_with_its_message(method, code):
    """少了任何一路，那天的判斷就缺一條腿（沒有筆差、沒有成交價）。在開始收行情之前
    就講清楚是哪一步、群益怎麼說，而不是安靜地少一種事件。"""
    skcom = FakeSkcom(quote_codes={method: code})
    with pytest.raises(QuoteUnavailable, match=f"群益訊息{code}"):
        _market(_broker(skcom).events(TMF_CODE))


def test_market_data_before_logging_in_is_quote_unavailable():
    broker = CapitalBroker("A100000000", "pw", com=FakeSkcom())
    with pytest.raises(QuoteUnavailable, match="尚未登入"):
        next(broker.events(TMF_CODE))


def test_a_tick_whose_time_cannot_be_read_is_skipped_and_the_stream_goes_on(caplog):
    """事件處理函式裡拋出的例外會被 COM 吞掉，之後的事件也可能跟著不見。
    看不懂的那一筆跳過、記下來，後面的照常。"""
    skcom = FakeSkcom(market=[
        _tick(TX_INDEX, 0, 84500, 4500000),
        _tick(TX_INDEX, 20261109, 84501, 4501000),
    ])
    trades = [(e.time, e.price) for e in _market(_broker(skcom).events(TMF_CODE))]
    assert trades == [(datetime(2026, 11, 9, 8, 45, 1), 45010.0)]
    assert "看不懂" in caplog.text
