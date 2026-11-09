"""測試用的假 broker —— 之後每一張 ticket 的流程測試都從這裡控制輸入、觀察委託。

它做兩件事：**照劇本回放**（行情事件、前幾天的 1 分 K、成交結果、失敗），以及
**記錄自己收到了哪些委託**。後者是關鍵：「自動下單關閉時，下單函式一次都沒被呼叫」
這種驗收條件，只有在能觀察呼叫紀錄時才證明得了（規格 Testing Decisions）。

送單前的規則（過期合約不送、口數上限、當沖註記）和群益 broker 共用
`broker/__init__.py` 的同一份程式，`tests/test_broker_contract.py` 對兩者跑同一組測試。
"""

from __future__ import annotations

from datetime import date
from typing import Iterable, Iterator

from broker import (
    ContractInfo,
    MarketEvent,
    MinuteBar,
    OrderRequest,
    OrderResult,
    ProductTrade,
    ensure_sendable,
)


class FakeBroker:
    """照劇本回放一個交易日。

    - `events`：`events()` 依序產生的行情事件。
    - `minute_bars`：{交易日: 那天的大台 1 分 K}。
    - `fill_price`：成交價。沒給的話用劇本裡**已經回放過的**最後一筆 `ProductTrade`
      ——市價單成交在當下的價格。兩者都沒有就拒絕送單（那是測試沒寫對）。
    - `fills`：每筆委託實際成交幾口，逐筆消耗。沒給就是全部成交。
    - `order_errors`：每筆委託要拋的例外（`None` 代表照常成交），逐筆消耗。
    """

    def __init__(
        self,
        *,
        contracts: dict[str, ContractInfo] | None = None,
        events: Iterable[MarketEvent] = (),
        minute_bars: dict[date, list[MinuteBar]] | None = None,
        fill_price: float | None = None,
        fills: Iterable[int] | None = None,
        order_errors: Iterable[Exception | None] = (),
        login_error: Exception | None = None,
        contracts_error: Exception | None = None,
    ):
        self._contracts = contracts or {}
        self._events = list(events)
        self._minute_bars = minute_bars or {}
        self._fill_price = fill_price
        self._last_product_price: float | None = None
        self._fills = list(fills) if fills is not None else None
        self._order_errors = list(order_errors)
        self._login_error = login_error
        self._contracts_error = contracts_error
        # 收到的委託，依序。「一次都沒被呼叫」就是斷言 `orders == []`——
        # 這是唯一會動到錢的路徑，證明它沒被走過比證明它走對了更重要。
        # 被 `ensure_sendable` 擋下的委託沒有送出去，所以不在這裡。
        self.orders: list[OrderRequest] = []

    def login(self) -> None:
        if self._login_error is not None:
            raise self._login_error

    def get_contracts(self) -> dict[str, ContractInfo]:
        if self._contracts_error is not None:
            raise self._contracts_error
        return dict(self._contracts)

    def minute_bars(self, day: date) -> list[MinuteBar]:
        return list(self._minute_bars.get(day, []))

    def events(self) -> Iterator[MarketEvent]:
        for event in self._events:
            if isinstance(event, ProductTrade):
                self._last_product_price = event.price
            yield event

    def place_order(self, request: OrderRequest) -> OrderResult:
        ensure_sendable(request)
        self.orders.append(request)
        seq = f"{2315610000000 + len(self.orders):013d}"

        error = self._order_errors.pop(0) if self._order_errors else None
        if error is not None:
            raise error

        filled = self._fills.pop(0) if self._fills else request.lots
        if not filled:
            return OrderResult(order_seq=seq, filled_lots=0, avg_price=None)
        price = self._fill_price if self._fill_price is not None else self._last_product_price
        if price is None:
            raise AssertionError(
                "這個測試沒有安排成交價：給 fill_price=，或在劇本裡放交易商品的成交（ProductTrade）")
        return OrderResult(order_seq=seq, filled_lots=filled, avg_price=price)
