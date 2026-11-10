"""broker 的契約：假 broker 與群益 broker 守同一組規則（規格 Testing Decisions「接縫」）。

假 broker 是之後每一張 ticket 的流程測試要用的接縫：照劇本回放一整個交易日的事件，
並記錄收到的委託。它和真的 broker 不一致的地方，就是流程測試會說謊的地方——
所以「送單前的規則」兩邊跑同一組測試，而且規則本身只寫在 `broker/__init__.py` 一處。
"""

from datetime import date, datetime

import pytest

from broker import (
    BUY,
    SELL,
    TMF_CODE,
    TX_CODE,
    Broker,
    Clock,
    FillUnknown,
    MinuteBar,
    OrderFailed,
    ProductTrade,
    TxTrade,
    TxTradeCounts,
)
from broker.fake import FakeBroker
from day_script import contracts, order_request
from fake_skcom import FakeSkcom, capital_broker

class _Fake:
    def __init__(self):
        self.broker = FakeBroker(contracts=contracts(), fill_price=45000.0)

    def sent(self) -> int:
        return len(self.broker.orders)


class _Capital:
    def __init__(self):
        self.skcom = FakeSkcom()
        self.broker = capital_broker(self.skcom, fill_timeout=0.3, pump_interval=0.05)
        self.broker.login()

    def sent(self) -> int:
        return len(self.skcom.sent)


BOTH = pytest.mark.parametrize("make", [_Fake, _Capital], ids=["fake", "capital"])


# --- 兩個 broker 共用的規則 ---


@BOTH
def test_both_brokers_offer_the_whole_contract(make):
    """登入、商品清單、1 分 K 歷史、事件串流、送單——規格 Implementation Decisions 的五項。"""
    assert isinstance(make().broker, Broker)


@BOTH
def test_an_expired_contract_is_never_sent_by_either_broker(make):
    side = make()
    with pytest.raises(OrderFailed, match="最後交易日"):
        side.broker.place_order(order_request(day=date(2026, 11, 19)))
    assert side.sent() == 0


@BOTH
def test_the_settlement_day_is_sent_by_either_broker(make):
    side = make()
    side.broker.place_order(order_request(day=date(2026, 11, 18)))
    assert side.sent() == 1


@pytest.mark.parametrize("lots", [0, 11])
def test_an_order_outside_one_to_ten_lots_cannot_even_be_written(lots):
    """期交所每筆市價單最多 10 口。超過要拆單（出場程式），這裡擋的是呼叫端寫錯。"""
    with pytest.raises(ValueError, match="10"):
        order_request(lots=lots)


@BOTH
def test_ten_lots_is_the_cap_itself_and_is_sent_by_either_broker(make):
    """10 口是上限本身，要送得出去。出場程式拆單時每筆就是 10 口（規格第 33 條）——
    上界錯一格的話，13:25 的微台平倉會直接拋例外，部位就過夜了。"""
    side = make()
    assert side.broker.place_order(order_request(lots=10)).filled_lots == 10
    assert side.sent() == 1


# --- 假 broker：記錄收到的委託 ---


def test_the_fake_records_every_order_it_receives_with_the_day_trade_mark():
    """流程測試斷言「送出了哪些委託」就是讀這裡。當沖註記由商品決定，看得到才測得到。"""
    fake = FakeBroker(contracts=contracts(), fill_price=45000.0)
    fake.place_order(order_request(TX_CODE, side=SELL, lots=2))
    fake.place_order(order_request(TMF_CODE))
    assert [(o.contract.order_code, o.side, o.lots, o.day_trade) for o in fake.orders] == [
        ("TX11", SELL, 2, True),
        ("TM2611", BUY, 1, False),
    ]


def test_the_fake_fills_everything_at_the_last_traded_price_by_default():
    """市價單成交在當下的價格。劇本裡交易商品最後一筆成交價就是成交價。"""
    t = datetime(2026, 11, 9, 8, 50)
    fake = FakeBroker(contracts=contracts(), events=[ProductTrade(t, 45010.0),
                                                   ProductTrade(t, 45012.0)])
    for _ in fake.events(TMF_CODE):
        pass
    result = fake.place_order(order_request(lots=2))
    assert (result.filled_lots, result.avg_price) == (2, 45012.0)
    assert result.order_seq


def test_the_fake_can_fill_part_of_an_order():
    fake = FakeBroker(contracts=contracts(), fill_price=45000.0, fills=[1, 0])
    assert fake.place_order(order_request(lots=3)).filled_lots == 1
    nothing = fake.place_order(order_request(lots=3))
    assert (nothing.filled_lots, nothing.avg_price) == (0, None)


def test_the_fake_plays_back_both_kinds_of_failure_inorder_request():
    """「拒單之後當天停止」「不確定之後當天停止」這類流程測試靠它（ticket 07）。"""
    fake = FakeBroker(contracts=contracts(), fill_price=45000.0, order_errors=[
        OrderFailed("保證金不足"),
        FillUnknown("沒等到回報", order_seq="2315610000001"),
        None,
    ])
    with pytest.raises(OrderFailed):
        fake.place_order(order_request())
    with pytest.raises(FillUnknown):
        fake.place_order(order_request())
    assert fake.place_order(order_request()).filled_lots == 1
    assert len(fake.orders) == 3, "失敗的委託也送出去過，要記下來"


def test_a_fake_without_a_price_refuses_to_invent_one():
    """沒安排成交價的測試卻送了單——那是測試沒寫對，不是成交在 0 元。"""
    fake = FakeBroker(contracts=contracts())
    with pytest.raises(AssertionError, match="成交價"):
        fake.place_order(order_request())


# --- 假 broker：照劇本回放一個交易日 ---


def test_the_fake_replays_the_days_events_inorder_request():
    t = datetime(2026, 11, 9, 8, 45)
    script = [Clock(t), TxTrade(t, 45000.0), TxTradeCounts(t, buy_count=10, sell_count=12),
              ProductTrade(t, 45001.0)]
    assert list(FakeBroker(events=script).events(TMF_CODE)) == script


@pytest.mark.parametrize("product", ["TMF", "TX00", ""])
def test_the_fake_refuses_to_stream_a_product_code_the_real_broker_would_not_subscribe(product):
    """劇本已經決定了交易商品的成交，假 broker 用不到這個參數——但真 broker 拿它去訂閱，
    流程傳錯代碼（設定檔的名字 TMF、不帶 AM 的全盤代碼）在實機上就是訂錯商品。"""
    with pytest.raises(ValueError, match="交易商品"):
        next(FakeBroker(events=[Clock(datetime(2026, 11, 9, 8, 45))]).events(product))


def test_the_fake_returns_the_minute_bars_of_the_day_asked_for():
    """暖身 RSI、取昨收、重啟時補回今天的 K 棒，都從這裡拿（ticket 04、09）。"""
    bar = MinuteBar(datetime(2026, 11, 6, 13, 44), 45000.0, 45010.0, 44990.0, 45005.0)
    fake = FakeBroker(minute_bars={date(2026, 11, 6): [bar]})
    assert fake.minute_bars(date(2026, 11, 6)) == [bar]
    assert fake.minute_bars(date(2026, 11, 5)) == []


def test_the_fake_can_fail_to_log_in_or_to_list_products():
    from broker import LoginFailed, ProductListUnavailable

    with pytest.raises(LoginFailed):
        FakeBroker(login_error=LoginFailed("密碼錯誤")).login()
    with pytest.raises(ProductListUnavailable):
        FakeBroker(contracts_error=ProductListUnavailable("查不到")).get_contracts()
