"""群益 broker —— 從公開介面打進去，底下的 COM 換成 `FakeSkcom`。

斷言兩種東西：**送到群益的是什麼**（函式呼叫與委託欄位），以及 **broker 回了什麼**
（結果或兩類失敗）。期望值來自規格、官方文件與 invest-os 的實機紀錄，不是從程式反推的。
"""

import logging
import os
from datetime import date, datetime

import pytest

import paths
from broker import (BUY, MTX_CODE, SELL, TMF_CODE, TX_CODE, FillUnknown,
                    LoginFailed, LoginUnavailable, OrderFailed, OrderRequest, PastDeadline,
                    ProductListUnavailable)
from broker.capital import CapitalBroker
from day_script import contracts, order_request
from fake_skcom import (FakeSkcom, capital_broker, fills, full_fill, no_sequence,
                        refused_at_send, rejected, reply_row, silence)


def _broker(skcom: FakeSkcom, **kwargs) -> CapitalBroker:
    kwargs.setdefault("fill_timeout", 0.3)
    return capital_broker(skcom, **kwargs)


# --- 群益元件自己的 log（規格第 60 條）---


def test_the_component_writes_its_logs_into_the_project_log_directory():
    """**元件的 log 含身分證字號。** 不設定的話它寫進「執行檔所在目錄」——用 venv 跑時
    那是 Python 的安裝目錄，invest-os 從 2026-08 起累積在那裡、從未被清過。

    官方文件 4-1-1：「如要變更 LOG 路徑，此函式需最先呼叫」。所以斷言的是
    **第一個**呼叫，不只是「有呼叫」。
    """
    skcom = FakeSkcom()
    _broker(skcom).login()

    lib, method, args = skcom.first_call()
    assert (lib, method) == ("SKCenterLib", "SKCenterLib_SetLogPath")
    log_dir = args[0]
    assert log_dir == paths.capital_log_dir()
    assert os.path.commonpath([log_dir, paths.logs_dir()]) == paths.logs_dir(), (
        "元件的 log 要放在專案的 logs/ 底下，才會和其他 log 一起被清除"
    )
    assert os.path.isdir(log_dir), "目錄要先建好，元件不保證會自己建"


def test_a_failed_login_is_a_login_failure_with_the_brokers_message():
    with pytest.raises(LoginFailed, match="群益訊息1001"):
        _broker(FakeSkcom(login_code=1001)).login()


@pytest.mark.parametrize("user_id, password, missing", [("", "pw", "CAPITAL_USER_ID"),
                                                        ("A100000000", "", "CAPITAL_PASSWORD")],
                         ids=["沒有帳號", "沒有密碼"])
def test_a_login_without_a_user_id_or_password_fails_and_says_which_is_missing(
        user_id, password, missing):
    """設定檔讀不懂時，出場程式照樣要登入平倉，帳密是從 `.env` 讀得到多少用多少（`.env` 也缺值時
    就是空的）。空的帳號、密碼送給群益只會換來一個看不懂的代碼：先擋下來，講明是哪一個沒填。"""
    with pytest.raises(LoginFailed, match=missing):
        CapitalBroker(user_id, password, "F0000000000000", com=FakeSkcom()).login()


# 群益文件裡講明是網路、主機或「稍後再試」的登入代碼（《策略王COM元件使用說明_V2.13.58》
# 錯誤代碼表與《V2.13.45以上登入代碼定義》）。這些沒有驗證到密碼，再試不會累積密碼錯誤次數。
_TRY_AGAIN_LATER = {
    101: "登入token異常／請重新登入",
    156: "SK_ERROR_LOGIN_NO_SERVER_LIST：未能取得主機清單",
    1041: "SK_ERROR_SERVER_NOT_CONNECTED：與主機尚未連線",
    1097: "SK_ERROR_TELNET_LOGINSERVER_FAIL：Telnet登入主機失敗",
    1108: "SK_ERROR_TELNET_SERVER_FAIL：因網路異常，無法連線",
    1109: "SK_ERROR_THERE_IS_NO_TOKEN：因登入未取得時效token，請重新登入",
    1129: "SK_ERROR_LOGIN_FAIL_IN_PROCESSING：登入失敗，請稍等五秒後再試",
    2020: "SK_WARNING_PRECHECK_RESULT_FAIL：取得行情主機或回報主機連線資訊失敗",
    2021: "SK_WARNING_PRECHECK_RESULT_EMPTY：取得行情主機或回報主機連線資訊結果為空值",
    2029: "SK_WARNING_SHOULD_CHECK_NETWORK",
}


@pytest.mark.parametrize("code", _TRY_AGAIN_LATER, ids=_TRY_AGAIN_LATER.values())
def test_a_login_that_failed_on_the_network_or_the_brokers_servers_is_worth_trying_again(code):
    """`LoginFailed` 的意思是「重試無用，要人處理」：盤中程式看到它，今天就不再登入（帳密錯了
    每分鐘再試會把帳號鎖住，群益 307）。開盤時碰上一次斷線、群益主機忙，卻不是這一類——
    當成這一類的話，一次網路異常就賠掉一整天，排程的每分鐘重啟也接不住。"""
    with pytest.raises(LoginUnavailable, match=f"群益訊息{code}"):
        _broker(FakeSkcom(login_code=code)).login()


@pytest.mark.parametrize("code", [300, 306, 307, 600, 604, 151, 1001, 9999],
                         ids=["密碼錯誤", "身分證字號錯誤", "密碼被鎖定", "憑證錯誤", "憑證過期",
                              "舊版密碼錯誤", "登入失敗請看LOG", "文件沒有的代碼"])
def test_any_other_login_failure_is_not_worth_trying_again(code):
    """帳密、憑證，以及文件沒講清楚的代碼，都當成「重試無用」：分不清的時候，賠掉一天的進場
    好過把帳號鎖住——鎖住的帳號連使用者自己的 APP、OS 都登不進去。"""
    with pytest.raises(LoginFailed):
        _broker(FakeSkcom(login_code=code)).login()


class _BrokenOnce(FakeSkcom):
    """第一次載入元件就炸（例如換版後產生的介面檔不對），第二次正常。"""

    def __init__(self):
        super().__init__()
        self.loads = 0

    def load(self):
        self.loads += 1
        if self.loads == 1:
            raise OSError("TYPE_E_CANTLOADLIBRARY")
        super().load()


def test_a_component_that_cannot_be_loaded_is_a_login_failure_and_can_be_retried():
    """上層只認得 LoginFailed（要人處理、發告警）。原始的 COM 例外漏出去的話，
    流程會以 traceback 收場而不是一則講得出原因的告警。"""
    skcom = _BrokenOnce()
    broker = _broker(skcom)
    with pytest.raises(LoginFailed, match="TYPE_E_CANTLOADLIBRARY"):
        broker.login()
    broker.login()                      # 半套的狀態不可以卡住重試
    assert ("SKCenterLib", "SKCenterLib_LoginSetQuote") in [c[:2] for c in skcom.calls]


# --- 商品清單 ---


def test_the_near_month_contracts_come_from_the_components_product_list():
    """近月的下單代碼與最後交易日都讀自群益的商品清單（規格第 9、24 條）。
    清單分兩塊送達，要收齊才算。"""
    broker = _broker(FakeSkcom())
    broker.login()
    contracts = broker.get_contracts()
    assert {code: c.order_code for code, c in contracts.items()} == {
        TX_CODE: "TX11", MTX_CODE: "MTX11", TMF_CODE: "TM2611"}
    assert contracts[TX_CODE].last_trading_day == date(2026, 11, 18)


class _ListExplodes(FakeSkcom):
    def _SKQuoteLib_RequestStockList(self, market):
        raise OSError("RPC 伺服器無法使用")


def test_an_unexpected_error_while_listing_products_is_product_list_unavailable():
    """商品清單查不到是「可以重試」的那一類，上層靠例外型別分辨要不要重試。"""
    broker = _broker(_ListExplodes())
    broker.login()
    with pytest.raises(ProductListUnavailable, match="RPC"):
        broker.get_contracts()


def test_logging_in_and_listing_products_never_touch_the_order_side():
    """**自動下單關閉時完全不呼叫下單 API**（規格第 42 條），唯讀檢查工具也靠這一點。
    下單元件初始化、讀憑證、連回報主機都只能等到第一次送單才做。流程測試用的是
    假 broker，看不到群益 broker 在登入時偷跑了這些——只有這裡抓得到。"""
    skcom = FakeSkcom()
    broker = _broker(skcom)
    broker.login()
    broker.get_contracts()
    touched = [(lib, method) for lib, method, _ in skcom.calls
               if lib in ("SKOrderLib", "SKReplyLib")]
    assert touched == []


def _order_side(skcom: FakeSkcom) -> list[str]:
    return [method for lib, method, _ in skcom.calls if lib in ("SKOrderLib", "SKReplyLib")]


def test_orders_can_be_prepared_before_the_first_order():
    """自動下單打開時，盤中程式在訂閱行情之前就做好下單準備（`prepare_orders`）：下單元件、
    憑證、回報連線、帳號。之後的第一筆委託直接送出，不再準備一次。"""
    skcom = FakeSkcom(on_send=full_fill(45000.0))
    broker = _broker(skcom)
    broker.login()
    broker.prepare_orders()
    prepared = _order_side(skcom)
    assert "SKOrderLib_Initialize" in prepared and "ReadCertByID" in prepared
    assert skcom.sent == []

    broker.place_order(order_request())
    assert _order_side(skcom).count("SKOrderLib_Initialize") == 1


def test_an_order_past_its_deadline_when_it_would_go_out_is_not_sent():
    """委託帶著期限（盤中程式是 13:20 進場截止）。準備下單元件、重連回報要時間：送出的那一刻
    用本機時鐘再比一次，過了就不送（`PastDeadline`，確定沒有部位）。"""
    skcom = FakeSkcom(on_send=full_fill(45000.0))
    request = OrderRequest(contract=contracts()[TMF_CODE], side=BUY, lots=1,
                           trading_day=date(2026, 11, 9), deadline=datetime(2026, 11, 9, 13, 20))
    with pytest.raises(PastDeadline):
        _place(skcom, request, now=lambda: datetime(2026, 11, 9, 13, 20, 1))
    assert skcom.sent == []


# --- 委託欄位（規格第 27、28 條）---

def _place(skcom: FakeSkcom, request: OrderRequest, **kwargs):
    broker = _broker(skcom, **kwargs)
    broker.login()
    return broker.place_order(request)


@pytest.mark.parametrize("code, order_code, day_trade", [
    (TX_CODE, "TX11", 1),
    (MTX_CODE, "MTX11", 1),
    (TMF_CODE, "TM2611", 0),
])
def test_every_field_of_the_order_sent_to_the_broker(code, order_code, day_trade):
    """送到群益的 FUTUREORDER，逐欄比對。期望值來自規格與官方文件第 5 章：

      bstrPrice  "M"  市價（文件：只能搭配 IOC 或 FOK）
      sTradeType  1   IOC——單子不會掛在委託簿上（0 ROD、2 FOK）
      sNewClose   2   倉別「自動」——帳上有反向部位時「新倉」會被退單（invest-os 2026-08-24）
      sDayTrade       大台、小台 1（保證金減半，13:30 強平）；微台 0（不能標當沖，ADR-0002）
      sReserved   0   盤中單，不是預約單
      bstrStockNo     下單代碼（TX11），不是報價代碼（TX00AM）
    """
    skcom = FakeSkcom()
    _place(skcom, order_request(code, side=SELL, lots=3))
    assert vars(skcom.sent[0]) == {
        "bstrFullAccount": "F0000000000000",
        "bstrStockNo": order_code,
        "sBuySell": 1,
        "bstrPrice": "M",
        "sTradeType": 1,
        "nQty": 3,
        "sDayTrade": day_trade,
        "sNewClose": 2,
        "sReserved": 0,
    }


def test_buy_is_zero_on_the_wire():
    """群益的 sBuySell：0 買進、1 賣出。寫反就是開出完全相反的部位。"""
    skcom = FakeSkcom()
    _place(skcom, order_request(side=BUY))
    assert skcom.sent[0].sBuySell == 0


# --- 成交：記下**實際**成交的口數與價格（規格第 29 條）---


def test_a_full_fill_reports_the_sequence_lots_and_price():
    result = _place(FakeSkcom(on_send=full_fill(45123.0)), order_request(lots=2))
    assert (result.filled_lots, result.avg_price) == (2, 45123.0)
    assert len(result.order_seq) == 13, "13 碼委託序號：出場查詢與人工對帳都靠它"


def test_a_partial_fill_reports_only_what_was_filled():
    """IOC 只成交一部分時，剩下的被取消。記委託口數的話，出場就平錯量。"""
    result = _place(FakeSkcom(on_send=fills((1, 45000.0), cancel=2)), order_request(lots=3))
    assert (result.filled_lots, result.avg_price) == (1, 45000.0)


def test_nothing_filled_is_a_result_not_a_failure():
    """市價 IOC 當下沒有對手價：委託結束、0 口。確定沒有部位，但也不是被拒。"""
    result = _place(FakeSkcom(on_send=fills(cancel=1)), order_request(lots=1))
    assert (result.filled_lots, result.avg_price) == (0, None)


def test_fills_at_several_prices_report_the_lot_weighted_average():
    result = _place(FakeSkcom(on_send=fills((1, 45000.0), (2, 45003.0))), order_request(lots=3))
    assert (result.filled_lots, result.avg_price) == (3, 45002.0)


def test_old_replies_replayed_on_connect_are_not_counted():
    """連上回報主機時，伺服器會補送過去的回報（2026-11-07 實測：7 則，含 OS 的委託）。
    算進來的話，PTS 會記下一個它沒有的部位。"""
    replayed = tuple(
        line.strip() for line in open(os.path.join(os.path.dirname(__file__), "fixtures",
                                                    "onnewdata-crossing-zero-2026-08-24.txt"),
                                       encoding="utf-8")
        if line.strip() and not line.startswith("#"))
    result = _place(FakeSkcom(replayed_rows=replayed, on_send=full_fill(45000.0)), order_request())
    assert result.filled_lots == 1


# --- 委託沒有送出去：確定沒有部位（規格第 51 條）---


def test_a_send_refused_by_the_broker_is_order_failed_with_the_reason():
    with pytest.raises(OrderFailed, match="保證金不足"):
        _place(FakeSkcom(on_send=refused_at_send(1037, "保證金不足")), order_request())


def test_a_rejection_reported_after_the_send_is_order_failed_with_the_reason():
    """交易所收下之後才被拒（回報 OrderErr = Y）、沒有任何成交——確定沒有部位。"""
    with pytest.raises(OrderFailed, match="價格超出漲跌停"):
        _place(FakeSkcom(on_send=rejected("價格超出漲跌停")), order_request())


def test_an_expired_contract_is_never_sent():
    """**過期合約絕不送單。** 群益會把過期月份自動改送隔年同月，而且不報錯。"""
    skcom = FakeSkcom()
    with pytest.raises(OrderFailed, match="最後交易日"):
        _place(skcom, order_request(day=date(2026, 11, 19)))
    assert skcom.sent == []


def test_the_settlement_day_itself_is_not_expired():
    """結算日當天交易當天到期的合約，13:30 才停止交易（規格第 9 條）。"""
    skcom = FakeSkcom()
    _place(skcom, order_request(day=date(2026, 11, 18)))
    assert len(skcom.sent) == 1


def test_a_certificate_that_cannot_be_read_stops_before_anyorder_request():
    """少了憑證，送出去只會被回 1038「Cert Not Verified」，要人自己猜是哪一步漏了。"""
    skcom = FakeSkcom(cert_code=1038)
    with pytest.raises(OrderFailed, match="憑證"):
        _place(skcom, order_request())
    assert skcom.sent == []


class _CertExplodes(FakeSkcom):
    def _ReadCertByID(self, user_id):
        raise OSError("COM 物件已失效")


def test_an_unexpected_error_before_the_send_is_order_failed():
    """送出之前發生的任何事，都**確定**沒有送出去。原始例外漏出去的話，上層分不出
    這是「沒送出去」還是「不知道」——而 broker 只該讓這兩類失敗出去。"""
    skcom = _CertExplodes()
    with pytest.raises(OrderFailed, match="COM 物件已失效"):
        _place(skcom, order_request())
    assert skcom.sent == []


def test_no_futures_account_stops_before_anyorder_request():
    skcom = FakeSkcom()
    with pytest.raises(OrderFailed, match="期貨帳號"):
        _place(skcom, order_request(), account="")
    assert skcom.sent == []


# --- 委託送出去了，但不知道成交：可能有部位（規格第 50 條）---


def test_no_reply_after_the_send_is_fill_unknown_with_the_sequence():
    with pytest.raises(FillUnknown) as exc:
        _place(FakeSkcom(on_send=silence()), order_request())
    assert len(exc.value.order_seq) == 13, "序號要帶出去：出場查詢與人工對帳都靠它"
    assert exc.value.known_filled == 0


def test_a_partial_fill_that_never_ends_carries_the_known_lots():
    """成交了一部分、剩下的沒有下文。**這不等於只成交這些**——但至少有這些。"""
    with pytest.raises(FillUnknown) as exc:
        _place(FakeSkcom(on_send=fills((1, 45000.0))), order_request(lots=3))
    assert exc.value.known_filled == 1


def test_a_fill_we_cannot_read_is_fill_unknown_not_a_confident_zero():
    """成交列是自己的（序號對得上），價格卻是 0。接著剩餘量取消——看起來像「沒成交」。
    說成 0 口的話，部位檔少記一口，微台出場不會平它。看不懂就要說看不懂。"""
    def on_send(order, seq):
        return seq, 0, [reply_row("N", seq=seq, qty=3),
                        reply_row("D", seq=seq, qty=1, price=0.0),
                        reply_row("C", seq=seq, qty=2)]
    with pytest.raises(FillUnknown, match="看不懂") as exc:
        _place(FakeSkcom(on_send=on_send), order_request(lots=3))
    assert exc.value.order_seq


def test_more_fills_than_were_ordered_is_fill_unknown():
    """同一列成交回報到了兩次：委託 1 口，加總 2 口。這個數字寫進部位檔，
    出場就照 2 口平倉。不可能的數字只能說不知道。"""
    fills_twice = fills((1, 45000.0), (1, 45000.0))
    with pytest.raises(FillUnknown, match="超過委託") as exc:
        _place(FakeSkcom(on_send=fills_twice), order_request(lots=1))
    assert exc.value.known_filled <= 1, "說不通的口數不可以當成「至少成交」帶出去"


@pytest.mark.parametrize("error", [
    OSError("COM 物件已失效"),
    RuntimeError("訊息幫浦異常"),
    ImportError("pythoncom 不見了"),
])
def test_any_failure_after_the_send_is_fill_unknown_never_order_failed(error):
    """**單已經在市場上了**，這些意外都不代表「沒有部位」。說錯的後果是不寫部位檔，
    出場程式以為今天沒進場，部位直接過夜。三種例外是為了抓「漏掉某一類」。"""
    with pytest.raises(FillUnknown) as exc:
        _place(FakeSkcom(on_send=silence(), pump_error=error), order_request())
    assert exc.value.order_seq
    assert exc.value.__cause__ is error, "原始原因要留著，否則無從排查"


def test_an_error_raised_by_the_send_call_itself_is_fill_unknown():
    """送單那一個呼叫本身炸掉：單有沒有出去，我們不知道。不知道就是 FillUnknown。"""
    def explode(order, seq):
        raise OSError("RPC 伺服器無法使用")
    with pytest.raises(FillUnknown):
        _place(FakeSkcom(on_send=explode), order_request())


@pytest.mark.parametrize("message", ["", "   ", None])
def test_a_send_without_a_sequence_number_is_fill_unknown_at_once(message):
    """送出成功（code 0）卻沒有序號：回報認不出來，等下去只會等到別人的回報。
    `None` 要特別小心——`str(None)` 是 "None"，看起來是個正常的序號。"""
    with pytest.raises(FillUnknown, match="人工確認"):
        _place(FakeSkcom(on_send=no_sequence(message)), order_request(), fill_timeout=30)


# --- 回報連線（invest-os 掛錯事件名稱的修正）---
#
# 2.13.48 起回報連線改走 Solace：`OnSolaceReplyConnection`／`OnSolaceReplyDisconnect`。
# 舊名 `OnConnect`／`OnDisconnect` 已移除、不會觸發——事件照方法名稱送達，名稱寫錯
# 不會報錯，只是永遠收不到。invest-os 因此每天印「回報連線狀態未知」，盤中斷線也偵測不到。
#
# 成功連線時 `OnSolaceReplyConnection` 帶什麼代碼，文件沒寫（0？3001 連線？）。
# 所以「連上而且補送完畢」看的是 `OnComplete`（文件 4-3-c：「若未收到此通知，代表
# 新建立的回報連線及回傳回報資料異常」）。


def _reply_problems(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING and "回報" in r.getMessage()]


@pytest.mark.parametrize("code", [0, 3001])
def test_a_reply_link_that_finishes_its_replay_is_trusted_whatever_code_it_connected_with(
        code, caplog):
    """不可以把某個代碼寫死成「成功」：猜錯的話，每天都會報一個假的連線失敗。"""
    _place(FakeSkcom(reply_connection_code=code), order_request())
    assert _reply_problems(caplog) == []


def test_the_code_the_reply_link_connects_with_is_logged(caplog):
    """成功連線時的代碼文件沒寫，第一次實機就是靠這一行看出它是多少（ticket 01 Comments）。
    也守著事件名稱：掛回 invest-os 的舊名 `OnConnect` 的話，這一行永遠不會出現。"""
    caplog.set_level(logging.INFO, logger="broker.capital")
    _place(FakeSkcom(reply_connection_code=3001), order_request())
    assert any("回報連線通知" in m and "3001" in m for m in caplog.messages)


def test_a_reply_link_that_never_finishes_its_replay_is_reported(caplog):
    """收不到成交回報時，這一行是唯一告訴人「該往哪裡查」的東西。

    這種情況**照樣送單**，和下面「確定斷線」不同：沒等到 `OnComplete` 是弱訊號——
    沒有舊回報可補送時它還會不會來，實機還沒看過（2026-11-07 那次補送了 7 則）。
    在這個假設上擋單，猜錯的話是每天都進不了場。"""
    skcom = FakeSkcom(reply_completes=False)
    _place(skcom, order_request(), connect_timeout=0.2)
    assert any("OnComplete" in m for m in _reply_problems(caplog))
    assert len(skcom.sent) == 1


def test_a_reply_link_known_to_be_down_stops_the_order_before_it_is_sent(caplog):
    """**已經知道回報連線斷了，就不送。** 送出去一定收不到成交回報，結局必然是
    「不知道成交」：本來確定沒有部位，換成可能有部位。進場單沒有成交查詢可以補
    （規格第 50 條的兩段查詢只用在出場單），出場程式遇到不確定的部位不會自動平倉——
    微台那一口要靠人手動處理才不會過夜。不送則是「委託沒有送出去」：確定沒有部位。"""
    skcom = FakeSkcom(reply_drop_code=3002, on_send=silence())
    with pytest.raises(OrderFailed, match="3002"):
        _place(skcom, order_request(), connect_timeout=0.2)
    assert skcom.sent == []
    assert any("中斷" in m and "3002" in m for m in _reply_problems(caplog))


def test_a_reply_link_that_drops_during_the_session_is_checked_before_everyorder_request():
    """盤中才斷線的話，第一筆送單前那次檢查早就過了。每一筆都要再看一次，
    先試著重新連線，連不回來就不送。"""
    skcom = FakeSkcom()
    broker = _broker(skcom, connect_timeout=0.2)
    broker.login()
    broker.place_order(order_request())
    skcom.drop_reply_link(3002, reconnects=False)
    with pytest.raises(OrderFailed, match="回報連線"):
        broker.place_order(order_request())
    assert len(skcom.sent) == 1


def test_a_reply_link_that_reconnects_lets_the_next_order_through():
    """斷線不是永久的判決。出場程式對「沒送出去」的單每分鐘重試（規格第 34 條），
    重新連上之後就該送得出去，否則重試永遠白做。"""
    skcom = FakeSkcom()
    broker = _broker(skcom, connect_timeout=0.2)
    broker.login()
    broker.place_order(order_request())
    skcom.drop_reply_link(3002, reconnects=True)
    assert broker.place_order(order_request()).filled_lots == 1
    assert len(skcom.sent) == 2


# --- 券商回覆的原始存檔 ---
#
# 程式從回報裡挑出「成交幾口、什麼價」就把整段丟掉的話，出事時沒有原始資料可以回頭看。
# 存的是**送單之後整個視窗**收到的全部內容，不只自己那筆：invest-os 2026-08-19 就是靠
# 「別人的單也在同一條線上」這個證據，才發現序號比對的漏洞。


def _saved_replies() -> str:
    names = [n for n in os.listdir(paths.logs_dir()) if n.startswith("replies-")]
    return "".join(open(os.path.join(paths.logs_dir(), n), encoding="utf-8").read()
                   for n in names)


def test_the_replies_received_for_an_order_are_saved_under_logs():
    _place(FakeSkcom(on_send=full_fill(45000.0)), order_request())
    assert "45000.000000" in _saved_replies()


def test_replies_are_saved_even_when_the_fill_is_unknown():
    """**收不到回報那天最需要原始資料**——那正是要回頭查的時候。"""
    with pytest.raises(FillUnknown):
        _place(FakeSkcom(on_send=fills((1, 45001.0))), order_request(lots=2))
    assert "45001.000000" in _saved_replies()


def test_replies_replayed_before_the_order_are_not_saved_with_it():
    """連線時補送的舊回報與這筆委託無關，存進來只會讓排查的人以為它們有關聯。"""
    old = "9999999999999,TF,N,N,F000000,0000000," + ",".join([""] * 40) + ",OLDREPLY"
    _place(FakeSkcom(replayed_rows=(old,)), order_request())
    assert "OLDREPLY" not in _saved_replies()


def test_a_failed_save_never_masks_what_happened_to_theorder_request(monkeypatch, tmp_path):
    """存檔跑在 `finally` 裡，而 `finally` 拋出的例外會**取代**正在傳遞的那個——
    使用者會收到「寫檔失敗」而不是「不知道成交」。成交了的也不可以變成失敗。"""
    blocked = tmp_path / "blocked"
    blocked.write_text("我是檔案不是目錄", encoding="utf-8")
    monkeypatch.setattr(paths, "logs_dir", lambda: str(blocked / "logs"))

    assert _place(FakeSkcom(), order_request()).filled_lots == 1
    with pytest.raises(FillUnknown):
        _place(FakeSkcom(on_send=silence()), order_request())
