"""盤中程式：每個交易日 08:40 啟動，收大台行情、組 1 分 K、存原始行情，13:20 結束。

用法（在專案根目錄）：

    python intraday.py

ticket 02 只有最小路徑：**不判斷、不下單、不發訊息**。它的價值是讓實機資料盡早開始
累積（ticket 03 要用一週的原始行情回答筆差的事實問題）。之後的 ticket 在 `run()` 的
「一根 K 棒結束」那一點接上策略判斷，在成交事件上接突破進場的盯價。

## 時間

盤中程式的「現在」**一律取自事件上的時間**，不讀系統時鐘（規格「時間」一節）。
唯一的例外是「今天是哪一天」：它要在登入之前就知道（非交易日不登入），那時還沒有任何
事件，所以由 `main()` 在啟動時讀一次系統日期，當參數傳進來。

事件上的時間有兩種來源，這裡刻意分開用：

  * **成交（TxTrade）帶的是交易所的時間**。K 棒的分界只看它：K 棒要和看盤軟體一致，
    而本機時鐘和交易所差個一兩秒，就會把每一分鐘最後幾秒的成交切到下一根。
  * **時鐘（Clock）與累計筆數帶的是本機收到的時刻**。13:20 收工、報價交易日的判斷看它們，
    因為那兩件事在沒有成交的時候也要發生。
"""

from __future__ import annotations

import enum
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, time

import calendar_tw
import indicators
import paths
import settings
from broker import Broker, BrokerError, ProductTrade, TxTrade, TxTradeCounts
from raw_market import RawMarketWriter

logger = logging.getLogger(__name__)

# 13:20 進場截止，盤中程式在那一刻收工（規格「兩支程式」一節）。13:19 那根到這時結束，
# 照樣交出來；之後的事件不組 K 棒、也不寫進原始行情。
SESSION_END = time(13, 20)

# 報價交易日的判斷點：**08:45 那根 K 棒結束**時，還沒有今天日盤的成交，卻收過別的交易日
# 的成交，就是報價屬於前一個交易日（臨時休市，規格第 10 條）。
#
# ⚠️ 不能一收到前一天的成交就下結論：一般交易日 08:40 訂閱時，回補的也可能是前一個
#    交易日的成交（休市時回補最近一個日盤，2026-08-16 實測），今天的要到 08:45 才有。
# ⚠️ 一筆都沒收到不算數：那是報價中斷（ticket 09），不是證據。拿它收工的話，開盤時一次
#    斷線就賠掉一整天。
# 等到 08:46 的代價是零：不屬於今天的成交本來就不組 K 棒，不會被拿去判斷。
QUOTE_DAY_DEADLINE = time(8, 46)


class Ending(enum.Enum):
    """盤中程式為什麼結束。寫進 log，給人看的。"""

    NOT_A_TRADING_DAY = "非交易日，沒有登入"
    QUOTES_FROM_ANOTHER_DAY = "報價屬於前一個交易日，不判斷"
    CUTOFF = "13:20 收工"
    # 真的群益串流不會自己結束；會走到這裡的只有假 broker 的劇本放完了。
    STREAM_ENDED = "行情串流在 13:20 之前結束"


@dataclass(frozen=True)
class SessionOutcome:
    """一次盤中執行的結果：為什麼結束，以及今天組出來的每一根 1 分 K（依時間排序）。"""

    ending: Ending
    bars: tuple[indicators.ClosedBar, ...] = ()


def run(broker: Broker, config: settings.Config, *, today: date) -> SessionOutcome:
    """跑一個交易日的上午。`today` 是啟動當天的日期（見模組說明「時間」）。

    原始行情會**先暫存、確認報價屬於今天之後才寫檔**：開盤前的累計筆數（08:40 起）
    要留著回答「從什麼時候開始累計」（ticket 03），但臨時休市那天一行都不能寫——
    那一天沒有屬於它的行情。
    """
    if not calendar_tw.is_trading_day(today):
        logger.info("%s 不是交易日，不登入", today)
        return SessionOutcome(Ending.NOT_A_TRADING_DAY)

    broker.login()
    end_at = datetime.combine(today, SESSION_END)
    deadline = datetime.combine(today, QUOTE_DAY_DEADLINE)
    state = indicators.BarState()
    bars: list[indicators.ClosedBar] = []
    raw = RawMarketWriter(today)
    confirmed = False               # 收到今天日盤的成交了
    other_day: date | None = None   # 收過哪一個別的交易日的大台成交

    def _close(closed: indicators.ClosedBar | None) -> None:
        if closed is not None:
            bars.append(closed)
            _log_bar(closed)

    ending = Ending.STREAM_ENDED
    try:
        for event in broker.events(config.product):
            if isinstance(event, (TxTrade, ProductTrade)) and event.time.date() != today:
                # 不屬於今天的成交：不組 K 棒、不記、也不拿它的時間當「現在」
                if isinstance(event, TxTrade):
                    other_day = event.time.date()
                continue
            if event.time >= end_at:
                state, closed = indicators.close_forming(state)
                _close(closed)
                ending = Ending.CUTOFF
                break
            if not confirmed and other_day is not None and event.time >= deadline:
                logger.warning("%s 還沒有今天的成交，收到的是 %s 的報價（臨時休市？）",
                               f"{QUOTE_DAY_DEADLINE:%H:%M}", other_day)
                ending = Ending.QUOTES_FROM_ANOTHER_DAY
                break
            if isinstance(event, TxTrade):
                if not confirmed and indicators.in_day_session(event.time):
                    confirmed = True
                    raw.confirm()
                raw.trade(event)
                state, closed = indicators.add_trade(state, event)
                _close(closed)
            elif isinstance(event, TxTradeCounts):
                raw.counts(event)
                state = indicators.add_counts(state, event)
    finally:
        raw.close()

    logger.info("盤中程式結束：%s，今天共 %d 根 1 分 K", ending.value, len(bars))
    return SessionOutcome(ending, tuple(bars))


def _log_bar(closed: indicators.ClosedBar) -> None:
    """每一根都寫進 log：實機時要拿它和看盤軟體的大台 1 分 K 逐根核對。"""
    bar = closed.bar
    bichai = "未知（還沒收到累計成交筆數）" if closed.bichai is None else closed.bichai
    logger.info("1 分 K %s 開 %g 高 %g 低 %g 收 %g 筆差 %s",
                f"{bar.start:%H:%M}", bar.open, bar.high, bar.low, bar.close, bichai)


def main() -> int:
    """排程的進入點（ticket 10 建排程）。登入、收行情、13:20 結束。"""
    from broker.capital import CapitalBroker

    # 今天是哪一天：**只在這裡讀一次系統時鐘**（見模組說明「時間」）
    today = date.today()
    _setup_logging(today)

    config = settings.load()
    secrets = settings.load_secrets(config)
    broker = CapitalBroker(secrets.user_id, secrets.password, secrets.futures_account)
    try:
        run(broker, config, today=today)
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
