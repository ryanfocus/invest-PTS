"""盤中程式：每個交易日 08:40 啟動，收大台行情、組 1 分 K、存原始行情、判斷進場，13:20 結束。

用法（在專案根目錄）：

    python intraday.py

每一根 1 分 K 結束時交給策略判斷（`strategy.decide`）。目前只有模擬模式（ticket 04、05）：
條件成立就在交易商品當下的價格模擬成交一次，發一則標示「模擬」的訊息，
**完全不呼叫下單 API**。真實下單（ticket 06）之後接在同樣的地方（`_Trading._simulate`）。

## 突破進場的盯價

條件 3、4 在 09:45 之後不立刻進場，策略判斷回答的是「設定突破進場」（`strategy.Breakout`）。
盯價的是這裡（ADR-0001）：等待中的那一筆留在記憶體裡，每一筆**即時收到的**大台成交都問
它碰到觸發價了沒，碰到就對交易商品進場。同一筆成交如果剛好讓一根 K 棒結束，先判斷、再
盯價——剛設下的突破進場也要看這一筆。13:20 收工時，等待中的那一筆一併取消（規格第 26 條）；
它不寫進任何檔案，重啟後也不恢復（規格第 48 條）。

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
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

import calendar_tw
import indicators
import messages
import paths
import settings
import strategy
from broker import TX_CODE, Broker, BrokerError, Clock, ProductTrade, TxTrade, TxTradeCounts
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
    if config.auto_order_enabled:
        # ticket 06 接上真實下單時拿掉。在那之前照樣跑的話，使用者會以為條件成立時真的下了單。
        raise NotImplementedError(
            "自動下單（order.auto_enabled: true）還沒有實作，目前只能跑模擬模式。"
            "把 config/settings.yaml 的 auto_enabled 改回 false")
    if not calendar_tw.is_trading_day(today):
        logger.info("%s 不是交易日，不登入", today)
        return SessionOutcome(Ending.NOT_A_TRADING_DAY)

    broker.login()
    trading = _Trading(config, notify, _load_history(broker, today),
                       _load_settlement_day(broker, today))
    end_at = datetime.combine(today, SESSION_END)
    state = indicators.BarState()
    bars: list[indicators.ClosedBar] = []
    raw = RawMarketWriter(today)
    confirmed = False               # 收到今天日盤的成交了
    other_day: date | None = None   # 收過哪一個別的交易日的大台成交
    local_now: datetime | None = None   # 最新的本機時鐘（見模組說明「時間」）

    def _live(t: datetime) -> bool:
        """時間 `t` 的成交是不是即時收到的（見模組說明「時間」）。"""
        return local_now is not None and local_now - t <= _MAX_LAG

    def _close(closed: indicators.ClosedBar | None, now: datetime) -> None:
        if closed is None:
            return
        bars.append(closed)
        _log_bar(closed, trading.rsi(bars))
        if local_now is None:
            # 真的 broker 第一個事件一定是時鐘；沒有的話分不出這根是不是回補的
            logger.warning("1 分 K %s 結束時還沒收到本機時鐘，分不出是不是回補的舊成交，不判斷",
                           f"{closed.bar.start:%H:%M}")
        elif not _live(now):
            logger.info("1 分 K %s 結束於 %s，本機已經 %s（回補的舊成交或報價延遲），不判斷",
                        f"{closed.bar.start:%H:%M}", f"{now:%H:%M:%S}", f"{local_now:%H:%M:%S}")
        else:
            trading.on_bar(bars, now)

    ending = Ending.STREAM_ENDED
    try:
        for event in broker.events(config.product):
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
                trading.cancel_breakout()
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
                if config.product == TX_CODE:
                    # 交易大台時，這一筆就是交易商品的成交。群益對同一筆先送 TxTrade、再送
                    # ProductTrade；等後者才更新現價的話，下面的判斷與盯價都會成交在**前一筆**，
                    # 同一個商品就出現「觸發在 45180、成交在 45175」這種不可能的結果。
                    trading.product_price = event.price
                state, closed = indicators.add_trade(state, event)
                _close(closed, event.time)
                # 先判斷、再盯價：讓 K 棒結束的這一筆，已經是下一根的第一筆成交，
                # 剛設下的突破進場也要看它（回測裡它就是下一根的開盤價）。
                if _live(event.time):
                    trading.on_tx_trade(event)
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


def _load_settlement_day(broker: Broker, today: date) -> date | None:
    """近月合約的結算日：商品清單裡大台近月的最後交易日（規格第 24 條）。

    看大台，因為結算週停用條件 3、4 是為了大台近月的筆差（GLOSSARY「結算週」）。
    查不到就是 None：策略判斷因此停用條件 3、4（不知道是不是結算週，寧可少做），
    條件 1、2 照常判斷、原始行情照記。
    """
    try:
        contract = broker.get_contracts().get(TX_CODE)
    except BrokerError as exc:
        logger.error("查商品清單失敗，不知道今天是不是結算週，條件 3、4 今天停用：%s", exc)
        return None
    if contract is None:
        logger.error("商品清單裡沒有大台近月，不知道今天是不是結算週，條件 3、4 今天停用")
        return None
    day = contract.last_trading_day
    if strategy.in_settlement_week(today, day):
        logger.info("近月 %s 的結算日是 %s，今天在結算週，條件 3、4 停用", contract.order_code, day)
    else:
        logger.info("近月 %s 的結算日是 %s", contract.order_code, day)
    return day


# ── 判斷之後：模擬成交與通知 ──────────────────────────────────────────────


class _Trading:
    """每根 K 棒結束時問策略，條件成立就進場、累計部位、發通知；盯等待中的突破進場。

    目前只有模擬模式（ticket 04）：成交價是交易商品**當下的價格**（最新一筆成交），
    部位只留在記憶體裡——**不寫部位檔**（規格第 42 條）。出場程式看的是部位檔，
    模擬的口數不能讓它以為今天有部位。
    """

    def __init__(self, config: settings.Config, notify: Callable[[str], bool],
                 history: _History | None, settlement_day: date | None):
        self._config = config
        self._notify = notify
        self._history = history
        self._settlement_day = settlement_day
        self.position: strategy.Position | None = None
        # 交易商品的現價：部位不虧用它，模擬也成交在它
        self.product_price: float | None = None
        # 等待中的突破進場。同一時間最多一筆（規格第 21 條）；只在記憶體裡，重啟不恢復（第 48 條）
        self.breakout: strategy.Breakout | None = None

    def rsi(self, today: list[indicators.ClosedBar]) -> float | None:
        if self._history is None:
            return None
        return strategy.rsi(self._history.bars + tuple(today))

    def on_bar(self, today: list[indicators.ClosedBar], now: datetime) -> None:
        """一根 K 棒剛結束（`now` 是讓它結束的那個事件的時間）。"""
        if self._history is None:
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
            breakout=self.breakout,
            settlement_day=self._settlement_day,
        ))
        if isinstance(decision, strategy.Breakout):
            self.breakout = decision
            logger.info("%s 成立，設定突破進場：大台%s碰到 %g 時進 %d 口",
                        decision.condition.value,
                        "向上" if decision.direction == strategy.LONG else "向下",
                        decision.trigger, decision.lots)
        elif decision is not None:
            self._simulate(decision, now)

    def on_tx_trade(self, trade: TxTrade) -> None:
        """一筆即時收到的大台成交：盯等待中的突破進場（ADR-0001：由程式盯價）。"""
        breakout = self.breakout
        if breakout is None or not breakout.touched_by(trade.price):
            return
        self.breakout = None
        logger.info("大台成交 %g 碰到觸發價 %g，突破進場", trade.price, breakout.trigger)
        self._simulate(strategy.Enter(breakout.direction, breakout.condition, breakout.lots),
                       trade.time, trigger=breakout.trigger)

    def cancel_breakout(self) -> None:
        """13:20 進場截止：等待中的突破進場一併取消（規格第 26 條）。沒有進場，不發訊息。"""
        if self.breakout is not None:
            logger.info("%s 進場截止，取消等待中的突破進場（%s，觸發價 %g）",
                        f"{SESSION_END:%H:%M}", self.breakout.condition.value,
                        self.breakout.trigger)
            self.breakout = None

    def _simulate(self, decision: strategy.Enter, now: datetime, *,
                  trigger: float | None = None) -> None:
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
            at=now, product=self._config.product, direction=decision.direction,
            condition=decision.condition, lots=decision.lots, price=price,
            total_lots=total, avg_price=avg, simulated=True, trigger=trigger))

    def _send(self, text: str) -> None:
        if not self._config.discord_enabled:
            logger.info("Discord 已關閉，不發送")
            return
        self._notify(text)


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
    except (BrokerError, NotImplementedError) as exc:
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
