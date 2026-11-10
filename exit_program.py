"""出場程式：每個交易日 13:25 由排程叫醒一次，單獨負責 PTS 的出場（規格「兩支程式」一節）。

用法（在專案根目錄）：

    python exit_program.py

只照今天的部位檔平倉（之前的交易日沒平的只告警，見「給排程」），不看盤中程式還在不在
（規格第 49 條）：出場不能依賴盤中程式還活著，而且只有一個負責者，就不會兩邊都送平倉單、
反而開出反向新倉。

交易商品一律以部位檔記載的為準，不看設定檔（規格第 43 條）：

  * **微台**：市價平掉 PTS 自己的全部口數（ADR-0002）——部位檔裡還沒平的那幾口，不多也不少，
    帳上其他的部位（使用者自己的、OS 的）不碰（規格第 32 條）。每筆最多 10 口（第 33 條）。
    成交一筆就寫進部位檔的出場明細；出場口數等於進場口數就是**已了結**。
  * **大台、小台**：不送任何單、不登入。進場時標了當沖，券商 13:30 強平；結算日由交易所以
    最後結算價了結（規格第 30、35 條）。

另外檢查盤中程式今天有沒有跑完（心跳記錄，規格第 54 條）：今天沒跑、半路停了、或心跳記錄
讀不懂，就發一則 Discord。只告警、不影響出場——出場不依賴盤中程式。

任何不確定的情況都不猜，改發 Discord 請使用者處理：部位檔讀不懂、部位不確定、平倉單送出去
卻不知道成交（查過券商紀錄仍無法確認全部成交）、重試完仍有剩餘、13:45 之後才補跑、
結算日 13:30 近月停止交易時還有剩餘、設定檔有錯（`BrokenSettings`）。告警裡要人在幾點前
做什麼，照的是出場程式自己停止送單的同一個期限（`_deadline`）。
例行的平倉完成**不發訊息**（規格：只有「有入場」與「需要你動手」才發）。

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
"""

from __future__ import annotations

import enum
import logging
import os
import sys
import time as time_module
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Callable

import calendar_tw
import heartbeat
import housekeeping
import messages
import paths
import position_file
import settings
import state_files
import strategy
from broker import (BUY, DAY_TRADE_CODES, MARKET_ORDER_LOT_CAP, SELL, Broker, BrokerError,
                    FillUnknown, OrderFailed, OrderRequest)
from calendar_tw import DAY_SESSION_CLOSE

logger = logging.getLogger(__name__)

# 平倉的買賣別：多單賣出、空單買進
_EXIT_SIDE = {strategy.LONG: SELL, strategy.SHORT: BUY}

# 平倉單沒有全部成交時，每隔一分鐘重試、最多 3 次（規格第 34 條）。13:25 送出、
# 13:26／27／28 重試，13:30 券商當沖強平之前就結束（ADR-0002）。
_RETRIES = 3
_RETRY_INTERVAL = timedelta(minutes=1)

# 過了就不送平倉單。日盤收盤（`calendar_tw.DAY_SESSION_CLOSE`，規格第 55 條：之後才補跑就請
# 使用者在夜盤處理）；結算日的近月 13:30 就停止交易，還在的口數由交易所以最後結算價現金了結。
_SETTLEMENT_CLOSE = time(13, 30)


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
    """設定檔（或 `.env`）有錯時，`main()` 交給 `run()` 的東西，代替 `settings.Config`。

    使用者盤中改設定時打錯字（規格第 43 條預期盤中會改），出場程式不能就此不做事：
    帳上的微台沒人平就過夜，而平常沒有例行訊息能讓人發現。可是設定讀不懂，就不知道自動
    下單是不是開著——關掉就是不要程式碰下單 API（規格第 42 條）——所以一張單都不送、
    不登入（規格第 47 條：一啟動就擋下來），其餘照常：讀部位檔，有微台就告警「部位還在」
    並講明原因。`discord_enabled` 見 `_discord_switch_of_broken_settings`。
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
    say = lambda text: _send(config, notify, text)      # noqa: E731

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
        ending = _exit_today(broker, config, today=today, now=now, clock=clock, sleep=sleep,
                             say=say)
        _mark_done(today, ending.value)
    # 之前的交易日留下的排在今天的出場之後：這裡出了什麼事，都擋不到今天的微台平倉
    _report_missed(today, now, say)
    return _end(ending)


def _exit_today(broker: Broker, config: settings.Config | BrokenSettings, *, today: date,
                now: datetime, clock: Callable[[], datetime], sleep: Callable[[float], None],
                say: Callable[[str], None]) -> Ending:
    """今天的出場：進場截止之後才會走到這裡。"""
    _check_heartbeat(today, now, say)
    try:
        record = position_file.read(today)
    except position_file.Unreadable as exc:
        # 讀不懂就不知道帳上有沒有部位、有幾口、是哪個商品：照猜的口數平，平多了就是反向
        # 新倉；當成沒有部位，微台就過夜。檔案原封不動，留給人工處理。
        logger.error("部位檔讀不懂，不送任何單：%s", exc)
        say(messages.exit_position_file_unreadable(
            at=now, reason=str(exc), closed=now.time() >= DAY_SESSION_CLOSE))
        return Ending.POSITION_FILE_UNREADABLE
    if record is None:
        return Ending.NO_POSITION
    if record.status == position_file.UNCERTAIN:
        # 不知道帳上有幾口：照檔裡的口數平，可能留下沒平的口數，也可能多平開出反向新倉。
        # 出場單不確定時更不能再送——它很可能已經成交了（規格第 50 條）。
        # 這一條排在商品前面：大台、小台雖然不送單，不確定一樣要講。
        logger.error("部位檔記著一筆不確定的委託（%s），不送任何單", record.uncertain)
        say(messages.exit_uncertain(at=now, record=record, closed=now >= _deadline(record)))
        return Ending.UNCERTAIN
    if not record.open_lots:
        logger.info("部位檔的出場口數已經等於進場口數，部位已經了結")
        return Ending.ALREADY_CLOSED
    if record.product in DAY_TRADE_CODES:
        logger.info("今天的部位是 %s %s %d 口，標了當沖，交給券商 13:30 強平，不送單",
                    record.order_code, record.direction, record.open_lots)
        return Ending.LEFT_TO_BROKER

    ledger = _Ledger(record)
    ending = _MicroExit(broker, config, ledger, today=today, now=now, clock=clock,
                        sleep=sleep, say=say).run()
    if ledger.record != ledger.on_file:
        # 部位檔沒記到這次做的事。再執行一次出場程式，它會照舊檔把已經平掉的口數再平一次。
        logger.error("部位檔沒記到出場程式這次的平倉：實際還剩 %d 口（%s），檔裡還剩 %d 口（%s）",
                     ledger.record.open_lots, ledger.record.status, ledger.on_file.open_lots,
                     ledger.on_file.status)
        say(messages.exit_not_on_file(at=clock(), record=ledger.record, on_file=ledger.on_file,
                                      reason=ledger.error))
    return ending


def _report_missed(today: date, now: datetime, say: Callable[[str], None]) -> None:
    """之前的交易日還留著沒平的微台：出場程式那天沒有跑完，這一次才被叫醒（ticket 10）。

    排程「錯過就補跑」只補一次，而且是在電腦下一次能跑的時候：13:25 關機的話，補跑可能是
    隔天早上（進場截止之前）、隔天下午，或週末（非交易日）。只看「今天」的部位檔，三種都會
    安靜地結束，前一天的微台就這樣過夜（規格第 31 條）。

    **一張單都不送**：那天的日盤早就收了（規格第 55 條），使用者也可能已經自己平掉——出場程式
    不知道，再送就是反向新倉。只告警，請他確認帳戶。
    """
    for day in position_file.days():
        if day >= today or os.path.exists(done_path(day)):
            continue
        try:
            record = position_file.read(day)
        except position_file.Unreadable as exc:
            # 不知道那天留下什麼，照實講。今天的出場照常：一份舊檔不能讓今天的微台也過夜。
            logger.error("%s 的部位檔讀不懂，出場程式那天又沒有跑完：%s", day, exc)
            say(messages.exit_missed_unreadable(at=now, day=day, reason=str(exc)))
            record = None
        # 大台、小台那天 13:30 已經被券商當沖強平（規格第 30 條），跟出場程式有沒有跑無關
        if (record is not None and record.product not in DAY_TRADE_CODES
                and (record.open_lots or record.status == position_file.UNCERTAIN)):
            logger.error("%s 的部位檔還記著微台 %s %d 口沒平（%s）：出場程式那天沒有跑完，不送單",
                         day, record.direction, record.open_lots, record.status)
            say(messages.exit_missed(at=now, record=record))
        _mark_done(day, f"那天沒有跑完，{now:%Y-%m-%d %H:%M} 才發現")


def done_path(day: date) -> str:
    """那個交易日的出場記錄：出場程式已經處理過那一天的部位（ticket 10）。

    之後每一次執行都會往回看（`_report_missed`），看到它就知道那一天不必再講——部位檔永遠
    記著告警過的那幾口（使用者自己平掉，出場程式不會知道），沒有這份記錄就會每天再講一次。

    **做完才寫**：那天跑到一半當掉、被砍掉、斷電的話沒有這份記錄，下一次執行就會把那天
    當成沒跑完、照部位檔再講一次。寧可多講一次，不要安靜。
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


def _check_heartbeat(today: date, now: datetime, say: Callable[[str], None]) -> None:
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


def _deadline(record: position_file.PositionRecord) -> datetime:
    """微台最後能送平倉單的時刻：日盤收盤；結算日是近月停止交易的 13:30。
    出場程式過了它就不送單，告警也照它決定要人在日盤、夜盤處理，還是交給交易所了結。"""
    close = _SETTLEMENT_CLOSE if record.settles_today else DAY_SESSION_CLOSE
    return datetime.combine(record.trading_day, close)


class _Ledger:
    """出場程式這次的部位：`record` 是真實的（含這次的平倉），`on_file` 是部位檔裡的那一份。

    成交之後的下一件事就是寫檔（規格第 52 條）。寫不進去時**照樣繼續平倉**——微台不能過夜，
    而記憶體裡的口數是對的；兩份對不上就在最後告警（`run()`）。
    """

    def __init__(self, record: position_file.PositionRecord):
        self.record = record
        self.on_file = record
        self.error = ""

    def save(self, record: position_file.PositionRecord) -> None:
        self.record = record
        try:
            position_file.write(record)
        except Exception as exc:  # noqa: BLE001
            # 不管為什麼寫不進去（防毒鎖住、磁碟滿、權限），結果都一樣：帳上和檔裡對不上
            self.error = f"{type(exc).__name__}: {exc}"
            logger.error("部位檔寫不進去，繼續平倉：%s", self.error)
            return
        self.on_file = record


class _ExitUncertain(Exception):
    """一張平倉單送出去了，查詢卻沒能確認它全部成交。之後一張單都不送。`lookup` 是給人看的查詢結果。"""

    def __init__(self, lookup: str):
        super().__init__(lookup)
        self.lookup = lookup


class _MicroExit:
    """微台的平倉：拆單、重試、不確定時查券商紀錄（規格第 33、34、50 條）。"""

    def __init__(self, broker: Broker, config: settings.Config | BrokenSettings, ledger: _Ledger,
                 *, today: date, now: datetime, clock: Callable[[], datetime],
                 sleep: Callable[[float], None], say: Callable[[str], None]):
        self._broker = broker
        self._config = config
        self._ledger = ledger
        self._today = today
        self._started = now
        self._clock = clock
        self._sleep = sleep
        self._say = say
        self._settlement_day = ledger.record.settles_today
        self._deadline = _deadline(ledger.record)

    def run(self) -> Ending:
        """13:25 送出，沒有全部成交就每分鐘再送剩下的，最多重試 3 次。"""
        rounds = 0
        problem = ""
        while rounds <= _RETRIES:
            if rounds:
                self._sleep(_RETRY_INTERVAL.total_seconds())
            if self._clock() >= self._deadline:
                break
            if not rounds:
                blocked = self._blocked()
                if blocked:
                    return self._incomplete(blocked)
            try:
                problem = self._send_round()
            except _ExitUncertain as exc:
                record = self._ledger.record
                at = self._clock()
                logger.error("平倉委託不確定（%s），不再送任何單：%s", record.uncertain, exc.lookup)
                self._say(messages.exit_uncertain(at=at, record=record, lookup=exc.lookup,
                                                  closed=at >= self._deadline))
                return Ending.EXIT_UNCERTAIN
            rounds += 1
            if not self._ledger.record.open_lots:
                return Ending.CLOSED

        if rounds > _RETRIES:
            return self._incomplete(
                f"平倉單重試 {_RETRIES} 次都沒能送出（最後一次：{problem}）" if problem
                else f"市價 IOC 沒有全部成交，重試 {_RETRIES} 次仍有剩餘")
        # 過了期限：日盤收盤（之後只能在夜盤平），或結算日近月停止交易（之後由交易所以最後
        # 結算價現金了結，損益和回測有落差，規格第 35 條）。兩種都還剩口數，都要告警——
        # 結算日的「交易所會了結」是期交所規則，PTS 還沒有實機樣本，靠人在結算後確認。
        if self._settlement_day:
            close = f"近月 {_SETTLEMENT_CLOSE:%H:%M} 停止交易"
            closed = f"今天結算的近月 {_SETTLEMENT_CLOSE:%H:%M} 已經停止交易"
        else:
            close = f"日盤 {DAY_SESSION_CLOSE:%H:%M} 收盤"
            closed = f"日盤 {DAY_SESSION_CLOSE:%H:%M} 已經收盤"
        if rounds:
            reason = ((f"平倉單沒能送出（最後一次：{problem}），" if problem
                       else "市價 IOC 沒有全部成交，") + f"重試到{close}仍未完成")
        else:
            reason = f"出場程式 {self._started:%H:%M} 才執行，{closed}，沒有送出平倉單"
        self._incomplete(reason)
        return Ending.SETTLED_BY_EXCHANGE if self._settlement_day else Ending.NOT_CLOSED

    def _blocked(self) -> str:
        """第一次送單之前：有理由完全不送的話，回傳原因（給人看）。"""
        if isinstance(self._config, BrokenSettings):
            return f"設定檔有錯，出場程式不送單：{self._config.error}"
        if not self._config.auto_order_enabled:
            # 模擬模式不寫部位檔，所以這是今天進場之後才被關掉的。關掉就是不要程式碰下單 API
            # （規格第 42 條）——連登入都不做。但微台沒人平就過夜，一定要講。
            return "自動下單已關閉（order.auto_enabled: false），出場程式不送單"
        try:
            self._broker.login()
        except BrokerError as exc:
            # 密碼、憑證這類問題，一分鐘後再試也一樣；早點講，13:45 前還有時間讓人自己平
            return str(exc)
        return ""

    def _send_round(self) -> str:
        """送一輪平倉單：還沒平的口數拆成每筆最多 10 口，依序送出（規格第 33 條）。

        回傳這一輪「委託沒有送出去」的原因（都送出去了是空字串）。沒送出去就停下這一輪，
        一分鐘後剩下的整批重試；只成交一部分則照送下一筆。
        """
        lots = self._ledger.record.open_lots
        chunks = [MARKET_ORDER_LOT_CAP] * (lots // MARKET_ORDER_LOT_CAP)
        if lots % MARKET_ORDER_LOT_CAP:
            chunks.append(lots % MARKET_ORDER_LOT_CAP)
        for chunk in chunks:
            record = self._ledger.record
            try:
                result = self._broker.place_order(OrderRequest(
                    contract=record.contract, side=_EXIT_SIDE[record.direction], lots=chunk,
                    trading_day=self._today))
            except OrderFailed as exc:
                # 確定沒有送出去，也就確定沒有成交：重試是安全的
                logger.warning("平倉委託沒有送出去：%s", exc)
                return str(exc)
            except FillUnknown as exc:
                self._look_up(exc, chunk)
                continue
            if not result.filled_lots:
                logger.warning("平倉委託 %s 沒有成交（市價 IOC 當下沒有對手價）", result.order_seq)
                continue
            fill = position_file.Fill(result.order_seq, result.filled_lots, result.avg_price)
            logger.info("平倉成交：%s %d 口 @ %g（委託 %s）", record.order_code, fill.lots,
                        fill.price, fill.order_seq)
            self._ledger.save(replace(record, exits=record.exits + (fill,)))
        return ""

    def _look_up(self, exc: FillUnknown, lots: int) -> None:
        """平倉單送出去了，卻不知道成交幾口（規格第 50 條後半）。**絕不再送一次**：它很可能已經
        成交，再送就是反向新倉。改用這張單的委託序號向券商查成交紀錄（兩段查詢）。

        查到全部成交：記進出場明細、回到已確認，呼叫端照常送下一筆。其他結局（只成交一部分、
        查不到、查詢失敗、沒有序號可查）都拋 `_ExitUncertain`：帳上剩幾口已經不知道，
        還沒送的那幾筆也不送了。
        """
        seq = exc.order_seq
        logger.error("平倉委託送出去了，但不知道成交幾口（委託序號 %s）：%s", seq or "沒有", exc)
        record = self._ledger.record
        uncertain = position_file.UncertainOrder(
            stage=position_file.EXIT, order_seq=seq, requested_lots=lots,
            known_filled=exc.known_filled)
        # 先記下「不確定」再查：查詢至少要 5 秒，這段時間程式被關掉的話，重跑時看到的必須是
        # 「這筆出場單不確定」，而不是「這幾口還沒平」——後者會讓它再送一次。
        self._ledger.save(replace(record, status=position_file.UNCERTAIN, uncertain=uncertain))
        if not seq:
            raise _ExitUncertain("沒有委託序號，無法查詢")

        found = self._broker.query_fills(seq, self._today, lots)
        if found.lots == lots:
            logger.info("成交查詢：委託 %s 全部成交 %d 口 @ %g，視為已平倉", seq, lots,
                        found.avg_price)
            self._ledger.save(replace(record, exits=record.exits + (
                position_file.Fill(seq, lots, found.avg_price),)))
            return
        if found.lots:
            if found.lots > uncertain.known_filled:
                # 券商紀錄上的成交是確定的下限，記下來，人工對帳時少查一次
                self._ledger.save(replace(self._ledger.record, uncertain=replace(
                    uncertain, known_filled=found.lots)))
            raise _ExitUncertain(f"成交 {found.lots} 口（委託 {lots} 口）")
        if found.failure:
            raise _ExitUncertain(f"查詢失敗：{found.failure}")
        raise _ExitUncertain("查不到這筆委託的成交")

    def _incomplete(self, reason: str) -> Ending:
        """還有沒平的口數，而且出場程式不會再送了：告警「部位還在」，寫明還剩幾口。

        要人做什麼看的是**告警這一刻**過了期限沒有：還沒過就請他在期限前自己平；過了就是
        夜盤（一般日）或交易所了結（結算日）。和出場程式自己停止送單用的是同一個期限。
        """
        record = self._ledger.record
        at = self._clock()
        logger.error("微台平倉沒有完成，還剩 %d 口：%s", record.open_lots, reason)
        self._say(messages.exit_incomplete(at=at, record=record, reason=reason,
                                           closed=at >= self._deadline))
        return Ending.NOT_CLOSED


def _send(config: settings.Config | BrokenSettings, notify: Callable[[str], bool],
          text: str) -> None:
    """發一則 Discord。出場程式裡開關只在這裡把關（規格第 45 條）。"""
    if not config.discord_enabled:
        logger.info("Discord 已關閉，不發送")
        return
    notify(text)


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
        secrets = settings.load_secrets(config)
    except Exception as exc:  # noqa: BLE001
        # SettingsError、YAML 語法錯誤、檔案不見都一樣：這次的設定不能用（見 BrokenSettings）
        error = (str(exc) if isinstance(exc, settings.SettingsError)
                 else f"{type(exc).__name__}: {exc}")
        error = " ".join(error.split())          # YAML 的錯誤訊息有好幾行，告警裡只佔一行
        logger.error("設定有錯，出場程式不送單：%s", error)
        config = BrokenSettings(error=error, discord_enabled=_discord_switch_of_broken_settings())
        try:
            secrets = settings.read_secrets()
        except settings.SettingsError as missing:
            logger.error("也讀不到 .env，連告警都發不出去：%s", missing)
            return 1
        status = 1
    # 設定有錯時 broker 不會被用到（不登入），帳密可能是空的
    broker = connect(secrets)
    webhook = secrets.discord_webhook
    notify = lambda text: discord.send(text, webhook)        # noqa: E731
    try:
        run(broker, config, notify=notify, clock=clock, sleep=sleep)
    except Exception as exc:  # noqa: BLE001
        logger.exception("出場程式意外中止")
        now = clock()
        _send(config, notify, messages.exit_crashed(
            at=now, reason=f"{type(exc).__name__}: {exc}",
            closed=now.time() >= DAY_SESSION_CLOSE))
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
