"""broker —— PTS 與券商往來的唯一出入口，也是測試的兩個接縫之一。

群益的 API 是 Windows COM 元件，只能在裝好憑證的 Windows 上跑，而且會動到真錢。
把它隔離在這裡，策略、流程、部位檔、通知都能在沒有群益元件、沒有帳號的機器上驗證
（規格第 59 條）。所有外部的不確定因素（行情、成交、時間）都從這裡進來，
換掉它一個就能控制全部輸入。

實作有兩個，契約相同：

  - `broker.fake.FakeBroker`        測試用，照劇本回放並記錄收到的委託
  - `broker.capital.CapitalBroker`  群益 COM（從 invest-os 搬來後擴充）

本模組只放**兩個實作共用的東西**：商品代碼、資料結構、例外、送單前的檢查。
共用的好處是「假 broker 與真實 broker 守同一條規則」不是靠自律，而是靠結構。

⚠️ 任何真實 COM 的初始化都必須延遲到函式內執行，不可在模組載入時做——
   否則沒有群益元件的機器連 import 都會失敗，測試一行都跑不了。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterator, Protocol, Union, runtime_checkable

# 報價代碼（近月連續、日盤）。PTS 的指標一律看大台；交易商品三選一。
#
# ⚠️ **必須帶 `AM` 後綴**。群益對同一商品提供兩套代碼，不帶後綴的是全盤（含夜盤），
#    開盤價會是夜盤的開盤價。用錯不會報錯、數值也完全合理，但每天都會靜默失準
#    （invest-os ADR-0005）。微台用四個零，不是大台那種兩個零（2026-08-07 實測）。
TX_CODE = "TX00AM"
MTX_CODE = "MTX00AM"
TMF_CODE = "TM0000AM"
PRODUCT_CODES = (TX_CODE, MTX_CODE, TMF_CODE)

# 使用者在設定檔裡寫的名字 → 報價代碼。名字取期交所的商品代號，使用者認得。
PRODUCT_NAMES = {"TX": TX_CODE, "MTX": MTX_CODE, "TMF": TMF_CODE}

# 期交所對**每筆市價委託**的口數上限：一般交易時段 10 口（自 108/5/27 起）。
# PTS 只送市價 IOC、只在日盤交易，所以每一筆委託都受它限制；超過會被退單，
# 而退單訊息看不出是這個原因。要送更多口就拆成多筆（出場程式，規格第 33 條）。
MARKET_ORDER_LOT_CAP = 10


# ── 例外 ──────────────────────────────────────────────────────────────


class BrokerError(Exception):
    """broker 層的錯誤基底。"""


class LoginFailed(BrokerError):
    """登入失敗。重試無用，需要人介入（密碼、憑證、聲明書、元件沒註冊等）。"""


class ProductListUnavailable(BrokerError):
    """商品清單查不到、或缺少要用的商品。重試可能有幫助。"""


class QuoteUnavailable(BrokerError):
    """行情訂閱不起來（報價主機連不上、群益拒絕訂閱）。訊息講得出是哪一步、群益怎麼說。

    少了任何一路，那天的判斷就缺一條腿（沒有筆差或沒有成交價），所以不是安靜地少一種
    事件，而是在開始收行情之前就失敗。
    """


# 委託失敗只分兩類，界線是「單有沒有送出去」（規格第 41、50、51 條）。
# 這是整個系統最貴的一條分界線：
#
#   OrderFailed  → 確定沒有部位 → 當天停止進場、告警「委託沒有送出去」
#   FillUnknown  → **可能已經成交** → 部位記為「不確定」、出場程式不自動平倉、
#                  告警「委託送出去了，但…」
#
# 分不清楚的時候一律是 FillUnknown。把它說成 OrderFailed 的話，上層不會寫部位檔，
# 出場程式以為今天沒進場——部位直接過夜，而那是這個策略唯一不能接受的結果。


class OrderFailed(BrokerError):
    """委託沒有送出去（被拒、合約已過期、元件初始化失敗、憑證問題…）。**確定沒有部位。**

    訊息會進 Discord（規格第 51 條），所以寫給人看：原因要講得出來，
    而且**不可以帶未遮罩的帳號**。
    """


class FillUnknown(BrokerError):
    """**委託已經送出，但不知道成交幾口。** 回報沒回來、認不出是哪一筆、送出後元件出錯。

    `order_seq` 讓使用者能在券商 APP 直接查到那一筆，出場程式也拿它做兩段查詢
    （規格第 50 條）；`known_filled` 是目前已知**至少**成交了幾口。
    序號可能是空字串：送出成功卻沒拿到序號時，連查都沒辦法查。
    """

    def __init__(self, message: str, *, order_seq: str = "", known_filled: int = 0):
        super().__init__(message)
        self.order_seq = order_seq
        self.known_filled = known_filled


# ── 合約 ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ContractInfo:
    """近月合約，來自**商品清單查詢**。

    ⚠️ 刻意不由日期推算。「每月第三個週三」在最後交易日遇假日順延時會失準，
    而結算週的停用範圍（規格第 23、24 條）與「過期合約絕不送單」都依賴它。
    """

    code: str                  # 報價代碼（近月連續，如 TX00AM）
    last_trading_day: date     # 結算日
    # 下單代碼（指名月份，如 TX08）。**與報價代碼是兩回事**：三個商品的格式互不相同
    # （TX08／MTX08／TM2608），只能從商品清單讀出來，不可以自己組字串。
    order_code: str

    @property
    def contract_month(self) -> str:
        """契約年月（`yyyymm`）。最後交易日必定落在契約月份內。"""
        return self.last_trading_day.strftime("%Y%m")

    def has_expired(self, day: date) -> bool:
        """最後交易日是不是已經過了。**結算日當天不算過期**——那天 13:30 才停止交易。

        存在的理由是下單代碼的形式（`MTX08`，商品＋月份兩碼）：群益在該月已過期時
        會**自動改送隔年同月且不報錯**（官方文件 SendFutureOrderCLR 備註），
        於是 9 月送 `MTX08` 成交的會是隔年 8 月的合約——完全不同的東西，而且悄無聲息。
        """
        return self.last_trading_day < day


# ── 委託 ──────────────────────────────────────────────────────────────

# 委託買賣別。刻意不用群益的 0/1：那兩個數字在程式碼裡看不出誰是誰，
# 而寫反的後果是開出完全相反的部位。轉成群益格式的工作留在 broker.capital_wire。
BUY = "BUY"
SELL = "SELL"

# 標當沖的商品（規格第 28 條）。大台、小台標當沖，保證金減半才放得下 20 口，
# 並由券商 13:30 強平；**微台不能標**，改由出場程式 13:25 平倉（ADR-0002）。
DAY_TRADE_CODES = frozenset({TX_CODE, MTX_CODE})


@dataclass(frozen=True)
class OrderRequest:
    """一筆市價 IOC 委託。進場、出場都用它；倉別一律「自動」（見 capital_wire）。

    `trading_day` 是送單當天的交易日，用來擋過期合約。由呼叫端傳入而不是 broker
    自己讀時鐘：盤中程式的「現在」一律取自事件（規格「時間」一節）。
    """

    contract: ContractInfo
    side: str               # BUY / SELL
    lots: int
    trading_day: date

    def __post_init__(self) -> None:
        """這幾種錯誤是呼叫端寫錯了，不是市場狀況，所以在建立委託時就擋下。"""
        if self.side not in (BUY, SELL):
            raise ValueError(f"買賣別必須是 BUY 或 SELL，目前是 {self.side!r}")
        if not 1 <= self.lots <= MARKET_ORDER_LOT_CAP:
            raise ValueError(
                f"每筆委託必須是 1～{MARKET_ORDER_LOT_CAP} 口，目前是 {self.lots}。"
                "期交所限制每筆市價委託最多 10 口，要送更多就拆成多筆。"
            )
        if not self.contract.order_code:
            raise ValueError(f"{self.contract.code} 沒有下單代碼，無法送出委託")

    @property
    def day_trade(self) -> bool:
        """這筆要不要標當沖。由商品決定，呼叫端不必（也不能）自己選。"""
        return self.contract.code in DAY_TRADE_CODES


@dataclass(frozen=True)
class OrderResult:
    """委託的結局：**實際**成交的口數與價格（規格第 29 條）。

    市價 IOC 可能只成交一部分、甚至 0 口（當下沒有對手價）——那不是錯誤，
    是正常結局，記下實際口數就好。
    """

    order_seq: str              # 群益的 13 碼委託序號
    filled_lots: int
    avg_price: float | None     # 成交均價（依口數加權）；0 口時是 None


def ensure_sendable(request: OrderRequest) -> None:
    """送單前的最後一道檢查。兩個 broker 都在送出之前呼叫它，規則因此只有一份。

    過期合約**絕不送單**：理由見 `ContractInfo.has_expired`。這是「確定沒送出去」，
    所以是 OrderFailed——上層照「委託沒有送出去」處理，當天停止進場並告警。
    """
    contract = request.contract
    if contract.has_expired(request.trading_day):
        raise OrderFailed(
            f"{contract.order_code} 的最後交易日 {contract.last_trading_day:%Y/%m/%d} 已過，"
            f"今天是 {request.trading_day:%Y/%m/%d}，不送單。"
            "群益會把過期月份自動改送隔年同月，送出去就是另一個合約。"
        )


# ── 行情：事件串流與 1 分 K ──────────────────────────────────────────────
#
# 盤中程式的「現在」一律取自事件上的時間，不讀系統時鐘（規格「時間」一節）。
# 所以每一個事件都帶 `time`，由 broker 蓋上：
#
#   * 群益沒有每秒一次的事件（`OnTimer` 每分鐘一次），`Clock` 由 broker 在訊息幫浦
#     迴圈裡用本機時鐘產生。
#   * 筆差的來源 `OnNotifyFutureTradeInfoLONG` 沒有日期也沒有時間，`TxTradeCounts`
#     的時間是 broker 收到它的時刻。
#   * 成交價來自逐筆成交（`OnNotifyTicksLONG`），帶的是交易所的時間。試撮（`nSimulate=1`）
#     由 broker 濾掉；**不是今天的回補照樣送出**，日期留在時間上——「報價屬於前一個交易日」
#     是盤中流程要判斷的事（規格第 10 條），broker 先濾掉的話它就看不到證據了。
#
# 細節與出處見 scratchpad 的 notes/capital-api.md 第 1–4 節。


@dataclass(frozen=True)
class Clock:
    """每秒一次的時鐘。沒有成交的時候，13:20 截止、報價中斷偵測也要有時間可看。"""

    time: datetime


@dataclass(frozen=True)
class TxTrade:
    """大台的一筆成交。組 1 分 K、盯突破進場的觸發價都用它。"""

    time: datetime
    price: float


@dataclass(frozen=True)
class TxTradeCounts:
    """大台到這一刻為止的**累計**成交買進／賣出筆數。筆差 = 賣出筆數 − 買進筆數。

    累計起點（08:45？含夜盤？）還不知道，要等 ticket 03 的實機紀錄回答。
    """

    time: datetime
    buy_count: int
    sell_count: int


@dataclass(frozen=True)
class ProductTrade:
    """交易商品的一筆成交。「部位不虧」用它當現價。交易大台時與 TxTrade 是同一筆。"""

    time: datetime
    price: float


MarketEvent = Union[Clock, TxTrade, TxTradeCounts, ProductTrade]


@dataclass(frozen=True)
class MinuteBar:
    """大台日盤的一根 1 分 K。`start` 是這根 K 棒開始的那一分鐘（08:45 那根是 08:45）。

    券商 K 線標的是起點還是終點，文件沒寫（ticket 02 實機核對）；不管它怎麼標，
    進到這個型別時一律換算成起點。
    """

    start: datetime
    open: float
    high: float
    low: float
    close: float


# ── 契約 ──────────────────────────────────────────────────────────────


@runtime_checkable
class Broker(Protocol):
    """PTS 對券商的全部要求（規格 Implementation Decisions「broker」）。

    失敗只有兩類會從 `place_order` 出來：`OrderFailed`（確定沒送出去）與
    `FillUnknown`（送出去但不知道成交）。
    """

    def login(self) -> None:
        """失敗是 LoginFailed，重試無用。"""

    def get_contracts(self) -> dict[str, ContractInfo]:
        """大台、小台、微台的近月下單代碼與最後交易日，以報價代碼為鍵。"""

    def minute_bars(self, day: date) -> list[MinuteBar]:
        """指定交易日的大台日盤 1 分 K，依時間排序。那天沒有資料就是空的。"""

    def events(self, product: str) -> Iterator[MarketEvent]:
        """訂閱大台與交易商品 `product`（報價代碼），依序產生行情事件。

        真 broker 的串流不會自己結束，由呼叫端在 13:20 停止取用。
        """

    def place_order(self, request: OrderRequest) -> OrderResult:
        """送出市價 IOC，等到委託確定結束，回傳實際成交。"""
