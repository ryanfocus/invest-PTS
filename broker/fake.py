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
    PRODUCT_CODES,
    ContractInfo,
    FillQuery,
    MarketEvent,
    MinuteBar,
    OrderRequest,
    OrderResult,
    ProductTrade,
    ensure_sendable,
)


class FakeBroker:
    """照劇本回放一個交易日。

    - `events`：`events(product)` 依序產生的行情事件（一整個交易日的劇本）。
    - `minute_bars`：{交易日: 那天的大台 1 分 K}。
    - `fill_price`：成交價。沒給的話用劇本裡**已經回放過的**最後一筆 `ProductTrade`
      ——市價單成交在當下的價格。兩者都沒有就拒絕送單（那是測試沒寫對）。
    - `fills`：每筆委託實際成交幾口，逐筆消耗。沒給就是全部成交。
    - `order_errors`：每筆委託要拋的例外（`None` 代表照常成交），逐筆消耗。
    - `login_error`／`contracts_error`／`minute_bars_error`：登入、查商品清單、查 1 分 K 時要拋的
      例外。給一個例外是每次都拋；給一個清單是逐次消耗（`None` 代表那一次照常），用完之後照常
      ——「第一次斷線、一分鐘後好了」就是 `[LoginUnavailable(...), None]`。
    - `fill_queries`：成交查詢（`query_fills`）的答案，逐次消耗。**沒安排卻被查就是
      AssertionError**：查詢只該出現在出場單不確定之後，測試沒預期到它就是流程走錯路。
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
        login_error: Exception | list[Exception | None] | None = None,
        contracts_error: Exception | list[Exception | None] | None = None,
        minute_bars_error: Exception | list[Exception | None] | None = None,
        fill_queries: Iterable[FillQuery] = (),
    ):
        self._contracts = contracts or {}
        self._events = list(events)
        self._minute_bars = minute_bars or {}
        self._fill_price = fill_price
        self._last_product_price: float | None = None
        self._fills = list(fills) if fills is not None else None
        self._order_errors = list(order_errors)
        self._login_error = _Failures(login_error)
        self._contracts_error = _Failures(contracts_error)
        self._minute_bars_error = _Failures(minute_bars_error)
        self._fill_queries = list(fill_queries)
        # 被查過的委託，依序：(委託序號, 交易日, 委託口數)。出場單不確定時查的必須是
        # **那一張出場單**——拿進場單的序號去查，查到的是早上的成交，看起來就像已經平倉。
        self.queries: list[tuple[str, date, int]] = []
        # 收到的委託，依序。「一次都沒被呼叫」就是斷言 `orders == []`——
        # 這是唯一會動到錢的路徑，證明它沒被走過比證明它走對了更重要。
        # 被 `ensure_sendable` 擋下的委託沒有送出去，所以不在這裡。
        self.orders: list[OrderRequest] = []
        # `events(product)` 被要求訂閱的交易商品，依序。劇本用不到它，但真 broker 拿它去
        # 訂閱：流程訂錯商品，「部位不虧」看的就是別的商品的價格。
        self.streamed: list[str] = []

    def login(self) -> None:
        self._login_error.next()

    def get_contracts(self) -> dict[str, ContractInfo]:
        self._contracts_error.next()
        return dict(self._contracts)

    def minute_bars(self, day: date) -> list[MinuteBar]:
        self._minute_bars_error.next()
        return list(self._minute_bars.get(day, []))

    def events(self, product: str) -> Iterator[MarketEvent]:
        """劇本裡的事件，依序。`product` 只檢查不使用：劇本本身已經決定了交易商品的成交，
        但傳錯代碼的流程在真 broker 上會訂錯商品，不能在這裡默默放行。"""
        if product not in PRODUCT_CODES:
            raise ValueError(f"交易商品必須是 {PRODUCT_CODES} 其中之一，收到 {product!r}")
        self.streamed.append(product)
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

    def query_fills(self, order_seq: str, trading_day: date,
                    requested_lots: int) -> FillQuery:
        self.queries.append((order_seq, trading_day, requested_lots))
        if not self._fill_queries:
            raise AssertionError(
                f"這個測試沒有安排成交查詢的答案（委託 {order_seq}）：給 fill_queries=")
        answer = self._fill_queries.pop(0)
        if answer.lots is not None and not 1 <= answer.lots <= requested_lots:
            # 真的 broker 回不出這個值（超過委託量的加總會被判定為看不懂）
            raise AssertionError(f"查到 {answer.lots} 口，不在 1 到委託的 {requested_lots} 口之間")
        if answer.lots is not None and not answer.avg_price:
            # 真的 broker 認得出成交列就一定讀得到價格（讀不到是看不懂，算查詢失敗）
            raise AssertionError("查到成交卻沒有成交價：給 FillQuery(lots=…, avg_price=…)")
        return answer


class _Failures:
    """劇本裡某一個呼叫的失敗：一個例外是每次都拋，一個清單是逐次消耗、用完之後照常。"""

    def __init__(self, spec: Exception | list[Exception | None] | None):
        self._always = spec if isinstance(spec, BaseException) else None
        self._each = [] if spec is None or self._always is not None else list(spec)

    def next(self) -> None:
        """這一次呼叫該失敗就拋出例外。"""
        error = self._each.pop(0) if self._each else self._always
        if error is not None:
            raise error
