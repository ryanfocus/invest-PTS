"""委託出問題：被拒與成交不確定 —— 從最上層打進去（ticket 07）。

進場委託失敗只有兩種處境，界線是「單有沒有送出去」（規格第 41、50、51 條）：

  * **委託沒有送出去**（`OrderFailed`：被拒、憑證、回報連線斷了…）：確定沒有部位。
    今天不再進場，告警只發一次，附上原因。
  * **委託送出去了，但不知道成交幾口**（`FillUnknown`，以及送單時任何意外）：可能有部位。
    部位檔記成「不確定」，今天不再進場，告警請人看帳戶。

和 test_live_orders 一樣只看三種外部效果：送出的委託、發出的訊息、部位檔。
劇本 `rally("08:45", 8)` 讓條件 1 從 08:50 起每分鐘都成立——第一筆之後還送不送，
就是「今天停止進場」有沒有做到。
"""

import os

import pytest

from broker import FillUnknown, OrderFailed
from broker.fake import FakeBroker
from day_script import (DAY, PREVIOUS_DAY, at, calm_history, rally, restarted_at, trade,
                        until)
from recording import RecordingNotifier
from test_live_orders import (_MARKER, CONTRACTS, _config, _file, _locked_once_sent, _on_file,
                              _run)
import intraday
import paths
import position_file


def _all_morning():
    """08:50–08:53 每分鐘條件 1 都成立：四次進場的機會。"""
    return rally("08:45", 8) + [trade(at("08:53:00"), 45180)]


def _run_outcome(events, **broker_options):
    """同 `_run`，但回傳 (盤中程式的結局, 發出的訊息)：要斷言它跑到劇本結束、沒有中止。"""
    broker = FakeBroker(contracts=CONTRACTS, events=events,
                        minute_bars={PREVIOUS_DAY: calm_history(45000)}, **broker_options)
    notifier = RecordingNotifier()
    outcome = intraday.run(broker, _config(), today=DAY, notify=notifier)
    return outcome, notifier.sent


# --- 委託沒有送出去（規格第 51 條）---


def test_a_rejected_order_stops_entries_for_the_day_and_alerts_once():
    """08:50 的委託被拒（保證金不足）。同一個原因下一分鐘一定再被拒一次，所以今天不再送：
    08:51–08:53 條件照樣成立，一筆都不送；告警只有一則，講明原因與要做什麼。
    被拒就是確定沒有部位：不寫部位檔，也不叫人去看帳戶。"""
    broker, sent = _run(_all_morning(), order_errors=[OrderFailed("委託被拒：保證金不足")])

    assert len(broker.orders) == 1
    assert sent == [
        "【要你處理】PTS 委託沒有送出去，今天不再進場\n"
        "這次：買進微台 1 口\n"
        "原因：委託被拒：保證金不足\n"
        "帳上不會因為這筆多出部位，不必看帳戶。"
        "請依原因處理（例如補足保證金、更新憑證），明天開盤前排除。\n"
        "2026/11/09 08:50:00",
    ]
    assert not os.path.exists(position_file.path_for(DAY))


def test_after_a_rejection_a_restart_the_same_day_neither_orders_nor_alerts_again():
    """08:50 的委託被拒，之後盤中程式因為別的原因當掉，排程把它重啟（規格第 48 條）。
    被拒的原因（保證金不足）還在，而第一則告警已經說了「今天不再進場」：重啟後條件照樣
    每分鐘成立，也一筆都不送、不再告警。只記在記憶體裡的話，每重啟一次就再被拒一次、
    再告警一次。"""
    _run(until(_all_morning(), at("08:50:40")),
         order_errors=[OrderFailed("委託被拒：保證金不足")])

    broker, sent = _run(restarted_at(_all_morning(), at("08:51:10")),
                        order_errors=[OrderFailed("委託被拒：保證金不足")])

    assert broker.orders == []
    assert sent == []
    assert not os.path.exists(position_file.path_for(DAY))


def test_a_stop_record_that_cannot_be_read_still_means_no_entries_today():
    """停止進場記錄寫到一半斷電、或被別的程式寫壞了。它存在，就代表今天稍早停止過進場：
    讀不懂內容不是照常進場的理由，也不可以讓盤中程式在啟動時就當掉（原始行情還要記）。"""
    os.makedirs(os.path.dirname(intraday.stop_path(DAY)), exist_ok=True)
    with open(intraday.stop_path(DAY), "wb") as fh:
        fh.write(b"\xff\xfe\x00")

    outcome, sent = _run_outcome(_all_morning())

    assert outcome.ending is intraday.Ending.STREAM_ENDED
    assert sent == []
    assert not os.path.exists(position_file.path_for(DAY))


def test_when_the_stop_record_cannot_be_written_the_alert_still_goes_out_once(
        monkeypatch, tmp_path):
    """部位檔的資料夾不能寫（磁碟滿、權限被改）：停止進場記錄寫不進去。這時最要緊的是告警
    照樣發出、盤中程式不中止；讓例外冒出 `run()` 的話，程式帶著 traceback 結束，
    一則告警都沒有。"""
    blocked = tmp_path / "not-a-folder"
    blocked.write_text("")
    monkeypatch.setattr(paths, "state_dir", lambda: str(blocked / "state"))

    outcome, sent = _run_outcome(_all_morning(),
                                 order_errors=[OrderFailed("委託被拒：保證金不足")])

    assert outcome.ending is intraday.Ending.STREAM_ENDED
    assert len(sent) == 1
    assert sent[0].startswith("【要你處理】PTS 委託沒有送出去，今天不再進場\n")


def test_a_rejection_after_earlier_fills_leaves_them_on_file_as_confirmed():
    """08:50 成交一口，08:51 那筆被拒。被拒的那筆沒有部位，所以部位檔一個字都不改：仍是
    已確認的 1 口，出場程式照常平它。告警說稍早的口數不受影響——只說「不必看帳戶」的話，
    使用者會懷疑那一口是不是也沒了。"""
    broker, sent = _run(_all_morning(), order_errors=[None, OrderFailed("憑證讀取失敗")])

    assert len(broker.orders) == 2
    assert sent[1].splitlines()[1:4] == ["這次：買進微台 1 口",
                                         "原因：憑證讀取失敗",
                                         "今天稍早的多單 1 口不受影響，照常了結。"]
    assert _file()["status"] == "CONFIRMED"
    assert _file()["entries"] == [{"order_seq": "2315610000001", "lots": 1, "price": 45150.0}]


# --- 委託送出去了，但不知道成交幾口（規格第 50 條前半）---


def test_an_order_sent_without_a_fill_report_is_on_file_as_uncertain_and_ends_entries():
    """08:50 的委託送出去了，30 秒內等不到它結束。它可能已經成交：部位檔記成「不確定」，
    寫明是哪一筆進場單、委託幾口、已知至少成交幾口——出場程式靠這個狀態不自動平倉，
    人工對帳靠那個序號。帳上口數不知道，部位不虧、加碼上限都算不了，今天不再進場。
    微台沒有當沖強平，出場程式這次也不平，所以告警直接講：微台要你自己平。"""
    broker, sent = _run(_all_morning(), order_errors=[
        FillUnknown("30 秒內沒等到委託 2315610000001 結束", order_seq="2315610000001")])

    assert len(broker.orders) == 1
    assert _file() == {
        "trading_day": "2026-11-09",
        "product": "TM0000AM",
        "order_code": "TM2611",
        "contract_month": "202611",
        "direction": "LONG",
        "last_trading_day": "2026-11-18",
        "status": "UNCERTAIN",
        "entries": [],
        "exits": [],
        "uncertain": {"stage": "ENTRY", "order_seq": "2315610000001",
                      "requested_lots": 1, "known_filled": 0},
    }
    assert sent == [
        "【要你處理】PTS 委託送出去了，但不知道成交幾口\n"
        "這次：買進微台 1 口（委託序號 2315610000001）\n"
        "原因：30 秒內沒等到委託 2315610000001 結束\n"
        "部位檔已記為「不確定」：盤中程式今天不再進場，13:25 的出場程式也不會自動平倉。\n"
        "請到券商 APP 確認這筆成交了幾口。微台沒有當沖強平：PTS 今天的微台部位"
        "要你自己在 13:30 前平掉。\n"
        "2026/11/09 08:50:00",
    ]


def test_an_uncertain_order_after_earlier_fills_keeps_them_and_says_what_is_known():
    """重啟前已經有小台多單 2 口。這一筆委託 2 口，回報只看到 1 口就斷了：已知**至少**
    成交 1 口。確認過的 2 口照留在明細裡（它們是真的），不確定的這一筆另外記，連同
    已知的 1 口。小台標了當沖，券商 13:30 會強平，所以這則不叫人自己平。"""
    _on_file()
    broker, sent = _run(_all_morning(), config=_config(lots_per_entry=2), order_errors=[
        FillUnknown("30 秒內沒等到委託 2315610000001 結束（目前已知成交 1 口，委託 2 口）",
                    order_seq="2315610000001", known_filled=1)])

    assert [o.lots for o in broker.orders] == [2]
    assert _file()["status"] == "UNCERTAIN"
    assert _file()["entries"] == [{"order_seq": "2315619999999", "lots": 2, "price": 45100.0}]
    assert _file()["uncertain"] == {"stage": "ENTRY", "order_seq": "2315610000001",
                                    "requested_lots": 2, "known_filled": 1}
    assert len(sent) == 1
    assert sent[0].splitlines()[1] == "這次：買進小台 2 口（委託序號 2315610000001），已知至少成交 1 口"
    assert sent[0].splitlines()[4] == ("請到券商 APP 確認這筆成交了幾口。"
                                       "小台標了當沖，券商 13:30 會強平 PTS 今天的小台部位。")


def test_on_the_micro_the_alert_counts_the_confirmed_lots_the_user_must_also_close():
    """微台：08:50 成交 1 口，08:51 那筆不確定。出場程式看到「不確定」就整個不平，連那 1 口
    確認過的也不平——所以要人自己平的是「確認的 1 口，加上這筆實際成交的」。只說「這筆」
    的話，使用者平掉不確定的那幾口，確認的那 1 口就過夜了。"""
    _, sent = _run(_all_morning(), order_errors=[
        None, FillUnknown("30 秒內沒等到委託 2315610000002 結束", order_seq="2315610000002")])

    assert len(sent) == 2
    assert sent[1].splitlines()[4] == (
        "請到券商 APP 確認這筆成交了幾口。微台沒有當沖強平：PTS 今天的微台部位"
        "（部位檔已確認的多單 1 口，加上這筆實際成交的）要你自己在 13:30 前平掉。")
    assert _file()["entries"] == [{"order_seq": "2315610000001", "lots": 1, "price": 45150.0}]
    assert _file()["status"] == "UNCERTAIN"


def test_when_the_order_seq_cannot_be_written_the_file_still_says_uncertain_from_before_sending(
        monkeypatch):
    """不確定的那一筆要寫進部位檔時（委託序號、已知口數），檔案被鎖住。部位檔停在送單前記下的
    「不確定」（已確認的微台多單 2 口，加上這一筆、還沒有序號）：13:25 出場程式照樣不會自動平倉，
    所以這則要人自己平的是 PTS 今天**全部**的微台部位，和寫進去的時候一樣。委託序號告警裡有，
    對帳用它。今天照樣不再進場，盤中程式也不因此中止。"""
    content = _on_file(product="TM0000AM", order_code="TM2611")
    _locked_once_sent(monkeypatch)
    broker, sent = _run(_all_morning(), order_errors=[
        FillUnknown("30 秒內沒等到委託 2315610000001 結束", order_seq="2315610000001")])

    assert len(broker.orders) == 1
    assert _file() == dict(content, status="UNCERTAIN", uncertain=_MARKER)
    assert len(sent) == 1
    assert sent[0].splitlines()[3:5] == [
        "部位檔已記為「不確定」（這筆的委託序號沒寫進去：PermissionError: [Errno 13] 存取被拒）："
        "盤中程式今天不再進場，13:25 的出場程式也不會自動平倉。",
        "請到券商 APP 確認這筆成交了幾口。微台沒有當沖強平：PTS 今天的微台部位"
        "（部位檔已確認的多單 2 口，加上這筆實際成交的）要你自己在 13:30 前平掉。",
    ]


def test_when_the_uncertain_state_cannot_be_written_a_restart_still_enters_nothing(
        monkeypatch):
    """不確定的那一筆的委託序號寫不進部位檔，檔裡是已確認的 2 口與送單前記下的「不確定」；之後
    盤中程式重啟。帳上可能已經多了這一筆、口數不知道，告警也說了今天不再進場：重啟後照常加碼
    的話，部位不虧和加碼上限都在少算的口數上判斷。告警當時發過了，重啟不再發。"""
    content = _on_file(product="TM0000AM", order_code="TM2611")
    replace = os.replace
    _locked_once_sent(monkeypatch)
    _run(until(_all_morning(), at("08:50:40")), order_errors=[
        FillUnknown("30 秒內沒等到委託 2315610000001 結束", order_seq="2315610000001")])
    # 重啟時檔案已經不鎖了。⚠️ 不用 monkeypatch.undo()：它連 conftest 導開的資料夾也還原
    monkeypatch.setattr(os, "replace", replace)

    broker, sent = _run(restarted_at(_all_morning(), at("08:51:10")))

    assert broker.orders == []
    assert sent == []
    assert _file() == dict(content, status="UNCERTAIN", uncertain=_MARKER)


def test_when_the_first_order_is_uncertain_and_cannot_be_written_the_file_still_says_uncertain(
        monkeypatch):
    """今天第一筆就不確定、序號又寫不進去：部位檔是送單前記下的「不確定」、還沒有成交。出場程式
    看到它不平倉、會告警；這筆要人自己平。"""
    _locked_once_sent(monkeypatch)
    outcome, sent = _run_outcome(_all_morning(), order_errors=[FillUnknown("回報斷了")])

    assert outcome.ending is intraday.Ending.STREAM_ENDED
    assert _file()["status"] == "UNCERTAIN"
    assert _file()["entries"] == []
    assert sent[0].splitlines()[4] == ("請到券商 APP 確認這筆成交了幾口。微台沒有當沖強平："
                                       "PTS 今天的微台部位要你自己在 13:30 前平掉。")


# --- 三種處境，三種固定的說法（規格第 41 條）---

SITUATIONS = {
    "委託沒有送出去": dict(order_errors=[OrderFailed("委託被拒：保證金不足")]),
    "委託送出去了，但": dict(order_errors=[FillUnknown("回報斷了", order_seq="2315610000001")]),
    "部位還在": dict(locked=True),          # 成交了，部位檔寫不進去
}


@pytest.mark.parametrize("phrase", SITUATIONS.keys())
def test_each_situation_is_told_by_its_own_fixed_wording_and_never_the_others(
        phrase, monkeypatch):
    """使用者看第一行就要知道是哪一種：確定沒有部位、可能有部位、確定有部位，要做的事
    完全不同。所以每一則的標題帶著自己的說法，全文也不會出現另外兩種的說法。"""
    options = dict(SITUATIONS[phrase])
    if options.pop("locked", False):
        _locked_once_sent(monkeypatch)
    _, sent = _run(_all_morning(), **options)

    assert len(sent) == 1
    assert sent[0].startswith(f"【要你處理】PTS {phrase}")
    for other in SITUATIONS.keys() - {phrase}:
        assert other not in sent[0]


@pytest.mark.parametrize("error", [TimeoutError("等不到回報"), RuntimeError("元件壞掉"),
                                   OSError("COM 呼叫失敗")], ids=["逾時", "元件壞掉", "COM 錯誤"])
def test_any_unexpected_failure_while_ordering_counts_as_sent_with_the_fill_unknown(error):
    """broker 的契約是只拋 `OrderFailed` 或 `FillUnknown`，但它沒做到的那一天（漏接的例外、
    新版元件的怪錯），盤中程式分不出單出去了沒有。分不清就是「可能有部位」：當成「沒送出去」
    的話部位檔不記，出場程式以為今天沒進場，部位直接過夜。連序號都沒有，告警照實說。"""
    broker, sent = _run(_all_morning(), order_errors=[error])

    assert len(broker.orders) == 1
    assert _file()["status"] == "UNCERTAIN"
    assert _file()["uncertain"] == {"stage": "ENTRY", "order_seq": "",
                                    "requested_lots": 1, "known_filled": 0}
    assert len(sent) == 1
    assert sent[0].splitlines()[:3] == [
        "【要你處理】PTS 委託送出去了，但不知道成交幾口",
        "這次：買進微台 1 口（沒拿到委託序號）",
        f"原因：送單時發生意外（{type(error).__name__}）：{error}",
    ]
