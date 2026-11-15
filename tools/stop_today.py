"""收工指令：盤中讓 PTS 今天收工（GLOSSARY「收工指令」，規格第 61 條）。

用法（在 PTS 資料夾）：

    .\\.venv\\Scripts\\python.exe tools\\stop_today.py           今天不再進場；現有部位照常 13:25／13:30 了結
    .\\.venv\\Scripts\\python.exe tools\\stop_today.py --close   今天不再進場，並且現在就平掉 PTS 自己的口數

結果印在主控台，也寫進 `logs/stop-YYYYMMDD.log`。不發 Discord：這是使用者自己下的指令，人就在
電腦前。結束碼 0 是做完了；1 是有事要人看，最後印的那一段寫明是什麼事、要做什麼。非交易日什麼都
不做。

## 為什麼不在券商 APP 自己平

  * APP 看到的是淨部位：使用者自己的長期微台、OS 的口數都在同一個合約上，看不出哪幾口是 PTS 的。
  * 13:25 的出場程式只認部位檔，會照檔再平一次微台：反向新倉、過夜。
  * 盤中程式不知道部位已經平了，條件成立還會繼續加碼。

現有的機制也擋不住：當天的設定在第一次啟動時就凍結了（`day_settings`），關掉盤中程式排程每分鐘
就把它叫回來。所以 PTS 要自己停手、自己平倉，帳記在部位檔裡。

## 停止進場（兩種都會做）

寫下停止進場記錄（`intraday.stop_path`），盤中程式每個時鐘事件都看一次：看到就不再進場、取消
等待中的突破進場，寫下確認記錄（`intraday.stop_seen_path`）。**先寫記錄、再試鎖**
（`intraday.is_running`）：盤中程式沒在跑，之後起來的一開始就讀得到；在跑的話，等它的確認記錄
（`_intraday_stopped`）。確認記錄只在盤中程式的事件迴圈裡寫，它出現時，正在等成交回報的那一筆
進場委託一定已經記進部位檔——之後盤中程式不再寫部位檔，`--close` 記下的平倉才不會被它覆寫掉。

## --close：現在就平掉 PTS 自己的口數

  * **只在 13:20 進場截止之前開始，送到 13:24 為止**（出場程式 13:25 被叫醒的前一分鐘）。收工指令、
    出場程式、券商的當沖強平因此不會同時送單。之後才執行就不送單，講明由誰了結。
  * **只看部位檔**，和出場程式同一條規則：部位檔只有真實下單會寫，模擬的日子沒有它，不送單（規格
    第 42 條）；讀不懂、不確定的照出場程式的規則（`_close`）。
  * **平倉就是出場程式那一套**（`exit_program.close_now`）：每筆最多 10 口、沒送出去每分鐘重試、
    不知道成交就用委託序號查而絕不再送、每筆成交寫進部位檔。大台、小台也平，平倉單照商品標當沖。
  * 沒能平完時**不叫人自己平**（`_Tell`）：部位檔記著平掉的，剩下的照常由 13:25 的出場程式（微台）、
    券商 13:30 的當沖強平（大台、小台）了結。13:25 看到已經平完，就不送單。
  * 同一時間只有一個收工指令（`state/stop_today.lock`）：兩個一起平就是平兩次。13:25 的出場程式也拿
    這把鎖，等收工指令做完才讀部位檔（最多 3 分鐘）：13:24 前送出的最後一筆平倉單，13:25 可能還在等
    回報或在查成交。所以 13:25 之後才講還剩幾口時，不再說「照常由 13:25 平掉」，改講出場程式會等它、
    之後照部位檔處理，有告警就照告警的口數。
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import time as time_module
from datetime import date, datetime
from typing import Callable, Iterator

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import calendar_tw  # noqa: E402
import exit_program  # noqa: E402
import handoff  # noqa: E402
import intraday  # noqa: E402
import messages  # noqa: E402
import paths  # noqa: E402
import position_file  # noqa: E402
import settings  # noqa: E402
import state_files  # noqa: E402
import strategy  # noqa: E402
from broker import Broker  # noqa: E402

# 固定的名字：當成腳本執行時 `__name__` 是 `__main__`，log 裡看不出是誰
logger = logging.getLogger("stop_today")

def main(argv: list[str] | None = None, *,
         connect: Callable[[settings.Secrets], Broker] | None = None,
         clock: Callable[[], datetime] = datetime.now,
         sleep: Callable[[float], None] = time_module.sleep,
         wait_seconds: int = 120) -> int:
    """收工指令的進入點。回傳 0：做完了；1：有事要人看（印出來的最後一段寫明是什麼事、要做什麼）。

    **這裡是唯一建立真實 broker 的地方**，而且只在真的要平倉時：`connect` 用 `.env` 的帳密建立
    broker（預設是群益）。`clock`／`sleep` 是時間，`wait_seconds` 是最多等盤中程式確認多久——
    測試換掉的就是這幾個。
    """
    parser = argparse.ArgumentParser(prog="tools/stop_today.py")
    parser.add_argument("--close", action="store_true",
                        help="並且現在就平掉 PTS 自己的口數（13:20 之前）")
    args = parser.parse_args(argv)
    now = clock()
    with _console(now.date()):
        try:
            return _stop_today(now, close=args.close, connect=connect or _capital, clock=clock,
                               sleep=sleep, wait_seconds=wait_seconds)
        except Exception as exc:  # noqa: BLE001
            logger.exception("收工指令意外中止")
            _say(f"【要你處理】收工指令意外中止（{type(exc).__name__}: {exc}），詳情在 "
                 f"{paths.shown_log('stop', now.date())}。部位檔記到的口數照常由 13:25 的出場程式"
                 "（微台）、券商 13:30 當沖強平（大台、小台）了結；它要是已經送出平倉單，請到券商 APP "
                 "對照 log 裡的「平倉成交」確認帳戶。")
            return 1


def _stop_today(now: datetime, *, close: bool,
                connect: Callable[[settings.Secrets], Broker],
                clock: Callable[[], datetime], sleep: Callable[[float], None],
                wait_seconds: int) -> int:
    today = now.date()
    if not calendar_tw.is_trading_day(today):
        _say(f"{today:%Y/%m/%d} 不是交易日，收工指令什麼都不做。")
        return 0
    with state_files.exclusive(exit_program.stop_today_lock_path()) as alone:
        if not alone:
            # 兩個同時平倉就是平兩次：各自照同一份部位檔送單。13:25 的出場程式處理部位檔時
            # 也拿著這把鎖（`exit_program._after_stop_command`）
            _say("另一個收工指令或 13:25 的出場程式正在處理 PTS 的部位，這一個什麼都不做："
                 "照它印出的結果或 Discord 的告警處理。")
            return 1
        return _stop_alone(now, close=close, connect=connect, clock=clock, sleep=sleep,
                           wait_seconds=wait_seconds)





def _stop_alone(now: datetime, *, close: bool,
                connect: Callable[[settings.Secrets], Broker],
                clock: Callable[[], datetime], sleep: Callable[[float], None],
                wait_seconds: int) -> int:
    today = now.date()
    stop = paths.shown_state(intraday.stop_path(today))
    try:
        # 接在後面：今天稍早盤中程式自己停過（例如委託被拒）的話，那幾行原因留著
        state_files.write_marker(intraday.stop_path(today),
                                 f"{now:%Y/%m/%d %H:%M:%S} 使用者手動停止進場（tools/stop_today.py）",
                                 append=True)
    except OSError as exc:
        # 盤中程式不會知道要停，也就還會進場、還會寫部位檔：這時平倉，記下的平倉會被它覆寫掉
        _say(f"【要你處理】停止進場記錄寫不進 {stop}，盤中程式不會知道要停，也沒有平倉："
             f"{type(exc).__name__}: {exc}。排除原因（例如防毒鎖住、磁碟滿）之後再執行一次。")
        return 1
    _say(f"已寫下停止進場記錄（{stop}）：PTS 今天不再進場。")
    if not _intraday_stopped(today, sleep=sleep, wait_seconds=wait_seconds):
        _say(messages.stop_today_not_confirmed(waited=wait_seconds, close=close,
                                               log=paths.shown_log("intraday", today)))
        return 1
    try:
        record = position_file.read(today)
    except position_file.Unreadable as exc:
        # 不知道帳上有幾口、是哪個商品：不送單（和出場程式同一條規則）
        _say(messages.stop_today_position_file_unreadable(
            at=clock(), reason=str(exc), logs=handoff.fill_logs(today)))
        return 1
    if not close:
        return _show(record, clock())
    return _close(record, connect=connect, clock=clock, sleep=sleep)


def _show(record: position_file.PositionRecord | None, now: datetime) -> int:
    """部位檔的現況與誰會了結。不確定的話照 13:25 出場程式會怎麼做來講：它會先查的，現在沒事要做
    （回 0，但要講不要自己平）；它不查、不送的，要人自己處理（回 1）。"""
    if record is None or record.status != position_file.UNCERTAIN:
        _say(messages.stop_today_position(record, now))
        return 0
    if handoff.exit_looks_up(record):
        _say(messages.stop_today_entry_unresolved(at=now, record=record, lookup=None))
        return 0
    _say(messages.exit_uncertain(at=now, record=record))
    return 1


def _intraday_stopped(today: date, *, sleep: Callable[[float], None], wait_seconds: int) -> bool:
    """停止進場記錄寫下之後，確定盤中程式不會再進場、也不會再寫部位檔了沒有。

    **先寫記錄、再試鎖**（`intraday.is_running`）：鎖到（沒在跑）之後才啟動的盤中程式，啟動時
    一定讀得到記錄。在跑的話，等它的確認記錄（`intraday.stop_seen_path`），每秒看一次、最多
    `wait_seconds` 秒：它只在事件迴圈裡寫，出現時正在等成交回報的那一筆一定已經記進部位檔。
    等的時候它結束了（鎖放掉了）也算：結束的行程不會再送單，之後起來的一開始就讀到記錄。
    """
    if not intraday.is_running():
        _say("盤中程式沒在跑：之後啟動的盤中程式一開始就會讀到這份記錄，不再進場。")
        return True
    _say(f"盤中程式在跑：等它確認看到這份記錄（最多 {wait_seconds} 秒）。")
    seen = intraday.stop_seen_path(today)
    waited = 0
    while state_files.read_marker(seen) is None:
        if waited >= wait_seconds:
            return False
        sleep(1)
        waited += 1
        if state_files.read_marker(seen) is None and not intraday.is_running():
            _say("盤中程式結束了：之後啟動的盤中程式一開始就會讀到這份記錄，不再進場。")
            return True
    _say("盤中程式確認了：今天不再進場，等待中的突破進場也取消了。")
    return True


def _close(record: position_file.PositionRecord | None, *,
           connect: Callable[[settings.Secrets], Broker],
           clock: Callable[[], datetime], sleep: Callable[[float], None]) -> int:
    """`--close`：現在就平掉部位檔裡 PTS 自己還沒平的口數，和出場程式同一套平倉。

    只看部位檔（部位檔只有真實下單會寫，模擬的日子沒有它），不看設定檔。不確定的話照出場程式的
    規則：微台進場那一筆、有委託序號的先查，其他的不送。
    """
    now = clock()
    if now.time() >= strategy.ENTRY_CUTOFF:
        _say(messages.stop_today_after_cutoff())
        return _show(record, now)
    if record is None or (record.uncertain is None and not record.open_lots):
        _say(messages.stop_today_position(record, now))
        return 0
    if record.status == position_file.UNCERTAIN and not handoff.exit_looks_up(record):
        _say(messages.exit_uncertain(at=now, record=record))
        return 1
    try:
        secrets = _secrets()
    except settings.SettingsError as exc:
        _say(f"【要你處理】讀不到平倉要用的帳密，沒有平倉：{exc}\n"
             "部位照常了結：微台由 13:25 的出場程式平倉，大台、小台由券商 13:30 當沖強平"
             "（出場程式用的是同一份 .env）。")
        return 1
    broker = connect(secrets)
    until = handoff.stop_command_until(record.trading_day)
    closing = exit_program.close_now(broker, record, until=until, clock=clock, sleep=sleep,
                                     say=_say, tell=_Tell(record, until))
    if closing.record != closing.on_file:
        # 13:25 的出場程式只認部位檔：照舊檔把已經平掉的再平一次
        _say(messages.stop_today_not_on_file(at=clock(), record=closing.record,
                                             on_file=closing.on_file, reason=closing.file_error))
        return 1
    if closing.ending != exit_program.Ending.CLOSED:
        return 1            # `_Tell` 已經講了剩下的、或不確定的怎麼辦
    _say(messages.stop_today_closed(at=clock(), record=closing.record,
                                    lots=closing.record.exited_lots - record.exited_lots))
    return 0


def _secrets() -> settings.Secrets:
    """登入與下單要用的帳密（`.env`）：帳號、密碼、期貨帳號。不看設定檔——設定檔改壞了也照樣平倉。"""
    secrets = settings.load_secrets(None)
    if not secrets.futures_account:
        raise settings.SettingsError(f"{paths.env_path()} 缺少：CAPITAL_FUTURES_ACCOUNT")
    return secrets


class _Tell:
    """收工指令沒能平完、或不確定時怎麼講（`exit_program.Tell`）。

    和出場程式最大的不同：剩下的口數**不叫人自己平**。部位檔記著這次平掉的，剩下的照常由 13:25 的
    出場程式（微台）或券商 13:30 的當沖強平（大台、小台）了結——叫人自己平，13:25 就是再平一次。
    """

    def __init__(self, record: position_file.PositionRecord, until: datetime):
        self._before = record
        self._until = until

    def ran_out(self, *, record: position_file.PositionRecord, rounds: int,
                problem: str) -> str:
        why = f"收工指令只送到 {handoff.EXIT_START:%H:%M} 出場程式啟動之前"
        if not rounds:
            return f"已經過了 {self._until:%H:%M}，沒有送出平倉單（{why}）"
        return ((f"平倉單沒能送出（最後一次：{problem}），" if problem
                 else "市價 IOC 沒有全部成交，") + f"重試到 {self._until:%H:%M} 仍未完成（{why}）")

    def left(self, *, at: datetime, record: position_file.PositionRecord, reason: str) -> str:
        return messages.stop_today_left(at=at, record=record, reason=reason,
                                        lots=record.exited_lots - self._before.exited_lots)

    def unresolved(self, *, at: datetime, record: position_file.PositionRecord,
                   lookup: str | None) -> str:
        return messages.stop_today_entry_unresolved(at=at, record=record, lookup=lookup)

    def exit_uncertain(self, *, at: datetime, record: position_file.PositionRecord,
                       lookup: str) -> str:
        return messages.exit_uncertain(at=at, record=record, lookup=lookup)


def _capital(secrets: settings.Secrets) -> Broker:
    """真的群益 broker。延到這裡才 import：沒有群益元件的機器也要能載入這支工具。"""
    from broker.capital import CapitalBroker
    return CapitalBroker(secrets.user_id, secrets.password, secrets.futures_account)


def _say(text: str) -> bool:
    """印在主控台，也寫進當天的 log（`_console`）。"""
    logger.info(text)
    return True


@contextlib.contextmanager
def _console(today: date) -> Iterator[None]:
    """這一次執行的輸出：主控台只印訊息本身，`logs/stop-YYYYMMDD.log`（`paths.daily_log`，跟著
    `logs/` 30 天清除）另外記時間與來源。

    掛在 root logger 上：平倉時出場程式那一段的 log（「平倉成交」）也一起印出、寫進同一個檔。
    結束時拿掉，被測試或別的程式呼叫時不留下 handler、也不改 root 的層級。
    """
    os.makedirs(paths.logs_dir(), exist_ok=True)
    root = logging.getLogger()
    screen = logging.StreamHandler(sys.stdout)
    screen.setFormatter(logging.Formatter("%(message)s"))
    log_file = logging.FileHandler(paths.daily_log("stop", today), encoding="utf-8")
    log_file.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    level = root.level
    root.addHandler(screen)
    root.addHandler(log_file)
    root.setLevel(logging.INFO)
    try:
        yield
    finally:
        root.removeHandler(screen)
        root.removeHandler(log_file)
        log_file.close()
        root.setLevel(level)


if __name__ == "__main__":
    raise SystemExit(main())
