"""平倉：平掉部位檔裡 PTS 自己還沒平的口數，送到給定的期限為止（規格第 32～34、50 條）。

13:25 的出場程式拿它平微台（ADR-0002），收工指令（`tools/stop_today.py --close`）拿它在 13:24 之前
平任何商品：大台、小台的平倉單照樣標當沖（`OrderRequest.day_trade` 依商品決定）。兩者不同的只有
送單的期限，以及做完之後怎麼講——這裡**不講話**，只回傳發生了什麼（`Closed`），由呼叫端照
`handoff` 的答案講。

  * **要不要平只看部位檔**：沒有還沒平的口數就不登入；記著一筆查不了的「不確定」也不登入、
    一張單都不送（規格第 50 條，`handoff.exit_looks_up`）——不知道帳上有幾口，照檔裡的口數平，
    可能留下沒平的，也可能多平開出反向新倉；平倉單不確定時更不能再送，它很可能已經成交了。
  * 盤中留下一筆有委託序號的微台進場不確定時，登入之後**先查**券商紀錄，確定了才平。
  * 每筆最多 10 口（第 33 條），沒有全部成交就每分鐘再送剩下的，最多重試 3 次（第 34 條）。
  * 平倉單送出去卻不知道成交幾口：用委託序號查，**絕不再送一次**。
  * 每一筆成交的下一件事就是寫部位檔（第 52 條）；寫不進去照樣平完，帳上與檔裡各是多少都在
    `Closed` 裡。
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

import handoff
import position_file
import strategy
from broker import (BUY, MARKET_ORDER_LOT_CAP, SELL, Broker, BrokerError, FillUnknown,
                    LoginUnavailable, OrderFailed, OrderRequest)

logger = logging.getLogger(__name__)

# 平倉的買賣別：多單賣出、空單買進
_EXIT_SIDE = {strategy.LONG: SELL, strategy.SHORT: BUY}

# 平倉單沒有全部成交時，每隔一分鐘重試、最多 3 次（規格第 34 條）。13:25 送出、
# 13:26／27／28 重試，13:30 券商當沖強平之前就結束（ADR-0002）。
RETRIES = 3
_RETRY_INTERVAL = timedelta(minutes=1)


class Result(enum.Enum):
    """平倉怎麼結束的。"""

    NOTHING_OPEN = "部位檔裡沒有還沒平的口數，沒有登入"
    # 部位檔早就記著一筆查不了的「不確定」：沒有登入，一張單都沒送
    ON_FILE_UNCERTAIN = "部位檔記著一筆不確定的委託，沒有送單"
    CLOSED = "平完了"
    # 還有沒平的口數，而且不會再送了（為什麼：`Closed.short`）
    LEFT = "還有沒平的口數"
    # 盤中留下的進場不確定，沒能向券商確認全部成交：一張單都沒送
    ENTRY_UNRESOLVED = "盤中留下的進場不確定沒能確認，沒有送單"
    # 這一次送的平倉單不知道成交幾口，查了也沒能確認：之後一張都不送
    EXIT_UNCERTAIN = "平倉委託不確定，不再送單"


class Short(enum.Enum):
    """`Result.LEFT` 的原因。"""

    LOGIN_FAILED = "登入失敗（帳密、憑證），一分鐘後再試也一樣"
    RETRIES_USED = "重試 3 次仍有剩餘"
    OUT_OF_TIME = "過了送單的期限"


@dataclass(frozen=True)
class Closed:
    """`close` 做完之後。

    `record` 是帳上的（含這次的平倉），`on_file` 是部位檔裡的那一份：兩者不同就是部位檔沒記到
    （原因在 `file_error`），再執行一次出場程式或收工指令就會照舊檔再平一次。
    """

    result: Result
    record: position_file.PositionRecord
    on_file: position_file.PositionRecord
    file_error: str = ""
    # 這一次平掉的口數
    closed_lots: int = 0
    # `ON_FILE_UNCERTAIN` 以外的不確定：向券商查到的結果（給人看的）；沒能查就是 None
    lookup: str | None = None
    # `LEFT`：為什麼。`problem` 是最後一輪沒送出去（或登入失敗）的原因，空字串是送出去了、
    # 市價 IOC 沒有全部成交；`rounds` 是送過幾輪
    short: Short | None = None
    rounds: int = 0
    problem: str = ""

    @property
    def file_behind(self) -> bool:
        """部位檔落後到下一個讀它的程式會做錯事（`position_file.behind`）。"""
        return position_file.behind(self.record, self.on_file)


def close(record: position_file.PositionRecord, *, broker: Callable[[], Broker],
          until: datetime, clock: Callable[[], datetime],
          sleep: Callable[[float], None]) -> Closed:
    """平掉 `record`（今天的部位檔）裡還沒平的口數，送到 `until` 為止。

    `broker` 在真的要登入時才呼叫：沒有要平的、或部位檔記著查不了的「不確定」，就不必建立連線
    （收工指令讀不到帳密也能講清楚）。它拋出的例外原封不動往外拋。
    """
    if record.status == position_file.UNCERTAIN and not handoff.exit_looks_up(record):
        logger.error("部位檔記著一筆不確定的委託（%s），不送任何單", record.uncertain)
        return Closed(Result.ON_FILE_UNCERTAIN, record, record)
    if record.uncertain is None and not record.open_lots:
        logger.info("部位檔的出場口數已經等於進場口數，部位已經了結")
        return Closed(Result.NOTHING_OPEN, record, record)
    return _Closing(broker(), record, until=until, clock=clock, sleep=sleep).run()


class _Uncertain(Exception):
    """一筆委託送出去了（或盤中留下的），查詢卻沒能確認它全部成交。`lookup` 是給人看的查詢結果。"""

    def __init__(self, lookup: str):
        super().__init__(lookup)
        self.lookup = lookup


class _Closing:
    def __init__(self, broker: Broker, record: position_file.PositionRecord, *,
                 until: datetime, clock: Callable[[], datetime],
                 sleep: Callable[[float], None]):
        self._broker = broker
        self._before = record
        self._ledger = position_file.Ledger(record)
        self._today: date = record.trading_day
        self._until = until
        self._clock = clock
        self._sleep = sleep

    def run(self) -> Closed:
        """送出，沒有全部成交就每分鐘再送剩下的，最多重試 3 次。

        登入也算在一輪裡：網路、群益主機的問題（`LoginUnavailable`，13:25 剛從睡眠醒來、網路
        還沒好就是這樣）時一張平倉單都還沒送出去，帳上確定沒有異動，和「平倉單沒送出去」一樣
        一分鐘後再登入、再試（規格第 34 條）。帳密、憑證這類登入失敗一分鐘後再試也一樣，立刻結束。

        部位檔記著盤中程式留下的進場不確定時（`handoff.exit_looks_up`），登入之後先查券商紀錄，
        確定了才送平倉單；確定不了就一張都不送。
        """
        rounds = 0
        problem = ""
        logged_in = False
        while rounds <= RETRIES:
            if rounds:
                self._sleep(_RETRY_INTERVAL.total_seconds())
            if self._clock() >= self._until:
                break
            if not logged_in:
                try:
                    self._broker.login()
                except LoginUnavailable as exc:
                    logger.warning("登入沒有成功（網路或群益主機），一分鐘後再試：%s", exc)
                    problem = str(exc)
                    rounds += 1
                    continue
                except BrokerError as exc:
                    # 密碼、憑證這類問題，一分鐘後再試也一樣；早點講，期限前還有時間讓人處理
                    if self._ledger.record.uncertain is not None:
                        return self._unresolved(f"沒能查詢（{exc}）")
                    return self._left(Short.LOGIN_FAILED, rounds, str(exc))
                logged_in = True
            if self._ledger.record.uncertain is not None:
                # 盤中留下的進場不確定（`handoff.exit_looks_up`）：確定了才平
                try:
                    self._resolve()
                except _Uncertain as exc:
                    return self._unresolved(exc.lookup)
            try:
                problem = self._send_round()
            except _Uncertain as exc:
                logger.error("平倉委託不確定（%s），不再送任何單：%s",
                             self._ledger.record.uncertain, exc.lookup)
                return self._end(Result.EXIT_UNCERTAIN, lookup=exc.lookup)
            rounds += 1
            if not self._ledger.record.open_lots:
                return self._end(Result.CLOSED)

        if self._ledger.record.uncertain is not None:
            # 進場不確定還沒查到：登入一直沒成功，或已經過了期限（太晚才執行）
            return self._unresolved(f"沒能查詢（登入沒有成功，最後一次：{problem}）" if problem
                                    else None)
        if rounds > RETRIES:
            return self._left(Short.RETRIES_USED, rounds, problem)
        return self._left(Short.OUT_OF_TIME, rounds, problem)

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
                    trading_day=self._today, deadline=self._until))
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
            self._ledger.save(record.closed_by(fill))
        return ""

    def _look_up(self, exc: FillUnknown, lots: int) -> None:
        """平倉單送出去了，卻不知道成交幾口（規格第 50 條後半）。**絕不再送一次**：它很可能已經
        成交，再送就是反向新倉。改用這張單的委託序號向券商查（`_resolve`）；確定不了就拋
        `_Uncertain`：帳上剩幾口已經不知道，還沒送的那幾筆也不送了。
        """
        seq = exc.order_seq
        logger.error("平倉委託送出去了，但不知道成交幾口（委託序號 %s）：%s", seq or "沒有", exc)
        # 先記下「不確定」再查：查詢至少要 5 秒，這段時間程式被關掉的話，重跑時看到的必須是
        # 「這筆出場單不確定」，而不是「這幾口還沒平」——後者會讓它再送一次。
        self._ledger.save(self._ledger.record.unsure(position_file.EXIT, seq, lots,
                                                     exc.known_filled))
        if not seq:
            raise _Uncertain("沒有委託序號，無法查詢")
        self._resolve()

    def _resolve(self) -> None:
        """部位檔記著的那一筆不確定的委託——剛送出的平倉單，或盤中留下的進場單（規格第 50 條）——
        用它的委託序號查券商紀錄（兩段查詢）。

        查到全部成交：記進明細、回到已確認，照常往下做——盤中那則告警請使用者等到 13:25、
        不要自己平，靠的就是這一步。其他結局都拋 `_Uncertain`。**只成交一部分也不算確定**：查詢
        萬一漏了一列，照少算的口數平，微台就過夜。查到的口數是確定的下限，記進部位檔，人工對帳時
        少查一次。查不到不是 0 口（`broker.FillQuery`）。
        """
        record = self._ledger.record
        u = record.uncertain
        found = self._broker.query_fills(u.order_seq, self._today, u.requested_lots)
        if found.lots == u.requested_lots:
            settled = record.settled(position_file.Fill(u.order_seq, found.lots, found.avg_price))
            # 和送單當下就知道成交的那種寫成同一個字：告警叫人從 log 算口數時找的就是它們
            # （`messages._count_from_logs`）
            if u.stage == position_file.EXIT:
                logger.info("平倉成交（成交查詢確認）：%s %d 口 @ %g（委託 %s）",
                            record.order_code, found.lots, found.avg_price, u.order_seq)
            else:
                logger.info("進場成交（成交查詢確認）：%s %d 口 @ %g（委託 %s），累計 %d 口",
                            record.direction, found.lots, found.avg_price, u.order_seq,
                            settled.entered_lots)
            self._ledger.save(settled)
            return
        if found.lots:
            self._ledger.save(record.at_least(found.lots))
            raise _Uncertain(f"成交 {found.lots} 口（委託 {u.requested_lots} 口）")
        if found.failure:
            raise _Uncertain(f"查詢失敗：{found.failure}")
        raise _Uncertain("查不到這筆委託的成交")

    def _unresolved(self, lookup: str | None) -> Closed:
        """進場不確定沒能確認：一張單都不送（規格第 50 條）。"""
        logger.error("部位檔記著一筆不確定的進場委託（%s），沒能確認，不送任何單：%s",
                     self._ledger.record.uncertain, lookup)
        return self._end(Result.ENTRY_UNRESOLVED, lookup=lookup)

    def _left(self, short: Short, rounds: int, problem: str) -> Closed:
        """還有沒平的口數，而且不會再送了。"""
        record = self._ledger.record
        logger.error("%s 平倉沒有完成，還剩 %d 口：%s（%s）", record.order_code, record.open_lots,
                     short.value, problem or "市價 IOC 沒有全部成交")
        return self._end(Result.LEFT, short=short, rounds=rounds, problem=problem)

    def _end(self, result: Result, **details) -> Closed:
        ledger = self._ledger
        return Closed(result, ledger.record, ledger.on_file, ledger.error,
                      closed_lots=ledger.record.exited_lots - self._before.exited_lots,
                      **details)
