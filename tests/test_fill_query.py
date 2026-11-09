"""出場單不確定時的成交查詢（規格第 50 條後半）——兩段查詢的線路格式與群益 broker。

## 為什麼要兩段

委託序號只存在於其中一種查詢回傳裡（invest-os 2026-08-19 實測）：

    OnNewData          [0] 與 [47]   ✅
    GetOrderReport     [8] 與 [9]    ✅
    GetFulfillReport   （沒有）       ❌  [8] 是成交編號

所以先用序號在委託查詢裡找到自己那列、讀出**委託書號**，再拿它去成交查詢裡加總。

## 為什麼處處在「拒絕回答」

查詢回傳是整個帳戶的：使用者手動下的單、OS 的單、前幾天還沒平的部位都在裡面。認錯的後果
是把別人的成交當成 PTS 的平倉 → 以為平完了 → 微台過夜。回不出答案的代價只是告警請人看帳戶。

欄位位置是 invest-os 用真實回傳驗證過的（`fixtures/reports-*.txt`，帳號已置換）。
⚠️ 樣本全部是微台、不標當沖。大台、小台與當沖列還沒有樣本（規格 Further Notes 第 5 條，ticket 11）。
"""

import os
from datetime import date

import pytest

from broker import FillQuery, FillUnknown, OrderRequest, TMF_CODE, ContractInfo, SELL
from broker.capital import CapitalBroker
from broker.capital_wire import ReportUnreadable, parse_fills, parse_order_book_no
from fake_skcom import FakeSkcom, silence

CRLF = "\r\n"            # 群益查詢回傳的換行
FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _reports(name: str) -> tuple[str, str]:
    """真實回傳的（委託查詢, 成交查詢）。委託查詢一列 77 欄、成交查詢 52 欄，用長度分得開，
    不依賴標題文字。"""
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        rows = [line.strip() for line in fh if line.startswith("TF,")]
    order = [r for r in rows if len(r.split(",")) > 55]
    fulfill = [r for r in rows if len(r.split(",")) <= 55]
    return CRLF.join(order), CRLF.join(fulfill)


# 2026-08-21：OS 自己的微台單腳進出場各 1 口（出場在前，時間倒序）
SL_DAY = date(2026, 8, 21)
SL_ENTRY_SEQ, SL_ENTRY_BOOK = "2315609807637", "g0106"     # 09:00 買 1 口 @ 44838
SL_EXIT_SEQ, SL_EXIT_BOOK = "2315609905580", "s0838"       # 13:40 賣 1 口 @ 45135

# 2026-08-24：跨越零那天，含一筆退單（書號 00000）與使用者 08-21 的小台（e0905）
CZ_DAY = date(2026, 8, 24)
CZ_REJECTED_SEQ = "2315610040954"

# 2026-08-19：使用者換倉的價差單
SPREAD_DAY = date(2026, 8, 19)
SPREAD_SEQ, SPREAD_BOOK = "2315609394137", "x0582"


def _order_row(*, seq: str = SL_EXIT_SEQ, book: str = SL_EXIT_BOOK,
               day: str = "20260821") -> str:
    """一列格式合法的委託查詢回傳。內容不代表真實資料，只用來測比對規則。"""
    f = [""] * 77
    f[0], f[1] = "TF", "FUT"
    f[7], f[8], f[9] = book, seq, seq
    f[11], f[12] = day, "134008"
    return ",".join(f)


def _fill_row(*, book: str = SL_EXIT_BOOK, day: str = "20260821", qty: str = "1",
              price: str = "45135.0000") -> str:
    """一列格式合法的成交查詢回傳（單腳）。"""
    f = [""] * 52
    f[0], f[1] = "TF", "FUT"
    f[7], f[8] = book, "0000006609"
    f[9], f[10] = day, "134008"
    f[12], f[21] = "TMFI6", "S"
    f[25], f[26] = price, qty
    return ",".join(f)


# ─────────────────────────────────────────────────────────
# 第一段：用委託序號換出委託書號
# ─────────────────────────────────────────────────────────


def test_the_real_exit_order_yields_its_book_number():
    """對著真實回傳斷言：兩列委託（進場、出場）裡找到出場那一列。"""
    order, _ = _reports("reports-singleleg-2026-08-21.txt")
    assert parse_order_book_no(order, SL_EXIT_SEQ, SL_DAY) == SL_EXIT_BOOK
    assert parse_order_book_no(order, SL_ENTRY_SEQ, SL_DAY) == SL_ENTRY_BOOK


def test_someone_elses_order_is_not_matched():
    order, _ = _reports("reports-singleleg-2026-08-21.txt")
    assert parse_order_book_no(order, "2315600000001", SL_DAY) is None


def test_an_order_from_another_day_is_not_matched():
    """委託書號、委託序號的唯一性沒有驗證過（每天重來？），日期是擋住舊紀錄的唯一一道。"""
    order, _ = _reports("reports-singleleg-2026-08-21.txt")
    assert parse_order_book_no(order, SL_EXIT_SEQ, date(2026, 8, 22)) is None


@pytest.mark.parametrize("text", ["M003", "", "   ", CRLF])
def test_no_data_is_no_answer(text):
    """`M003` 是查無資料，空的回傳也一樣：查不到，不是「沒有成交」。"""
    assert parse_order_book_no(text, SL_EXIT_SEQ, SL_DAY) is None


def test_a_query_error_is_a_failure_not_no_data():
    """`M999` 是查詢本身壞了，和「查不到」的意思相反：告警要講得分開，否則查詢元件壞掉
    看起來就和單還沒成交一模一樣。"""
    with pytest.raises(ReportUnreadable, match="M999"):
        parse_order_book_no("M999 查詢錯誤", SL_EXIT_SEQ, SL_DAY)


def test_an_error_marker_with_many_commas_is_still_an_error():
    with pytest.raises(ReportUnreadable):
        parse_order_book_no("M999," + ",".join(["x"] * 80), SL_EXIT_SEQ, SL_DAY)


def test_an_empty_sequence_matches_nothing():
    """空字串是任何字串的子字串。"""
    assert parse_order_book_no(_order_row(), "", SL_DAY) is None


def test_the_sequence_is_matched_exactly_not_as_a_substring():
    """多一位數、少一位數的別人的序號都不能比中——比中就是拿別人的書號去加總別人的成交。"""
    text = CRLF.join([_order_row(seq=SL_EXIT_SEQ + "0", book="z9999"),
                      _order_row(seq=SL_EXIT_SEQ[:-1], book="z8888")])
    assert parse_order_book_no(text, SL_EXIT_SEQ, SL_DAY) is None
    assert parse_order_book_no(CRLF.join([text, _order_row()]), SL_EXIT_SEQ,
                               SL_DAY) == SL_EXIT_BOOK


def test_a_row_without_a_book_number_or_from_another_market_or_too_short_is_skipped():
    rows = [_order_row(book=""), _order_row().replace("TF,FUT", "TS,STK", 1), "TF,FUT,TAIFEX"]
    assert parse_order_book_no(CRLF.join(rows), SL_EXIT_SEQ, SL_DAY) is None


# ─────────────────────────────────────────────────────────
# 第二段：用委託書號加總成交
# ─────────────────────────────────────────────────────────


def test_the_real_single_leg_fills_are_counted_with_their_price():
    """**這是整個查詢最重要的斷言。** 1 口的委託、1 口的成交，價格在 [25]。兩列（進場、出場）
    只算自己那一列——書號比對失效的話會得到 2 口。"""
    _, fulfill = _reports("reports-singleleg-2026-08-21.txt")
    assert parse_fills(fulfill, SL_EXIT_BOOK, SL_DAY, requested_lots=1) == (1, 45135.0)
    assert parse_fills(fulfill, SL_ENTRY_BOOK, SL_DAY, requested_lots=1) == (1, 44838.0)


def test_several_fills_of_one_order_are_added_up_with_the_lot_weighted_price():
    """市價 IOC 可能分好幾個價位成交：(45130 × 1 + 45136 × 2) ÷ 3 = 45134。"""
    text = CRLF.join([_fill_row(qty="1", price="45130.0000"),
                      _fill_row(qty="2", price="45136.0000"),
                      _fill_row(book="x9999", qty="5")])
    assert parse_fills(text, SL_EXIT_BOOK, SL_DAY, requested_lots=3) == (3, 45134.0)


def test_a_rejected_order_never_reaches_the_fulfill_report():
    """真實的退單（2026-08-24 08:51）：委託查詢裡有它（書號 00000、成交 0），成交查詢裡沒有。
    所以一筆被拒的平倉單查到的是「查不到」，不會被算成成交。"""
    order, fulfill = _reports("reports-crossing-zero-2026-08-24.txt")
    book = parse_order_book_no(order, CZ_REJECTED_SEQ, CZ_DAY)
    assert book == "00000"
    assert parse_fills(fulfill, book, CZ_DAY, requested_lots=1) is None


def test_fills_of_another_day_in_the_same_report_are_not_counted():
    """成交查詢不只有當天的成交：08-24 的回傳裡有使用者 08-21 買進、還沒平的小台（e0905）。
    口數刻意在委託量之內，擋下它的是日期，不是「超過委託量」。"""
    _, fulfill = _reports("reports-crossing-zero-2026-08-24.txt")
    assert parse_fills(fulfill, "e0905", CZ_DAY, requested_lots=1) is None
    assert parse_fills(_fill_row(day="20260820"), SL_EXIT_BOOK, SL_DAY, requested_lots=1) is None


@pytest.mark.parametrize("text", ["M003", "", "   "])
def test_no_fill_row_is_no_answer_not_zero(text):
    """**「查不到成交」不等於「確定沒成交」。** 紀錄可能只是還沒反映。"""
    assert parse_fills(text, SL_EXIT_BOOK, SL_DAY, requested_lots=1) is None
    assert parse_fills(_fill_row(book="x9999"), SL_EXIT_BOOK, SL_DAY, requested_lots=1) is None


def test_an_empty_or_partial_book_number_matches_nothing():
    assert parse_fills(_fill_row(), "", SL_DAY, requested_lots=1) is None
    assert parse_fills(_fill_row(book=SL_EXIT_BOOK + "0"), SL_EXIT_BOOK, SL_DAY,
                       requested_lots=1) is None


def test_a_fill_row_of_another_market_is_not_counted():
    text = CRLF.join([_fill_row().replace("TF,FUT", "TS,STK", 1), _fill_row()])
    assert parse_fills(text, SL_EXIT_BOOK, SL_DAY, requested_lots=1) == (1, 45135.0)


# 下面這些是「答案本身說不通」：欄位位置錯了最可能的症狀就是撿到一個不相干的數字。
# 說不通不是「查不到」，是查詢的結果不能信——拋 ReportUnreadable，上層當成查詢失敗。


def test_more_lots_than_ordered_is_refused():
    """委託 1 口卻加總出 5 口：不可能是真的。當成「全部成交」的話，帳上可能還有沒平的口數。"""
    with pytest.raises(ReportUnreadable, match="超過"):
        parse_fills(_fill_row(qty="5"), SL_EXIT_BOOK, SL_DAY, requested_lots=1)


def test_the_real_spread_fills_are_refused():
    """真實的價差成交：書號、日期都對得上，只有「這是價差」擋得住它（一張委託兩列，加總會翻倍）。
    委託口數刻意給 2，讓「超過委託量」那道不會先擋下來。真實資料的斜線只在最後一欄。"""
    _, fulfill = _reports("reports-2026-08-19.txt")
    with pytest.raises(ReportUnreadable, match="價差"):
        parse_fills(fulfill, SPREAD_BOOK, SPREAD_DAY, requested_lots=2)


@pytest.mark.parametrize("qty, price", [("一", "45135.0000"), ("0", "45135.0000"),
                                        ("1", "不是價格"), ("1", "0.0000")],
                         ids=["口數不是數字", "0 口", "價格不是數字", "價格是 0"])
def test_a_fill_row_that_makes_no_sense_is_refused(qty, price):
    with pytest.raises(ReportUnreadable):
        parse_fills(_fill_row(qty=qty, price=price), SL_EXIT_BOOK, SL_DAY, requested_lots=1)


def test_a_query_error_in_the_fulfill_report_is_a_failure():
    with pytest.raises(ReportUnreadable, match="M999"):
        parse_fills("M999 查詢錯誤", SL_EXIT_BOOK, SL_DAY, requested_lots=1)


# ─────────────────────────────────────────────────────────
# 群益 broker：從公開介面打進去，底下的 COM 換成 FakeSkcom
# ─────────────────────────────────────────────────────────


def _broker(skcom: FakeSkcom, **kwargs) -> CapitalBroker:
    """`sleep` 記進 skcom.calls，才看得出它夾在兩段查詢的哪裡。"""
    kwargs.setdefault("account", "F0000000000000")
    broker = CapitalBroker("A100000000", "pw", com=skcom, settle_seconds=0, fill_timeout=0.2,
                           sleep=lambda s: skcom.calls.append(("time", "sleep", (s,))),
                           **kwargs)
    broker.login()
    return broker


def _queries(skcom: FakeSkcom) -> list[tuple]:
    return [c for c in skcom.calls
            if c[1] in ("GetOrderReport", "GetFulfillReport", "sleep")]


def test_a_lookup_asks_for_the_book_number_waits_five_seconds_then_adds_up_the_fills():
    """官方文件：「限制每次查詢間需間隔五秒」。兩段查詢之間一定要等，用期貨帳號查全部委託
    （格式 1）、全部成交（格式 1）。"""
    order, fulfill = _reports("reports-singleleg-2026-08-21.txt")
    skcom = FakeSkcom(order_report=order, fulfill_report=fulfill)
    found = _broker(skcom).query_fills(SL_EXIT_SEQ, SL_DAY, 1)

    assert found == FillQuery(lots=1, avg_price=45135.0)
    calls = _queries(skcom)
    assert [c[1] for c in calls] == ["GetOrderReport", "sleep", "GetFulfillReport"]
    assert calls[0][2] == ("A100000000", "F0000000000000", 1)
    assert calls[1][2][0] >= 5
    assert calls[2][2] == ("A100000000", "F0000000000000", 1)


def test_an_order_not_in_the_order_report_is_not_found_without_a_second_query():
    """第一段就查不到：沒有書號可比，不必再等五秒查第二段。"""
    order, fulfill = _reports("reports-singleleg-2026-08-21.txt")
    skcom = FakeSkcom(order_report=order, fulfill_report=fulfill)
    assert _broker(skcom).query_fills("2315600000001", SL_DAY, 1) == FillQuery(lots=None)
    assert [c[1] for c in _queries(skcom)] == ["GetOrderReport"]


def test_a_query_error_is_reported_as_a_failed_lookup():
    skcom = FakeSkcom(order_report="M999 查詢錯誤")
    found = _broker(skcom).query_fills(SL_EXIT_SEQ, SL_DAY, 1)
    assert found.lots is None
    assert "M999" in found.failure


def test_a_fill_report_that_makes_no_sense_is_a_failed_lookup():
    skcom = FakeSkcom(order_report=_order_row(), fulfill_report=_fill_row(qty="5"))
    found = _broker(skcom).query_fills(SL_EXIT_SEQ, SL_DAY, 1)
    assert found.lots is None
    assert "超過" in found.failure


class _ReportExplodes(FakeSkcom):
    def _GetOrderReport(self, user_id, account, fmt):
        raise OSError("RPC 伺服器無法使用")


def test_a_component_error_during_the_lookup_is_a_failed_lookup_not_an_exception():
    """查詢是失敗路徑上的補救。自己再拋例外的話，出場程式就在告警之前當掉了。"""
    found = _broker(_ReportExplodes()).query_fills(SL_EXIT_SEQ, SL_DAY, 1)
    assert found.lots is None
    assert "RPC" in found.failure


def test_without_a_futures_account_the_lookup_fails_without_asking():
    skcom = FakeSkcom(order_report=_order_row(), fulfill_report=_fill_row())
    found = _broker(skcom, account="").query_fills(SL_EXIT_SEQ, SL_DAY, 1)
    assert found.lots is None
    assert found.failure
    assert _queries(skcom) == []


def test_a_dropped_reply_link_does_not_stop_the_lookup():
    """平倉單沒有回報，最常見的原因就是回報連線斷了。查詢不走回報連線（它是下單元件的阻塞式
    查詢），所以回報連線斷了照樣查——送單前那道「斷線就不送」的檢查不能擋到這裡。"""
    skcom = FakeSkcom(on_send=silence(), order_report=_order_row(seq="2315610000001"),
                      fulfill_report=_fill_row(qty="3"))
    broker = _broker(skcom)
    contract = ContractInfo(TMF_CODE, date(2026, 8, 31), "TM2608")
    with pytest.raises(FillUnknown):
        broker.place_order(OrderRequest(contract, SELL, 3, SL_DAY))
    skcom.drop_reply_link(3002, reconnects=False)
    assert broker.query_fills("2315610000001", SL_DAY, 3) == FillQuery(lots=3, avg_price=45135.0)
