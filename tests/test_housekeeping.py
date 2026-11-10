"""log 保留 30 天（規格第 58、60 條）——用真實檔案測，驗的就是檔案系統的行為。

`logs/` 裡有我們控制不了內容的東西：群益元件自己寫的 log（含身分證字號）、券商回覆的原始
存檔（含未遮罩的期貨帳號）。清理的目的是**限制個資留在磁碟上的時間**，省空間只是附帶。
原始行情記錄、部位檔、心跳記錄不在清除範圍內：前者要長期保留，後兩者是兩支程式的溝通管道。
"""

import os
import time

import paths
from housekeeping import purge_old_logs

SECONDS_PER_DAY = 86400


def _aged(path: str, *, days_old: float) -> str:
    """造一個「幾天前最後修改」的檔案（資料夾不存在就建）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("x")
    stamp = time.time() - days_old * SECONDS_PER_DAY
    os.utime(path, (stamp, stamp))
    return path


def _in_logs(*parts: str, days_old: float) -> str:
    return _aged(os.path.join(paths.logs_dir(), *parts), days_old=days_old)


def test_logs_older_than_30_days_are_removed_including_the_capital_component_logs():
    """群益元件的 log 導到 `logs/capital/`，它可能在底下再建一層 `CapitalLog`（文件沒講，
    待實機看）——兩種位置都要清得到，否則含身分證字號的檔永遠留著。"""
    old = [
        _in_logs("intraday-20261001.log", days_old=31),
        _in_logs("replies-20261001-091500-2315610000001.txt", days_old=31),
        _in_logs("capital", "20261001_Center.log", days_old=31),
        _in_logs("capital", "CapitalLog", "20261001_Reply.log", days_old=31),
    ]
    fresh = [
        _in_logs("intraday-20261109.log", days_old=1),
        _in_logs("capital", "Center.log", days_old=0),
        _in_logs("capital", "CapitalLog", "Reply.log", days_old=0),
    ]

    purge_old_logs()

    assert [p for p in old if os.path.exists(p)] == []
    assert [p for p in fresh if not os.path.exists(p)] == []


def test_a_log_is_kept_for_exactly_30_days_and_removed_one_second_later():
    """「保留 30 天」（規格第 60 條）：剛好 30 天的還在——自然的理解是「30 天內的都在」，砍掉
    邊界會讓最舊的那天撲空——多一秒就刪。30 天寫成字面值，不從程式的常數算：改了保留天數、
    或把邊界從「留」改成「刪」，這裡都要紅。"""
    written = 1_790_000_000.0                   # 2026-09-21 前後，哪一刻都一樣
    path = _in_logs("exit-20260921.log", days_old=0)
    os.utime(path, (written, written))

    purge_old_logs(now=written + 30 * SECONDS_PER_DAY)
    assert os.path.exists(path), "剛好 30 天的 log 要留著"

    purge_old_logs(now=written + 30 * SECONDS_PER_DAY + 1)
    assert not os.path.exists(path), "超過 30 天的 log 要刪掉"


def test_raw_market_data_and_state_are_never_purged():
    """原始行情記錄要長期保留（規格第 58 條：盤中重啟靠它補回 K 棒與筆差，作者日後拿它回測）；
    部位檔被清掉的那一刻，出場程式就以為今天沒有部位。兩者都不在 `logs/` 底下。"""
    market = _aged(os.path.join(paths.market_data_dir(), "TX-20250101.csv"), days_old=400)
    state = _aged(os.path.join(paths.state_dir(), "heartbeat.json"), days_old=400)
    purge_old_logs()
    assert os.path.exists(market) and os.path.exists(state)


def test_in_the_real_layout_raw_market_data_and_state_are_outside_logs():
    """上一條測試的資料夾是 conftest 導開的；這條看真正的位置（沒被測試改過的 `paths`）。
    哪天有人把原始行情搬進 `logs/` 底下，清理就會在 30 天後默默吃掉它。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_real_paths", paths.__file__)
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    logs = os.path.join(real.logs_dir(), "")
    assert real.capital_log_dir().startswith(logs)
    for kept in (real.market_data_dir(), real.state_dir()):
        assert not os.path.join(kept, "").startswith(logs), kept


def test_a_log_that_cannot_be_removed_does_not_stop_the_others(monkeypatch):
    """Windows 上檔案被別的行程開著就刪不掉，群益元件正是會這樣。一個刪不掉，其餘照刪；
    清理是收尾動作，永遠不拋例外。"""
    locked = _in_logs("capital", "20261001_Quote.log", days_old=40)
    other = _in_logs("capital", "20261001_Order.log", days_old=40)
    real_remove = os.remove

    def stubborn(path):
        if path == locked:
            raise PermissionError(32, "檔案正被另一個程序使用")
        real_remove(path)
    monkeypatch.setattr(os, "remove", stubborn)

    assert purge_old_logs() == 1
    assert os.path.exists(locked) and not os.path.exists(other)


# --- 誰來清：出場程式每天 13:25 做完正事之後 ---


def test_the_exit_program_clears_old_logs_every_day(tmp_path, monkeypatch):
    """出場程式的排程每天都會叫醒它（非交易日也是），盤中程式則可能一天被叫醒好幾次
    （失敗重啟），所以由出場程式在做完正事之後清。正事出錯也照清：個資的保存期限不該
    因為某天程式出錯就延長。"""
    import exit_program

    env = tmp_path / ".env"
    env.write_text("CAPITAL_USER_ID=A100000000\nCAPITAL_PASSWORD=pw\n"
                   "DISCORD_WEBHOOK_URL=https://discord.invalid/api/webhooks/1/x\n",
                   encoding="utf-8")
    monkeypatch.setattr(paths, "env_path", lambda: str(env))
    monkeypatch.setattr(exit_program, "_setup_logging", lambda today: None)

    def broken(*args, **kwargs):
        raise RuntimeError("意料之外的錯誤")
    monkeypatch.setattr(exit_program, "run", broken)
    from notifiers import discord
    monkeypatch.setattr(discord, "send", lambda text, webhook: True)
    old = _in_logs("capital", "20261001_Center.log", days_old=31)

    assert exit_program.main() == 1
    assert not os.path.exists(old)
