"""心跳記錄：盤中程式今天有沒有跑，出場程式 13:25 檢查（規格第 54 條）——從兩支程式的最上層打進去。

平常沒有例行訊息，盤中程式沒跑（排程壞了、當掉後沒被重啟）的話，使用者不會發現。兩支程式
之間只靠部位檔與心跳記錄溝通：盤中程式每分鐘寫一次，出場程式讀它。

斷言的是外部效果：出場程式發了哪些訊息、照常做了該做的事；心跳記錄本身是兩支程式之間的
約定，格式逐字斷言。
"""

import json
import os
from datetime import date

import pytest

from broker import SELL, Clock
from broker.fake import FakeBroker
from day_script import (DAY, PREVIOUS_DAY, at, clock, make_config, position_content, rally,
                        run_day, trade, whole_morning, write_position_file)
from recording import RecordingNotifier
import exit_program
import paths


def _exit(when: str = "13:25", day: date = DAY, **broker_options) -> tuple[FakeBroker, list[str]]:
    """出場程式在 `when` 被叫醒。回傳 (broker, 發出的訊息)。"""
    broker_options.setdefault("fill_price", 45200.0)
    broker = FakeBroker(**broker_options)
    notifier = RecordingNotifier()
    now = at(f"{when}:00", day)
    exit_program.run(broker, make_config(auto_order_enabled=True), notify=notifier,
                     clock=lambda: now,
                     sleep=lambda seconds: None)
    return broker, notifier.sent


def _intraday(events, *, config=None, **broker_options) -> tuple[FakeBroker, list[str]]:
    broker, sent, _ = run_day(events, config=config, **broker_options)
    return broker, sent


def _micro_on_file() -> None:
    """盤中程式今天寫下的部位檔：微台多單 1 口（`position_content` 的預設）。"""
    write_position_file(position_content())


# --- 盤中程式今天沒有跑 ---


def test_when_the_intraday_program_did_not_run_today_the_exit_program_says_so_and_still_exits():
    """今天沒有任何心跳：排程壞了、電腦沒開、程式一啟動就失敗。平常沒有例行訊息，不講的話
    使用者不會發現。出場不依賴盤中程式（規格第 49 條）：部位檔裡的微台照樣平掉。"""
    _micro_on_file()
    broker, sent = _exit()

    assert [(o.side, o.lots) for o in broker.orders] == [(SELL, 1)]
    assert sent == [
        "【要你處理】PTS 盤中程式今天沒有跑\n"
        "今天沒有判斷進場，也沒有記錄原始行情。出場程式照常處理部位檔裡的部位。\n"
        "請檢查工作排程器裡盤中程式的排程，以及 logs/intraday-20261109.log，"
        "下一個交易日 08:40 之前排除。\n"
        "2026/11/09 13:25:00",
    ]


def _write_heartbeat(text: str) -> None:
    os.makedirs(paths.state_dir(), exist_ok=True)
    with open(os.path.join(paths.state_dir(), "heartbeat.json"), "w", encoding="utf-8") as fh:
        fh.write(text)


def test_a_heartbeat_left_from_the_previous_trading_day_means_it_did_not_run_today():
    """心跳記錄只有一個檔，每次覆寫。上一個交易日跑完留下的那一份，不代表今天有跑。"""
    _write_heartbeat('{"trading_day": "2026-11-06", "last_alive": "2026-11-06T13:20:00", '
                     '"finished": true}')
    _, sent = _exit()
    assert [m.splitlines()[0] for m in sent] == ["【要你處理】PTS 盤中程式今天沒有跑"]


HEARTBEAT_DAMAGE = {
    "寫到一半": '{"trading_day": "2026-11-09", "last_al',
    "空檔": "",
    "少了欄位": '{"trading_day": "2026-11-09", "finished": true}',
    "日期寫錯": '{"trading_day": "2026/11/09", "last_alive": null, "finished": true}',
    "時刻寫錯": '{"trading_day": "2026-11-09", "last_alive": "10:00", "finished": false}',
    "收工寫成字串": '{"trading_day": "2026-11-09", "last_alive": null, "finished": "true"}',
    "沒收工卻沒有存活時刻": '{"trading_day": "2026-11-09", "last_alive": null, "finished": false}',
}


@pytest.mark.parametrize("text", HEARTBEAT_DAMAGE.values(), ids=HEARTBEAT_DAMAGE.keys())
def test_a_heartbeat_that_cannot_be_read_is_reported_and_the_exit_goes_on(text):
    """讀不懂就不知道盤中程式今天有沒有跑：當成「有跑」就是安靜地漏掉，當成「沒跑」又是
    在講不知道的事。照實講讀不懂、原因是什麼；出場照常。"""
    _write_heartbeat(text)
    _micro_on_file()
    broker, sent = _exit()

    assert [(o.side, o.lots) for o in broker.orders] == [(SELL, 1)]
    assert len(sent) == 1
    lines = sent[0].splitlines()
    assert lines[0] == "【要你處理】PTS 心跳記錄讀不懂，不知道盤中程式今天有沒有跑完"
    assert lines[1].startswith("原因：")
    assert lines[2:] == [
        "出場程式照常處理部位檔裡的部位。",
        "請看 logs/intraday-20261109.log 確認盤中程式今天有沒有跑完，下一個交易日 08:40 之前排除。",
        "2026/11/09 13:25:00",
    ]


def _heartbeat() -> dict:
    with open(os.path.join(paths.state_dir(), "heartbeat.json"), encoding="utf-8") as fh:
        return json.load(fh)


# --- 盤中程式照常跑完 ---


def test_after_a_whole_morning_the_exit_program_finds_the_intraday_program_ran():
    """平常的一天：盤中程式 08:40 跑到 13:20 收工，沒有進場。出場程式看到今天的心跳、
    而且盤中程式自己收工了，什麼都不講——沒有進場的日子完全安靜（規格第 39 條）。

    心跳記錄是兩支程式之間的約定，逐字斷言：最後存活時刻是收工那一刻的本機時鐘。"""
    _intraday(whole_morning())
    assert _heartbeat() == {"trading_day": "2026-11-09", "last_alive": "2026-11-09T13:20:00",
                            "finished": True}

    _, sent = _exit()
    assert sent == []


def test_a_day_off_found_only_by_the_quotes_is_not_reported_as_the_program_not_running():
    """臨時休市（颱風假）：日曆說要開盤，收到的卻是前一個交易日的報價。盤中程式照規則空跑到
    13:20、自己收工（規格第 10 條）——那是它該做的事，出場程式不必講「盤中程式沒有跑」。
    （盤中程式 08:48 那則報價中斷的告警分不出休市與斷線，見 test_quote_outage。）"""
    events = ([trade(at("13:44:59", PREVIOUS_DAY), 44100)]
              + [e for e in whole_morning() if isinstance(e, Clock)])
    _, sent = _intraday(events)
    assert [m.splitlines()[0] for m in sent] == ["【要你處理】PTS 盤中報價中斷超過 3 分鐘"]
    assert _heartbeat()["finished"] is True

    _, sent = _exit()
    assert sent == []


def test_when_the_position_file_stops_the_intraday_program_only_that_is_reported():
    """部位檔讀不懂：盤中程式一開始就告警、今天不進場（ticket 06），不登入、也收不到任何事件。
    那是它自己下的結論，不是當掉——出場程式照樣講部位檔讀不懂（它自己也讀不懂），但不再多講
    一則「盤中程式沒有跑」，同一件事講兩次，第二則還講錯原因。"""
    write_position_file("{寫到一半")
    _, sent = _intraday([], login_error=AssertionError("部位檔讀不懂時不該登入"))
    assert [m.splitlines()[0] for m in sent] == [
        "【要你處理】PTS 部位檔讀不懂，盤中程式今天不進場"]

    _, sent = _exit()
    assert [m.splitlines()[0] for m in sent] == [
        "【要你處理】PTS 部位檔讀不懂，出場程式沒有平倉"]


def test_a_heartbeat_that_cannot_be_written_does_not_stop_the_intraday_program():
    """心跳記錄寫不進去（這裡讓同名的資料夾佔住它）：代價是 13:25 多一則「沒有跑」的告警。
    為了它讓盤中程式中止的話，連進場都沒了——強勢的早上照樣進場。"""
    os.makedirs(os.path.join(paths.state_dir(), "heartbeat.json"))
    _, sent = _intraday(rally("08:45", 5) + [clock(at("08:50:00")), trade(at("08:50:00"), 45150)])
    assert [m.splitlines()[0] for m in sent] == ["【模擬】PTS 做多：買進微台 1 口 @ 45150"]


def _denied(*args):
    raise PermissionError(13, "存取被拒")


WRITE_FAILURES = {
    # Windows 上防毒或索引程式剛好開著正式檔時，置換那一步就是這樣失敗
    "置換失敗": "replace",
    # 內容寫了、還沒落到磁碟就出事（磁碟錯誤；斷電時也停在這附近）
    "寫到一半": "fsync",
}


@pytest.mark.parametrize("step", WRITE_FAILURES.values(), ids=WRITE_FAILURES.keys())
def test_a_heartbeat_write_that_fails_midway_leaves_the_previous_one_intact(step, monkeypatch):
    """心跳記錄只有一個檔，每分鐘整個覆寫：寫壞的那一刻如果留下半個檔，出場程式讀到的就是
    「讀不懂」，而不是事實。盤中程式 10:00 多停了；重啟後那一輪的心跳每一次都寫不成。
    心跳記錄必須還是停之前那一份，出場程式照樣講得出停在幾點；資料夾裡也不留下寫到一半的
    暫存檔（每分鐘一個，一天就是幾百個）。"""
    _intraday(_morning_until("10:00:30"))
    before = _heartbeat()

    with monkeypatch.context() as m:        # 出場程式時已經不鎖了
        m.setattr(os, step, _denied)
        _intraday([e for e in whole_morning() if at("10:05:00") <= e.time <= at("10:07:30")])

    assert _heartbeat() == before
    assert [n for n in os.listdir(paths.state_dir()) if n.endswith(".tmp")] == []
    _, sent = _exit()
    assert [m.splitlines()[0] for m in sent] == ["【要你處理】PTS 盤中程式今天 10:00 之後就沒有再跑"]


def _morning_until(hhmmss: str) -> list:
    """一個平常的上午，在 `hhmmss` 之後就沒有了：盤中程式在那裡停了（當掉、被關掉）。"""
    return [e for e in whole_morning() if e.time <= at(hhmmss)]


def test_when_the_intraday_program_stopped_partway_the_exit_program_says_when():
    """盤中程式 10:00 多當掉，排程沒有把它重啟（或重啟後又停了）。心跳每分鐘更新，所以
    出場程式講得出它停在幾點：那之後都沒有判斷進場。只說「今天有跑」的話，這種日子和平常
    分不出來。部位照常由出場程式處理。"""
    _intraday(_morning_until("10:00:30"))
    assert _heartbeat() == {"trading_day": "2026-11-09", "last_alive": "2026-11-09T10:00:00",
                            "finished": False}

    _, sent = _exit()
    assert sent == [
        "【要你處理】PTS 盤中程式今天 10:00 之後就沒有再跑\n"
        "10:00 之後沒有判斷進場：盤中程式停了，排程沒有把它重啟，或重啟之後又停了。"
        "出場程式照常處理部位檔裡的部位。\n"
        "請看 logs/intraday-20261109.log 最後幾行找原因，並檢查工作排程器裡盤中程式的排程，"
        "下一個交易日 08:40 之前排除。\n"
        "2026/11/09 13:25:00",
    ]


# --- 13:20 之後才起來的那一輪不算「跑完了」---


def test_a_restart_that_comes_up_only_after_1320_does_not_hide_that_the_morning_was_lost():
    """09:00 多當掉，排程到 13:21 才把它重啟成功。那一輪一起來就過了進場截止，什麼都沒判斷：
    寫「收工了」的話，蓋掉的正是「09:00 之後就沒有再跑」——一整個交易時段沒人判斷進場，
    出場程式卻什麼都不講。"""
    _intraday(_morning_until("09:00:30"))
    _intraday([Clock(at("13:21:00"))])

    _, sent = _exit()
    assert [m.splitlines()[0] for m in sent] == ["【要你處理】PTS 盤中程式今天 09:00 之後就沒有再跑"]


def test_a_first_start_after_1320_is_reported_as_not_having_run_today():
    """今天第一次啟動就已經 13:22（電腦晚開機、排程補跑）：它沒有判斷過任何一分鐘。"""
    _intraday([Clock(at("13:22:00"))])

    _, sent = _exit()
    assert [m.splitlines()[0] for m in sent] == ["【要你處理】PTS 盤中程式今天沒有跑"]
