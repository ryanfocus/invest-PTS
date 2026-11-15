"""出場程式：每個交易日 13:25 由排程叫醒一次，單獨負責 PTS 的出場（規格「兩支程式」一節）。

用法（在專案根目錄）：

    python exit_program.py

只照今天的部位檔平倉（之前的交易日沒平的只告警，見「給排程」），不看盤中程式還在不在
（規格第 49 條）：出場不能依賴盤中程式還活著，而且只有一個負責者，就不會兩邊都送平倉單、
反而開出反向新倉。

**要不要平、平什麼一律以部位檔為準，不看設定檔**（規格第 43 條：改設定最快下一個交易日才生效）。
部位檔只有真實下單會寫，所以有部位檔就是今天真的進場了：之後才把自動下單關掉、或把設定檔改壞，
今天的微台照樣平（設定檔讀不懂時另外告警，`BrokenSettings`）。帳密在 `.env`：

  * **微台**：市價平掉 PTS 自己的全部口數（ADR-0002）——部位檔裡還沒平的那幾口，不多也不少，
    帳上其他的部位（使用者自己的、OS 的）不碰（規格第 32 條）。每筆最多 10 口（第 33 條）。
    成交一筆就寫進部位檔的出場明細；出場口數等於進場口數就是**已了結**。
  * **大台、小台**：不送任何單、不登入。進場時標了當沖，券商 13:30 強平；結算日由交易所以
    最後結算價了結（規格第 30、35 條）。

另外檢查盤中程式今天有沒有跑完（心跳記錄，規格第 54 條）：今天沒跑、半路停了、或心跳記錄
讀不懂，就發一則 Discord。只告警、不影響出場——出場不依賴盤中程式。

任何不確定的情況都不猜，改發 Discord 請使用者處理：部位檔讀不懂、部位不確定、平倉單送出去
卻不知道成交（查過券商紀錄仍無法確認全部成交）、重試完仍有剩餘、13:45 之後才補跑、
結算日 13:30 近月停止交易時還有剩餘、設定檔有錯但已照部位檔平倉。告警裡要人在幾點前
做什麼，照的是出場程式自己停止送單的同一個期限（`handoff.deadline`）。
例行的平倉完成**不發訊息**（規格：只有「有入場」與「需要你動手」才發）。

## 和收工指令同一套平倉

平倉本身在 `closing`：拆單、重試、不確定時查券商紀錄、每筆成交寫進部位檔。使用者盤中執行
`tools/stop_today.py --close` 時用的也是它：在 13:24 之前平掉部位檔裡 PTS 的口數（大台、小台也平），
成交寫進同一份部位檔。13:25 這裡看到已經平完，就是「部位已經了結」，不送單。兩者不同的只有送單的
期限與做完之後怎麼講：收工指令不叫人自己平，剩下的交給這裡。

兩者不會同時碰部位檔：讀今天的部位檔之前先拿收工指令的鎖，收工指令還在跑就等它做完，最多 3 分鐘
（`_after_stop_command`）。它 13:24 前送出的最後一筆平倉單，13:25 可能還在等回報或在查成交。

## 時間

出場程式在啟動時讀一次系統時鐘（規格「時間」一節），用它判斷今天是哪一天、是不是已經
過了 13:45。重試前要等一分鐘，等完再看一次時鐘：日盤收盤（結算日是近月停止交易的 13:30）
之後不再送單。兩者都由 `run()` 的參數注入，測試才能把「13:25 送出、13:26 重試」放在劇本的
時間上。

## 給排程（ticket 10）

**不要設定失敗時自動重跑。** 告警之後使用者可能已經自己平掉剩下的口數；這時再跑一次，
出場程式照部位檔把那幾口再平一次，就是反向新倉（「部位還在」的告警也叫人自己平掉之後
不要再執行出場程式）。`main()` 只有在程式本身出錯、或設定有錯時回 1，告警過的結局也回 0。

排程叫醒它的入口是 `scheduled.py`，排程本身由 `tools/schedule.py` 建立。出場程式的排程每天都會
叫醒它，所以 `logs/` 的 30 天清除也由它在做完正事之後執行（`housekeeping`）。

**錯過就補跑，可能落在別天。** 13:25 電腦關機的話，補跑是下一次開機的時候：隔天早上、隔天
下午或週末，那時系統日期已經不是部位的那一天。所以每一次執行都先往回看（`_report_missed`）：
之前的交易日部位檔還記著沒平的微台、那天又沒有出場記錄（`done_path`），就告警、不送單。
**告警送不出去的那一次也不寫出場記錄**：13:25 網路斷了的話，平倉單送不出去、「部位還在」
也送不到，不寫的話下一次執行就不會再講，微台安靜地過夜。
"""

from __future__ import annotations

import contextlib
import enum
import logging
import os
import sys
import time as time_module
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable, Iterator

import calendar_tw
import closing
import handoff
import heartbeat
import housekeeping
import messages
import paths
import position_file
import settings
import state_files
import strategy
from broker import Broker
from closing import Result

logger = logging.getLogger(__name__)

# 收工指令還在跑時，最多等它多久、多久看一次（`_after_stop_command`）。13:25 起最多等到 13:28：
# 結算日的近月 13:30 就停止交易，再等下去出場程式就沒有用了。
_STOP_COMMAND_WAIT = timedelta(minutes=3)
_STOP_COMMAND_POLL = timedelta(seconds=1)


class Ending(enum.Enum):
    """出場程式為什麼結束。寫進 log，給人看的。"""

    NOT_A_TRADING_DAY = "非交易日，沒有登入"
    # 13:20 進場截止之前：盤中程式可能還在進場，平掉的口數它不知道，會被它覆寫掉
    TOO_EARLY = "進場截止之前被執行，什麼都沒做"
    POSITION_FILE_UNREADABLE = "部位檔讀不懂，沒有送單，已告警"
    NO_POSITION = "今天沒有部位"
    UNCERTAIN = "部位不確定，沒有送單，已告警"
    ALREADY_CLOSED = "部位已經了結"
    LEFT_TO_BROKER = "大台、小台交給券商 13:30 當沖強平"
    CLOSED = "微台平倉完成"
    SETTLED_BY_EXCHANGE = "結算日 13:30 近月停止交易時微台還沒平完，由交易所以最後結算價了結，已告警"
    NOT_CLOSED = "微台平倉沒有完成，已告警"
    EXIT_UNCERTAIN = "平倉委託不確定，不再送單，已告警"


@dataclass(frozen=True)
class ExitOutcome:
    ending: Ending


@dataclass(frozen=True)
class BrokenSettings:
    """設定檔讀不懂時，`main()` 交給 `run()` 的東西，代替 `settings.Config`。

    使用者盤中改設定時打錯字（規格第 43 條預期盤中會改），出場程式不能就此不做事：帳上的
    微台沒人平就過夜。出場程式本來就用不到設定檔裡的值——要不要平、平什麼只看部位檔，帳密在
    `.env`——所以照常平倉，另外告警設定檔有錯：不修好的話，下一個交易日盤中程式一啟動就會被
    擋下來（規格第 47 條）。`discord_enabled` 見 `_discord_switch_of_broken_settings`。
    """

    error: str
    discord_enabled: bool


def run(broker: Broker, config: settings.Config | BrokenSettings, *,
        notify: Callable[[str], bool],
        clock: Callable[[], datetime] = datetime.now,
        sleep: Callable[[float], None] = time_module.sleep) -> ExitOutcome:
    """13:25 的出場。`notify` 送出一則 Discord（測試換成記錄器），開關在這裡把關。
    `clock`／`sleep` 見模組說明「時間」。"""
    now = clock()
    today = now.date()
    alerts = _Alerts(config, notify)
    say = alerts.say

    if not calendar_tw.is_trading_day(today):
        logger.info("%s 不是交易日，不登入", today)
        ending = Ending.NOT_A_TRADING_DAY
    elif now.time() < strategy.ENTRY_CUTOFF:
        # 盤中程式還在進場：它下一筆成交會用自己記憶體裡的部位整份覆寫部位檔，這裡記下的
        # 出場明細就不見了，13:25 那一次再照舊檔平一次。會走到這裡的是有人手動執行，或排程
        # 補跑落在隔天早上——後者要處理的是前一天的部位，下面的 `_report_missed` 會講。
        logger.error("現在 %s，還沒到進場截止 %s，盤中程式可能還在進場，出場程式不動作",
                     f"{now:%H:%M}", f"{strategy.ENTRY_CUTOFF:%H:%M}")
        ending = Ending.TOO_EARLY
    else:
        with _after_stop_command(clock=clock, sleep=sleep):
            # 等過收工指令的話，開始處理今天部位的「現在」已經不是被叫醒的那一刻
            ending = _exit_today(broker, config, today=today, now=clock(), clock=clock,
                                 sleep=sleep, say=say)
            if alerts.all_sent:
                _mark_done(today, ending.value)
            else:
                # 告警沒送到，使用者就不知道今天的事（網路斷了的話，平倉多半也沒做成）。不寫出場
                # 記錄：下一次執行（補跑或下一個交易日）會把今天還在的微台再講一次（`_report_missed`）。
                logger.error("有告警沒有送出去，不寫今天的出場記錄：下一次執行會再講一次今天的部位")
    # 之前的交易日留下的排在今天的出場之後：這裡出了什麼事，都擋不到今天的微台平倉
    _report_missed(today, now, say)
    return _end(ending)


@contextlib.contextmanager
def _after_stop_command(*, clock: Callable[[], datetime],
                        sleep: Callable[[float], None]) -> Iterator[None]:
    """等收工指令（`tools/stop_today.py`）做完，才讓出場程式讀今天的部位檔；讀、平倉的時候拿著
    同一把鎖（`stop_today_lock_path`），兩者不會同時碰部位檔。

    收工指令 13:24 之前送出的最後一筆平倉單，13:25 可能還在等回報（30 秒）或在查成交：部位檔
    這時記著「這筆平倉不確定」。照它講，就是叫人自己平一筆一秒後就查到成交的單。

    **最多等 `_STOP_COMMAND_WAIT`**（3 分鐘），用注入的 `clock`／`sleep` 每秒看一次：收工指令卡住
    的話，再等下去結算日的近月 13:30 就停止交易了。等不到就照現在的部位檔做，記 log。沒有收工
    指令在跑時立刻拿到鎖，一秒都不等，出場程式的行為和以前一模一樣。
    """
    give_up = clock() + _STOP_COMMAND_WAIT
    waited = False
    while True:
        with state_files.exclusive(stop_today_lock_path()) as alone:
            if alone:
                if waited:
                    logger.info("收工指令做完了，照它留下的部位檔處理")
                yield
                return
        if clock() >= give_up:
            logger.error("等了 %d 分鐘收工指令都沒有做完（卡住了？），不再等，照現在的部位檔處理",
                         _STOP_COMMAND_WAIT.seconds // 60)
            yield
            return
        if not waited:
            waited = True
            logger.warning("收工指令正在跑（%s 被鎖住），等它做完再讀部位檔（最多 %d 分鐘）",
                           stop_today_lock_path(), _STOP_COMMAND_WAIT.seconds // 60)
        sleep(_STOP_COMMAND_POLL.total_seconds())


def _exit_today(broker: Broker, config: settings.Config | BrokenSettings, *, today: date,
                now: datetime, clock: Callable[[], datetime], sleep: Callable[[float], None],
                say: Callable[[str], bool]) -> Ending:
    """今天的出場：進場截止之後才會走到這裡。"""
    _check_heartbeat(today, now, say)
    try:
        record = position_file.read(today)
    except position_file.Unreadable as exc:
        # 讀不懂就不知道帳上有沒有部位、有幾口、是哪個商品：照猜的口數平，平多了就是反向
        # 新倉；當成沒有部位，微台就過夜。檔案原封不動，留給人工處理。
        logger.error("部位檔讀不懂，不送任何單：%s", exc)
        say(messages.exit_position_file_unreadable(at=now, reason=str(exc),
                                                   logs=handoff.fill_logs(today)))
        return Ending.POSITION_FILE_UNREADABLE
    if record is None:
        return Ending.NO_POSITION
    # 不確定的排在商品前面（`closing.close` 不送單、這裡照樣講）：大台、小台雖然不送單，
    # 不確定一樣要講
    if (handoff.left_to_broker(record.product) and record.uncertain is None
            and record.open_lots):
        logger.info("今天的部位是 %s %s %d 口，標了當沖，交給券商 13:30 強平，不送單",
                    record.order_code, record.direction, record.open_lots)
        return Ending.LEFT_TO_BROKER

    closed = closing.close(record, broker=lambda: broker, until=handoff.deadline(record),
                           clock=clock, sleep=sleep)
    ending = _report(closed, started=now, at=clock(), say=say)
    if isinstance(config, BrokenSettings) and closed.result in _WENT_TO_BROKER:
        say(messages.exit_settings_broken(at=clock(), error=config.error))
    if closed.file_behind:
        # 部位檔沒記到這次做的事。再執行一次出場程式，它會照舊檔把已經平掉的口數再平一次。
        logger.error("部位檔沒記到出場程式這次的平倉：實際還剩 %d 口（%s），檔裡還剩 %d 口（%s）",
                     closed.record.open_lots, closed.record.status, closed.on_file.open_lots,
                     closed.on_file.status)
        say(messages.exit_not_on_file(at=clock(), record=closed.record, on_file=closed.on_file,
                                      reason=closed.file_error))
    return ending


# 有登入、照部位檔處理了今天的微台（設定檔有錯時另外講一則，`exit_settings_broken`）
_WENT_TO_BROKER = frozenset({Result.CLOSED, Result.LEFT, Result.ENTRY_UNRESOLVED,
                             Result.EXIT_UNCERTAIN})


def _report(closed: closing.Closed, *, started: datetime, at: datetime,
            say: Callable[[str], bool]) -> Ending:
    """平倉的結果：例行的平完不發訊息；平不完、不確定就告警（`at` 這一刻要人做什麼，
    `handoff.decide`）。`started` 是出場程式開始處理今天部位的時刻。"""
    if closed.result is Result.NOTHING_OPEN:
        return Ending.ALREADY_CLOSED
    if closed.result is Result.CLOSED:
        return Ending.CLOSED
    if closed.result is Result.LEFT:
        say(messages.exit_incomplete(at=at, record=closed.record,
                                     reason=messages.exit_shortfall(closed, started=started)))
        if closed.short is closing.Short.OUT_OF_TIME and closed.record.settles_today:
            return Ending.SETTLED_BY_EXCHANGE
        return Ending.NOT_CLOSED
    say(messages.exit_uncertain(at=at, record=closed.record, lookup=closed.lookup))
    return (Ending.EXIT_UNCERTAIN if closed.result is Result.EXIT_UNCERTAIN
            else Ending.UNCERTAIN)


def _report_missed(today: date, now: datetime, say: Callable[[str], bool]) -> None:
    """之前的交易日還留著沒平的微台：出場程式那天沒有跑完，這一次才被叫醒（ticket 10）。

    排程「錯過就補跑」只補一次，而且是在電腦下一次能跑的時候：13:25 關機的話，補跑可能是
    隔天早上（進場截止之前）、隔天下午，或週末（非交易日）。只看「今天」的部位檔，三種都會
    安靜地結束，前一天的微台就這樣過夜（規格第 31 條）。

    **一張單都不送**：那天的日盤早就收了（規格第 55 條），使用者也可能已經自己平掉——出場程式
    不知道，再送就是反向新倉。只告警，請他確認帳戶。告警沒送出去就不寫那天的出場記錄：
    下一次執行再講一次。
    """
    for day in position_file.days():
        if day >= today or os.path.exists(done_path(day)):
            continue
        told = True
        try:
            record = position_file.read(day)
        except position_file.Unreadable as exc:
            # 不知道那天留下什麼，照實講。今天的出場照常：一份舊檔不能讓今天的微台也過夜。
            logger.error("%s 的部位檔讀不懂，出場程式那天又沒有跑完：%s", day, exc)
            told = say(messages.exit_missed_unreadable(at=now, day=day, reason=str(exc),
                                                       logs=handoff.fill_logs(day)))
            record = None
        # 大台、小台那天 13:30 已經被券商當沖強平（規格第 30 條），跟出場程式有沒有跑無關
        if (record is not None and not handoff.left_to_broker(record.product)
                and (record.open_lots or record.status == position_file.UNCERTAIN)):
            logger.error("%s 的部位檔還記著微台 %s %d 口沒平（%s）：出場程式那天沒有跑完，不送單",
                         day, record.direction, record.open_lots, record.status)
            told = say(messages.exit_missed(at=now, record=record))
        if told:
            _mark_done(day, f"那天沒有跑完，{now:%Y-%m-%d %H:%M} 才發現")


def stop_today_lock_path() -> str:
    """收工指令（`tools/stop_today.py`）的鎖 `state/stop_today.lock`：同一時間只有一個收工指令，
    13:25 的出場程式也拿同一把鎖，等收工指令做完才讀部位檔（`_after_stop_command`）。"""
    return os.path.join(paths.state_dir(), "stop_today.lock")


def done_path(day: date) -> str:
    """那個交易日的出場記錄：出場程式已經處理過那一天的部位（ticket 10）。

    之後每一次執行都會往回看（`_report_missed`），看到它就知道那一天不必再講——部位檔永遠
    記著告警過的那幾口（使用者自己平掉，出場程式不會知道），沒有這份記錄就會每天再講一次。

    **做完、而且告警都送出去了才寫**：那天跑到一半當掉、被砍掉、斷電，或網路斷了、告警沒送到
    的話沒有這份記錄，下一次執行就會把那天當成沒跑完、照部位檔再講一次。寧可多講一次，不要安靜。
    標記檔：存在就算數，內容只是給人看的（`state_files`）。
    """
    return state_files.daily("exit-done", day)


def _mark_done(day: date, how: str) -> None:
    """寫下出場記錄。寫不進去只記 log：代價是下一次執行再講一次那天的部位。"""
    path = done_path(day)
    try:
        state_files.write_marker(path, how)
    except Exception as exc:  # noqa: BLE001
        logger.error("出場記錄寫不進 %s（%s: %s）：下一次執行會再講一次 %s 的部位",
                     path, type(exc).__name__, exc, day)


def _check_heartbeat(today: date, now: datetime, say: Callable[[str], bool]) -> None:
    """盤中程式今天有沒有跑（規格第 54 條）。只告警、不擋出場：出場不依賴盤中程式（第 49 條），
    不論今天有沒有部位都要查——沒跑的那天正是不會有部位的那天。"""
    try:
        beat = heartbeat.read()
    except heartbeat.Unreadable as exc:
        # 當成「有跑」是安靜地漏掉，當成「沒跑」是在講不知道的事
        logger.error("心跳記錄讀不懂，不知道盤中程式今天有沒有跑完：%s", exc)
        say(messages.heartbeat_unreadable(at=now, reason=str(exc),
                                          log=paths.shown_log("intraday", today)))
        return
    if beat is None or beat.trading_day != today:
        logger.error("今天沒有盤中程式的心跳記錄（%s），盤中程式今天沒有跑", beat)
        say(messages.intraday_not_running(at=now, log=paths.shown_log("intraday", today)))
    elif not beat.finished:
        # 盤中程式 13:20 收工時會寫「收工了」。出場程式在進場截止之後才走到這裡，
        # 所以還沒收工就是半路停了，而且之後沒有再起來。
        logger.error("盤中程式最後的心跳是 %s，之後沒有再跑，也沒有收工", beat.last_alive)
        say(messages.intraday_not_running(at=now, log=paths.shown_log("intraday", today),
                                          stopped_at=beat.last_alive))
    else:
        logger.info("盤中程式今天跑完了（最後的心跳 %s）", beat.last_alive)


def _send(config: settings.Config | BrokenSettings, notify: Callable[[str], bool],
          text: str) -> bool:
    """發一則 Discord，回傳該講的有沒有講到。出場程式裡開關只在這裡把關（規格第 45 條）：
    關著就是使用者不要，算講到了。

    送不出去（回報失敗、或送的那一步拋例外）只記 log、不往外拋：出場程式還有微台要平。"""
    if not config.discord_enabled:
        logger.info("Discord 已關閉，不發送")
        return True
    try:
        return bool(notify(text))
    except Exception as exc:  # noqa: BLE001
        logger.error("Discord 發送時出錯（%s: %s）", type(exc).__name__, exc)
        return False


class _Alerts:
    """這一次執行的告警：每一則都經過 `say`，記下有沒有哪一則沒送出去（`all_sent`）。"""

    def __init__(self, config: settings.Config | BrokenSettings, notify: Callable[[str], bool]):
        self._config = config
        self._notify = notify
        self.all_sent = True

    def say(self, text: str) -> bool:
        sent = _send(self._config, self._notify, text)
        self.all_sent = self.all_sent and sent
        return sent


def _end(ending: Ending) -> ExitOutcome:
    logger.info("出場程式結束：%s", ending.value)
    return ExitOutcome(ending)


def main(*, connect: Callable[[settings.Secrets], Broker] | None = None,
         clock: Callable[[], datetime] = datetime.now,
         sleep: Callable[[float], None] = time_module.sleep) -> int:
    """排程的進入點（ticket 10 建排程）。

    **這裡是唯一建立真實 broker 與 Discord 連線的地方**：`run()` 不知道自己拿到的是
    真的還是假的。`connect` 用 `.env` 的帳密建立 broker（預設是群益），`clock`／`sleep` 見
    模組說明「時間」——測試換掉的就是這兩個接縫：broker 與時間。

    告警過的結局也回 0（理由見模組說明「給排程」）；回 1 代表程式本身出錯，或設定有錯——
    那時也盡量發一則告警，因為微台可能就這樣過夜，而平常沒有例行訊息能讓人發現。
    """
    _setup_logging(clock().date())
    try:
        return _scheduled_exit(connect or _capital, clock=clock, sleep=sleep)
    finally:
        # 收尾：log 保留 30 天（ticket 10）。出場程式的排程每天都會叫醒它，正事出錯也照清——
        # 個資的保存期限不該因為某天程式出錯就延長。永遠不拋例外。
        housekeeping.purge_old_logs()


def _capital(secrets: settings.Secrets) -> Broker:
    """真的群益 broker。延到這裡才 import：沒有群益元件的機器也要能載入這個模組。"""
    from broker.capital import CapitalBroker
    return CapitalBroker(secrets.user_id, secrets.password, secrets.futures_account)


def _scheduled_exit(connect: Callable[[settings.Secrets], Broker], *,
                    clock: Callable[[], datetime], sleep: Callable[[float], None]) -> int:
    from notifiers import discord

    status = 0
    try:
        config: settings.Config | BrokenSettings = settings.load()
    except Exception as exc:  # noqa: BLE001
        # SettingsError、YAML 語法錯誤、檔案不見都一樣：這次的設定不能用（見 BrokenSettings）
        error = (str(exc) if isinstance(exc, settings.SettingsError)
                 else f"{type(exc).__name__}: {exc}")
        error = " ".join(error.split())          # YAML 的錯誤訊息有好幾行，告警裡只佔一行
        logger.error("設定檔有錯，照部位檔平倉：%s", error)
        config = BrokenSettings(error=error, discord_enabled=_discord_switch_of_broken_settings())
        status = 1
    try:
        secrets = settings.load_secrets(config if isinstance(config, settings.Config) else None)
    except settings.SettingsError as missing:
        # 缺帳號、密碼的話登入會失敗（`LoginFailed`），有微台就告警「部位還在」；缺期貨帳號
        # 的話平倉單送不出去，一樣告警。缺 webhook 的話告警送不出去，今天就不算處理完。
        logger.error(".env 缺值：%s", missing)
        status = 1
        try:
            secrets = settings.read_secrets()
        except settings.SettingsError as absent:
            logger.error("讀不到 .env，登入、告警都做不了：%s", absent)
            return 1
    broker = connect(secrets)
    webhook = secrets.discord_webhook
    notify = lambda text: discord.send(text, webhook)        # noqa: E731
    try:
        run(broker, config, notify=notify, clock=clock, sleep=sleep)
    except Exception as exc:  # noqa: BLE001
        logger.exception("出場程式意外中止")
        now = clock()
        _send(config, notify, messages.exit_crashed(
            at=now, reason=f"{type(exc).__name__}: {exc}", logs=handoff.fill_logs(now.date())))
        return 1
    return status


def _discord_switch_of_broken_settings() -> bool:
    """設定檔有錯時要不要發 Discord：開關本身讀得到、而且明白寫著 false，才不發（規格第 45 條，
    使用者自己關掉的）。開關本身也讀不到（YAML 壞掉、鍵名打錯）就當成開著：不發的話，
    微台就安靜地過夜。"""
    try:
        return settings.read_raw()["discord"]["enabled"] is not False
    except Exception:  # noqa: BLE001
        return True


def _setup_logging(today: date) -> None:
    """同時寫到畫面與 `logs/exit-YYYYMMDD.log`（`paths.daily_log`，跟著 `logs/` 30 天清除，
    ticket 10）。log 檔延到第一次寫入才打開，理由同 `intraday._setup_logging`。"""
    os.makedirs(paths.logs_dir(), exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr),
                  logging.FileHandler(paths.daily_log("exit", today), encoding="utf-8",
                                      delay=True)],
    )


if __name__ == "__main__":
    raise SystemExit(main())
