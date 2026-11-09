"""成交回報（`OnNewData`）的解析 —— 規格 Testing Decisions「broker 的線路格式」。

## 期望值的來源

`fixtures/onnewdata-*.txt` 是 invest-os 從券商收到的**真實字串**，已去識別化
（分公司代碼、帳號、帳號級識別碼都換成 0／F；委託序號、委託書號、成交價、時間保留，
因為跨格式的比對靠的就是它們）。期望值來自交易所，不是來自誰對文件的推導：

  * `onnewdata-real-2026-08-17.txt`           使用者在 APP 手動下的 1 口微台（委託＋成交）
  * `onnewdata-crossing-zero-2026-08-24.txt`  OS 自己送的兩筆市價 IOC（進場、出場各一對）
  * `onnewdata-spread-2026-08-19.txt`         使用者換倉的價差單——「別人的單」長什麼樣

欄位位置（2026-08-17 起實機驗證）：`[0]` 委託序號（成交列是空的）、`[1]` 市場、
`[2]` 類別、`[3]` 是否失敗、`[11]` 價格、`[20]` 口數、`[47]` 序號（每一列都有）。
`[44]` 失敗原因只來自官方文件（4-3-g），還沒有真實的被拒樣本。
"""

import os

import pytest

from broker.capital_wire import parse_reply_row, summarize_fills

_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _rows(name: str) -> list[str]:
    with open(os.path.join(_FIXTURES, name), encoding="utf-8") as fh:
        return [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]


def _real():
    return _rows("onnewdata-real-2026-08-17.txt")


def _ours():
    """OS 自己送的市價 IOC：[進場委託, 進場成交, 出場委託, 出場成交]。"""
    return _rows("onnewdata-crossing-zero-2026-08-24.txt")


def _spread():
    return _rows("onnewdata-spread-2026-08-19.txt")


# --- 對著真實資料：欄位位置 ---


def test_a_real_order_acknowledgement_is_read():
    ack = parse_reply_row(_real()[0])
    assert (ack.type, ack.failed, ack.qty) == ("N", False, 1)


def test_a_real_fill_is_read_with_its_quantity_and_price():
    """成交口數與成交價是**整個系統最貴的兩個欄位**：部位檔記錯，出場就平錯量，
    「部位不虧」也會用錯的均價判斷。期望值直接讀自交易所回來的那一列。"""
    fill = parse_reply_row(_ours()[1])
    assert (fill.type, fill.qty, fill.price) == ("D", 1, 45245.0)


def test_a_fill_row_has_no_order_key_but_is_still_counted_by_its_trailing_sequence():
    """官方文件：「國內期選：成交單無此欄，可使用新增的 SeqNo 比對」。真實資料證實了：
    成交列的 `[0]` 是空的，序號只在最後一欄。只看 `[0]` 的話，成交那一列配不上自己的委託，
    成交口數就少算了。"""
    rows = _real()
    assert rows[1].split(",")[0] == ""
    summary = summarize_fills(rows, "9999999999999")
    assert summary.filled_lots == parse_reply_row(rows[1]).qty == 1


# --- 形狀檢查：看不懂就回 None，絕不猜 ---


def _row(row_type, *, seq="SEQ0000000001", err="N", qty="2", price="0.0000", error=""):
    """組一列格式合法的 TF 回報。欄位位置比照真實資料（48 欄）。"""
    fields = [""] * 48
    fields[0] = seq if row_type != "D" else ""
    fields[1], fields[2], fields[3] = "TF", row_type, err
    fields[11], fields[20], fields[44], fields[47] = price, qty, error, seq
    return ",".join(fields)


def test_a_row_from_another_market_is_ignored():
    """證券（TS）與海期（OF）的回報也走同一條線，欄位定義卻不同。"""
    assert parse_reply_row(_row("N")) is not None              # 對照：TF 認得
    assert parse_reply_row(_row("N").replace(",TF,", ",TS,", 1)) is None


def test_a_truncated_row_is_ignored():
    assert parse_reply_row("SEQ1,TF,D,N") is None
    assert parse_reply_row("") is None


@pytest.mark.parametrize("qty", ["13:45:01", "", "TM2608", "-1"])
def test_a_quantity_that_is_not_a_number_is_ignored(qty):
    """**欄位位置推錯時最可能的徵兆**是撈到日期、代碼或空字串。"""
    assert parse_reply_row(_row("D", qty=qty, price="45000")) is None


@pytest.mark.parametrize("row_type", ["X", "20260817", ""])
def test_an_unknown_row_type_is_ignored(row_type):
    assert parse_reply_row(_row(row_type)) is None


@pytest.mark.parametrize("price", ["", "TM2609", "0.000000", "-1"])
def test_a_fill_without_a_usable_price_is_ignored(price):
    """成交列一定有成交價。讀不出來代表欄位對錯位了——口數也就不可信。"""
    assert parse_reply_row(_row("D", qty="1", price=price)) is None


# --- 彙整同一筆委託：只認自己的序號（規格第 29、56 條）---


def test_our_market_order_is_summarized_from_real_rows():
    """OS 2026-08-24 送出的市價 IOC：委託一列、成交一列，1 口 @ 45245。"""
    rows = _ours()
    summary = summarize_fills(rows, "2315610059573")
    assert (summary.filled_lots, summary.avg_price) == (1, 45245.0)
    assert summary.is_settled(requested_lots=1)


def test_someone_elses_orders_on_the_same_line_are_not_counted():
    """**回報頻道是共用的。** OS 的單、使用者手動的價差單、連線時補送的舊回報，
    都會出現在 PTS 的串流上（2026-11-07 實測：連上就補送 7 則過去的回報）。
    算進來的話，部位檔就記下一個 PTS 沒有的部位，出場時會平錯量。"""
    rows = _spread() + _real() + _ours()
    summary = summarize_fills(rows, "2315610127873")          # 出場那一筆
    assert (summary.filled_lots, summary.avg_price) == (1, 44680.0)
    assert summary.is_settled(requested_lots=1)


def test_without_a_sequence_number_nothing_is_counted():
    """空字串是任何字串的子字串。invest-os 原本寫 `if seq and seq not in row`，
    序號拿不到時整條過濾被跳過，視窗內**每一列**期貨回報都被算成自己的成交。"""
    summary = summarize_fills(_ours() + _real(), "")
    assert summary.filled_lots == 0 and not summary.rejected
    assert not summary.is_settled(requested_lots=1)


# --- 委託什麼時候算結束 ---
#
# IOC 的回報依序是：N 委託（交易所收下，單還在市場上）→ D 成交（可能好幾列）
# → C 剩餘量取消。只看「有沒有收到回報」的話，N 一到就會以為結束了。


def test_an_acknowledgement_alone_is_not_the_end():
    summary = summarize_fills([_row("N", qty="2")], "SEQ0000000001")
    assert not summary.is_settled(requested_lots=2)


def test_a_partial_fill_ends_when_the_rest_is_cancelled():
    """市價 IOC 的正常結局之一：成交一部分、剩下的取消。記的是**實際**成交的口數。"""
    rows = [_row("N", qty="3"), _row("D", qty="1", price="45000"), _row("C", qty="2")]
    summary = summarize_fills(rows, "SEQ0000000001")
    assert summary.is_settled(requested_lots=3)
    assert (summary.filled_lots, summary.avg_price) == (1, 45000.0)
    assert not summary.rejected


def test_a_full_fill_ends_without_waiting_for_a_cancel():
    """全部成交就沒有剩餘量可以取消，等 C 只會等到逾時。"""
    rows = [_row("N", qty="2"), _row("D", qty="2", price="45000")]
    assert summarize_fills(rows, "SEQ0000000001").is_settled(requested_lots=2)


def test_a_fill_row_we_cannot_read_keeps_the_order_open_even_after_the_cancel():
    """成交列帶著自己的序號，價格卻讀不出來：那一列的口數算不算數，我們不知道。

    把它當成沒看到的話，後面的取消列會讓這筆委託看起來「確定結束、0 口」——
    一個**有把握的錯誤答案**。部位檔少記一口，微台的出場程式就不會去平它，直接過夜。
    invest-os 不看成交列的價格，所以那邊沒有這個洞；PTS 多了價格檢查，就要一起
    承認看不懂。"""
    rows = [_row("N", qty="3"), _row("D", qty="1", price="0.000000"), _row("C", qty="2")]
    summary = summarize_fills(rows, "SEQ0000000001")
    assert not summary.is_settled(requested_lots=3)


def test_more_fills_than_were_ordered_is_never_a_settled_answer():
    """委託 1 口、成交加總 2 口——不可能是真的（多半是同一列成交回報被補送了兩次）。
    照 2 口寫進部位檔的話，出場就平 2 口，多的那口變成反向新倉。
    invest-os 的成交查詢遇到超過委託量會拒絕；推播這條路要有同一道防線。"""
    fill = _row("D", qty="1", price="45000")
    summary = summarize_fills([_row("N", qty="1"), fill, fill], "SEQ0000000001")
    assert not summary.is_settled(requested_lots=1)


def test_fills_at_different_prices_are_averaged_by_lots():
    """一筆市價單可能吃到好幾檔價位。部位檔記的價格是這筆委託的成交均價。"""
    rows = [_row("D", qty="1", price="45000"), _row("D", qty="3", price="45004")]
    summary = summarize_fills(rows, "SEQ0000000001")
    assert (summary.filled_lots, summary.avg_price) == (4, 45003.0)


# --- 被拒 ---


def test_a_rejection_with_no_fill_carries_the_brokers_reason():
    """規格第 51 條：被拒時發 Discord 附上原因（例如保證金不足）。"""
    rows = [_row("N", err="Y", qty="1", error="保證金不足")]
    summary = summarize_fills(rows, "SEQ0000000001")
    assert summary.rejected and summary.is_settled(requested_lots=1)
    assert summary.reject_reason == "保證金不足"


def test_the_reject_reason_never_carries_the_raw_row():
    """原因會被寫進 Discord。整列含未遮罩的帳號，invest-os 曾經把整列當原因送出去。"""
    row = _row("N", err="Y", qty="1", error="")
    fields = row.split(",")
    fields[4], fields[5] = "F999", "1234567"
    summary = summarize_fills([",".join(fields)], "SEQ0000000001")
    assert summary.rejected
    assert "1234567" not in summary.reject_reason


def test_a_fill_means_it_was_not_rejected():
    """只要有成交就不算失敗——把它當失敗的話，已成交的部位就沒有人知道。"""
    rows = [_row("D", qty="1", price="45000"), _row("N", err="Y", qty="1", error="x")]
    summary = summarize_fills(rows, "SEQ0000000001")
    assert not summary.rejected and summary.filled_lots == 1
