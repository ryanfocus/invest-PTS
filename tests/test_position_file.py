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
