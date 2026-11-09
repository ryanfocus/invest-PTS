"""出場程式：每個交易日 13:25 由排程叫醒一次，單獨負責 PTS 的出場（規格「兩支程式」一節）。

用法（在專案根目錄）：

    python exit_program.py

只看今天的部位檔，不看盤中程式還在不在（規格第 49 條）：出場不能依賴盤中程式還活著，
而且只有一個負責者，就不會兩邊都送平倉單、反而開出反向新倉。

交易商品一律以部位檔記載的為準，不看設定檔（規格第 43 條）：

  * **微台**：市價平掉 PTS 自己的全部口數（ADR-0002）——部位檔裡還沒平的那幾口，不多也不少，
    帳上其他的部位（使用者自己的、OS 的）不碰（規格第 32 條）。每筆最多 10 口（第 33 條）。
    成交一筆就寫進部位檔的出場明細；出場口數等於進場口數就是**已了結**。
  * **大台、小台**：不送任何單、不登入。進場時標了當沖，券商 13:30 強平；結算日由交易所以
    最後結算價了結（規格第 30、35 條）。

任何不確定的情況都不猜，改發 Discord 請使用者處理：部位檔讀不懂、部位不確定、平倉單送出去
卻不知道成交（查過券商紀錄仍無法確認全部成交）、重試完仍有剩餘、13:45 之後才補跑。
例行的平倉完成**不發訊息**（規格：只有「有入場」與「需要你動手」才發）。

## 時間

出場程式在啟動時讀一次系統時鐘（規格「時間」一節），用它判斷今天是哪一天、是不是已經
過了 13:45。重試前要等一分鐘，等完再看一次時鐘：日盤收盤（結算日是近月停止交易的 13:30）
之後不再送單。兩者都由 `run()` 的參數注入，測試才能把「13:25 送出、13:26 重試」放在劇本的
時間上。

## 給排程（ticket 10）

**不要設定失敗時自動重跑。** 告警之後使用者可能已經自己平掉剩下的口數；這時再跑一次，
出場程式照部位檔把那幾口再平一次，就是反向新倉。`main()` 只有在程式本身出錯時回 1，
告警過的結局也回 0。
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
import messages
import paths
import position_file
import settings
import strategy
from broker import (BUY, MARKET_ORDER_LOT_CAP, SELL, TMF_CODE, Broker, BrokerError,
                    FillUnknown, OrderFailed, OrderRequest)

logger = logging.getLogger(__name__)

# 平倉的買賣別：多單賣出、空單買進
_EXIT_SIDE = {strategy.LONG: SELL, strategy.SHORT: BUY}

# 平倉單沒有全部成交時，每隔一分鐘重試、最多 3 次（規格第 34 條）。13:25 送出、
# 13:26／27／28 重試，13:30 券商當沖強平之前就結束（ADR-0002）。
_RETRIES = 3
_RETRY_INTERVAL = timedelta(minutes=1)

# 過了就不送平倉單。日盤 13:45 收盤（規格第 55 條：之後才補跑就請使用者在夜盤處理）；
# 結算日的近月 13:30 就停止交易，還在的口數由交易所以最後結算價現金了結。
_DAY_SESSION_CLOSE = time(13, 45)
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
    SETTLED_BY_EXCHANGE = "結算日 13:30 之後，微台由交易所以最後結算價了結"
    NOT_CLOSED = "微台平倉沒有完成，已告警"
    EXIT_UNCERTAIN = "平倉委託不確定，不再送單，已告警"


@dataclass(frozen=True)
class ExitOutcome:
    ending: Ending


def run(broker: Broker, config: settings.Config, *, notify: Callable[[str], bool],
        clock: Callable[[], datetime] = datetime.now,
        sleep: Callable[[float], None] = time_module.sleep) -> ExitOutcome:
    """13:25 的出場。`notify` 送出一則 Discord（測試換成記錄器），開關在這裡把關。
    `clock`／`sleep` 見模組說明「時間」。"""
    now = clock()
    today = now.date()
    say = lambda text: _send(config, notify, text)      # noqa: E731

    if not calendar_tw.is_trading_day(today):
        logger.info("%s 不是交易日，不登入", today)
        return _end(Ending.NOT_A_TRADING_DAY)
    if now.time() < strategy.ENTRY_CUTOFF:
        # 盤中程式還在進場：它下一筆成交會用自己記憶體裡的部位整份覆寫部位檔，這裡記下的
        # 出場明細就不見了，13:25 那一次再照舊檔平一次。只會是有人手動執行。
        logger.error("現在 %s，還沒到進場截止 %s，盤中程式可能還在進場，出場程式不動作",
                     f"{now:%H:%M}", f"{strategy.ENTRY_CUTOFF:%H:%M}")
        return _end(Ending.TOO_EARLY)

    try:
        record = position_file.read(today)
    except position_file.Unreadable as exc:
        # 讀不懂就不知道帳上有沒有部位、有幾口、是哪個商品：照猜的口數平，平多了就是反向
        # 新倉；當成沒有部位，微台就過夜。檔案原封不動，留給人工處理。
        logger.error("部位檔讀不懂，不送任何單：%s", exc)
        say(messages.exit_position_file_unreadable(at=now, reason=str(exc)))
        return _end(Ending.POSITION_FILE_UNREADABLE)
    if record is None:
        return _end(Ending.NO_POSITION)
    if record.status == position_file.UNCERTAIN:
        # 不知道帳上有幾口：照檔裡的口數平，可能留下沒平的口數，也可能多平開出反向新倉。
        # 出場單不確定時更不能再送——它很可能已經成交了（規格第 50 條）。
        # 這一條排在商品前面：大台、小台雖然不送單，不確定一樣要講。
        logger.error("部位檔記著一筆不確定的委託（%s），不送任何單", record.uncertain)
        say(messages.exit_uncertain(at=now, record=record))
        return _end(Ending.UNCERTAIN)
    if not record.open_lots:
        logger.info("部位檔的出場口數已經等於進場口數，部位已經了結")
        return _end(Ending.ALREADY_CLOSED)
    if record.product != TMF_CODE:
        logger.info("今天的部位是 %s %s %d 口，標了當沖，交給券商 13:30 強平，不送單",
                    record.order_code, record.direction, record.open_lots)
        return _end(Ending.LEFT_TO_BROKER)

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
    return _end(ending)


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

    def __init__(self, broker: Broker, config: settings.Config, ledger: _Ledger, *, today: date,
                 now: datetime, clock: Callable[[], datetime],
                 sleep: Callable[[float], None], say: Callable[[str], None]):
        self._broker = broker
        self._config = config
        self._ledger = ledger
        self._today = today
        self._started = now
        self._clock = clock
        self._sleep = sleep
        self._say = say
        settlement_day = ledger.record.last_trading_day == today
        self._settlement_day = settlement_day
        self._deadline = datetime.combine(
            today, _SETTLEMENT_CLOSE if settlement_day else _DAY_SESSION_CLOSE)

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
                logger.error("平倉委託不確定（%s），不再送任何單：%s", record.uncertain, exc.lookup)
                self._say(messages.exit_uncertain(at=self._clock(), record=record,
                                                  lookup=exc.lookup))
                return Ending.EXIT_UNCERTAIN
            rounds += 1
            if not self._ledger.record.open_lots:
                return Ending.CLOSED

        if rounds > _RETRIES:
            return self._incomplete(
                f"委託沒有送出去，重試 {_RETRIES} 次仍然失敗（最後一次：{problem}）" if problem
                else f"市價 IOC 沒有全部成交，重試 {_RETRIES} 次仍有剩餘")
        if self._settlement_day:
            # 近月已經停止交易，還在的口數由交易所以最後結算價現金了結：不會過夜，也沒有人
            # 能再做什麼，所以不告警。損益會和回測有落差（規格第 35 條），記進 log。
            logger.warning("結算日 %s 之後近月已停止交易，還剩 %d 口微台由交易所以最後結算價了結",
                           f"{_SETTLEMENT_CLOSE:%H:%M}", self._ledger.record.open_lots)
            return Ending.SETTLED_BY_EXCHANGE
        if rounds:
            return self._incomplete(
                (f"委託沒有送出去（最後一次：{problem}），" if problem
                 else "市價 IOC 沒有全部成交，")
                + f"重試到日盤 {_DAY_SESSION_CLOSE:%H:%M} 收盤仍未完成", night=True)
        return self._incomplete(
            f"出場程式 {self._started:%H:%M} 才執行，日盤 {_DAY_SESSION_CLOSE:%H:%M} 已經收盤，"
            "沒有送出平倉單", night=True)

    def _blocked(self) -> str:
        """第一次送單之前：有理由完全不送的話，回傳原因（給人看）。"""
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

    def _incomplete(self, reason: str, *, night: bool = False) -> Ending:
        """還有沒平的口數，而且出場程式不會再送了：告警「部位還在」，寫明還剩幾口。"""
        record = self._ledger.record
        logger.error("微台平倉沒有完成，還剩 %d 口：%s", record.open_lots, reason)
        self._say(messages.exit_incomplete(at=self._clock(), record=record, reason=reason,
                                           night=night))
        return Ending.NOT_CLOSED


def _send(config: settings.Config, notify: Callable[[str], bool], text: str) -> None:
    """發一則 Discord。出場程式裡開關只在這裡把關（規格第 45 條）。"""
    if not config.discord_enabled:
        logger.info("Discord 已關閉，不發送")
        return
    notify(text)


def _end(ending: Ending) -> ExitOutcome:
    logger.info("出場程式結束：%s", ending.value)
    return ExitOutcome(ending)


def main() -> int:
    """排程的進入點（ticket 10 建排程）。

    **這裡是唯一建立真實 broker 與 Discord 連線的地方**：`run()` 不知道自己拿到的是
    真的還是假的。告警過的結局也回 0（理由見模組說明「給排程」）；回 1 只代表程式本身
    出錯——那時也盡量發一則告警，因為微台可能就這樣過夜，而平常沒有例行訊息能讓人發現。
    """
    from broker.capital import CapitalBroker
    from notifiers import discord

    _setup_logging(date.today())
    config = settings.load()
    secrets = settings.load_secrets(config)
    broker = CapitalBroker(secrets.user_id, secrets.password, secrets.futures_account)
    webhook = secrets.discord_webhook
    notify = lambda text: discord.send(text, webhook)        # noqa: E731
    try:
        run(broker, config, notify=notify)
    except Exception as exc:  # noqa: BLE001
        logger.exception("出場程式意外中止")
        _send(config, notify, messages.exit_crashed(at=datetime.now(),
                                                    reason=f"{type(exc).__name__}: {exc}"))
        return 1
    return 0


def _setup_logging(today: date) -> None:
    """同時寫到畫面與 `logs/exit-YYYYMMDD.log`（跟著 `logs/` 30 天清除，ticket 10）。"""
    os.makedirs(paths.logs_dir(), exist_ok=True)
    log_file = os.path.join(paths.logs_dir(), f"exit-{today:%Y%m%d}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr),
                  logging.FileHandler(log_file, encoding="utf-8")],
    )


if __name__ == "__main__":
    raise SystemExit(main())
