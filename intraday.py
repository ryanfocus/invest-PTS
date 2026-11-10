"""盤中程式：每個交易日 08:40 啟動，收大台行情、組 1 分 K、存原始行情、判斷進場，13:20 結束。

用法（在專案根目錄）：

    python intraday.py

排程叫醒它的入口是 `scheduled.py`（ticket 10）。

每一根 1 分 K 結束時交給策略判斷（`strategy.decide`）。條件成立時：

  * 自動下單打開：送出市價 IOC → 把實際成交寫進部位檔 → 發一則成交訊息（ticket 06）。
  * 自動下單關閉（模擬模式）：在交易商品當下的價格模擬成交，發一則標示「模擬」的訊息，
    **完全不呼叫下單 API**、不寫部位檔（ticket 04）。模擬的部位記在另一個檔（`sim_position`），
    重啟後接著累計，出場程式不看它。

立刻進場與突破進場被觸發走的是同一條路（`_Trading._execute`），開關只在那裡分岔。

## 委託出事（ticket 07）

分界線是「單有沒有送出去」（規格第 41、50、51 條）。不管哪一種，**今天都不再進場**，
發一則告警，盤中程式照樣跑到 13:20（原始行情繼續記）：

  * **委託沒有送出去**（`OrderFailed`：被拒、憑證、回報連線斷了，或送單前部位檔就寫不進去）：
    確定沒有部位，部位檔維持送單前的樣子。不再進場是因為同一個原因下一分鐘還在，否則每分鐘
    都會再被拒、再告警一次。
  * **委託送出去了，但不知道成交幾口**（`FillUnknown`，以及送單時任何其他意外——這裡分不出
    單出去了沒有，分不清就是可能有部位）：部位檔是「不確定」，出場程式看到就不自動平倉。
  * **部位還在**，部位檔卻沒記到（ticket 06 的 `_FillNotOnFile`）。

三則告警用固定的說法開頭，使用者一眼分得出是哪一種（`messages`）。

**每一筆進場單送出之前，部位檔先記成「不確定」**（`_Orders.enter`，ticket 09）：送出去之後、
成交寫進部位檔之前盤中程式被硬性中止的話，重啟後的盤中程式與 13:25 的出場程式都看得到「今天
有一筆送出去、不知道成交的單」。所以成交或「不確定」的細節寫不進去時，檔裡也不會是送單前那份
「已確認」，出場程式不會照少算的口數平倉。

「今天不再進場」寫成檔（停止進場記錄，`stop_path`），告警之前先寫：盤中程式當掉被重啟
（規格第 48 條）之後看到它，照樣不進場、不再告警。只放在記憶體的話，被拒的單會在重啟後
再送一次、再告警一次。部位檔記著「不確定」卻沒有停止進場記錄，就是送單的那一輪來不及告警：
重啟後的這一輪替它告警（`_Trading.report_uncertain_entry_left_by_last_run`）。

## 可靠度（ticket 09）

  * **心跳記錄**（`_Pulse`、`heartbeat`）：每分鐘寫一次，13:20 收工時寫「收工了」（這一輪在
    13:20 之前活過才寫）。13:25 的出場程式靠它發現盤中程式今天沒跑、或半路停了（規格第 54 條）。
  * **報價中斷**（`_QuoteWatch`，規格第 53 條）：大台成交或累計筆數任一路連續 3 分鐘沒來，發一則
    告警，**同時取消等待中的突破進場**——只停了筆數時成交照常，不取消的話它照樣被碰到，告警裡
    「報價恢復之前不會進場」就不是真的。新的進場則判斷不出來：斷線前後缺了成交的 K 棒還在一次
    判斷會讀到的最近幾根裡時不判斷（`_missing_bars`），筆數停住過的筆差是不知道
    （`indicators._close`）。
  * **盤中重啟**（`_Restored`，規格第 48 條）：部位、停止進場記錄照舊從檔案讀，設定照當天第一次
    啟動時的快照（`day_settings`：改設定最快下一個交易日才生效）；今天已經過去的
    1 分 K 與每根的筆差從原始行情檔讀回，群益回補的成交裡重啟前就處理過的不再處理。
    當機期間記不到的筆差是不知道，不會被拿去算筆差跳增。
  * **開盤前該查好的資料查不到**（`_Lookups`）：前一交易日的 1 分 K（昨收、RSI 暖身）、大台近月的
    結算日、下單合約。不中止、行情照記，每分鐘再查，查到就從那一刻起照常判斷；到 08:50 還缺就
    告警一次，講明今天哪些條件判斷不了。
  * **排程每分鐘叫醒**（ticket 10，`tools/schedule.py`）：這就是失敗時的自動重啟，所以正常結束
    之後也會再被叫醒。今天已經走到結局（心跳記錄寫了「收工了」）就什麼都不做
    （`_finished_earlier`）；今天登入失敗過就不再登入（`login_failed_path`）——網路、群益主機的
    問題（`LoginUnavailable`）不算，下一分鐘照樣再登入。同一時間只有一個盤中程式（`_alone`）。

## 突破進場的盯價

條件 3、4 在 09:45 之後不立刻進場，策略判斷回答的是「設定突破進場」（`strategy.Breakout`）。
盯價的是這裡（ADR-0001）：等待中的那一筆留在記憶體裡，每一筆**即時收到的**大台成交都問
它碰到觸發價了沒，碰到就對交易商品進場。同一筆成交如果剛好讓一根 K 棒結束，先判斷、再
盯價——剛設下的突破進場也要看這一筆。13:20 收工時，等待中的那一筆一併取消（規格第 26 條），
報價中斷告警時也是（`_QuoteWatch`）；它不寫進任何檔案，重啟後也不恢復（規格第 48 條）。

13:20 之後不能有任何新成交，所以每一筆進場單都帶著截止時刻（`OrderRequest.deadline`）：13:20 前
被觸發、送出那一刻 broker 的本機時鐘已經過了的，不送，只記 log（`PastDeadline`）。為了不讓這種
事常發生，自動下單打開時在訂閱行情之前就做好下單準備（`_prepare_orders`）。

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

import contextlib
import enum
import logging
import os
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Callable, Iterator

import calendar_tw
import day_settings
import heartbeat
import indicators
import messages
import paths
import position_file
import raw_market
import settings
import sim_position
import state_files
import strategy
from broker import (BUY, SELL, TX_CODE, Broker, BrokerError, Clock, ContractInfo,
                    FillUnknown, LoginFailed, OrderFailed, OrderRequest, PastDeadline,
                    ProductTrade, TxTrade, TxTradeCounts)
from raw_market import RawMarketWriter

logger = logging.getLogger(__name__)

# 進場截止（GLOSSARY），盤中程式在那一刻收工（規格「兩支程式」一節）。13:19 那根到這時結束，
# 照樣交出來；之後的事件不組 K 棒、也不寫進原始行情。時刻是策略常數，定義在策略判斷。
ENTRY_CUTOFF = strategy.ENTRY_CUTOFF

# 成交最多可以比本機時鐘晚多久，還算是即時收到的。要大過本機時鐘和交易所的誤差
# （一兩秒，久沒對時可能更多）加上成交時間只到秒、時鐘每秒才一個；要小過重啟所需的時間
# （排程最快一分鐘後重啟，再加上登入），回補的分鐘才不會被當成即時的再判斷一次。
# 重啟得比這還快時，重啟前判斷過的那一根靠原始行情檔認出來（`_Restored.covers`）。
_MAX_LAG = timedelta(seconds=30)

# 連續這麼久沒有即時的大台報價，就發 Discord（規格第 53 條）。
_OUTAGE_ALERT = timedelta(minutes=3)

# 大台日盤每秒都有好幾筆成交。相鄰兩筆隔得比這還久，中間就是報價斷了，不是沒有人交易：
# 斷掉的那一段碰到的 K 棒缺了成交（開高低收都可能不對），見 `_missing_bars`。
_TRADE_GAP = timedelta(seconds=30)

# 往前找幾個交易日的 1 分 K 當暖身與昨收。日曆認得國定假日，認不得颱風假這類臨時休市
# （那天查不到 K 棒），所以查不到就再往前一天。
_HISTORY_LOOKBACK = 5


class Ending(enum.Enum):
    """盤中程式為什麼結束。寫進 log，給人看的。"""

    NOT_A_TRADING_DAY = "非交易日，沒有登入"
    # 到 13:20 都沒有今天日盤的成交，收過的卻是別的交易日的（臨時休市，規格第 10 條）。
    # 結論為什麼要等到 13:20，見 `run()` 裡的說明。
    QUOTES_FROM_ANOTHER_DAY = "報價屬於前一個交易日，不判斷"
    CUTOFF = f"{ENTRY_CUTOFF:%H:%M} 進場截止，收工"
    # 真的群益串流不會自己結束；會走到這裡的只有假 broker 的劇本放完了。
    STREAM_ENDED = f"行情串流在 {ENTRY_CUTOFF:%H:%M} 進場截止之前結束"
    # 排程每分鐘叫醒盤中程式（ticket 10）：今天稍早那一輪已經走到結局，這一輪什麼都不做
    FINISHED_EARLIER = "今天稍早已經收工，這次被叫醒什麼都不做"
    # 今天稍早登入失敗過（`login_failed_path`）：重試沒有用，帳密錯了還會把帳號鎖住
    LOGIN_FAILED_EARLIER = "今天登入失敗過，這次被叫醒不再登入"


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
    if _finished_earlier(today):
        return SessionOutcome(Ending.FINISHED_EARLIER)
    if (failed := state_files.read_marker(login_failed_path(today))) is not None:
        logger.error("今天登入失敗過（%s），不再登入。排除原因之後刪掉 %s，排程下一次叫醒時"
                     "就會再登入", failed, login_failed_path(today))
        return SessionOutcome(Ending.LOGIN_FAILED_EARLIER)

    # 改設定最快下一個交易日才生效：自動下單開關、口數、加碼上限、交易商品照今天第一次啟動時的
    # （`day_settings`）。重讀的話，生效與否就看盤中程式有沒有剛好當掉重啟。
    config = day_settings.for_today(config, today)
    pulse = _Pulse(today)
    # 當天已經有部位（盤中重啟）：交易商品、合約、口數一律以部位檔為準，不看設定檔
    # （規格第 43 條）。進場和出場就不會是不同的商品。
    try:
        record = position_file.read(today)
        unreadable = False
    except position_file.Unreadable as exc:
        record, unreadable = None, True
        _stop_for_unreadable_position_file(config, notify, today, exc)
    product = config.product if record is None else record.product
    if record is not None and product != config.product:
        logger.warning("設定檔的交易商品是 %s，但今天已經有 %s 的部位，今天照部位檔交易 %s",
                       config.product, product, product)

    try:
        broker.login()
    except LoginFailed as exc:
        # 寫下登入失敗記錄的同時講一則（之後被叫醒看到記錄就不再登入，也就不再講）
        _write_login_failed(today, str(exc))
        _send(config, notify, messages.login_failed(
            day=today, reason=str(exc), marker=paths.shown_state(login_failed_path(today))))
        raise
    # `LoginUnavailable`（網路、群益主機）不寫：往外拋、這一輪結束，下一分鐘排程叫醒時再登入
    live = config.auto_order_enabled
    trading = _Trading(config, notify, live=live, today=today, product=product, record=record,
                       stopped=unreadable)
    if live and record is not None:
        # 已經有部位時照部位檔的合約下單，和出場程式平的是同一個
        trading.orders = _Orders(broker, today, record.contract, record)
    # 昨收與 RSI 暖身、結算日、下單合約（近月，結算日當天就是當天到期的那個，規格第 9 條）在開盤
    # 前查好；查不到不中止，每分鐘再查（`_Lookups`）
    lookups = _Lookups(broker, trading, today, product=product,
                       need_contract=live and record is None and not unreadable)
    if live and not trading.stopped:
        _prepare_orders(broker)
    end_at = datetime.combine(today, ENTRY_CUTOFF)
    # 盤中重啟：今天稍早收到的行情在原始行情檔裡，K 棒與筆差從那裡接著組（見 `_Restored`）
    restored = _Restored(raw_market.read(today))
    state = restored.state
    bars: list[indicators.ClosedBar] = list(restored.bars)
    raw = RawMarketWriter(today)
    watch = _QuoteWatch(today)
    confirmed = restored.confirmed  # 收到今天日盤的成交了（重啟前那一輪收到的也算）
    if confirmed:
        raw.confirm()
    other_day: date | None = None   # 收過哪一個別的交易日的大台成交
    local_now: datetime | None = None   # 最新的本機時鐘（見模組說明「時間」）
    last_trade = restored.last_trade    # 上一筆今天日盤的大台成交（交易所時間）
    # 報價斷過、缺了成交的 K 棒（起始分鐘），重啟前那一輪遇到的也算
    incomplete: set[datetime] = set(restored.incomplete)

    def _live(t: datetime) -> bool:
        """時間 `t` 的成交是不是即時收到的（見模組說明「時間」）。"""
        return local_now is not None and local_now - t <= _MAX_LAG

    def _close(closed: indicators.ClosedBar | None, now: datetime) -> None:
        if closed is None:
            return
        if closed.bar.start == restored.unfinished and closed.bichai is not None:
            # 重啟前那一輪當掉時，這一根還沒結束：結束那一刻的筆數沒有記到。手上的是當掉前的值，
            # 拿它和重啟後的值相減，就是把當機前後的累積算成一根的筆差跳增（ticket 09）。
            logger.info("1 分 K %s 在上一輪當掉時還沒結束，結束時的筆差不知道",
                        f"{closed.bar.start:%H:%M}")
            closed = replace(closed, bichai=None)
        bars.append(closed)
        _log_bar(closed, trading.rsi(bars))
        if local_now is None:
            # 真的 broker 第一個事件一定是時鐘；沒有的話分不出這根是不是回補的
            logger.warning("1 分 K %s 結束時還沒收到本機時鐘，分不出是不是回補的舊成交，不判斷",
                           f"{closed.bar.start:%H:%M}")
        elif not _live(now):
            logger.info("1 分 K %s 結束於 %s，本機已經 %s（回補的舊成交或報價延遲），不判斷",
                        f"{closed.bar.start:%H:%M}", f"{now:%H:%M:%S}", f"{local_now:%H:%M:%S}")
        elif missing := [b.bar.start for b in bars[-strategy.LOOKBACK_BARS:]
                         if b.bar.start in incomplete]:
            # 報價中斷期間不進場（規格第 53 條）：缺了成交的 K 棒開高低收都不可靠。一次判斷
            # 讀到的不只最後一根（紅K、筆差跳增看兩根，RSI、突破的觸發價看最近幾根），所以它
            # 還在 `LOOKBACK_BARS` 裡就不判斷。再往前的只剩 RSI 的平滑還帶著一點，每根衰減 1/5。
            logger.info("1 分 K %s 不判斷：最近 %d 根裡 %s 那根報價斷過、缺了成交",
                        f"{closed.bar.start:%H:%M}", strategy.LOOKBACK_BARS,
                        "、".join(f"{m:%H:%M}" for m in missing))
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
            if isinstance(event, TxTrade) and restored.covers(event):
                continue    # 重啟前那一輪就收過、記過、組進 K 棒了
            # 只有第一個事件會做事：上一輪送出進場單之後、告警之前就停了，這一輪替它告警
            trading.report_uncertain_entry_left_by_last_run(event.time)
            if isinstance(event, Clock):
                local_now = event.time
            if event.time >= end_at:
                state, closed = indicators.close_forming(state)
                _close(closed, event.time)
                ending = Ending.CUTOFF
                trading.cancel_breakout(f"{ENTRY_CUTOFF:%H:%M} 進場截止")
                if not confirmed and other_day is not None:
                    logger.warning("到 %s 都沒有今天的成交，收到的是 %s 的報價（臨時休市？）",
                                   f"{ENTRY_CUTOFF:%H:%M}", other_day)
                    ending = Ending.QUOTES_FROM_ANOTHER_DAY
                pulse.finish(event.time)
                break
            if isinstance(event, Clock):
                pulse.beat(event.time)
                lookups.on_clock(event.time)
                outage = watch.check(event.time)
                if outage is not None:
                    # 告警講「報價恢復之前不會進場」：只停了累計筆數時成交照常，等待中的
                    # 突破進場還會被碰到（見 `_QuoteWatch`）
                    trading.cancel_breakout("報價中斷")
                    _send(config, notify, outage)
            if isinstance(event, TxTrade):
                if not confirmed and indicators.in_day_session(event.time):
                    confirmed = True
                    raw.confirm()
                raw.trade(event)
                if product == TX_CODE:
                    # 交易大台時，這一筆就是交易商品的成交。群益對同一筆先送 TxTrade、再送
                    # ProductTrade；等後者才更新現價的話，下面的判斷與盯價都會成交在**前一筆**，
                    # 同一個商品就出現「觸發在 45180、成交在 45175」這種不可能的結果。
                    trading.product_price = event.price
                if indicators.in_day_session(event.time):
                    missing = (set() if last_trade is None
                               else _missing_bars(last_trade, event.time))
                    if missing:
                        incomplete |= missing
                        logger.warning("大台成交從 %s 到 %s 之間斷了，%s 那根缺了成交",
                                       f"{last_trade:%H:%M:%S}", f"{event.time:%H:%M:%S}",
                                       "、".join(f"{m:%H:%M}" for m in sorted(missing)))
                    last_trade = event.time if last_trade is None else max(last_trade,
                                                                           event.time)
                state, closed = indicators.add_trade(state, event)
                _close(closed, event.time)
                # 先判斷、再盯價：讓 K 棒結束的這一筆，已經是下一根的第一筆成交，
                # 剛設下的突破進場也要看它（回測裡它就是下一根的開盤價）。
                if _live(event.time):
                    watch.trade_at = event.time
                    trading.on_tx_trade(event)
            elif isinstance(event, TxTradeCounts):
                raw.counts(event)
                watch.counts_at = event.time
                state = indicators.add_counts(state, event)
            elif isinstance(event, ProductTrade):
                trading.product_price = event.price
    finally:
        raw.close()

    logger.info("盤中程式結束：%s，今天共 %d 根 1 分 K", ending.value, len(bars))
    return SessionOutcome(ending, tuple(bars))


class _Restored:
    """盤中重啟（規格第 48 條）：今天稍早那一輪收到的行情，從原始行情檔讀回來。

    兩件事靠它：

      * **補回今天已經過去的 1 分 K，連同每根結束時的筆差。** 群益重啟後回補的只有成交，筆差
        沒有歷史回補，只能從 PTS 自己的原始行情記錄讀回：照檔案順序重播一次 `indicators`，得到的
        就是當時的 K 棒與筆差。這些 K 棒當時判斷過了，這裡不再判斷。
      * **認出群益回補的成交裡，哪些重啟前就處理過**（`covers`）。不認的話，它們會再記進原始
        行情檔一次；更糟的是，當掉前一刻剛結束的那一根，會被回補的同一筆成交再結束一次——
        重啟得快（30 秒內）時那筆還算即時，同一分鐘就判斷兩次、送兩次單。

    當機期間的成交，原始行情檔裡沒有：照群益回補的組 K 棒。它們結束時不是即時的，不判斷。
    筆差不會被拿去算當機前後的差距：程式不在的那幾分鐘沒有收到累計筆數，筆差是不知道
    （`indicators._bichai_at_close`）；當掉時還沒結束的那一根（`unfinished`）手上倒是有筆數，
    卻是當掉之前的，也當成不知道（`run()` 的 `_close`）。等待中的突破進場只在記憶體裡，
    不恢復（規格第 48 條）。
    """

    def __init__(self, events: list[TxTrade | TxTradeCounts]):
        state = indicators.BarState()
        bars: list[indicators.ClosedBar] = []
        for event in events:
            if isinstance(event, TxTrade):
                state, closed = indicators.add_trade(state, event)
                if closed is not None:
                    bars.append(closed)
            else:
                state = indicators.add_counts(state, event)
        self.state = state
        self.bars = tuple(bars)
        # 當掉時還沒結束的那一根（起始分鐘）。它結束那一刻的筆數沒有記到，筆差是不知道。
        self.unfinished = state.forming.start if state.forming is not None else None
        trades = [e.time for e in events if isinstance(e, TxTrade)]
        in_session = [t for t in trades if indicators.in_day_session(t)]
        # 原始行情檔要收到今天日盤的成交才會建（`RawMarketWriter.confirm`）
        self.confirmed = bool(in_session)
        self.last_trade = max(in_session) if in_session else None
        # 重啟前那一輪遇過的報價中斷：缺了成交的 K 棒還在最近幾根裡時，重啟後照樣不判斷
        self.incomplete: set[datetime] = set()
        previous = None
        for t in in_session:
            if previous is not None:
                self.incomplete |= _missing_bars(previous, t)
            previous = t if previous is None else max(previous, t)
        self._last = max(trades) if trades else None
        self._left = trades.count(self._last)        # 同一秒可能有好幾筆
        if events:
            logger.info("今天稍早跑過：從原始行情檔讀回 %d 個事件、%d 根 1 分 K，最後一筆成交 %s",
                        len(events), len(bars), self._last)

    def covers(self, trade: TxTrade) -> bool:
        """這一筆今天的成交，重啟前那一輪是不是就收過了。

        群益照成交的順序回補，所以比檔裡最後一筆還早的都收過；和最後一筆同一秒的，檔裡有幾筆
        就略過幾筆（成交時間只到秒）。第一輪自己遇過報價中斷、中間缺了一段的話，回補的會把
        那一段補齊——這裡照樣略過：那幾分鐘當時就沒有 K 棒，現在也不該冒出來。
        """
        if self._last is None or trade.time > self._last:
            return False
        if trade.time == self._last:
            if not self._left:
                return False
            self._left -= 1
        return True


def _prepare_orders(broker: Broker) -> None:
    """自動下單打開：訂閱行情之前就做好下單準備（讀憑證、連回報、等回報補送完）。

    留到第一筆委託才做的話要多等幾秒到幾十秒：盤中重啟後的第一筆可能是 13:19 觸發的突破進場，
    準備完已經過了 13:20（規格第 26 條）。失敗只記 log：還沒有任何委託，不必打擾人；第一筆委託
    送出時 broker 會再準備一次，那時還不行才是「委託沒有送出去」。
    """
    try:
        broker.prepare_orders()
    except BrokerError as exc:
        logger.warning("下單準備沒有做好，第一筆委託時再試：%s", exc)


def _stop_for_unreadable_position_file(config: settings.Config, notify: Callable[[str], bool],
                                       today: date, exc: position_file.Unreadable) -> None:
    """今天的部位檔讀不懂：今天不進場，告警一次。

    讀不懂的檔可能記著今天已經成交的口數：當成「沒有部位」照常進場的話，口數、均價、加碼上限
    甚至商品都會錯。檔案原封不動，留給人工處理。盤中程式**照樣登入、收行情**：原始行情每個交易日
    都要記（規格第 58 條），心跳照寫、13:20 照常收工，出場程式就不會多講一則「沒有跑」。

    告警之前寫下停止進場記錄：當掉重啟後看到它就不再講一次（記錄已經在的話，今天稍早就講過，
    或因為別的原因停止進場、那一則已經發過——出場程式 13:25 也會講部位檔讀不懂）。
    """
    logger.error("部位檔讀不懂，今天不進場（行情照記）：%s", exc)
    if state_files.read_marker(stop_path(today)) is not None:
        logger.info("今天稍早已經停止進場（%s），不再告警", stop_path(today))
        return
    _write_stop(today, f"{today:%Y/%m/%d} 部位檔讀不懂：{exc}")
    _send(config, notify, messages.position_file_unreadable(day=today, reason=str(exc)))


def _missing_bars(previous: datetime, t: datetime) -> set[datetime]:
    """相鄰兩筆今天日盤的大台成交 `previous` → `t`：中間斷了的話，缺了成交的 K 棒（起始分鐘）。

    斷掉的那一段碰到的每一分鐘都缺了成交。整分鐘都在斷線裡的根本沒有 K 棒；有 K 棒的是兩頭：
    斷之前那一根少了後段（收盤價、高低點），接回來那一根少了前段——它的開盤價其實是斷線後
    第一筆的價格，而紅K 就是拿開盤價比（規格第 6 條）。接回來那一筆正好在整分鐘上的話，
    那一根什麼都沒少。
    """
    if t - previous <= _TRADE_GAP:
        return set()
    before = previous.replace(second=0, microsecond=0)
    after = t.replace(second=0, microsecond=0)
    return {before, after} if t > after else {before}


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


class _Lookups:
    """開盤前要查好的三樣東西，查不到不中止，每分鐘再查（規格第 4、24、58 條）：

      * **前一交易日的大台 1 分 K**：昨收與 RSI 暖身。沒有它，條件 1～4 都判斷不了。
      * **大台近月的結算日**（商品清單）：不知道今天是不是結算週，條件 3、4 停用（寧可少做）。
      * **下單合約**（自動下單打開、今天還沒有部位時，商品清單裡交易商品的近月）：沒有它不能下單。

    查不到時盤中程式照樣訂閱、記原始行情、寫心跳——原始行情每個交易日都要記，而一次 08:40 的
    逾時不能安靜地賠掉一整天：在之後的時鐘事件上每分鐘再查一次（時間取自事件），查到就從那一刻
    起照常判斷；到 08:50（條件 1、2 開始判斷）還缺，就講**一則**哪些條件判斷不了。不講的話，
    使用者分不出「今天沒有訊號」和「今天程式是瞎的」，程式又正常收工，排程的重啟、13:25 的出場
    程式都不會接手。盤中程式當掉重啟、還是查不到的話，重啟後那一輪會再講一次。
    """

    def __init__(self, broker: Broker, trading: "_Trading", today: date, *, product: str,
                 need_contract: bool):
        self._broker = broker
        self._trading = trading
        self._today = today
        self._product = product
        self._have_history = False
        self._have_settlement_day = False
        self._have_contract = not need_contract
        self._next_try: datetime | None = None
        self._told = False
        self._look_up()                         # 啟動時查第一次（還沒有任何事件）

    def on_clock(self, now: datetime) -> None:
        """本機時鐘走到 `now`：缺的每分鐘再查一次；到 08:50 還缺就講一次。"""
        if self._complete:
            return
        minute = now.replace(second=0, microsecond=0)
        if self._next_try is None:
            # 第一個時鐘：啟動時剛查過，下一分鐘再查
            self._next_try = minute + timedelta(minutes=1)
        elif now >= self._next_try:
            self._next_try = minute + timedelta(minutes=1)
            self._look_up()
        if not self._complete and not self._told and now.time() >= strategy.EARLY_START:
            self._told = True
            logger.error("到 %s 還有資料查不到（有沒有查到：前一交易日 1 分 K %s、結算日 %s、"
                         "下單合約 %s）", f"{strategy.EARLY_START:%H:%M}", self._have_history,
                         self._have_settlement_day, self._have_contract)
            self._trading.say(messages.entries_not_fully_judged(
                at=now, no_history=not self._have_history,
                no_settlement_day=not self._have_settlement_day,
                no_contract_for=None if self._have_contract else self._product,
                log=paths.shown_log("intraday", self._today)))

    @property
    def _complete(self) -> bool:
        return self._have_history and self._have_settlement_day and self._have_contract

    def _look_up(self) -> None:
        if not self._have_history:
            history = _load_history(self._broker, self._today)
            if history is not None:
                self._have_history = True
                self._trading.history = history
        if self._have_settlement_day and self._have_contract:
            return
        try:
            contracts = self._broker.get_contracts()
        except BrokerError as exc:
            logger.error("查商品清單失敗，一分鐘後再查：%s", exc)
            return
        if not self._have_settlement_day:
            day = _settlement_day(contracts.get(TX_CODE), self._today)
            if day is not None:
                self._have_settlement_day = True
                self._trading.settlement_day = day
        if not self._have_contract:
            contract = contracts.get(self._product)
            if contract is None:
                logger.error("商品清單裡沒有 %s 的近月，還不能下單，一分鐘後再查", self._product)
            else:
                self._have_contract = True
                self._trading.orders = _Orders(self._broker, self._today, contract, None)


def _load_history(broker: Broker, today: date) -> _History | None:
    """往前找最近一個有 1 分 K 的交易日。查詢失敗、或都查不到就是 None（`_Lookups` 一分鐘後再查）。"""
    day = today
    for _ in range(_HISTORY_LOOKBACK):
        day -= timedelta(days=1)
        while not calendar_tw.is_trading_day(day):
            day -= timedelta(days=1)
        try:
            bars = broker.minute_bars(day)
        except BrokerError as exc:
            logger.error("取 %s 的大台 1 分 K 失敗，還不能判斷進場，一分鐘後再查：%s", day, exc)
            return None
        if bars:
            history = _History(day, tuple(indicators.ClosedBar(b, None) for b in bars))
            logger.info("昨收 %g（%s %s 那根的收盤價），暖身 %d 根",
                        history.close, day, f"{bars[-1].start:%H:%M}", len(bars))
            return history
        logger.info("%s 查不到大台 1 分 K（臨時休市？），再往前一個交易日", day)
    logger.error("往前 %d 個交易日都查不到大台 1 分 K，沒有昨收，還不能判斷進場，一分鐘後再查",
                 _HISTORY_LOOKBACK)
    return None


def _settlement_day(contract: ContractInfo | None, today: date) -> date | None:
    """近月合約的結算日：商品清單裡大台近月的最後交易日（規格第 24 條）。

    看大台，因為結算週停用條件 3、4 是為了大台近月的筆差（GLOSSARY「結算週」）。
    沒有大台近月就是 None：策略判斷因此停用條件 3、4（不知道是不是結算週，寧可少做），
    條件 1、2 照常判斷。
    """
    if contract is None:
        logger.error("商品清單裡沒有大台近月，不知道今天是不是結算週，條件 3、4 先停用")
        return None
    day = contract.last_trading_day
    if strategy.in_settlement_week(today, day):
        logger.info("近月 %s 的結算日是 %s，今天在結算週，條件 3、4 停用", contract.order_code, day)
    else:
        logger.info("近月 %s 的結算日是 %s", contract.order_code, day)
    return day


# ── 判斷之後：進場與通知 ──────────────────────────────────────────────────

_ENTRY_SIDE = {strategy.LONG: BUY, strategy.SHORT: SELL}


class _FillNotOnFile(Exception):
    """單已經成交，部位檔卻寫不進去。`record` 是含這一筆的**真實**部位，`on_file` 是送單前
    那一份（還沒有檔就是 None）：檔裡是它的成交，加上送單前記下的「不確定」。"""

    def __init__(self, fill: position_file.Fill, record: position_file.PositionRecord,
                 on_file: position_file.PositionRecord | None, cause: Exception):
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.fill = fill
        self.record = record
        self.on_file = on_file


class _Uncertain(Exception):
    """進場單送出去了，卻不知道成交幾口。訊息是不確定的原因。

    `file_error` 是 None 代表部位檔記下了這一筆的序號與已知口數；否則是寫不進去的原因，
    部位檔還是送單前記下的「不確定」（少了序號）——出場程式照樣不自動平倉。
    """

    def __init__(self, *, order_seq: str, requested_lots: int, known_filled: int, reason: str,
                 file_error: str | None):
        super().__init__(reason)
        self.order_seq = order_seq
        self.requested_lots = requested_lots
        self.known_filled = known_filled
        self.file_error = file_error


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

        **送單之前，部位檔先記成「不確定」**（`_mark_sending`）：送出去之後、成交寫進部位檔之前
        （等成交回報最長 30 秒），盤中程式可能被硬性中止。那時沒有任何紀錄的話，重啟後這張單
        等於沒送過——同一個條件再送一次；它的回報也不會再被認領（只認自己的委託序號，規格第 56 條），
        13:25 出場程式照舊的口數平倉，多出來的那幾口過夜。先記成「不確定」，最壞的情況就是
        規格第 50 條：當天不再進場，出場程式不自動平倉、告警。寫不進去就不送（`OrderFailed`）。
        確定沒有部位的結局（被拒、0 口）再改回送單前的樣子。

        出事的時候拋三種例外之一，分界線是「單有沒有送出去、成交有沒有記到」：

          * `OrderFailed`：委託沒有送出去，確定沒有部位。部位檔改回送單前的樣子。
          * `_Uncertain`：送出去了，但不知道成交幾口（`FillUnknown`，或送單時任何其他意外）。
            部位檔記著這一筆的委託序號與已知口數——寫不進去的話（原因在 `file_error`），
            還是送單前記下的「不確定」。
          * `_FillNotOnFile`：成交了卻寫不進部位檔；檔裡還是送單前記下的「不確定」，
            `self.record` 維持送單前那一份。
        """
        # 13:20 進場截止之後不能有任何新成交（規格第 26 條）：決定送單的是事件上的時間，送出的那一刻
        # 由 broker 用本機時鐘再比一次（`OrderRequest.deadline`）
        request = OrderRequest(contract=self._contract, side=_ENTRY_SIDE[direction], lots=lots,
                               trading_day=self._today,
                               deadline=datetime.combine(self._today, ENTRY_CUTOFF))
        self._mark_sending(direction, lots)
        try:
            result = self._broker.place_order(request)
        except OrderFailed:
            self._unmark()
            raise
        except FillUnknown as exc:
            raise self._uncertain(direction, lots, exc.order_seq, exc.known_filled,
                                  str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            # broker 的契約是只拋上面兩種，但它沒做到的時候，這裡分不出單出去了沒有。
            # 分不清就是「可能有部位」（broker/__init__.py 的分界線）：當成沒送出去的話，
            # 部位檔不記，出場程式以為今天沒進場，部位直接過夜。
            raise self._uncertain(direction, lots, "", 0,
                                  f"送單時發生意外（{type(exc).__name__}）：{exc}") from exc
        if not result.filled_lots:
            logger.warning("委託 %s 沒有成交（市價 IOC 當下沒有對手價），部位不變",
                           result.order_seq)
            self._unmark()
            return None
        fill = position_file.Fill(result.order_seq, result.filled_lots, result.avg_price)
        record = self._current(direction)
        record = replace(record, entries=record.entries + (fill,))
        # ⚠️ 成交之後的**下一件事**就是寫檔（規格第 52 條）：發通知、記 log 都排在後面。
        #    中間任何一步出事（Discord 掛了、程式被關掉），出場程式仍然知道這幾口。
        try:
            position_file.write(record)
        except Exception as exc:  # noqa: BLE001
            # 不管為什麼寫不進去（檔案被防毒鎖住、磁碟滿、權限），結果都一樣：帳上有、
            # 檔裡沒有。讓它當成一般例外冒出去的話，程式帶著 traceback 結束，一則告警都
            # 沒有，13:25 出場程式照舊檔平倉（沒有檔就什麼都不平）。
            raise _FillNotOnFile(fill, record, self.record, exc) from exc
        self.record = record
        return fill

    def _mark_sending(self, direction: str, lots: int) -> None:
        """送單前把部位檔記成「不確定」：這一筆還沒有序號、已知成交 0 口。寫不進去就不送。"""
        marker = replace(self._current(direction), status=position_file.UNCERTAIN,
                         uncertain=position_file.UncertainOrder(
                             stage=position_file.ENTRY, order_seq="", requested_lots=lots,
                             known_filled=0))
        try:
            position_file.write(marker)
        except Exception as exc:  # noqa: BLE001
            # 送出去之後當掉的話沒人知道帳上多了這筆：寧可這一次不送。部位檔還是之前那一份，
            # 確定沒有部位——和被拒同一種處境，今天不再進場（下一分鐘多半也寫不進去）。
            raise OrderFailed(f"送單前部位檔寫不進去，沒有送出委託"
                              f"（{type(exc).__name__}: {exc}）") from exc

    def _unmark(self) -> None:
        """確定沒有多出部位（被拒、0 口）：部位檔改回送單前的樣子——今天第一筆的話，就是沒有檔。

        改不回去的話，檔裡留著「不確定」：出場程式不會自動平倉、會告警，盤中程式重啟後也不再
        進場。多打擾一次，但不會平錯。
        """
        try:
            if self.record is None:
                os.remove(position_file.path_for(self._today))
            else:
                position_file.write(self.record)
        except Exception as exc:  # noqa: BLE001
            logger.error("部位檔改不回送單前的樣子，留著「不確定」（13:25 出場程式不會自動平倉）："
                         "%s: %s", type(exc).__name__, exc)

    def _current(self, direction: str) -> position_file.PositionRecord:
        """今天的部位檔內容；還沒有檔的話，用這次要下單的合約與方向開一份空的。"""
        return self.record or position_file.PositionRecord(
            trading_day=self._today, product=self._contract.code,
            order_code=self._contract.order_code,
            contract_month=self._contract.contract_month, direction=direction,
            last_trading_day=self._contract.last_trading_day,
            status=position_file.CONFIRMED, entries=())

    def _uncertain(self, direction: str, lots: int, order_seq: str, known_filled: int,
                   reason: str) -> _Uncertain:
        """把送出去卻不知道成交幾口的那一筆記進部位檔，狀態改成「不確定」。

        已經確認的成交照留在 `entries`：它們是真的。不確定的那一筆不放進 `entries`——
        不知道口數，也沒有成交價——而是記在 `uncertain`，出場程式看到就不自動平倉。
        """
        def uncertain(file_error: str | None) -> _Uncertain:
            return _Uncertain(order_seq=order_seq, requested_lots=lots,
                              known_filled=known_filled, reason=reason, file_error=file_error)

        try:
            order = position_file.UncertainOrder(
                stage=position_file.ENTRY, order_seq=order_seq, requested_lots=lots,
                known_filled=known_filled)
            record = replace(self._current(direction), status=position_file.UNCERTAIN,
                             uncertain=order)
            position_file.write(record)
        except Exception as exc:  # noqa: BLE001
            # 寫不進去（檔案被鎖、磁碟滿），或 broker 給的數字組不成一筆記錄：部位檔還是
            # 送單前記下的「不確定」，只是少了序號。冒出 `run()` 的話程式帶著 traceback 結束，
            # 一則告警都沒有；所以照樣告警，序號寫在告警裡。
            return uncertain(f"{type(exc).__name__}: {exc}")
        self.record = record
        return uncertain(None)


def _held(record: position_file.PositionRecord | None) -> strategy.Position | None:
    """部位檔記載的部位，換成策略判斷看的樣子。還沒有任何成交就是沒有部位。"""
    if record is None or not record.entered_lots:
        return None
    return strategy.Position(record.direction, record.entered_lots, record.avg_price)


class _Trading:
    """每根 K 棒結束時問策略，條件成立就進場、累計部位、發通知；盯等待中的突破進場。

    自動下單關閉時是模擬模式（規格第 42 條）：成交價是交易商品**當下的價格**（最新一筆
    成交），部位記在模擬部位（`sim_position`）——**不寫部位檔**。出場程式看的是部位檔，模擬的
    口數不能讓它以為今天有部位。打開時（`live`）才有 `orders`，那是唯一會呼叫下單函式的路徑；
    下單合約還沒查到（`_Lookups`）的時候 `orders` 是 None，條件成立也不進場——不是模擬。

    `history`（昨收與暖身）、`settlement_day`、`orders` 由 `_Lookups` 查到時放進來。
    """

    def __init__(self, config: settings.Config, notify: Callable[[str], bool], *, live: bool,
                 today: date, product: str, record: position_file.PositionRecord | None,
                 stopped: bool = False):
        self._config = config
        self._notify = notify
        self._live = live
        self.history: _History | None = None
        self.settlement_day: date | None = None
        self.orders: _Orders | None = None
        self._today = today
        self._product = product
        self.position = _held(record) if live else self._simulated_so_far()
        # 當天不再進場。部位不確定時，帳上的口數不知道，部位不虧、加碼上限都算不了
        # （規格第 50 條）——啟動時讀到「不確定」、或今天這裡送出的委託變成不確定
        # （`_fill_unknown`）；委託被拒（`_not_sent`）、成交寫不進部位檔（`_not_on_file`）
        # 時也打開。打開時寫下停止進場記錄（`_stop`），重啟後照樣不進場。
        self._stopped = record is not None and record.status == position_file.UNCERTAIN
        # 部位檔記著一筆不確定的進場單，卻沒有停止進場記錄：送出那筆委託的那一輪在告警之前就停了
        # （送單前記下「不確定」之後被硬性中止，`_Orders.enter`）。這一輪替它告警
        # （`report_uncertain_entry_left_by_last_run`）。
        self._unannounced: position_file.PositionRecord | None = None
        if self._stopped:
            logger.warning("部位檔記著一筆不確定的委託（%s），今天不再進場", record.uncertain)
            if (record.uncertain.stage == position_file.ENTRY
                    and state_files.read_marker(stop_path(today)) is None):
                self._unannounced = record
        else:
            earlier = state_files.read_marker(stop_path(today))
            if earlier is not None:
                self._stopped = True
                logger.warning("今天稍早已經停止進場，重啟後照樣不進場：%s", earlier)
        # 部位檔讀不懂（`_stop_for_unreadable_position_file`）：一開始就不進場
        self._stopped = self._stopped or stopped
        # 交易商品的現價：部位不虧用它，模擬也成交在它
        self.product_price: float | None = None
        # 等待中的突破進場。同一時間最多一筆（規格第 21 條）；只在記憶體裡，重啟不恢復（第 48 條）
        self.breakout: strategy.Breakout | None = None

    def _simulated_so_far(self) -> strategy.Position | None:
        """模擬模式：今天稍早（重啟之前）模擬累計的部位。讀不懂的話今天不再模擬進場——累計口數與
        加碼上限都算不了；只是模擬，記 log 就好。"""
        try:
            return sim_position.read(self._today)
        except sim_position.Unreadable as exc:
            logger.error("模擬部位讀不懂，今天不再模擬進場：%s", exc)
            self._stopped = True
            return None

    @property
    def stopped(self) -> bool:
        """今天已經停止進場了沒有。"""
        return self._stopped

    def report_uncertain_entry_left_by_last_run(self, now: datetime) -> None:
        """重啟前那一輪送出進場單之後、告警之前就停了：替它發「委託送出去了，但…」的告警
        （規格第 50 條），並寫下停止進場記錄——之後再重啟就不再講一次。

        每個事件都可以呼叫，只有第一次會做事：`now` 要用這一輪第一個事件的時間（啟動時還沒有
        任何時間，見模組說明「時間」），所以不在啟動時發。"""
        record, self._unannounced = self._unannounced, None
        if record is None:
            return
        u = record.uncertain
        why = ("盤中程式送出這筆委託之後、記下成交之前就停了（當掉或被關掉），"
               "重啟後收不到它的回報")
        self._stop(now, f"委託 {u.order_seq or '（沒有序號）'} 送出去了，但不知道成交幾口：{why}")
        logger.error("今天稍早送出的委託 %s（%d 口）不知道成交幾口，送出它的那一輪沒來得及告警",
                     u.order_seq or "（沒有序號）", u.requested_lots)
        self.say(messages.fill_unknown(
            at=now, product=record.product, direction=record.direction, lots=u.requested_lots,
            order_seq=u.order_seq, known_filled=u.known_filled, reason=why,
            held_lots=record.entered_lots, file_error=None))

    def rsi(self, today: list[indicators.ClosedBar]) -> float | None:
        if self.history is None:
            return None
        return strategy.rsi(self.history.bars + tuple(today))

    def on_bar(self, today: list[indicators.ClosedBar], now: datetime) -> None:
        """一根 K 棒剛結束（`now` 是讓它結束的那個事件的時間）。"""
        if self.history is None or self._stopped:
            return
        first = today[0].bar
        # 開盤價是 08:45 那根的開盤價。今天第一根不是 08:45（開盤時報價斷了），
        # 就不知道今天是開高還是開低，條件 1、2 整天都不會成立。
        today_open = first.open if first.start.time() == calendar_tw.DAY_SESSION_OPEN else None
        if today_open is None and len(today) == 1:
            logger.warning("今天第一根 1 分 K 是 %s 不是 08:45，不知道開盤價，條件 1、2 今天不判斷",
                           f"{first.start:%H:%M}")
        decision = strategy.decide(strategy.Situation(
            bars=self.history.bars + tuple(today),
            today_open=today_open,
            yesterday_close=self.history.close,
            now=now,
            position=self.position,
            product_price=self.product_price,
            lots_per_entry=self._config.lots_per_entry,
            max_position=self._config.max_position,
            breakout=self.breakout,
            settlement_day=self.settlement_day,
        ))
        if isinstance(decision, strategy.Breakout):
            self.breakout = decision
            logger.info("%s 成立，設定突破進場：大台%s碰到 %g 時進 %d 口",
                        decision.condition.value,
                        "向上" if decision.direction == strategy.LONG else "向下",
                        decision.trigger, decision.lots)
        elif decision is not None:
            self._execute(decision, now)

    def on_tx_trade(self, trade: TxTrade) -> None:
        """一筆即時收到的大台成交：盯等待中的突破進場（ADR-0001：由程式盯價）。"""
        breakout = self.breakout
        if breakout is None:
            return
        if self._stopped:
            # 等待中時當天停止進場了（例如另一筆成交寫不進部位檔）：這一筆不再觸發。
            logger.info("今天已經停止進場，取消等待中的突破進場（%s，觸發價 %g）",
                        breakout.condition.value, breakout.trigger)
            self.breakout = None
            return
        if not breakout.touched_by(trade.price):
            return
        self.breakout = None
        logger.info("大台成交 %g 碰到觸發價 %g，突破進場", trade.price, breakout.trigger)
        self._execute(strategy.Enter(breakout.direction, breakout.condition, breakout.lots),
                      trade.time, trigger=breakout.trigger)

    def cancel_breakout(self, why: str) -> None:
        """取消等待中的突破進場。沒有進場，不發訊息。兩個時候：13:20 進場截止（規格第 26 條），
        與報價中斷告警的那一刻（`_QuoteWatch`）。取消就是沒了，之後不恢復。"""
        if self.breakout is not None:
            logger.info("%s，取消等待中的突破進場（%s，觸發價 %g）", why,
                        self.breakout.condition.value, self.breakout.trigger)
            self.breakout = None

    def _execute(self, decision: strategy.Enter, now: datetime, *,
                 trigger: float | None = None) -> None:
        """進場：自動下單打開就真的下單，關閉就模擬。立刻進場與突破進場被觸發都走這裡，
        真實下單才不會只接到其中一種。`trigger` 是突破進場的觸發價（立刻進場是 None）。"""
        if not self._live:
            self._simulate(decision, now, trigger=trigger)
        elif self.orders is None:
            logger.warning("%s 成立，但還沒查到下單合約（商品清單），這一次不進場",
                           decision.condition.value)
        else:
            self._enter(decision, now, trigger=trigger)

    def _against_position(self, decision: strategy.Enter) -> bool:
        """這次要進的方向和手上的部位相反：不進場（規格第 17 條，一天只做一個方向）。
        真實下單與模擬的進場都先過這一關。

        條件 1、2 只看開高／開低，不看手上部位的方向；正常情況下兩者一定一致，但盤中重啟時
        昨收是重新查的（前一交易日的 1 分 K 這次查不到，就往前多退一天），開高／開低可能
        整個翻過來。這時送出的反向單會被倉別「自動」當成平倉，部位檔卻記成同方向加碼：
        出場程式照檔平倉，就開出反向的新倉帶過夜。
        """
        held = self.position
        if held is None or held.direction == decision.direction:
            return False
        logger.error("%s 要%s，但今天的部位是 %s %d 口：一天只做一個方向，不進場"
                     "（昨收或開盤價和建立部位時不一樣？）", decision.condition.value,
                     decision.direction, held.direction, held.lots)
        return True

    def _enter(self, decision: strategy.Enter, now: datetime, *,
               trigger: float | None = None) -> None:
        if self._against_position(decision):
            return
        try:
            fill = self.orders.enter(decision.direction, decision.lots)
        except PastDeadline as exc:
            # 13:20 前觸發、送出時已經過了截止：沒有送出，確定沒有部位，也不必使用者做什麼——只記 log
            logger.info("%s 的委託沒有送出：%s", decision.condition.value, exc)
            return
        except OrderFailed as exc:
            self._not_sent(decision, exc, now)
            return
        except _Uncertain as exc:
            self._fill_unknown(decision, exc, now)
            return
        except _FillNotOnFile as exc:
            self._not_on_file(exc, now)
            return
        if fill is None:
            return
        record = self.orders.record
        self.position = _held(record)
        logger.info("進場成交：%s %s %d 口 @ %g（委託 %s），累計 %d 口，均價 %.2f",
                    decision.condition.value, record.direction, fill.lots, fill.price,
                    fill.order_seq, record.entered_lots, record.avg_price)
        self.say(messages.entry(
            at=now, product=record.product, direction=record.direction,
            condition=decision.condition, lots=fill.lots, price=fill.price,
            total_lots=record.entered_lots, avg_price=record.avg_price, simulated=False,
            trigger=trigger))

    def _not_sent(self, decision: strategy.Enter, exc: OrderFailed, now: datetime) -> None:
        """委託沒有送出去：確定沒有部位，部位檔不動。今天不再進場，告警只發這一次。

        不再進場，是因為被拒的原因（保證金不足、憑證問題）下一分鐘還在：條件每分鐘成立，
        就每分鐘再被拒一次、再發一則告警（規格第 51 條）。
        """
        self._stop(now, f"委託沒有送出去：{exc}")
        logger.error("%s 的委託沒有送出去，今天不再進場：%s", decision.condition.value, exc)
        self.say(messages.order_not_sent(
            at=now, product=self._product, direction=decision.direction, lots=decision.lots,
            reason=str(exc), held_lots=self.position.lots if self.position else 0))

    def _fill_unknown(self, decision: strategy.Enter, exc: _Uncertain, now: datetime) -> None:
        """委託送出去了，但不知道成交幾口：部位檔已記成「不確定」。今天不再進場、告警。

        不再進場，是因為帳上的口數從這一刻起不知道：部位不虧、加碼上限都算不了
        （規格第 50 條）。盤中程式照樣跑到收工，原始行情繼續記。

        停止進場記錄寫在告警之前：重啟後看到它，就知道這一則已經發過
        （`report_uncertain_entry_left_by_last_run` 不再發）。
        """
        self._stop(now, f"委託 {exc.order_seq or '（沒有序號）'} 送出去了，"
                        f"但不知道成交幾口：{exc}")
        logger.error("%s 的委託 %s 送出去了，但不知道成交幾口（已知至少 %d 口），"
                     "今天不再進場：%s", decision.condition.value,
                     exc.order_seq or "（沒有序號）", exc.known_filled, exc)
        if exc.file_error is None:
            logger.error("部位檔已記為不確定")
        else:
            logger.error("這筆的序號寫不進部位檔，檔裡是送單前記下的「不確定」：%s",
                         exc.file_error)
        self.say(messages.fill_unknown(
            at=now, product=self._product, direction=decision.direction,
            lots=exc.requested_lots, order_seq=exc.order_seq, known_filled=exc.known_filled,
            reason=str(exc), held_lots=self.position.lots if self.position else 0,
            file_error=exc.file_error))

    def _not_on_file(self, exc: _FillNotOnFile, now: datetime) -> None:
        """成交了、部位檔卻沒記到：告警（「部位還在」），今天不再進場。

        不再進場，是因為從這一刻起部位檔和帳上對不上：再成交下去，告警裡講好要人自己平的
        口數就不再準。部位檔停在送單前記下的「不確定」，出場程式不會自動平倉。盤中程式照樣跑到
        收工，原始行情繼續記。停止進場記錄寫在告警之前，重啟後就知道這一則已經發過。
        """
        record, fill = exc.record, exc.fill
        self._stop(now, f"成交 {fill.lots} 口（委託 {fill.order_seq}）寫不進部位檔：{exc}")
        on_file = exc.on_file.entered_lots if exc.on_file is not None else 0
        self.position = _held(record)
        logger.error("成交 %s %d 口 @ %g（委託 %s）寫不進部位檔，帳上共 %d 口、檔裡 %d 口，"
                     "今天不再進場：%s", record.direction, fill.lots, fill.price,
                     fill.order_seq, record.entered_lots, on_file, exc)
        self.say(messages.fill_not_on_file(
            at=now, product=record.product, direction=record.direction, lots=fill.lots,
            price=fill.price, order_seq=fill.order_seq, total_lots=record.entered_lots,
            on_file_lots=on_file, reason=str(exc)))

    def _simulate(self, decision: strategy.Enter, now: datetime, *,
                  trigger: float | None = None) -> None:
        if self._against_position(decision):
            return
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
        try:
            sim_position.write(self._today, self.position)
        except Exception as exc:  # noqa: BLE001
            # 只是模擬：記 log、照樣發訊息。代價是重啟後從檔裡那一份接著累計。
            logger.error("模擬部位寫不進 %s（%s: %s）", sim_position.path_for(self._today),
                         type(exc).__name__, exc)
        self.say(messages.entry(
            at=now, product=self._product, direction=decision.direction,
            condition=decision.condition, lots=decision.lots, price=price,
            total_lots=total, avg_price=avg, simulated=True, trigger=trigger))

    def _stop(self, now: datetime, why: str) -> None:
        """今天不再進場，並寫下停止進場記錄。告警之前先寫：發 Discord 會卡住、會失敗。"""
        self._stopped = True
        _write_stop(self._today, f"{now:%Y/%m/%d %H:%M:%S} {why}")

    def say(self, text: str) -> None:
        """發一則 Discord（開關在 `_send` 把關）。"""
        _send(self._config, self._notify, text)


# ── 報價中斷 ─────────────────────────────────────────────────────────────


class _QuoteWatch:
    """報價中斷偵測：日盤開盤到進場截止之間，連續 3 分鐘沒有即時的大台報價就發一則告警
    （規格第 53 條）。

    看兩路：大台的即時成交（K 棒與盯價用）與累計成交筆數（筆差用）。少了任何一路，判斷就缺
    一條腿，所以從**比較早斷的那一路**算起。群益 broker 斷線時時鐘事件照樣每秒來（它是訊息
    幫浦自己產生的），所以中斷看得出來；整個程式卡住的話連時鐘都沒有，那是心跳記錄的事。

    中斷期間不進場分兩半：

      * **新的進場判斷不出來**，不靠這裡：成交斷了，K 棒不會即時結束，斷線碰到的 K 棒也不判斷
        （`_missing_bars`）；只有筆數停了，之後每一根的筆差都是不知道，條件 1～4 都要筆差。
      * **等待中的突破進場在告警的那一刻取消**（`run()` 呼叫 `_Trading.cancel_breakout`）。
        只停了筆數時大台成交照常，它照樣會被碰到——告警裡說了「報價恢復之前不會進場」，
        就要做到。取消之後不恢復：觸發價是中斷之前的 5 根算的，恢復時價格可能早就遠遠超過，
        那時才進場就是追高；和重啟一樣（規格第 48 條、ADR-0001「報價中斷時那一筆就不會進場」）。
        告警之前（筆數停了不到 3 分鐘）它照樣會進場：設定之後只看價格（規格第 21 條），
        價格是即時收到的；筆數停了不到 3 分鐘還不算中斷，只讓那幾根的筆差變成不知道
        （`indicators._close`）。

    從什麼時候開始算：日盤 08:45 開盤，或這一輪盤中程式收到第一個時鐘事件（晚啟動、重啟），
    兩者取晚的——重啟之前的空白是心跳記錄要講的事，不是這裡。

    ⚠️ 開盤後都沒有今天的成交，可能是臨時休市（颱風假），也可能是開盤時報價斷了：13:20 之前
       兩者看起來一模一樣（`run()` 的說明）。照樣告警，訊息裡講清楚臨時休市的話不必處理：
       不講的話，真的斷線時一整天沒人知道。

    一次中斷只發一則。兩路都恢復之後再斷，是新的一次。
    """

    def __init__(self, today: date):
        # 從日盤開盤算起；盤中程式在進場截止就收工，偵測也就做到那時
        self._open = datetime.combine(today, calendar_tw.DAY_SESSION_OPEN)
        self._end = datetime.combine(today, ENTRY_CUTOFF)
        self._started: datetime | None = None
        self.trade_at: datetime | None = None       # 最後一筆即時收到的大台成交
        self.counts_at: datetime | None = None      # 最後一次累計成交筆數
        self._alerted_at: datetime | None = None

    def check(self, now: datetime) -> str | None:
        """本機時鐘走到 `now`：該告警就回傳訊息。"""
        if self._started is None:
            self._started = now
        if self._alerted_at is not None:
            if (self.trade_at is not None and self.counts_at is not None
                    and min(self.trade_at, self.counts_at) > self._alerted_at):
                self._alerted_at = None             # 兩路都恢復了，這一次中斷結束
            else:
                return None
        if not self._open <= now < self._end:
            return None
        since = max(self._open, self._started)
        if self.trade_at is not None and self.counts_at is not None:
            since = max(since, min(self.trade_at, self.counts_at))
        if now - since < _OUTAGE_ALERT:
            return None
        self._alerted_at = now
        logger.error("大台報價中斷超過 %d 分鐘：最後的即時成交 %s，最後的累計成交筆數 %s",
                     _OUTAGE_ALERT.seconds // 60, self.trade_at, self.counts_at)
        return messages.quote_outage(at=now, trade_at=self.trade_at, counts_at=self.counts_at,
                                     outage=_OUTAGE_ALERT)


# ── 心跳記錄 ─────────────────────────────────────────────────────────────


class _Pulse:
    """心跳記錄（規格第 54 條）：每分鐘寫一次「今天、現在還活著」，自己走到結局時寫「收工了」。

    「現在」是本機時鐘事件的時間，和盤中程式其他地方一樣不讀系統時鐘（見模組說明「時間」）。
    第一次寫在第一個時鐘事件：登入、訂閱失敗而中止的那一輪不寫，出場程式看到的就還是
    前一輪（或沒有），講的是「盤中程式沒有跑」或「幾點之後沒有再跑」——都是事實。

    寫不進去只記 log、不往外拋：代價是 13:25 多一則「沒有跑」的告警；為它讓盤中程式中止的話，
    連進場都沒了。
    """

    def __init__(self, today: date):
        self._today = today
        self._minute: datetime | None = None

    def beat(self, now: datetime) -> None:
        minute = now.replace(second=0, microsecond=0)
        if minute != self._minute:
            self._minute = minute
            self._write(now, finished=False)

    def finish(self, now: datetime) -> None:
        """13:20 收工（臨時休市那天也是這時下結論）。

        **這一輪在 13:20 之前活過，才寫「收工了」。** 排程補跑或失敗重啟時，一起來就過了進場
        截止的那一輪什麼都沒判斷過：寫「收工了」的話，蓋掉的是前一輪留下的「幾點之後沒有再跑」
        （或今天根本沒有心跳），一整個交易時段沒人判斷進場，出場程式卻什麼都不講（規格第 54 條）。
        這時心跳記錄原封不動。
        """
        if self._minute is None:
            logger.warning("這一輪 %s 才起來，已經過了進場截止，什麼都沒判斷：心跳記錄不寫「收工了」",
                           f"{now:%H:%M:%S}")
            return
        self._write(now, finished=True)

    def _write(self, now: datetime | None, *, finished: bool) -> None:
        try:
            heartbeat.write(heartbeat.Heartbeat(self._today, now, finished))
        except Exception as exc:  # noqa: BLE001
            logger.error("心跳記錄寫不進 %s（%s: %s）：出場程式會以為盤中程式沒有跑",
                         heartbeat.path(), type(exc).__name__, exc)


def _finished_earlier(today: date) -> bool:
    """今天稍早那一輪已經走到結局了沒有（心跳記錄寫了「收工了」）。

    排程從 08:40 起每分鐘叫醒盤中程式一次，直到進場截止（ticket 10），所以正常結束之後
    還會再被叫醒。只有 13:20 收工（臨時休市那天也是）會寫「收工了」（`_Pulse.finish`），
    當掉、被關掉的不會——那正是要重啟的。

    心跳記錄讀不懂就當成還沒收工：照常跑，它會被這一輪的心跳覆寫掉。
    """
    try:
        beat = heartbeat.read()
    except heartbeat.Unreadable as exc:
        logger.warning("心跳記錄讀不懂，當成今天還沒收工：%s", exc)
        return False
    if beat is None or beat.trading_day != today or not beat.finished:
        return False
    logger.info("今天稍早已經收工（心跳記錄 %s），這次被叫醒什麼都不做", heartbeat.path())
    return True


def login_failed_path(day: date) -> str:
    """那個交易日的登入失敗記錄：今天為什麼不再登入（ticket 10）。

    排程每分鐘叫醒盤中程式一次（失敗時自動重啟），登入失敗卻是重試沒有用、要人處理的事
    （`broker.LoginFailed`：帳密、憑證、群益元件沒註冊，以及分不清是哪一類的代碼）。帳密錯了還
    每分鐘再試，一天就是兩百多次錯誤的登入，可能把帳號鎖住——連使用者自己的 APP、OS 都登不
    進去。所以寫成檔，之後被叫醒看到它就不再登入。

    網路或群益主機的問題（`broker.LoginUnavailable`：斷線、取不到主機清單、「請稍後再試」）
    **不寫**：沒有驗證到密碼，不會鎖帳號；下一分鐘再登入一次，開盤時的一次斷線才不會賠掉一整天。

    寫下它的同時發一則 Discord（`messages.login_failed`），講原因與排除之後怎麼讓它再登入：
    等 13:25 才知道的話，當天的進場時段已經過完了。之後被叫醒看到它就不再登入，也就不再講。

    **不寫進心跳記錄**：這不是「收工了」。13:25 出場程式照樣講「盤中程式今天沒有跑」（規格
    第 54 條），心跳記錄寫了「收工了」，出場程式就不講了。

    排除原因之後刪掉它，排程下一次叫醒時就會再登入；放著不管，隔一個交易日也照常登入。
    檔案存在就算數，內容只是給人看的原因（標記檔，`state_files`）。
    """
    return state_files.daily("login-failed", day)


def _write_login_failed(day: date, reason: str) -> None:
    """寫不進去只記 log：這一輪照樣以登入失敗結束，代價是下一分鐘會再試一次。"""
    path = login_failed_path(day)
    try:
        state_files.write_marker(path, reason)
    except Exception as exc:  # noqa: BLE001
        logger.error("登入失敗記錄寫不進 %s（%s: %s）：排程下一次叫醒時會再登入一次",
                     path, type(exc).__name__, exc)


# ── 停止進場記錄 ─────────────────────────────────────────────────────────


def stop_path(day: date) -> str:
    """那個交易日的停止進場記錄：今天為什麼不再進場（ticket 07）。

    「今天不再進場」只放在記憶體的話，盤中程式一重啟（規格第 48 條）就忘了：被拒的單
    再送一次、再告警一次（規格第 51 條）；「不確定」或成交寫不進部位檔時，檔裡看不出
    今天出過事，重啟後照常加碼。所以寫成檔，重啟時看到它就不進場。

    只有盤中程式自己讀，出場程式不看（兩支程式之間只靠部位檔與心跳記錄，規格「兩支程式」
    一節）。**檔案存在就是停過**，內容只是寫給人看的原因；讀不懂（寫到一半斷電、被改壞）也一樣
    不進場（`state_files.read_marker`）。
    """
    return state_files.daily("entries-stopped", day)


def _write_stop(day: date, line: str) -> None:
    """寫下停止進場記錄：接在後面，一次停止一行。

    不像部位檔先寫暫存檔再置換（理由見 `state_files`）：部位檔被防毒鎖住、`os.replace` 失敗的
    時候，這裡照樣寫得進去——那正是最需要它的時候（「不確定」或成交寫不進部位檔）。

    寫不進去只記 log，不往外拋：接著要發的告警比它要緊，冒出 `run()` 的話一則都發不出去。
    今天這一輪照樣停止進場（記憶體裡），只是重啟後記不得。
    """
    path = stop_path(day)
    try:
        state_files.write_marker(path, line, append=True)
    except Exception as exc:  # noqa: BLE001
        logger.error("停止進場記錄寫不進 %s（%s: %s）：盤中程式若重啟，今天可能會再進場",
                     path, type(exc).__name__, exc)


def _send(config: settings.Config, notify: Callable[[str], bool], text: str) -> None:
    """發一則 Discord。開關只在這裡把關（規格第 45 條）。"""
    if not config.discord_enabled:
        logger.info("Discord 已關閉，不發送")
        return
    notify(text)


def main(*, connect: Callable[[settings.Secrets], Broker] | None = None,
         today: Callable[[], date] = date.today) -> int:
    """排程的進入點（ticket 10 建排程）。登入、收行情、判斷進場、13:20 結束。

    **這裡是唯一建立真實 broker 與 Discord 連線的地方**：`run()` 不知道自己拿到的是
    真的還是假的，測試因此能在沒有群益元件的機器上跑完整個流程。`connect` 用 `.env` 的帳密
    建立 broker（預設是群益），`today` 回答今天是哪一天（預設讀系統日期）——測試換掉的就是
    這兩個接縫：broker 與時間。
    """
    from notifiers import discord

    # 今天是哪一天：**只在這裡讀一次系統時鐘**（見模組說明「時間」）
    day = today()
    _setup_logging(day)

    with _alone() as alone:
        if not alone:
            logger.info("另一個盤中程式正在跑（%s 被鎖住），這一個什麼都不做", _lock_path())
            return 0
        config = settings.load()
        secrets = settings.load_secrets(config)
        broker = (connect or _capital)(secrets)
        webhook = secrets.discord_webhook
        try:
            run(broker, config, today=day, notify=lambda text: discord.send(text, webhook))
        except BrokerError as exc:
            logger.error("盤中程式中止：%s", exc)
            return 1
        return 0


def _capital(secrets: settings.Secrets) -> Broker:
    """真的群益 broker。延到這裡才 import：沒有群益元件的機器也要能載入這個模組。"""
    from broker.capital import CapitalBroker
    return CapitalBroker(secrets.user_id, secrets.password, secrets.futures_account)


def _lock_path() -> str:
    return os.path.join(paths.state_dir(), "intraday.lock")


@contextlib.contextmanager
def _alone() -> Iterator[bool]:
    """同一時間只有一個盤中程式（ticket 09、10）：鎖住 `state/intraday.lock`，鎖不到就是
    另一個正在跑，交出 False。

    兩個同時跑，同一分鐘就會進兩次場。排程設了「已經在跑就不啟動新的」，但它只管得到自己
    叫醒的那一個行程：有人在命令列手動再跑一次，或排程只砍掉了外層的行程（venv 的
    `python.exe` 是一層轉手、真正的直譯器是它的子行程），第二個就起來了。

    用作業系統的檔案鎖，不用「檔案在不在」：行程不管怎麼死，鎖都跟著放掉；留下一個檔案的話，
    當掉之後的每一次重啟都會以為還有人在跑。鎖檔本身留著，不刪。
    """
    path = _lock_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        try:
            _lock(fd, True)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            _lock(fd, False)
    finally:
        os.close(fd)


def _lock(fd: int, on: bool) -> None:
    """鎖住（或放掉）檔案的第一個位元組，鎖不到立刻拋 OSError、不等。"""
    if sys.platform == "win32":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)        # msvcrt 鎖的是「目前位置起」的位元組
        msvcrt.locking(fd, msvcrt.LK_NBLCK if on else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, (fcntl.LOCK_EX | fcntl.LOCK_NB) if on else fcntl.LOCK_UN)


def _setup_logging(today: date) -> None:
    """同時寫到畫面與 `logs/intraday-YYYYMMDD.log`（`paths.daily_log`）。後者跟著 `logs/`
    30 天清除（ticket 10）；要長期保留的是原始行情，K 棒隨時能從它重算。

    root logger 已經有 handler 時（例如測試裡）`basicConfig` 什麼都不做，所以 log 檔延到第一次
    寫入才打開：沒被用上的 handler 不會留著一個開著的檔。"""
    os.makedirs(paths.logs_dir(), exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr),
                  logging.FileHandler(paths.daily_log("intraday", today), encoding="utf-8",
                                      delay=True)],
    )


if __name__ == "__main__":
    raise SystemExit(main())
