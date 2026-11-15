"""了結交接：PTS 還沒了結的口數由誰平、幾點前、平幾口（GLOSSARY「了結交接」）。

同一筆部位會被三支程式講到：盤中程式的告警、收工指令印的、13:25 出場程式的告警。三者講的必須是
同一個答案，而且和出場程式**實際會做的**一致——講錯了，使用者自己平一次、程式再平一次，就是
反向新倉；或是兩邊都以為對方會平，微台就過夜。所以答案只在這裡算：出場程式拿它決定送不送單、
送到幾點（`deadline`、`exit_looks_up`），`messages` 拿它決定怎麼講（`decide`、`lots_to_close`）。

## 收盤前的時間表（ADR-0002）

    13:20  進場截止（`strategy.ENTRY_CUTOFF`）：收工指令只在這之前開始平倉
    13:24  收工指令最後能送平倉單的時刻（`STOP_COMMAND_UNTIL`）
    13:25  出場程式被叫醒（`EXIT_START`），平微台
    13:30  券商當沖強平大台、小台（`DAY_TRADE_CLOSE`）；結算日的近月停止交易（`SETTLEMENT_CLOSE`）
    13:45  日盤收盤（`calendar_tw.DAY_SESSION_CLOSE`）：之後只能在夜盤平

## 誰來平看部位檔，平幾口看帳上

出場程式只認部位檔：檔裡記著什麼，它就照什麼做。所以「13:25 會不會平、會不會先查」問的是
**部位檔裡的那一份**（`decide` 的 `on_file`）：盤中程式寫檔失敗時，檔裡是送單前記下的「不確定」，
沒有委託序號，出場程式查不了，帳上真正多出的那筆它也不知道。要人平幾口問的則是**帳上**
（`lots_to_close` 的 `record`）：包含檔裡沒記到的那幾口。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date, datetime, time

import paths
import position_file
from broker import DAY_TRADE_CODES
from calendar_tw import DAY_SESSION_CLOSE

# 出場程式被叫醒（排程 `tools/schedule.py` 照它建）。13:20 進場截止之後、券商 13:30 當沖強平之前。
EXIT_START = time(13, 25)
# 收工指令送平倉單送到出場程式被叫醒的前一分鐘：兩者不會同時送單
STOP_COMMAND_UNTIL = time(13, 24)
# 券商自動平掉標了當沖的部位（大台、小台，ADR-0002）
DAY_TRADE_CLOSE = time(13, 30)
# 結算日的近月停止交易：還在的部位由交易所以最後結算價現金了結
SETTLEMENT_CLOSE = time(13, 30)


class Who(enum.Enum):
    """PTS 還沒了結的口數由誰平。"""

    EXIT_PROGRAM = "13:25 的出場程式照部位檔平"
    # 盤中留下一筆有委託序號的微台進場不確定：13:25 先查，查到全部成交才平，查不到就告警
    EXIT_LOOKS_UP = "13:25 的出場程式先用委託序號查，查到全部成交就平"
    BROKER = "券商 13:30 當沖強平"
    USER = "要你自己平"
    # 結算日 13:30 之後還在的微台：不能再平
    EXCHANGE = "交易所以最後結算價現金了結"


@dataclass(frozen=True)
class Handoff:
    """`decide` 的答案。"""

    who: Who
    # 微台：最後能送平倉單的時刻（`deadline`）；大台、小台：券商當沖強平的時刻
    deadline: datetime
    # 說的這一刻已經過了 `deadline`：微台只能在夜盤平（結算日的話就是 `Who.EXCHANGE`）
    passed: bool
    # 說的這一刻 13:25 已經到了：出場程式已經起來（它可能正等著收工指令做完，也可能已經告警過了）
    exit_started: bool
    settles_today: bool


@dataclass(frozen=True)
class Lots:
    """要人平幾口：部位檔確定還沒平的 `held` 口，加上（進場）或減掉（平倉）`uncertain` 那一筆實際
    成交的口數，所以是 `low`～`high` 口。沒有不確定的委託時 `low == high == held`。"""

    held: int
    low: int
    high: int
    uncertain: position_file.UncertainOrder | None = None

    @property
    def adds(self) -> bool:
        """不確定的那一筆是進場單：它成交幾口，就要多平幾口。"""
        return self.uncertain is not None and self.uncertain.stage == position_file.ENTRY


@dataclass(frozen=True)
class FillLogs:
    """PTS 自己也不知道口數時（部位檔讀不懂、出場程式中止），口數從哪幾份 log 算：盤中程式的
    「進場成交」，出場程式與收工指令的「平倉成交」。**每一份都要列**：漏掉收工指令的，算出來就
    多了它平掉的口數，使用者照著平就是反向新倉。"""

    entry: str
    closes: tuple[str, ...]


def deadline(record: position_file.PositionRecord) -> datetime:
    """微台最後能送平倉單的時刻：日盤收盤；結算日是近月停止交易的 13:30。

    出場程式、收工指令過了它就不送單（收工指令還有自己更早的期限），告警裡要人在幾點前平、
    過了就講夜盤或交易所了結，都照它。"""
    close = SETTLEMENT_CLOSE if record.settles_today else DAY_SESSION_CLOSE
    return datetime.combine(record.trading_day, close)


def stop_command_until(day: date) -> datetime:
    """收工指令最後能送平倉單的時刻（13:24）：13:25 的出場程式被叫醒之前。"""
    return datetime.combine(day, STOP_COMMAND_UNTIL)


def exit_start(day: date) -> datetime:
    return datetime.combine(day, EXIT_START)


def day_closed(at: datetime) -> bool:
    """日盤已經收盤（13:45），只能在夜盤平。不知道是哪個商品、是不是結算日的時候（部位檔讀不懂、
    出場程式中止）只看得出這一點；知道的話用 `decide`。"""
    return at.time() >= DAY_SESSION_CLOSE


def left_to_broker(product: str) -> bool:
    """大台、小台進場時標了當沖，券商 13:30 強平：出場程式不送單，告警也不叫人自己平——
    兩邊都平，多平的就是反向新倉（ADR-0002）。"""
    return product in DAY_TRADE_CODES


def exit_looks_up(record: position_file.PositionRecord) -> bool:
    """部位檔記著的「不確定」，13:25 能不能用委託序號向券商查（`closing` 的進場查詢）。

    只查微台的進場單：大台、小台交給券商 13:30 當沖強平，查到也不會送單（而且大台、小台的
    成交列欄位還沒實機驗證，ticket 11）；出場單的不確定是送出它的那一方自己留下的，當時已經查過；
    沒有委託序號（送單前記下的「不確定」、序號沒寫進去）就沒有東西可查。

    這一條同時決定兩件事：出場程式、收工指令要不要先查再平，與盤中的告警叫不叫人等 13:25。
    兩者不一致的話，使用者自己平了，13:25 查到成交又平一次。
    """
    u = record.uncertain
    return (u is not None and u.stage == position_file.ENTRY and bool(u.order_seq)
            and not left_to_broker(record.product))


def decide(on_file: position_file.PositionRecord, *, at: datetime,
           exit_done: bool) -> Handoff:
    """`at` 這一刻，部位檔記著 `on_file` 時，PTS 還沒了結的口數由誰平。

    `exit_done`：13:25 的出場程式已經處理完這一份、不會再送單了——說話的就是出場程式本身
    （它平不完、碰到不確定時才講），或是它之前的交易日（補跑發現）。盤中程式與收工指令講的時候，
    出場程式還會照部位檔做一次。
    """
    started = at >= exit_start(on_file.trading_day)
    if left_to_broker(on_file.product):
        close = datetime.combine(on_file.trading_day, DAY_TRADE_CLOSE)
        return Handoff(Who.BROKER, close, at >= close, started, on_file.settles_today)
    last = deadline(on_file)
    passed = at >= last
    if not exit_done:
        if exit_looks_up(on_file):
            return Handoff(Who.EXIT_LOOKS_UP, last, passed, started, on_file.settles_today)
        if on_file.uncertain is None:
            return Handoff(Who.EXIT_PROGRAM, last, passed, started, on_file.settles_today)
        # 出場程式看到查不了的「不確定」一張單都不送（規格第 50 條）：從現在起就是你的
    who = Who.EXCHANGE if passed and on_file.settles_today else Who.USER
    return Handoff(who, last, passed, started, on_file.settles_today)


def lots_to_close(record: position_file.PositionRecord) -> Lots:
    """帳上 PTS 還有幾口要平（`record` 是帳上的那一份，不是部位檔的）。

    進場那一筆不確定：成交了幾口就多平幾口。平倉那一筆不確定：成交了幾口就少平幾口——檔裡的口數
    還沒扣掉它。
    """
    return _lots(record.open_lots, record.uncertain)


def entry_lots(held: int, *, requested: int, known_filled: int) -> Lots:
    """盤中送出的進場單不知道成交幾口時（`intraday`），連同部位檔已確認的 `held` 口要平幾口。

    和 `lots_to_close` 同一條算式，只是不必先組出一份部位檔：broker 給的數字組不成一筆不確定的
    委託時（`position_file.UncertainOrder` 會擋），告警照樣要發。"""
    return Lots(held, held + known_filled, held + requested)


def _lots(held: int, u: position_file.UncertainOrder | None) -> Lots:
    if u is None:
        return Lots(held, held, held)
    if u.stage == position_file.ENTRY:
        return Lots(held, held + u.known_filled, held + u.requested_lots, u)
    return Lots(held, max(0, held - u.requested_lots), held - u.known_filled, u)


def fill_logs(day: date) -> FillLogs:
    """那一天記著 PTS 每一筆成交的 log（`FillLogs`）。"""
    return FillLogs(entry=paths.shown_log("intraday", day),
                    closes=(paths.shown_log("exit", day), paths.shown_log("stop", day)))
