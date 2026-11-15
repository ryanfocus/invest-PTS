"""部位檔本身：盤中程式寫、出場程式讀，兩支程式之間唯一的溝通管道。

在暫存資料夾裡測真實的讀寫（規格 Testing Decisions）：原子寫入、讀不懂時的行為，
正是要測的東西。盤中程式那一側的行為（何時寫、讀不懂時怎麼辦）在 test_live_orders。
"""

import os
from datetime import date

import pytest

import position_file
from position_file import (CONFIRMED, EXIT, UNCERTAIN, Fill, PositionRecord, UncertainOrder)

DAY = date(2026, 11, 18)


def _record(**overrides) -> PositionRecord:
    values = dict(trading_day=DAY, product="TM0000AM", order_code="TM2611",
                  contract_month="202611", direction="SHORT", last_trading_day=DAY,
                  status=CONFIRMED,
                  entries=(Fill("2315610000001", 3, 45150.0), Fill("2315610000002", 1, 45140.5)))
    values.update(overrides)
    return PositionRecord(**values)


def test_no_file_means_no_position_that_day():
    assert position_file.read(DAY) is None


def test_what_one_program_writes_the_other_reads_back_whole():
    """出場程式（ticket 08）會寫出場委託與「出場單不確定」；這些欄位在這張 ticket 就定案，
    兩邊才能同時開工。讀回來的必須一模一樣：差一口就是平錯量。"""
    record = _record(exits=(Fill("2315610000003", 2, 45100.0),), status=UNCERTAIN,
                     uncertain=UncertainOrder(EXIT, "2315610000004", 2, 1))
    position_file.write(record)
    assert position_file.read(DAY) == record


def test_the_average_comes_from_the_fills():
    """(45150 × 3 + 45140.5 × 1) ÷ 4 = 45147.625。"""
    record = _record()
    assert (record.entered_lots, record.avg_price) == (4, 45147.625)


def test_an_interrupted_write_leaves_the_previous_file_whole(monkeypatch, state_dir):
    """寫到一半斷電（這裡用落磁碟那一步失敗來模擬）：正式檔仍然是上一份完整的內容，
    也不留暫存檔在旁邊混淆人。"""
    position_file.write(_record())
    before = open(position_file.path_for(DAY), encoding="utf-8").read()

    def power_cut(fd):
        raise OSError("斷電")

    monkeypatch.setattr(os, "fsync", power_cut)
    with pytest.raises(OSError, match="斷電"):
        position_file.write(_record(entries=_record().entries + (Fill("2315610000009", 1, 1.0),)))

    assert open(position_file.path_for(DAY), encoding="utf-8").read() == before
    assert sorted(os.listdir(state_dir)) == ["position-20261118.json"]


def test_a_record_that_contradicts_itself_cannot_even_be_written():
    """寫的那一側也擋：出場程式拿著自相矛盾的記錄去下單，比沒有記錄更糟。"""
    with pytest.raises(ValueError, match="平倉"):
        _record(exits=(Fill("2315610000003", 5, 45100.0),))
    with pytest.raises(ValueError, match="不確定"):
        _record(status=UNCERTAIN)


# --- 狀態的變化：狀態與「哪一筆不確定」一起改 ---


def test_an_entry_sent_without_knowing_its_fill_and_then_settled():
    """送單前記下「不確定」（還沒有序號），補上序號，最後查到成交：回到已確認，成交進了進場明細。"""
    sending = _record().unsure(position_file.ENTRY, "", 2)
    assert (sending.status, sending.uncertain.order_seq) == (UNCERTAIN, "")
    sent = sending.unsure(position_file.ENTRY, "2315610000009", 2, known_filled=1)
    settled = sent.settled(Fill("2315610000009", 2, 45130.0))
    assert (settled.status, settled.uncertain) == (CONFIRMED, None)
    assert settled.entered_lots == 6
    assert settled == _record(entries=_record().entries + (Fill("2315610000009", 2, 45130.0),))


def test_a_close_order_settled_by_the_lookup_counts_as_closed():
    unsure = _record().unsure(EXIT, "2315610000009", 4)
    settled = unsure.settled(Fill("2315610000009", 4, 45100.0))
    assert (settled.status, settled.open_lots) == (CONFIRMED, 0)


def test_what_a_lookup_found_only_ever_raises_the_lower_bound():
    unsure = _record().unsure(EXIT, "2315610000009", 4, known_filled=2)
    assert unsure.at_least(3).uncertain.known_filled == 3
    assert unsure.at_least(1) is unsure


_CLOSING = "2315610000009"


def _closing_unsure() -> PositionRecord:
    return _record().unsure(EXIT, _CLOSING, 2)


#                         帳上                                              檔裡             落後
BEHIND = {
    "一樣":                (_closing_unsure(),                                 _closing_unsure(), False),
    "少了查到的平倉成交":   (_closing_unsure().settled(Fill(_CLOSING, 2, 45100.0)), _closing_unsure(), True),
    "少了那筆不確定的平倉": (_closing_unsure(),                                 _record(),         True),
    "只少了已知至少成交幾口":(_closing_unsure().at_least(1),                    _closing_unsure(), False),
    # 盤中補不上序號：檔裡那筆「不確定」沒有序號，出場程式查不了
    "不確定的那一筆少了序號":(_record().unsure(position_file.ENTRY, _CLOSING, 2),
                            _record().unsure(position_file.ENTRY, "", 2),       True),
}


@pytest.mark.parametrize("actual, on_file, lags", BEHIND.values(), ids=BEHIND.keys())
def test_the_file_is_behind_only_when_the_next_reader_would_act_wrongly(actual, on_file, lags):
    """部位檔寫不進去時，什麼樣的落後要叫人「不要再執行出場程式」：下一個讀它的會照檔再平一次、
    或把不確定的當成確定。已知成交口數是給人對帳的下限，下一個讀它的會再查，不算。"""
    assert position_file.behind(actual, on_file) is lags


def test_the_ledger_writes_again_until_the_file_catches_up(monkeypatch, state_dir):
    """寫不進去之後，同一份再存一次就再寫一次：檔案這次寫得進去的話就跟上了。"""
    record = _record()
    ledger = position_file.Ledger(record)
    closed = record.closed_by(Fill("2315610000009", 1, 45100.0))
    real_write = position_file.write
    attempts = []

    def flaky(r):
        attempts.append(r)
        if len(attempts) == 1:
            raise PermissionError(13, "存取被拒")
        real_write(r)
    monkeypatch.setattr(position_file, "write", flaky)

    ledger.save(closed)
    assert ledger.behind
    ledger.save(closed)
    assert not ledger.behind
    assert position_file.read(DAY) == closed
