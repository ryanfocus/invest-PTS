r"""群益策略王 COM 元件的 broker 實作。從 invest-os 的 `broker/capital.py` 搬來後擴充。

⚠️ **本模組在載入時不碰 COM。** `comtypes`／`pythoncom` 的 import 與物件建立都在函式內，
   否則沒有群益元件的機器連 `import` 都會失敗，測試一行都跑不了。
   群益官方範例在模組層級就建立物件，**不要照抄那部分**。

COM 本身被包在 `_RealCom` 這個小介面後面（載入、建立元件、掛事件、打訊息幫浦、
建委託物件）。正式執行用 `_RealCom`；測試換成 `tests/fake_skcom.py`，從公開介面打進來。

這裡的每一段幾乎都是 invest-os 踩過坑換來的，順序不能隨意調動：

  0. `SKCenterLib_SetLogPath` **必須最先呼叫**（官方文件 4-1-1）。不設定的話元件把
     含身分證字號的 log 寫進執行檔所在目錄，也就是 Python 的安裝目錄（規格第 60 條）。
  1. `GetModule` 必須用**絕對路徑**，從註冊表讀，不寫死 `C:\SKCOM`。
     用相對檔名會拿到 `TYPE_E_CANTLOADLIBRARY`。
  2. 公告事件（`OnReplyMessage`）必須在登入之前註冊，且必須回傳 -1。
  3. `OnShowAgreement` 也必須註冊，否則 `EnterMonitorLONG` 會回 2018。
  4. 登入用 `SKCenterLib_LoginSetQuote(id, pw, "Y")`，第三個參數明確啟用報價元件。
  5. `EnterMonitorLONG` 之後要等 `OnConnection` 回 3003（商品資料就緒）。
  6. 送單前要 `ReadCertByID`，少了它委託會被回 1038（2026-08-21 實機）。
  7. 回報連線的事件是 `OnSolaceReplyConnection`／`OnSolaceReplyDisconnect`。
     invest-os 掛的舊名 `OnConnect`／`OnDisconnect` 在 2.13.48 起已經移除，不會觸發——
     那邊因此每天印「回報連線狀態未知」，盤中斷線也偵測不到。成功連線時的代碼文件沒寫，
     所以「連上而且補送完畢」看的是 `OnComplete`。

對外只會拋出 broker 定義的例外：登入是 `LoginFailed`、商品清單是
`ProductListUnavailable`、送單只有兩類——`OrderFailed`（確定沒送出去）與
`FillUnknown`（送出去但不知道成交）。元件自己拋的例外都會依發生在送出之前或之後
轉成其中一類，上層不必猜。
"""

from __future__ import annotations

import ctypes
import logging
import os
import time
from collections import deque
from datetime import date, datetime, timedelta
from typing import Callable, Iterator

import paths
from broker import (
    Clock,
    ContractInfo,
    FillUnknown,
    LoginFailed,
    MarketEvent,
    MinuteBar,
    OrderFailed,
    OrderRequest,
    OrderResult,
    ProductListUnavailable,
    ProductTrade,
    QuoteUnavailable,
    TxTrade,
    TxTradeCounts,
    TX_CODE,
    ensure_sendable,
)
from broker.capital_wire import (build_future_order_fields, parse_minute_bars,
                                 parse_product_list, summarize_fills)

logger = logging.getLogger(__name__)

__all__ = ["CapitalBroker"]

# SKCenterLib 的 CLSID，用來從註冊表反查 SKCOM.dll 的實際位置
_SKCENTER_CLSID = "{AC30BAB5-194A-4515-A8D3-6260749F8577}"

# 報價主機的連線狀態代碼：收到它才代表商品資料下載完成
_STOCKS_READY = 3003

# 商品清單的市場別：0 上市、1 上櫃、2 期貨、3 選擇權
_MARKET_FUTURES = 2

# 報價的價格是整數、放大 100 倍（官方範例與 invest-os 實測都是 /100，和期交所一致）
_PRICE_SCALE = 100

# 每隔幾秒問一次主機時間，保持報價連線（文件 4-4-5：「請固定每十五秒呼叫此函式」）
_KEEPALIVE = timedelta(seconds=15)

# 1 分 K 歷史的查詢參數（文件 4-4-24）。AM 盤由 sTradeSession 選，代碼不帶 AM。
_KLINE_CODE = "TX00"
_KLINE_MINUTES = 0          # sKLineType：0 分線
_KLINE_NEW_FORMAT = 1       # sOutType：1 新版（價格已處理過小數點）
_KLINE_AM_SESSION = 1       # sTradeSession：1 AM 盤（日盤）


class _RealCom:
    """真的 COM 執行環境。所有 `comtypes`／`pythoncom` 的使用都關在這裡。

    測試把它整個換掉（`CapitalBroker(com=...)`），而 `tests/conftest.py` 會讓
    任何沒換掉的測試直接失敗——這台開發機裝著群益元件，忘了換就是真的登入。
    """

    def __init__(self) -> None:
        self._sk = None

    def load(self) -> None:
        import comtypes.client

        comtypes.client.GetModule(_resolve_dll_path())
        import comtypes.gen.SKCOMLib as sk

        self._sk = sk

    def create(self, name: str):
        """建立一個群益元件，例如 `create("SKCenterLib")`。"""
        import comtypes.client

        return comtypes.client.CreateObject(
            getattr(self._sk, name), interface=getattr(self._sk, f"I{name}"))

    def advise(self, lib, sink):
        """掛上事件接收端。回傳的 handle 要留著，被回收就收不到事件了。"""
        import comtypes.client

        return comtypes.client.GetEvents(lib, sink)

    def pump(self) -> None:
        """COM 事件需要訊息幫浦才會觸發。官方範例靠 tkinter 的 mainloop，我們自己打。"""
        import pythoncom

        pythoncom.PumpWaitingMessages()

    def new_future_order(self):
        return self._sk.FUTUREORDER()

    def new_stock(self):
        return self._sk.SKSTOCKLONG()


def _resolve_dll_path() -> str:
    """從註冊表取得 SKCOM.dll 的絕對路徑。"""
    import winreg

    key_path = rf"CLSID\{_SKCENTER_CLSID}\InprocServer32"
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, key_path) as key:
            return winreg.QueryValueEx(key, "")[0]
    except FileNotFoundError as exc:
        raise LoginFailed(
            "群益元件（SKCOM）尚未註冊。請以系統管理員身分執行群益元件資料夾裡的 install.bat"
        ) from exc


class _CenterEvents:
    """聲明書狀態接收端。沒有它，`EnterMonitorLONG` 會回 2018。"""

    def OnShowAgreement(self, bstrData):
        logger.info("聲明書狀態：%s", bstrData)


class _ReplyEvents:
    """公告、回報連線狀態、委託回報的接收端。

    ⚠️ 事件是**照方法名稱**送達的：名稱寫錯不會報錯，只是永遠收不到。

    「連上而且補送完畢」看的是 `OnComplete`，不是 `OnSolaceReplyConnection` 的代碼：
    成功時那個代碼是多少文件沒寫（0？3001？），寫死哪一個都可能每天報一個假的失敗。
    文件 4-3-c：「若未收到此通知，代表新建立的回報連線及回傳回報資料異常」。
    """

    def __init__(self) -> None:
        self.rows: list[str] = []
        # 收到 OnComplete 才是 True。**預設不可以是 True**——樂觀的預設值等於沒有這個檢查。
        self.complete = False
        self.dropped = ""

    def OnReplyMessage(self, bstrUserID, bstrMessages):
        return -1                 # 官方要求：公告事件必須回傳 -1

    def OnNewData(self, bstrUserID, bstrData):
        self.rows.append(bstrData)

    def OnSolaceReplyConnection(self, bstrUserID, nErrorCode):
        # 只記下原始代碼，不判斷成敗（理由見類別說明）。第一次實機之後可以回來釘死。
        logger.info("回報連線通知，代碼 %s", nErrorCode)

    def OnSolaceReplyDisconnect(self, bstrUserID, nErrorCode):
        # 中途斷線與「從未連上」的後果一樣：收不到成交回報。送單前會看這個值（不送）。
        self.dropped = f"回報連線中斷，代碼 {nErrorCode}"
        logger.error("%s。連回來之前不送委託", self.dropped)

    def OnComplete(self, bstrUserID):
        # 「連上而且補送完畢」——斷線之後元件自己重連成功的話，也是靠它知道。
        self.complete = True
        self.dropped = ""
        logger.info("回報連線完成，過去的回報已補送完畢")


class _QuoteEvents:
    """報價連線狀態、商品清單與行情的接收端。

    行情事件在訊息幫浦裡被呼叫，這裡只把它們翻成 PTS 的行情事件、排進 `market`，
    由 `CapitalBroker.events()` 依序交出去。

    ⚠️ 這些函式裡**不可以呼叫群益的查詢函式**（`GetStockByIndexLONG`、`GetTickLONG`，
       文件 4-4-l／m 備註）。所以「哪個索引是哪個商品」是訂閱時先查好的（`tick_codes`）。
    ⚠️ 這裡拋出的例外會被 comtypes 吞掉只印出來，所以看不懂的資料自己跳過、記下來。
    """

    def __init__(self) -> None:
        self.stocks_ready = False
        self.commodity_chunks: list[str] = []
        self.market: deque[MarketEvent] = deque()
        # 商品的系統索引 → 報價代碼。逐筆成交事件只帶索引，不帶代碼。
        self.tick_codes: dict[int, str] = {}
        self.product = ""
        # 這一輪訊息幫浦開始的時刻。累計筆數事件沒有時間，用它當「收到的時刻」。
        self.received_at = datetime.min
        self.trade_info_seen = False
        self.unknown_indexes: set[int] = set()
        # 1 分 K 歷史：一次只查一天，收齊（OnKLineComplete）之前不送下一個查詢
        self.kline_rows: list[str] = []
        self.kline_done = False

    def OnConnection(self, nKind, nCode):
        if nKind == _STOCKS_READY:
            self.stocks_ready = True

    def OnNotifyCommodityListWithTypeNo(self, sMarketNo, bstrCommodityData):
        # 清單很長（實測 46 萬字元），會分多次送來，全部接完再一起解析
        self.commodity_chunks.append(bstrCommodityData)

    def OnNotifyTicksLONG(self, sMarketNo, nIndex, nPtr, nDate, nTimehms, nTimemillismicros,
                          nBid, nAsk, nClose, nQty, nSimulate):
        """一筆成交（文件 4-4-m）。回補（History）的參數完全相同，同一個函式處理。

        ⚠️ 試撮（`nSimulate=1`）不是成交：08:30 起就有，2026-08-10 最早的試撮價 44981，
           真正的開盤價 44985 在 08:45:00。
        ⚠️ `nTimehms` 是整數，08:45:00 是 `84500`，用整數除法拆，不能切字串。
        ⚠️ **不在這裡濾掉別的交易日的成交**：那是盤中流程的報價交易日檢查要看的證據
           （規格第 10 條），日期原樣放在事件的時間上。
        `nTimemillismicros` 不用：文件說目前只有證券商品有值。
        """
        if nSimulate:
            return
        code = self.tick_codes.get(nIndex)
        if code is None:
            if nIndex not in self.unknown_indexes:
                self.unknown_indexes.add(nIndex)
                logger.warning("收到索引 %s 的成交，但訂閱時查到的是 %s，這個索引的成交全部略過",
                               nIndex, self.tick_codes)
            return
        try:
            t = datetime(nDate // 10000, nDate // 100 % 100, nDate % 100,
                         nTimehms // 10000, nTimehms // 100 % 100, nTimehms % 100)
        except ValueError:
            logger.warning("看不懂的逐筆成交時間（nDate=%s nTimehms=%s），略過這一筆",
                           nDate, nTimehms)
            return
        price = nClose / _PRICE_SCALE
        if code == TX_CODE:
            self.market.append(TxTrade(t, price))
        if code == self.product:
            self.market.append(ProductTrade(t, price))

    OnNotifyHistoryTicksLONG = OnNotifyTicksLONG

    def OnNotifyKLineData(self, bstrStockNo, bstrData):
        """一根 1 分 K 的字串（文件 4-4-b）。解析留到收齊之後：這裡拋例外會被 COM 吞掉。

        文件 4-4-t 的備註說結束時會「回傳一筆以『##』開頭的內容」，沒說是哪個事件，
        所以這裡也認它。
        """
        if bstrData.startswith("##"):
            self.kline_done = True
            return
        self.kline_rows.append(bstrData)

    def OnKLineComplete(self, bstrEndString):
        """「收完歷史KLine，等收到此事件通知後表示回補完成」（文件 4-4-t）。"""
        self.kline_done = True

    def OnNotifyFutureTradeInfoLONG(self, bstrStockNo, sMarketNo, nStockIdx, nBuyTotalCount,
                                    nSellTotalCount, nBuyTotalQty, nSellTotalQty,
                                    nBuyDealTotalCount, nSellDealTotalCount):
        """大台的累計筆數（文件 4-4-q）。筆差用的是最後兩個：**成交**買進／賣出筆數。
        前四個是**委託**的筆數與口數，名字很像，用錯不會報錯，但筆差就完全不是同一個東西。

        事件沒有日期也沒有時間，所以時間是收到它的時刻。從什麼時候開始累計、多久推一次，
        文件都沒寫（ticket 03 實機回答）。
        """
        if not self.trade_info_seen:
            self.trade_info_seen = True
            # 文件沒說回來的代碼是 TX00AM 還是別的寫法；第一次實機時看一眼
            logger.info("第一筆累計成交筆數：代碼 %s", bstrStockNo)
        self.market.append(TxTradeCounts(self.received_at, buy_count=nBuyDealTotalCount,
                                         sell_count=nSellDealTotalCount))


class CapitalBroker:
    """群益 COM 的 broker 實作。與 `broker.fake.FakeBroker` 同契約。"""

    def __init__(
        self,
        user_id: str,
        password: str,
        account: str = "",
        *,
        connect_timeout: float = 30.0,
        fill_timeout: float = 30.0,
        settle_seconds: float = 2.0,
        pump_interval: float = 0.05,
        now: Callable[[], datetime] = datetime.now,
        com=None,
    ):
        self._user_id = user_id
        self._password = password
        # 期貨帳號由不進版控的設定檔明確指定，不從 OnAccount 自動挑——
        # 有多個帳號時「自動挑一個」會把單下到使用者沒預期的帳戶上。
        self._account = account
        self._connect_timeout = connect_timeout
        self._fill_timeout = fill_timeout
        # 等「不一定會來」的非同步事件的時間：聲明書狀態、回報連線結果、帳號查詢。
        # 這些事件沒有可以等的完成條件（全部簽過的聲明書就不再回傳），只能等一段時間。
        self._settle_seconds = settle_seconds
        self._pump_interval = pump_interval
        self._now = now
        self._com = com
        self._center = None
        self._handlers: list = []
        self._monitoring = False
        self._order_ready = False

    # --- 內部：COM 生命週期 ---

    def _ensure_com(self) -> None:
        """建立 COM 物件並註冊事件。刻意延遲到這裡，模組載入時不執行。

        失敗時把狀態還原：半套的元件留著的話，下一次重試會以為已經建好了。
        """
        if self._center is not None:
            return
        if self._com is None:
            self._com = _RealCom()
        try:
            self._build_com(self._com)
        except BaseException:
            self._center = None
            raise

    def _build_com(self, com) -> None:
        com.load()
        center = com.create("SKCenterLib")
        self._center = center
        self._redirect_component_logs()

        self._reply = com.create("SKReplyLib")
        self._quote = com.create("SKQuoteLib")
        self._order = com.create("SKOrderLib")
        self._reply_events = _ReplyEvents()
        self._quote_events = _QuoteEvents()
        # 順序有意義：公告與聲明書都必須在登入前就有接收端
        self._handlers = [
            com.advise(self._reply, self._reply_events),
            com.advise(center, _CenterEvents()),
            com.advise(self._quote, self._quote_events),
        ]

    def _redirect_component_logs(self) -> None:
        """把群益元件自己寫的 log 導到 `logs/capital/`。**必須是第一個呼叫**（文件 4-1-1）。

        失敗時記 error 但不擋登入：log 寫錯地方是個資保存期限的問題，不會讓交易出錯；
        為它賠掉整天的交易不成比例。
        """
        log_dir = paths.capital_log_dir()
        try:
            os.makedirs(log_dir, exist_ok=True)
        except OSError as exc:
            logger.error("建不了群益元件的 log 目錄 %s（%s），它會寫回執行檔所在目錄",
                         log_dir, exc)
            return
        code = self._center.SKCenterLib_SetLogPath(log_dir)
        if code != 0:
            logger.error("群益元件的 log 路徑設定失敗（%s），它會寫回執行檔所在目錄",
                         self._message(code))

    def _pump_until(self, predicate: Callable[[], bool], seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while True:
            self._com.pump()
            if predicate():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def _pump_for(self, seconds: float) -> None:
        self._pump_until(lambda: False, seconds)

    def _message(self, code: int) -> str:
        return f"代碼 {code}：{self._center.SKCenterLib_GetReturnCodeMessage(code)}"

    # --- 對外契約 ---

    def login(self) -> None:
        """**只做身分驗證**。失敗一律 raise LoginFailed（重試無用，要人處理）。

        報價主機的連線刻意不放在這裡：那是會暫時性失敗的東西，歸類成 LoginFailed
        會讓它跳過重試。它屬於 `get_contracts` 與事件串流的範圍。
        """
        try:
            self._ensure_com()
        except LoginFailed:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LoginFailed(
                f"群益元件無法載入（{type(exc).__name__}）：{exc}。"
                "換版之後發生的話，刪掉 comtypes 產生的介面檔再試") from exc
        code = self._center.SKCenterLib_LoginSetQuote(self._user_id, self._password, "Y")
        if code != 0:
            raise LoginFailed(f"群益登入失敗，{self._message(code)}")
        logger.info("群益登入成功")
        # 聲明書狀態是登入後非同步查回來的，沒等它就連報價主機會拿到 2018
        self._center.SKCenterLib_RequestAgreement(self._user_id)
        self._pump_for(self._settle_seconds)

    def _ensure_quote_connection(self) -> None:
        """連上報價主機。失敗是 ProductListUnavailable——這些都值得重試。"""
        if self._monitoring:
            return
        code = self._quote.SKQuoteLib_EnterMonitorLONG()
        if code != 0:
            raise ProductListUnavailable(f"連線報價主機失敗，{self._message(code)}")
        if not self._pump_until(lambda: self._quote_events.stocks_ready, self._connect_timeout):
            raise ProductListUnavailable(
                f"{self._connect_timeout:.0f} 秒內未收到商品資料就緒通知")
        logger.info("報價主機連線完成")
        self._monitoring = True

    def get_contracts(self, wait: float = 8.0) -> dict[str, ContractInfo]:
        """查商品清單，取出大台、小台、微台的近月下單代碼與最後交易日。"""
        if self._center is None:
            raise ProductListUnavailable("尚未登入")
        try:
            return self._query_contracts(wait)
        except ProductListUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProductListUnavailable(
                f"查商品清單時元件發生錯誤（{type(exc).__name__}）：{exc}") from exc

    def _query_contracts(self, wait: float) -> dict[str, ContractInfo]:
        self._ensure_quote_connection()
        self._quote_events.commodity_chunks.clear()

        code = self._quote.SKQuoteLib_RequestStockList(_MARKET_FUTURES)
        if code != 0:
            raise ProductListUnavailable(f"查詢商品清單失敗，{self._message(code)}")

        # 清單分多次送達。用「解析得出來」當停止條件，而不是「字串裡看得到代碼」——
        # 後者在分塊邊界剛好切在某一列中間時會誤判成收齊，拿到截斷的資料。
        parsed: dict[str, ContractInfo] = {}

        def _parsed_ok() -> bool:
            nonlocal parsed
            try:
                parsed = parse_product_list("".join(self._quote_events.commodity_chunks))
                return True
            except ProductListUnavailable:
                return False

        if not self._pump_until(_parsed_ok, wait):
            raise ProductListUnavailable(f"{wait:.0f} 秒內未取得完整商品清單")
        return parsed

    # --- 行情 ---

    def minute_bars(self, day: date) -> list[MinuteBar]:
        """指定交易日的大台日盤 1 分 K（細節與出處見 notes/capital-api.md 第 3 節）。

        `SKQuoteLib_RequestKLineAMByDate(TX00, 0 分線, 1 新版格式, 1 AM 盤, 日期, 日期, 1 分)`，
        從 `OnNotifyKLineData` 一列一列收，收到結束通知才算齊。盤別由 `sTradeSession`
        選，所以代碼不帶 AM（帶 AM 的寫法只有逐筆與報價的文件提到）。

        ⚠️ 三件事文件沒寫、要實機核對（ticket 04 的 Comments）：新版格式的真實字串、
           K 棒時間標的是起點還是終點（`parse_minute_bars` 從資料本身分辨）、
           `TX00` 加 AM 盤是不是真的只給日盤。

        沒有資料（臨時休市）是空的 list；查不了、沒收齊、看不懂是 `QuoteUnavailable`——
        元件自己拋的例外也是，不讓它漏出去中止整個盤中程式。
        """
        if self._center is None:
            raise QuoteUnavailable("尚未登入，不能查 1 分 K")
        try:
            return self._query_minute_bars(day)
        except QuoteUnavailable:
            raise
        except ProductListUnavailable as exc:
            raise QuoteUnavailable(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise QuoteUnavailable(
                f"查 1 分 K 時元件發生錯誤（{type(exc).__name__}）：{exc}") from exc

    def _query_minute_bars(self, day: date) -> list[MinuteBar]:
        self._ensure_quote_connection()
        events = self._quote_events
        events.kline_rows = []
        events.kline_done = False
        ymd = f"{day:%Y%m%d}"
        code = self._quote.SKQuoteLib_RequestKLineAMByDate(
            _KLINE_CODE, _KLINE_MINUTES, _KLINE_NEW_FORMAT, _KLINE_AM_SESSION, ymd, ymd, 1)
        if code != 0:
            raise QuoteUnavailable(f"查 {day:%Y/%m/%d} 的大台 1 分 K 失敗，{self._message(code)}")
        if not self._pump_until(lambda: events.kline_done, self._connect_timeout):
            raise QuoteUnavailable(
                f"{self._connect_timeout:.0f} 秒內沒有收齊 {day:%Y/%m/%d} 的大台 1 分 K"
                f"（收到 {len(events.kline_rows)} 根，沒有結束通知）")
        bars = parse_minute_bars(events.kline_rows, day)
        if bars:
            # 原始的第一列與最後一列留在 log：實機時要看群益標的是起點還是終點
            logger.info("%s 的大台 1 分 K：%d 根（群益的第一列 %r，最後一列 %r）",
                        f"{day:%Y/%m/%d}", len(bars), events.kline_rows[0],
                        events.kline_rows[-1])
        return bars

    def events(self, product: str) -> Iterator[MarketEvent]:
        """訂閱大台與交易商品 `product` 的行情，依收到的順序產生行情事件。串流不會自己結束。

        訂閱三樣東西（細節與出處見 notes/capital-api.md 第 1、2、4 節）：

          * 大台的逐筆成交（`RequestTicks`）→ `TxTrade`。第一次訂閱會先回補當天的成交，
            休市時回補的是最近一個日盤。
          * 交易商品的逐筆成交 → `ProductTrade`。交易大台時同一筆成交兩種事件各一個。
          * 大台的累計成交筆數（`RequestFutureTradeInfo`）→ `TxTradeCounts`。

        另外每跨過本機時鐘的一個整秒產生一個 `Clock`：群益沒有每秒一次的事件
        （`OnTimer` 每分鐘一次），而 13:20 收工、報價中斷偵測在沒有成交時也要知道現在幾點。

        訂閱不起來是 `QuoteUnavailable`，在交出第一個事件之前。
        """
        if self._center is None:
            raise QuoteUnavailable("尚未登入")
        try:
            self._ensure_quote_connection()
        except ProductListUnavailable as exc:
            raise QuoteUnavailable(str(exc)) from exc
        self._quote_events.product = product
        for page, code in enumerate(dict.fromkeys((TX_CODE, product))):
            _, ret = self._quote.SKQuoteLib_RequestTicks(page, code)
            if ret != 0:
                raise QuoteUnavailable(f"訂閱 {code} 的逐筆成交失敗，{self._message(ret)}")
            stock, ret = self._quote.SKQuoteLib_GetStockByNoLONG(code, self._com.new_stock())
            if ret != 0:
                raise QuoteUnavailable(f"查不到 {code} 的商品資料，{self._message(ret)}")
            self._quote_events.tick_codes[stock.nStockIdx] = code
            logger.info("已訂閱 %s 的逐筆成交（page %d，索引 %s）", code, page, stock.nStockIdx)
        ret = self._quote.SKQuoteLib_RequestFutureTradeInfo(ctypes.c_short(0), TX_CODE)
        if ret != 0:
            raise QuoteUnavailable(f"訂閱 {TX_CODE} 的累計成交筆數失敗，{self._message(ret)}")
        last_second = None
        last_keepalive: datetime | None = None
        while True:
            now = self._now()
            if last_keepalive is None or now - last_keepalive >= _KEEPALIVE:
                last_keepalive = now
                self._quote.SKQuoteLib_RequestServerTime()
            self._quote_events.received_at = now
            self._com.pump()
            second = now.replace(microsecond=0)
            if second != last_second:
                last_second = second
                yield Clock(now)
            while self._quote_events.market:
                yield self._quote_events.market.popleft()
            time.sleep(self._pump_interval)

    # --- 下單 ---

    def _ensure_order_ready(self) -> None:
        """初始化下單元件、讀憑證、連上回報、查過帳號；之後每一筆都再確認回報連線還在。

        ⚠️ **刻意不在 `login()` 裡做。** 自動下單關閉時整條路徑都不該被走到
        （規格第 42 條：完全不呼叫下單 API）。
        """
        if not self._order_ready:
            self._prepare_order_components()
        else:
            # 盤中斷線的通知可能還排在訊息幫浦裡，先收進來再判斷
            self._com.pump()
            if self._reply_events.dropped:
                logger.warning("%s，送單前重新連線", self._reply_events.dropped)
                self._connect_reply()
        # ⚠️ **已經知道回報連線斷了就不送。** 送出去一定收不到成交回報，結局必然是
        #    FillUnknown：把「確定沒有部位」換成「可能有部位」。進場單沒有成交查詢可以補
        #    （規格第 50 條的兩段查詢只用在出場單），出場程式遇到不確定的部位也不自動平倉。
        #    不送則是 OrderFailed：確定沒有部位，原因講得出來；出場程式的重試
        #    （規格第 34 條）下一次會再走到上面的重新連線。
        if self._reply_events.dropped:
            raise OrderFailed(
                f"{self._reply_events.dropped}，連不回來。"
                "送出去會收不到成交回報，所以這筆委託沒有送出。")

    def _prepare_order_components(self) -> None:
        code = self._order.SKOrderLib_Initialize()
        if code != 0:
            raise OrderFailed(f"下單元件初始化失敗，{self._message(code)}")

        # ⚠️ 少了這一步，送出委託會被回 1038 SK_ERROR_CERT_NOT_VERIFIED（2026-08-21 實機）。
        #    看報價、查商品清單都不需要憑證，只有送單需要——所以這個洞在真的下單之前
        #    不可能浮出來。在這裡失敗，訊息可以直接講「憑證讀取失敗」。
        code = self._order.ReadCertByID(self._user_id)
        if code != 0:
            raise OrderFailed(f"憑證讀取失敗，{self._message(code)}")

        self._connect_reply()

        # 群益要求下單前先查過帳號，即使用的是設定檔指定的那一個
        code = self._order.GetUserAccount()
        if code != 0:
            raise OrderFailed(f"取得帳號失敗，{self._message(code)}")
        self._pump_for(self._settle_seconds)
        self._order_ready = True

    def _connect_reply(self) -> None:
        """連上回報主機並等它補送完畢。沒有它就收不到 OnNewData，也就不知道成交幾口。

        連線結果記在 `_reply_events`，由呼叫端判斷要不要送單。
        """
        events = self._reply_events
        events.dropped, events.complete = "", False
        code = self._reply.SKReplyLib_ConnectByID(self._user_id)
        if code != 0:
            events.dropped = f"回報連線失敗，{self._message(code)}"
            raise OrderFailed(events.dropped)
        # 回 0 只代表「請求已受理」。連線之後伺服器會先補送過去的回報（2026-11-07 實測：
        # 7 則，含 OS 的委託），補完才送 OnComplete。補送的那些靠委託序號過濾掉。
        #
        # 等不到 OnComplete 時**照樣送單**，和確定斷線（dropped）不同：它是弱訊號——
        # 沒有舊回報可補送時它還會不會來，實機還沒看過。在這個假設上擋單，猜錯就是
        # 每天都進不了場。但一定要記進 log：等不到成交回報的那天，這一行是唯一告訴人
        # 「該往哪裡查」的東西。
        self._pump_until(lambda: events.complete or bool(events.dropped), self._connect_timeout)
        if not events.complete and not events.dropped:
            logger.warning("%.0f 秒內沒收到回報回補完成通知（OnComplete），"
                           "回報連線可能異常，送出的委託可能收不到成交回報",
                           self._connect_timeout)

    def place_order(self, request: OrderRequest) -> OrderResult:
        """送出市價 IOC 委託，等到它確定結束，回傳**實際**成交的口數與均價。

        ⚠️ **失敗的分類以「單有沒有送出去」為界：**

            送出之前失敗 → OrderFailed   確定沒有部位
            送出之後失敗 → FillUnknown   不知道結局，可能已經成交

        後者包含逾時、元件壞掉、訊息幫浦拋例外——任何意外都算（規格第 50 條）。
        送單那一個呼叫**本身**拋例外時，單有沒有出去也不知道，同樣算 FillUnknown。
        """
        ensure_sendable(request)
        if self._center is None:
            raise OrderFailed("尚未登入")
        if not self._account:
            raise OrderFailed("未設定期貨帳號（.env 的 CAPITAL_FUTURES_ACCOUNT）")
        try:
            self._ensure_order_ready()
            order = self._com.new_future_order()
            for field, value in build_future_order_fields(request, self._account).items():
                setattr(order, field, value)
        except OrderFailed:
            raise
        except Exception as exc:  # noqa: BLE001
            # 還沒呼叫送單，任何意外都**確定**沒有送出去。
            raise OrderFailed(
                f"準備送單時元件發生錯誤（{type(exc).__name__}）：{exc}") from exc

        before = len(self._reply_events.rows)
        try:
            message, code = self._order.SendFutureOrderCLR(self._user_id, False, order)
        except Exception as exc:  # noqa: BLE001
            raise FillUnknown(
                f"送出委託時元件發生錯誤（{type(exc).__name__}）：{exc}。"
                "不確定委託有沒有送出去，請人工確認帳戶。",
            ) from exc
        if code != 0:
            # 送出這一步就被擋下 → 確定沒有部位產生。文件：此時 message 是失敗原因。
            raise OrderFailed(f"委託送出失敗，{self._message(code)}（{message}）")

        # ⚠️ **過了這一行，單就在市場上了。** 之後任何一種失敗都必須是 FillUnknown。
        # ⚠️ `str(None)` 是 "None"——一個看起來很正常的非空字串，所以先判斷 None。
        seq = "" if message is None else str(message).strip()
        if not seq:
            # 回報頻道是共用的，序號是認出自己那筆的唯一依據。沒有序號就只能承認不知道，
            # 不能等到逾時再說——那時的訊息會是「仍未結束」，聽起來像市場沒成交。
            raise FillUnknown(
                "委託已送出但沒有拿到委託序號，認不出它的成交回報。請人工確認帳戶。")
        logger.info("委託已送出，序號 %s", seq)
        try:
            return self._await_fill(seq=seq, since=before, requested_lots=request.lots)
        except (OrderFailed, FillUnknown):
            raise
        except Exception as exc:  # noqa: BLE001
            raise FillUnknown(
                f"等待委託 {seq} 的回報時發生錯誤（{type(exc).__name__}）：{exc}",
                order_seq=seq,
            ) from exc
        finally:
            # 不管成功、失敗、還是不確定，都把券商回的原始內容留下來。
            # **收不到回報那天最需要它**——那正是要回頭查的時候。
            self._save_raw_replies(seq, since=before)

    def _await_fill(self, seq: str, since: int, requested_lots: int) -> OrderResult:
        """等到這筆委託確定結束為止。等的是 `is_settled()`，不是「有沒有收到回報」。"""

        def _summary():
            return summarize_fills(self._reply_events.rows[since:], seq)

        def _done() -> bool:
            # 說不通的彙整不會因為多等而變對，等到逾時只是晚 30 秒告訴人。
            summary = _summary()
            return bool(summary.doubt(requested_lots)) or summary.is_settled(requested_lots)

        self._pump_until(_done, self._fill_timeout)
        summary = _summary()
        doubt = summary.doubt(requested_lots)
        if doubt:
            raise FillUnknown(
                f"委託 {seq} 的成交回報看不懂：{doubt}。請人工確認帳戶。",
                order_seq=seq, known_filled=summary.known_filled(requested_lots))
        if not summary.is_settled(requested_lots):
            # 不知道結局。**這不等於沒有成交**——已知成交的部分也一併講出來，
            # 使用者去對帳時知道至少要看到幾口。
            known = (f"（目前已知成交 {summary.filled_lots} 口，委託 {requested_lots} 口）"
                     if summary.filled_lots else "")
            raise FillUnknown(
                f"{self._fill_timeout:.0f} 秒內沒等到委託 {seq} 結束{known}",
                order_seq=seq, known_filled=summary.filled_lots)
        if summary.rejected:
            raise OrderFailed(f"委託被拒：{summary.reject_reason}")
        if summary.filled_lots == 0:
            logger.warning("委託 %s 沒有成交（市價 IOC 當下沒有對手價）", seq)
        return OrderResult(order_seq=seq, filled_lots=summary.filled_lots,
                           avg_price=summary.avg_price)

    def _save_raw_replies(self, seq: str, since: int) -> None:
        """把這次委託期間收到的回覆原封不動存進 `logs/`。存**整個視窗**，不只自己那筆。

        ⚠️ 跑在 `finally` 裡，所以**絕對不可以拋例外**：`finally` 拋出的東西會取代
        正在傳遞的那個，使用者收到的會是「寫檔失敗」而不是「收不到成交回報」。
        同理，它也不可以把一筆成功的下單變成失敗。

        ⚠️ 內容未遮罩（含期貨帳號）。`logs/` 不進版控，30 天後清除（ticket 10）。
        """
        try:
            rows = self._reply_events.rows[since:]
            if not rows:
                return
            directory = paths.logs_dir()
            os.makedirs(directory, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = os.path.join(directory, f"replies-{stamp}-{seq or 'noseq'}.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(rows) + "\n")
            logger.info("已存下 %d 列原始回覆：%s", len(rows), os.path.basename(path))
        except Exception as exc:  # noqa: BLE001
            logger.warning("原始回覆存檔失敗（%s）：%s", type(exc).__name__, exc)
