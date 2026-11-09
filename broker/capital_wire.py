"""群益的**線路格式** —— 從券商回來的字串長什麼樣、送過去的欄位怎麼填。

這裡沒有一行碰 COM。它是純資料處理：吃字串、吐結構。從 invest-os 的
`broker/capital_wire.py` 搬來，欄位位置都是那邊用真實字串驗證過的；
PTS 拿掉了 OS 專屬的部分（倉別 N／O 的對帳、三商品比價），加上成交價與失敗原因。

## 為什麼這裡處處在「拒絕回答」

回報頻道是**共用**的：OS 的單、使用者手動下的單、連線時補送的前一批回報，
全部走同一條線進來（invest-os 2026-08-19 換倉實測、2026-11-07 同時登入實測）。
認錯的後果是把別人的成交寫進 PTS 的部位檔 → 出場照那個口數平倉 →
多平的部分變成方向相反、沒人管的新倉。

而回不出答案的代價只是「不知道成交」——發個 Discord 要人看一眼帳戶。
兩者差了一個數量級，所以**只要有一絲不確定就承認看不懂**。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from broker import BUY, PRODUCT_CODES, ContractInfo, OrderRequest, ProductListUnavailable

# ── OnNewData 的欄位位置 ──────────────────────────────────────────────
#
# 依官方文件 4-3-g 的欄位順序，invest-os 2026-08-17 起用真實字串逐一驗證
# （tests/fixtures/onnewdata-*.txt）。
_KEY_NO = 0          # 原始 13 碼委託序號。**成交列是空的**
_MARKET = 1          # TF = 國內期貨
_TYPE = 2            # N 委託、C 取消、U 改量、P 改價、D 成交、B 改價改量、S 動態退單
_ORDER_ERR = 3       # Y 失敗、T 逾時、N 正常
_PRICE = 11          # N：委託價（市價單是 0）；D：成交價
_QTY = 20            # N：委託量；D：成交量；C：原委託剩量
_ERROR_MSG = 44      # 「當第四欄位 OrderErr 為 Y 時，委託單錯誤訊息」——⚠️ 只有文件，沒有真實樣本
_SEQ_NO = 47         # 13 碼序號，每一列都有（成交列只能靠它比對）

_FUTURES = "TF"
_TYPES = frozenset("NCUPDBS")
FILLED = "D"
CANCELLED = "C"      # IOC 的剩餘量會走這裡，收到它才代表委託真的結束


@dataclass(frozen=True)
class ReplyRow:
    """一列已經看懂的委託回報。"""

    seq: str
    type: str
    failed: bool
    qty: int
    # 只有成交列（D）才有意義；其他列是 None。
    price: float | None = None
    # 被拒時券商給的原因（`[44]`）。**刻意不拿整列當原因**：整列含未遮罩的帳號，
    # 而原因會被寫進 Discord（規格第 51 條）。
    error: str = ""


@dataclass(frozen=True)
class FillSummary:
    """一筆委託目前為止的回報彙整。

    ⚠️ **「收到回報」不等於「這筆委託結束了」。** IOC 的回報依序是：

        N 委託回報 → 交易所收下了，**單還在市場上**
        D 成交回報 → 成交一批（可能有好幾列、好幾個價位）
        C 取消回報 → 剩餘量取消，到這裡才真的結束

    要問的問題是 `is_settled()`，不是「有沒有收到回報」。
    """

    filled_lots: int
    # 這筆委託的成交均價（依口數加權）。沒有成交時是 None。
    avg_price: float | None
    rejected: bool
    matched_rows: int
    saw_cancel: bool = False
    reject_reason: str = ""
    # 帶著這筆委託序號、卻解析不了的列數。不是 0 就代表彙整出來的口數可能少算。
    unreadable_rows: int = 0

    def doubt(self, requested_lots: int) -> str:
        """這份彙整**說不通**的原因；說得通時是空字串。

        兩種情況，答案都不會因為多等一下而變對：

          * 有自己序號的列看不懂 → 那一列可能是成交，口數可能少算
          * 成交加總超過委託口數 → 不可能是真的（多半是同一列被補送了兩次）。
            invest-os 的成交查詢解析有同一道檢查；推播這條路原本沒有。

        說不通的時候，任何「確定」的答案都可能是錯的，只能承認不知道成交。
        """
        if self.unreadable_rows:
            return f"有 {self.unreadable_rows} 列帶著這筆委託序號的回報看不懂"
        if self.filled_lots > requested_lots:
            return f"回報加總成交 {self.filled_lots} 口，超過委託的 {requested_lots} 口"
        return ""

    def known_filled(self, requested_lots: int) -> int:
        """可以當成「**至少**成交了幾口」帶出去的數字。

        加總超過委託量時那個數字本身就是錯的，連下限都算不上，所以是 0；
        只是有列看不懂時，看得懂的那幾列仍然成立。
        """
        return 0 if self.filled_lots > requested_lots else self.filled_lots

    def is_settled(self, requested_lots: int) -> bool:
        """這筆委託確定不會再有成交了嗎？

        三種確定的結局：被拒（沒有部位）、剩餘量已取消（成交多少就是多少）、
        全部成交（沒有剩餘量可取消，等 C 只會等到逾時）。其他一律是「還沒結束」——
        包括彙整本身說不通的時候（`doubt()`）：那時被拒、取消、成交口數都不可信。
        """
        if self.doubt(requested_lots):
            return False
        return self.rejected or self.saw_cancel or self.filled_lots >= requested_lots


_NO_REASON = "券商沒有附上原因"


def summarize_fills(rows, seq: str) -> FillSummary:
    """彙整同一筆委託的回報，算出實際成交的口數與均價。

    四條規則，都是不對稱的——因為代價不對稱：

    1. **只要有成交就不算失敗。** 市價 IOC 的正常結局就是「成交一部分、剩下的取消」。
       把它當成失敗的話，已成交的部位就沒有人知道。
    2. **完全沒成交才談被拒。** 那時候確實沒有部位產生。
    3. **認不出是哪一筆就一列都不算。** 加總到別人的成交上，部位檔就記錯口數。
    4. **認得出是自己的、卻看不懂內容，就記下來（`unreadable_rows`）。** 跳過它的話，
       一列讀不出價格的成交加上後面的取消列，會彙整成「確定結束、0 口」。
       invest-os 不檢查成交價所以沒有這個洞；PTS 檢查了，就必須一起承認看不懂。

    比對序號用**整列子字串搜尋**而不是特定欄位：成交列的 `[0]` 是空的，序號只在最後一欄
    （2026-08-17 真實資料）。

    ⚠️ **`seq` 為空字串時一列都不算**——空字串是任何字串的子字串。回空的彙整而不是
       拋例外，讓上層的 `is_settled()` 判定為「還沒結束」，自然走到「不知道成交」：
       單確實送出去了，只是我們認不出回報，**絕不等於沒有部位**。
    """
    if not seq:
        return FillSummary(filled_lots=0, avg_price=None, rejected=False, matched_rows=0)

    filled = 0
    cost = 0.0
    matched = 0
    unreadable = 0
    reject_reason = ""
    saw_cancel = False
    for row in rows:
        if seq not in row:
            continue
        parsed = parse_reply_row(row)
        if parsed is None:
            unreadable += 1
            continue
        matched += 1
        if parsed.failed and not reject_reason:
            reject_reason = parsed.error or _NO_REASON
        if parsed.type == FILLED:
            filled += parsed.qty
            cost += parsed.qty * parsed.price
        elif parsed.type == CANCELLED:
            saw_cancel = True

    return FillSummary(
        filled_lots=filled,
        avg_price=cost / filled if filled else None,
        # 有成交就不是失敗——剩餘量被取消是 IOC 的常態，不是錯誤
        rejected=bool(reject_reason) and filled == 0,
        matched_rows=matched,
        saw_cancel=saw_cancel,
        reject_reason=reject_reason if filled == 0 else "",
        unreadable_rows=unreadable,
    )


def parse_reply_row(row: str) -> ReplyRow | None:
    """解析一列 `OnNewData`。看不懂就回 `None`。

    形狀檢查在有真實樣本之後仍然必要：證券（TS）、海期（OF）等格式完全不同的東西
    走同一條線進來，而樣本只涵蓋一種商品、幾種情境。遇到沒見過的格式時，
    唯一安全的行為是承認看不懂——上層會當成「沒收到回報」，最後歸為「不知道成交」。
    """
    fields = row.split(",")
    if len(fields) <= _QTY:
        return None
    if fields[_MARKET].strip() != _FUTURES:
        return None
    row_type = fields[_TYPE].strip()
    if row_type not in _TYPES:
        return None
    qty = fields[_QTY].strip()
    if not qty.isdigit():
        return None

    price = None
    if row_type == FILLED:
        # 成交列的口數與價格必須**同時**說得通。只驗口數的話，欄位整個位移時
        # 仍可能剛好撿到一個合法的小數字。
        try:
            price = float(fields[_PRICE].strip())
        except ValueError:
            return None
        if price <= 0:
            return None

    return ReplyRow(
        seq=fields[_KEY_NO].strip() or (
            fields[_SEQ_NO].strip() if len(fields) > _SEQ_NO else ""),
        type=row_type,
        failed=fields[_ORDER_ERR].strip() == "Y",
        qty=int(qty),
        price=price,
        error=fields[_ERROR_MSG].strip() if len(fields) > _ERROR_MSG else "",
    )


# ── 商品清單（OnNotifyCommodityListWithTypeNo）─────────────────────────


def parse_product_list(raw: str) -> dict[str, ContractInfo]:
    """解析群益商品清單，取出大台、小台、微台的近月合約。

    格式：以 `;` 分隔的 `商品代碼,名稱,最後交易日,交易所代碼`，整串還夾雜
    `%類別碼%類別名%` 的分類標頭——不剝掉的話第一筆代碼會變成 `%201%期指數%TX00AM`
    而永遠對不上。標頭可能出現在中段，不只開頭。

    近月連續代碼（`TX00AM`）在清單裡本身就帶最後交易日；結算日當天它仍指向
    當天到期的合約，那正是 PTS 要的（規格第 9 條）。

    殘缺的列直接跳過（清單裡混雜殘列很正常）；三個商品缺任一個就 raise。
    """
    entries: dict[str, date] = {}
    for entry in raw.replace("\n", ";").split(";"):
        if entry.startswith("%"):
            entry = entry.rsplit("%", 1)[-1]
        fields = entry.split(",")
        if len(fields) < 3:
            continue
        code, last_day = fields[0].strip(), fields[2].strip()
        try:
            entries[code] = datetime.strptime(last_day, "%Y%m%d").date()
        except ValueError:
            continue

    missing = [c for c in PRODUCT_CODES if c not in entries]
    if missing:
        raise ProductListUnavailable(f"商品清單缺少：{', '.join(missing)}")
    return {
        code: ContractInfo(
            code=code,
            last_trading_day=entries[code],
            order_code=_find_order_code(code, entries[code], entries),
        )
        for code in PRODUCT_CODES
    }


def _find_order_code(quote_code: str, last_day: date, entries: dict[str, date]) -> str:
    """從清單中找出對應的**下單代碼**（指名月份的那個）。

    配對條件是「同商品前綴 + 同最後交易日」：清單裡同時有 TX08（8/19）與
    TX09（9/16），用最後交易日才分得開。

    找不到就 raise。退而求其次用近月連續代碼是不行的：那個代碼能不能下單
    沒有文件保證，猜錯的後果是委託被拒、或下到完全不同的商品上。
    """
    prefix = quote_code[:-2].rstrip("0")      # TX00AM → TX00 → TX
    candidates = [
        code for code, day in entries.items()
        if day == last_day
        and code != quote_code
        and code.endswith("AM")
        and code.startswith(prefix)
        # 前綴之後必須全是數字，否則 TX 會撈到選擇權之類的其他商品
        and code[len(prefix):-2].isdigit()
    ]
    if not candidates:
        raise ProductListUnavailable(
            f"商品清單找不到 {quote_code}（最後交易日 {last_day:%Y%m%d}）對應的下單代碼"
        )
    # 同前綴同最後交易日理應只有一個；真有多個時取最短的，那是「商品＋月份」最基本的形式。
    return min(candidates, key=len)[:-2]       # 去掉 AM：下單不帶盤別代碼


# ── 送出的委託（FUTUREORDER，官方文件第 5 章）──────────────────────────

_BUY, _SELL = 0, 1          # sBuySell
_MARKET_PRICE = "M"         # bstrPrice：「M」市價、「P」範圍市價；限 IOC/FOK
_TRADE_TYPE_IOC = 1         # sTradeType：0 ROD、1 IOC、2 FOK
_SESSION_INTRADAY = 0       # sReserved：0 盤中（T盤及T+1盤）、1 T盤預約

# 倉別 sNewClose：0 新倉、1 平倉、2 自動。**進出場都送 2（自動）**（規格第 27 條）。
#
# invest-os 2026-08-24 實機：帳上有 1 口多單時送「賣 1 口、新倉」，被退單——
#     代碼 980：[980] 勾選新倉而有對應反向部位,退單!
# 同帳號同商品同月份採淨額計算，「新倉」跨不過零。PTS 和 OS（以及使用者自己的部位）
# 可能交易同一個商品，所以同樣的情境一定會遇到。「自動」在 2026-08-21／24
# 實機成交過進場與出場（含跨越零）。
_NEW_CLOSE_AUTO = 2


def build_future_order_fields(request: OrderRequest, account: str) -> dict:
    """把一筆 `OrderRequest` 轉成 `FUTUREORDER` 的欄位值。

    這些欄位寫錯的後果各不相同但都很貴：時效寫成 ROD 會掛單整天、買賣別寫反
    會開出完全相反的部位、微台標了當沖會被拒、大台小台沒標當沖保證金就放不下 20 口。

    下單代碼帶月份（`TX11`）而不是用 2.13.54 起的 `bstrCIDTandem`＋`bstrSettlementMonth`：
    前者是 invest-os 實機驗證過的路徑（ADR-0001 理由 3）；它「過期月份自動改送隔年」的
    陷阱由 `broker.ensure_sendable` 擋住。
    """
    return {
        "bstrFullAccount": account,
        "bstrStockNo": request.contract.order_code,
        "sBuySell": _BUY if request.side == BUY else _SELL,
        "bstrPrice": _MARKET_PRICE,
        "sTradeType": _TRADE_TYPE_IOC,
        "nQty": request.lots,
        "sDayTrade": 1 if request.day_trade else 0,
        "sNewClose": _NEW_CLOSE_AUTO,
        "sReserved": _SESSION_INTRADAY,
    }
