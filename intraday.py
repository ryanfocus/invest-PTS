"""盤中程式：每個交易日 08:40 啟動，收大台行情、組 1 分 K、存原始行情、判斷進場，13:20 結束。

用法（在專案根目錄）：

    python intraday.py

每一根 1 分 K 結束時交給策略判斷（`strategy.decide`）。條件成立時：

  * 自動下單打開：送出市價 IOC → 把實際成交寫進部位檔 → 發一則成交訊息（ticket 06）。
  * 自動下單關閉（模擬模式）：在交易商品當下的價格模擬成交，發一則標示「模擬」的訊息，
    **完全不呼叫下單 API**、不寫部位檔（ticket 04）。

條件 3、4 與突破進場的盯價（ticket 05）接在同樣的地方。

## 時間

盤中程式的「現在」**一律取自事件上的時間**，不讀系統時鐘（規格「時間」一節）。
唯一的例外是「今天是哪一天」：它要在登入之前就知道（非交易日不登入），那時還沒有任何
事件，所以由 `main()` 在啟動時讀一次系統日期，當參數傳進來。

事件上的時間有兩種來源，這裡刻意分開用：

  * **成交（TxTrade）帶的是交易所的時間**。K 棒的分界只看它：K 棒要和看盤軟體一致，
    而本機時鐘和交易所差個一兩秒，就會把每一分鐘最後幾秒的成交切到下一根。
  * **時鐘（Clock）與累計筆數帶的是本機收到的時刻**。13:20 收工看它們，因為沒有成交的
    時候也要收工（臨時休市那天正是一筆今天的成交都沒有）。

兩種時間**都要看**的地方是判斷：K 棒結束時，讓它結束的那筆成交如果比本機時鐘晚了
`_MAX_LAG` 以上，那一根照樣組起來（開盤價、RSI 要用），但不判斷。晚啟動或重啟時，群益
第一次訂閱會先回補當天已經過去的成交，時間是交易所當時的時間；只看成交自己的時間，
09:30 才啟動的話 08:49 起的每一根都會被當成「剛結束」，在現在的價格補進場——實機下單
之後就是對過去的分鐘再送一次單。報價延遲送到的成交也是同一回事（規格第 53 條）。
"""

from __future__ import annotations

import enum
import logging
import os
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Callable

import calendar_tw
import indicators
import messages
import paths
import position_file
import settings
import strategy
from broker import (BUY, SELL, Broker, BrokerError, Clock, ContractInfo, OrderRequest,
                    ProductTrade, TxTrade, TxTradeCounts)
from raw_market import RawMarketWriter

logger = logging.getLogger(__name__)

# 13:20 進場截止，盤中程式在那一刻收工（規格「兩支程式」一節）。13:19 那根到這時結束，
# 照樣交出來；之後的事件不組 K 棒、也不寫進原始行情。時刻是策略常數，定義在策略判斷。
SESSION_END = strategy.ENTRY_CUTOFF

# 成交最多可以比本機時鐘晚多久，還算是即時收到的。要大過本機時鐘和交易所的誤差
# （一兩秒，久沒對時可能更多）加上成交時間只到秒、時鐘每秒才一個；要小過重啟所需的時間
# （排程最快一分鐘後重啟，再加上登入），回補的分鐘才不會被當成即時的再判斷一次。
_MAX_LAG = timedelta(seconds=30)

# 往前找幾個交易日的 1 分 K 當暖身與昨收。日曆認得國定假日，認不得颱風假這類臨時休市
# （那天查不到 K 棒），所以查不到就再往前一天。
_HISTORY_LOOKBACK = 5


class Ending(enum.Enum):
    """盤中程式為什麼結束。寫進 log，給人看的。"""

    NOT_A_TRADING_DAY = "非交易日，沒有登入"
    # 今天的部位檔存在卻讀不懂：已經發了 Discord，沒有登入、不進場。
    POSITION_FILE_UNREADABLE = "部位檔讀不懂，今天不進場"
    # 到 13:20 都沒有今天日盤的成交，收過的卻是別的交易日的（臨時休市，規格第 10 條）。
    # 結論為什麼要等到 13:20，見 `run()` 裡的說明。
    QUOTES_FROM_ANOTHER_DAY = "報價屬於前一個交易日，不判斷"
    CUTOFF = "13:20 收工"
    # 真的群益串流不會自己結束；會走到這裡的只有假 broker 的劇本放完了。
    STREAM_ENDED = "行情串流在 13:20 之前結束"


@dataclass(frozen=True)
class SessionOutcome:
    """一次盤中執行的結果：為什麼結束，以及今天組出來的每一根 1 分 K（依時間排序）。"""

    ending: Ending
    bars: tuple[indicators.ClosedBar, ...] = ()


def run(broker: Broker, config: settings.Config, *, today: date,
        notify: Callable[[str], bool]) -> SessionOutcome:
    """跑一個交易日的上午。`today` 是啟動當天的日期（見模組說明「時間」）。
    `notify` 送出一則 Discord（測試換成記錄器）；Discord 開關在這裡把關，關著就不呼叫它。

    原始行情會**先暫存、確認報價屬於今天之後才寫檔**：開盤前的累計筆數（08:40 起）
    要留著回答「從什麼時候開始累計」（ticket 03），但臨時休市那天一行都不能寫——
    那一天沒有屬於它的行情。

    報價交易日（規格第 10 條）分兩半處理：

      * **不屬於今天的成交，每一筆都直接略過**：不組 K 棒、不記、不拿它的時間當「現在」。
        不拿前一天的價格下單，靠的是這一條，不是收工。
      * **「今天臨時休市」的結論到 13:20 才下**：收到一筆今天日盤的成交就確認報價屬於今天；
        到 13:20 都沒有、又收過別天的成交，才以 `QUOTES_FROM_ANOTHER_DAY` 結束。

    ⚠️ 不能提早下結論。一般交易日 08:40 訂閱時，回補的常常就是前一個交易日的成交
       （休市時回補最近一個日盤，2026-08-16 實測），所以「收過別天的、還沒有今天的」
       在開盤前後是常態；這時報價再斷個一兩分鐘（規格第 53 條），或今天第一筆晚到，
       看起來就和臨時休市一模一樣。拿它收工的話，一次開盤時的斷線就賠掉一整天，
       而且程式正常結束，排程的失敗重啟也不會接手。等的代價是零（見第一點）。
    """
    if not calendar_tw.is_trading_day(today):
        logger.info("%s 不是交易日，不登入", today)
        return SessionOutcome(Ending.NOT_A_TRADING_DAY)

    # 當天已經有部位（盤中重啟）：交易商品、合約、口數一律以部位檔為準，不看設定檔
    # （規格第 43 條）。改設定最快下一個交易日才生效，進場和出場就不會是不同的商品。
    try:
        record = position_file.read(today)
    except position_file.Unreadable as exc:
        # 讀不懂的檔可能記著今天已經成交的口數：當成「沒有部位」照常進場的話，口數、
        # 均價、加碼上限甚至商品都會錯。這一天不進場；檔案原封不動，留給人工處理。
        logger.error("部位檔讀不懂，今天不進場：%s", exc)
        _send(config, notify, messages.position_file_unreadable(day=today, reason=str(exc)))
        return SessionOutcome(Ending.POSITION_FILE_UNREADABLE)
    product = config.product if record is None else record.product
    if record is not None and product != config.product:
        logger.warning("設定檔的交易商品是 %s，但今天已經有 %s 的部位，今天照部位檔交易 %s",
                       config.product, product, product)

    broker.login()
    orders = None
    if config.auto_order_enabled:
        # 近月合約（結算日當天就是當天到期的那個，規格第 9 條）在開盤前查好：查不到是
        # ProductListUnavailable，盤中程式在 08:40 就中止，而不是等到條件成立才發現。
        # 已經有部位時照部位檔的合約下單，和出場程式平的是同一個。
        contract = broker.get_contracts()[product] if record is None else record.contract
        orders = _Orders(broker, today, contract, record)
    trading = _Trading(config, notify, _load_history(broker, today), orders,
                       product=product, record=record)
    end_at = datetime.combine(today, SESSION_END)
    state = indicators.BarState()
    bars: list[indicators.ClosedBar] = []
    raw = RawMarketWriter(today)
    confirmed = False               # 收到今天日盤的成交了
    other_day: date | None = None   # 收過哪一個別的交易日的大台成交
    local_now: datetime | None = None   # 最新的本機時鐘（見模組說明「時間」）

    def _close(closed: indicators.ClosedBar | None, now: datetime) -> None:
        if closed is None:
            return
        bars.append(closed)
        _log_bar(closed, trading.rsi(bars))
        if local_now is None:
            # 真的 broker 第一個事件一定是時鐘；沒有的話分不出這根是不是回補的
            logger.warning("1 分 K %s 結束時還沒收到本機時鐘，分不出是不是回補的舊成交，不判斷",
                           f"{closed.bar.start:%H:%M}")
        elif local_now - now > _MAX_LAG:
            logger.info("1 分 K %s 結束於 %s，本機已經 %s（回補的舊成交或報價延遲），不判斷",
                        f"{closed.bar.start:%H:%M}", f"{now:%H:%M:%S}", f"{local_now:%H:%M:%S}")
        else:
            trading.on_bar(bars, now)

    ending = Ending.STREAM_ENDED
    try:
        for event in broker.events(product):
            if isinstance(event, (TxTrade, ProductTrade)) and event.time.date() != today:
                # 不屬於今天的成交：不組 K 棒、不記、也不拿它的時間當「現在」
                if isinstance(event, TxTrade):
                    other_day = event.time.date()
                continue
            if isinstance(event, Clock):
                local_now = event.time
            if event.time >= end_at:
                state, closed = indicators.close_forming(state)
                _close(closed, event.time)
                ending = Ending.CUTOFF
                if not confirmed and other_day is not None:
                    logger.warning("到 %s 都沒有今天的成交，收到的是 %s 的報價（臨時休市？）",
                                   f"{SESSION_END:%H:%M}", other_day)
                    ending = Ending.QUOTES_FROM_ANOTHER_DAY
                break
            if isinstance(event, TxTrade):
                if not confirmed and indicators.in_day_session(event.time):
                    confirmed = True
                    raw.confirm()
                raw.trade(event)
                state, closed = indicators.add_trade(state, event)
                _close(closed, event.time)
            elif isinstance(event, TxTradeCounts):
                raw.counts(event)
                state = indicators.add_counts(state, event)
            elif isinstance(event, ProductTrade):
                trading.product_price = event.price
    finally:
        raw.close()

    logger.info("盤中程式結束：%s，今天共 %d 根 1 分 K", ending.value, len(bars))
    return SessionOutcome(ending, tuple(bars))


def _log_bar(closed: indicators.ClosedBar, rsi: float | None) -> None:
    """每一根都寫進 log：實機時要拿它（含 RSI）和看盤軟體的大台 1 分 K 逐根核對。"""
    bar = closed.bar
    bichai = "未知（還沒收到累計成交筆數）" if closed.bichai is None else closed.bichai
    logger.info("1 分 K %s 開 %g 高 %g 低 %g 收 %g 筆差 %s RSI %s",
                f"{bar.start:%H:%M}", bar.open, bar.high, bar.low, bar.close, bichai,
                "未知" if rsi is None else f"{rsi:.2f}")


# ── 暖身與昨收 ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _History:
    """前一個交易日的大台日盤 1 分 K。RSI 的暖身用它，昨收是它最後一根的收盤價。

    ⚠️ 昨收**不是**結算價，也不是群益商品資料裡的參考價（`nRef`）：兩者可能差到幾十點，
       開高／開低就會判斷錯（規格第 5 條）。
    """

    day: date
    bars: tuple[indicators.ClosedBar, ...]

    @property
    def close(self) -> float:
        return self.bars[-1].bar.close


def _load_history(broker: Broker, today: date) -> _History | None:
    """往前找最近一個有 1 分 K 的交易日。查不到就是 None：今天不判斷，但照常收行情。

    查詢失敗不中止盤中程式：原始行情照樣要記（ticket 03 要一週的資料），只是今天沒有
    昨收與暖身，任何條件都判斷不了。
    """
    day = today
    for _ in range(_HISTORY_LOOKBACK):
        day -= timedelta(days=1)
        while not calendar_tw.is_trading_day(day):
            day -= timedelta(days=1)
        try:
            bars = broker.minute_bars(day)
        except BrokerError as exc:
            logger.error("取 %s 的大台 1 分 K 失敗，今天不判斷進場：%s", day, exc)
            return None
        if bars:
            history = _History(day, tuple(indicators.ClosedBar(b, None) for b in bars))
            logger.info("昨收 %g（%s %s 那根的收盤價），暖身 %d 根",
                        history.close, day, f"{bars[-1].start:%H:%M}", len(bars))
            return history
        logger.info("%s 查不到大台 1 分 K（臨時休市？），再往前一個交易日", day)
    logger.error("往前 %d 個交易日都查不到大台 1 分 K，沒有昨收，今天不判斷進場",
                 _HISTORY_LOOKBACK)
    return None


# ── 判斷之後：進場與通知 ──────────────────────────────────────────────────

_ENTRY_SIDE = {strategy.LONG: BUY, strategy.SHORT: SELL}


class _Orders:
    """真實下單：送出市價 IOC → 寫部位檔。只有自動下單打開時才存在。

    `record` 是今天的部位檔內容（還沒有成交時是 None），每次成交後整份重寫：
    出場程式照它平倉，所以它必須永遠是**全部**的成交，不只是最新一筆。
    """

    def __init__(self, broker: Broker, today: date, contract: ContractInfo,
                 record: position_file.PositionRecord | None):
        self._broker = broker
        self._today = today
        self._contract = contract
        self.record = record

    def enter(self, direction: str, lots: int) -> position_file.Fill | None:
        """送出進場單，回傳**實際**成交（規格第 29 條）。0 口（當下沒有對手價）是 None。

        委託失敗（`OrderFailed`／`FillUnknown`）目前原樣往外拋，盤中程式中止。
        「當天停止進場、記成不確定、告警」是 ticket 07 的事，接在這裡。
        """
        result = self._broker.place_order(OrderRequest(
            contract=self._contract, side=_ENTRY_SIDE[direction], lots=lots,
            trading_day=self._today))
        if not result.filled_lots:
            logger.warning("委託 %s 沒有成交（市價 IOC 當下沒有對手價），部位不變",
                           result.order_seq)
            return None
        fill = position_file.Fill(result.order_seq, result.filled_lots, result.avg_price)
        record = self.record or position_file.PositionRecord(
            trading_day=self._today, product=self._contract.code,
            order_code=self._contract.order_code,
            contract_month=self._contract.contract_month, direction=direction,
            last_trading_day=self._contract.last_trading_day,
            status=position_file.CONFIRMED, entries=())
        record = replace(record, entries=record.entries + (fill,))
        # ⚠️ 成交之後的**下一件事**就是寫檔（規格第 52 條）：發通知、記 log 都排在後面。
        #    中間任何一步出事（Discord 掛了、程式被關掉），出場程式仍然知道這幾口。
        position_file.write(record)
        self.record = record
        return fill


def _held(record: position_file.PositionRecord | None) -> strategy.Position | None:
    """部位檔記載的部位，換成策略判斷看的樣子。還沒有任何成交就是沒有部位。"""
    if record is None or not record.entered_lots:
        return None
    return strategy.Position(record.direction, record.entered_lots, record.avg_price)


class _Trading:
    """每根 K 棒結束時問策略，條件成立就進場、累計部位、發通知。

    自動下單關閉時是模擬模式（規格第 42 條）：成交價是交易商品**當下的價格**（最新一筆
    成交），部位只留在記憶體裡——**不寫部位檔**。出場程式看的是部位檔，模擬的口數不能
    讓它以為今天有部位。打開時才有 `orders`，那是唯一會呼叫下單函式的路徑。
    """

    def __init__(self, config: settings.Config, notify: Callable[[str], bool],
                 history: _History | None, orders: _Orders | None, *, product: str,
                 record: position_file.PositionRecord | None):
        self._config = config
        self._notify = notify
        self._history = history
        self._orders = orders
        self._product = product
        self.position = _held(record)
        # 當天不再進場。部位不確定時，帳上的口數不知道，部位不虧、加碼上限都算不了
        # （規格第 50 條）；ticket 07 在委託被拒、成交不確定時也會把它打開。
        self._stopped = record is not None and record.status == position_file.UNCERTAIN
        if self._stopped:
            logger.warning("部位檔記著一筆不確定的委託（%s），今天不再進場", record.uncertain)
        # 交易商品的現價：部位不虧用它，模擬也成交在它
        self.product_price: float | None = None

    def rsi(self, today: list[indicators.ClosedBar]) -> float | None:
        if self._history is None:
            return None
        return strategy.rsi(self._history.bars + tuple(today))

    def on_bar(self, today: list[indicators.ClosedBar], now: datetime) -> None:
        """一根 K 棒剛結束（`now` 是讓它結束的那個事件的時間）。"""
        if self._history is None or self._stopped:
            return
        first = today[0].bar
        # 開盤價是 08:45 那根的開盤價。今天第一根不是 08:45（開盤時報價斷了），
        # 就不知道今天是開高還是開低，條件 1、2 整天都不會成立。
        today_open = first.open if first.start.time() == indicators.DAY_SESSION_OPEN else None
        if today_open is None and len(today) == 1:
            logger.warning("今天第一根 1 分 K 是 %s 不是 08:45，不知道開盤價，條件 1、2 今天不判斷",
                           f"{first.start:%H:%M}")
        decision = strategy.decide(strategy.Situation(
            bars=self._history.bars + tuple(today),
            today_open=today_open,
            yesterday_close=self._history.close,
            now=now,
            position=self.position,
            product_price=self.product_price,
            lots_per_entry=self._config.lots_per_entry,
            max_position=self._config.max_position,
        ))
        if decision is None:
            return
        if self._orders is None:
            self._simulate(decision, now)
        else:
            self._enter(decision, now)

    def _enter(self, decision: strategy.Enter, now: datetime) -> None:
        fill = self._orders.enter(decision.direction, decision.lots)
        if fill is None:
            return
        record = self._orders.record
        self.position = _held(record)
        logger.info("進場成交：%s %s %d 口 @ %g（委託 %s），累計 %d 口，均價 %.2f",
                    decision.condition.value, record.direction, fill.lots, fill.price,
                    fill.order_seq, record.entered_lots, record.avg_price)
        self._send(messages.entry(
            at=now, product=record.product, direction=record.direction,
            condition=decision.condition, lots=fill.lots, price=fill.price,
            total_lots=record.entered_lots, avg_price=record.avg_price, simulated=False))

    def _simulate(self, decision: strategy.Enter, now: datetime) -> None:
        price = self.product_price
        if price is None:
            logger.warning("%s 成立，但還沒有交易商品的成交價，這一分鐘不進場",
                           decision.condition.value)
            return
        held = self.position
        total = decision.lots + (held.lots if held else 0)
        avg = (price * decision.lots + (held.avg_price * held.lots if held else 0)) / total
        self.position = strategy.Position(decision.direction, total, avg)
        logger.info("模擬進場：%s %s %d 口 @ %g，累計 %d 口，均價 %.2f",
                    decision.condition.value, decision.direction, decision.lots, price,
                    total, avg)
        self._send(messages.entry(
            at=now, product=self._product, direction=decision.direction,
            condition=decision.condition, lots=decision.lots, price=price,
            total_lots=total, avg_price=avg, simulated=True))

    def _send(self, text: str) -> None:
        _send(self._config, self._notify, text)


def _send(config: settings.Config, notify: Callable[[str], bool], text: str) -> None:
    """發一則 Discord。開關只在這裡把關（規格第 45 條）。"""
    if not config.discord_enabled:
        logger.info("Discord 已關閉，不發送")
        return
    notify(text)


def main() -> int:
    """排程的進入點（ticket 10 建排程）。登入、收行情、判斷進場、13:20 結束。

    **這裡是唯一建立真實 broker 與 Discord 連線的地方**：`run()` 不知道自己拿到的是
    真的還是假的，測試因此能在沒有群益元件的機器上跑完整個流程。
    """
    from broker.capital import CapitalBroker
    from notifiers import discord

    # 今天是哪一天：**只在這裡讀一次系統時鐘**（見模組說明「時間」）
    today = date.today()
    _setup_logging(today)

    config = settings.load()
    secrets = settings.load_secrets(config)
    broker = CapitalBroker(secrets.user_id, secrets.password, secrets.futures_account)
    webhook = secrets.discord_webhook
    try:
        run(broker, config, today=today, notify=lambda text: discord.send(text, webhook))
    except BrokerError as exc:
        logger.error("盤中程式中止：%s", exc)
        return 1
    return 0


def _setup_logging(today: date) -> None:
    """同時寫到畫面與 `logs/intraday-YYYYMMDD.log`。後者跟著 `logs/` 30 天清除（ticket 10）；
    要長期保留的是原始行情，K 棒隨時能從它重算。"""
    os.makedirs(paths.logs_dir(), exist_ok=True)
    log_file = os.path.join(paths.logs_dir(), f"intraday-{today:%Y%m%d}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr),
                  logging.FileHandler(log_file, encoding="utf-8")],
    )


if __name__ == "__main__":
    raise SystemExit(main())
