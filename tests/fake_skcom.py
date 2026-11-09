"""假的群益 SKCOM 元件 —— 換掉的是 `CapitalBroker` 底下的 COM，不是 `CapitalBroker` 本身。

為什麼要有它：`broker/capital.py` 是唯一會動到真錢的程式，而它只有在裝好群益元件與
憑證的 Windows 上才跑得到。invest-os 的做法是把 `CapitalBroker` 的私有屬性一個個換掉
（`broker._order = ...`），那綁死了內部結構。這裡改成在**系統邊界**換：
`CapitalBroker(com=FakeSkcom(...))`，測試只透過 `login()`／`get_contracts()`／
`place_order()` 打進去，斷言「送到群益的是什麼」與「broker 回了什麼」。

它只模擬測試用得到的那幾個函式與事件，行為照官方文件與 invest-os 的實測：

  * 每個函式回傳 0 代表成功；事件在 `pump()`（訊息幫浦）時才送達，和真的一樣是非同步的
  * `SKReplyLib_ConnectByID` 之後送 `OnSolaceReplyConnection`、補送舊回報、`OnComplete`
    ——**不送舊名 `OnConnect`**：這個版本已經移除它（文件 2.13.48）
  * `SendFutureOrderCLR` 成功時回 `(13 碼序號, 0)`，回報（`OnNewData`）之後才到
"""

from __future__ import annotations

from types import SimpleNamespace

STOCKS_READY = 3003


def reply_row(row_type: str, *, seq: str, qty: int, price: float = 0.0,
              err: str = "N", error: str = "") -> str:
    """組一列格式合法的 `OnNewData`（48 欄）。欄位位置比照真實資料：
    成交列（D）的 `[0]` 是空的，序號只在最後一欄 `[47]`。"""
    fields = [""] * 48
    fields[0] = "" if row_type == "D" else seq
    fields[1], fields[2], fields[3] = "TF", row_type, err
    fields[4], fields[5] = "F000000", "0000000"      # 帳號欄：假值
    fields[11] = f"{price:.6f}"
    fields[20] = str(qty)
    fields[44] = error
    fields[47] = seq
    return ",".join(fields)

PRODUCT_LIST = (
    "%201%期指數%"
    "TX00AM,台指近,20261118,TXFK600;"
    "TX11AM,台指11,20261118,TXFK6;"
    "TX12AM,台指12,20261216,TXFL6;"
    "MTX00AM,小台近,20261118,MXFK600;"
    "MTX11AM,小台11,20261118,MXFK6;"
    "TM0000AM,微台近,20261118,TMFK600;"
    "TM2611AM,微台2611,20261118,TMFK6;"
)


class FakeSkcom:
    """`CapitalBroker` 的 COM 執行環境替身。

    `on_send(order, seq) -> (message, code, rows)` 決定送單之後發生什麼：
    `message`／`code` 是 `SendFutureOrderCLR` 的回傳值，`rows` 是之後陸續送達的回報。
    預設：全部成交 @ 45000。
    """

    def __init__(self, *, login_code: int = 0, set_log_path_code: int = 0,
                 cert_code: int = 0, reply_connection_code: int = 0,
                 reply_completes: bool = True, reply_drop_code: int | None = None,
                 replayed_rows: tuple[str, ...] = (), on_send=None,
                 product_list: str = PRODUCT_LIST, pump_error: Exception | None = None):
        self.login_code = login_code
        self.set_log_path_code = set_log_path_code
        self.cert_code = cert_code
        self.reply_connection_code = reply_connection_code
        self.reply_completes = reply_completes
        self.reply_drop_code = reply_drop_code
        self.replayed_rows = replayed_rows
        self.on_send = on_send or full_fill(45000.0)
        self.product_list = product_list
        self.pump_error = pump_error
        # 所有函式呼叫，依序：(元件名, 函式名, 參數)
        self.calls: list[tuple] = []
        # 送到 SendFutureOrderCLR 的委託物件
        self.sent: list[SimpleNamespace] = []
        self._sinks: dict[str, list] = {}
        self._pending: list[tuple[str, str, tuple]] = []
        self._sent_any = False

    # --- CapitalBroker 用的執行環境介面 ---

    def load(self) -> None:
        self.calls.append(("comtypes", "GetModule", ()))

    def create(self, name: str):
        return _Lib(self, name)

    def advise(self, lib, sink):
        self._sinks.setdefault(lib.name, []).append(sink)
        return object()

    def pump(self) -> None:
        if self.pump_error is not None and self._sent_any:
            raise self.pump_error
        pending, self._pending = self._pending, []
        for lib_name, event, args in pending:
            for sink in self._sinks.get(lib_name, []):
                handler = getattr(sink, event, None)
                if handler is not None:
                    handler(*args)

    def new_future_order(self) -> SimpleNamespace:
        return SimpleNamespace()

    # --- 給元件替身用 ---

    def fire(self, lib_name: str, event: str, *args) -> None:
        self._pending.append((lib_name, event, args))

    def call(self, lib_name: str, method: str, args: tuple):
        self.calls.append((lib_name, method, args))
        handler = getattr(self, f"_{method}", None)
        return handler(*args) if handler else 0

    def first_call(self) -> tuple:
        return next(c for c in self.calls if c[0] != "comtypes")

    # --- 各函式的行為 ---

    def _SKCenterLib_SetLogPath(self, path):
        return self.set_log_path_code

    def _SKCenterLib_LoginSetQuote(self, user_id, password, quote):
        return self.login_code

    def _SKCenterLib_GetReturnCodeMessage(self, code):
        return f"群益訊息{code}"

    def _SKQuoteLib_EnterMonitorLONG(self):
        self.fire("SKQuoteLib", "OnConnection", 3001, 0)
        self.fire("SKQuoteLib", "OnConnection", STOCKS_READY, 0)
        return 0

    def _SKQuoteLib_RequestStockList(self, market):
        half = len(self.product_list) // 2         # 分兩塊送，和真的一樣
        self.fire("SKQuoteLib", "OnNotifyCommodityListWithTypeNo", market, self.product_list[:half])
        self.fire("SKQuoteLib", "OnNotifyCommodityListWithTypeNo", market, self.product_list[half:])
        return 0

    def _ReadCertByID(self, user_id):
        return self.cert_code

    def _SKReplyLib_ConnectByID(self, user_id):
        self.fire("SKReplyLib", "OnSolaceReplyConnection", user_id, self.reply_connection_code)
        for row in self.replayed_rows:
            self.fire("SKReplyLib", "OnNewData", user_id, row)
        if self.reply_drop_code is not None:
            self.fire("SKReplyLib", "OnSolaceReplyDisconnect", user_id, self.reply_drop_code)
        elif self.reply_completes:
            self.fire("SKReplyLib", "OnComplete", user_id)
        return 0

    def _SendFutureOrderCLR(self, user_id, is_async, order):
        self.sent.append(order)
        seq = f"{2315610000000 + len(self.sent):013d}"
        message, code, rows = self.on_send(order, seq)
        if code == 0:
            self._sent_any = True
            for row in rows:
                self.fire("SKReplyLib", "OnNewData", user_id, row)
        return message, code


class _Lib:
    """一個群益元件（SKCenterLib 等）。任何函式呼叫都轉給 FakeSkcom 記錄與處理。"""

    def __init__(self, skcom: FakeSkcom, name: str):
        self._skcom = skcom
        self.name = name

    def __getattr__(self, method: str):
        if method.startswith("_"):
            raise AttributeError(method)
        return lambda *args: self._skcom.call(self.name, method, args)


# --- 送單之後的劇本 ---


def full_fill(price: float):
    """委託、全部成交。"""
    def on_send(order, seq):
        return seq, 0, [reply_row("N", seq=seq, qty=order.nQty),
                        reply_row("D", seq=seq, qty=order.nQty, price=price)]
    return on_send


def fills(*parts: tuple[int, float], cancel: int = 0):
    """委託、分批成交（口數, 價格）、剩餘量取消。"""
    def on_send(order, seq):
        rows = [reply_row("N", seq=seq, qty=order.nQty)]
        rows += [reply_row("D", seq=seq, qty=q, price=p) for q, p in parts]
        if cancel:
            rows.append(reply_row("C", seq=seq, qty=cancel))
        return seq, 0, rows
    return on_send


def rejected(reason: str):
    """交易所收下之後才被拒（回報的 OrderErr = Y）。"""
    def on_send(order, seq):
        return seq, 0, [reply_row("N", seq=seq, qty=order.nQty, err="Y", error=reason)]
    return on_send


def refused_at_send(code: int, message: str):
    """送出這一步就被擋下（SendFutureOrderCLR 回非 0）。"""
    def on_send(order, seq):
        return message, code, []
    return on_send


def silence(message: str | None = None):
    """送出成功，之後什麼回報都沒有。`message` 是回傳的序號（None／空白＝沒拿到）。"""
    def on_send(order, seq):
        return (seq if message is None else message), 0, []
    return on_send


def no_sequence(message):
    """送出成功（code 0），卻沒有拿到委託序號。"""
    def on_send(order, seq):
        return message, 0, [reply_row("N", seq=seq, qty=order.nQty),
                            reply_row("D", seq=seq, qty=order.nQty, price=45000.0)]
    return on_send
