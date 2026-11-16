r"""筆差週報：從原始行情記錄算出 ticket 03 要回答的筆差事實。

用法（在專案根目錄，用排程同一個虛擬環境）：

    .\.venv\Scripts\python.exe tools\bichai_report.py                     # marketdata\ 裡的每一天
    .\.venv\Scripts\python.exe tools\bichai_report.py 20261117 20261118   # 只看這幾天

每一天印一段，兩天以上最後再印一段合計。ticket 03 要回答的事：

  * **累計起點**：群益給的累計成交筆數從 08:45 開始算，還是含夜盤。看開盤前（盤中程式 08:40
    起就在收）最後一次的累計筆數，以及開盤那一刻已經累計了全天日盤的幾成。
  * **正負方向**：大漲的時段筆差是不是正的、往上走。印出每分鐘筆差變化和價格變化的相關係數，
    以及當天漲最多、跌最多的 10 分鐘裡筆差怎麼變。
  * **±1000 的分鐘數、筆差跳增（±100）的次數**：對照作者說的「最近比較常不會進單」。
  * **累計筆數多久推一次**：`indicators` 假設群益照固定間隔推、或推得很密；不是的話很多 K 棒
    的筆差會是未知，條件就很少成立。

## 只讀原始行情記錄

不登入、不讀 log、不讀 `.env`。原始行情記錄沒有帳號資訊，印出來的東西可以直接貼給別人看。

K 棒與每根的筆差用盤中程式同一套 `indicators`、照檔案裡的順序重播（盤中重啟時讀回筆差也是
這樣做），門檻與跳增直接用 `strategy` 的常數。所以這裡的 ±1000 分鐘數、跳增次數，就是盤中程式
當天判斷時看到的。差別只在盤中重啟過的那天：當機那幾分鐘沒有記到行情，這裡沒有那幾根。
"""

from __future__ import annotations

import argparse
import os
import re
import statistics
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Sequence, TextIO

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import indicators  # noqa: E402
import paths  # noqa: E402
import raw_market  # noqa: E402
import strategy  # noqa: E402
from broker import TxTrade, TxTradeCounts  # noqa: E402
from calendar_tw import DAY_SESSION_OPEN  # noqa: E402

_WINDOW = timedelta(minutes=10)          # 「大漲的時段」看多長
_MINUTE = timedelta(minutes=1)
_FILE = re.compile(r"^TX-(\d{8})\.csv$")
_WEEKDAYS = "一二三四五六日"


@dataclass(frozen=True)
class Window:
    """一段 `_WINDOW` 長的時間：起訖是兩根 K 棒收盤的時刻，筆差是那兩根收盤時的（未知是 None）。"""

    start: datetime
    end: datetime
    change: float
    bichai_from: int | None
    bichai_to: int | None


@dataclass(frozen=True)
class DaySummary:
    day: date
    before_open: TxTradeCounts | None    # 08:45 之前收到的最後一次累計筆數
    at_open: TxTradeCounts | None        # 日盤裡收到的第一次
    at_close: TxTradeCounts | None       # 日盤裡收到的最後一次
    pushes: int                          # 日盤裡收到幾次累計筆數
    median_gap: float | None             # 相鄰兩次的間隔（秒）
    longest_gap: tuple[float, datetime] | None
    bars: int
    unknown: int                         # 筆差未知的 K 棒
    above: int                           # 筆差 > 門檻的分鐘數
    below: int                           # 筆差 < −門檻的分鐘數
    jumps_up: int
    jumps_up_above: int                  # 跳增 ≥ +100 而且這根筆差 > +1000（條件 3 的筆差部分）
    jumps_down: int
    jumps_down_below: int                # 條件 4 的筆差部分
    pairs: tuple[tuple[int, float], ...]  # 相鄰兩根都知道筆差時：（筆差變化, 收盤價變化）
    rise: Window | None                  # 漲最多的一段
    drop: Window | None                  # 跌最多的一段


def summarize(day: date, events: Sequence[TxTrade | TxTradeCounts]) -> DaySummary:
    """一天的原始行情事件（依收到的順序）→ 那一天的統計。"""
    bars = _replay(events)
    counts = [e for e in events if isinstance(e, TxTradeCounts)]
    before = [c for c in counts if c.time.time() < DAY_SESSION_OPEN]
    session = [c for c in counts if indicators.in_day_session(c.time)]
    gaps = [((b.time - a.time).total_seconds(), a.time) for a, b in zip(session, session[1:])]

    pairs: list[tuple[int, float]] = []
    up = up_above = down = down_below = 0
    for previous, last in zip(bars, bars[1:]):
        if previous.bichai is None or last.bichai is None:
            continue
        jump = last.bichai - previous.bichai          # 和 strategy._chase 同一個定義
        pairs.append((jump, last.bar.close - previous.bar.close))
        if jump >= strategy.BICHAI_JUMP:
            up += 1
            up_above += last.bichai > strategy.BICHAI_THRESHOLD
        elif jump <= -strategy.BICHAI_JUMP:
            down += 1
            down_below += last.bichai < -strategy.BICHAI_THRESHOLD

    windows = _windows(bars)
    rise = max(windows, key=lambda w: w.change, default=None)
    drop = min(windows, key=lambda w: w.change, default=None)
    known = [b.bichai for b in bars if b.bichai is not None]
    return DaySummary(
        day=day,
        before_open=before[-1] if before else None,
        at_open=session[0] if session else None,
        at_close=session[-1] if session else None,
        pushes=len(session),
        median_gap=statistics.median(g for g, _ in gaps) if gaps else None,
        longest_gap=max(gaps) if gaps else None,
        bars=len(bars),
        unknown=len(bars) - len(known),
        above=sum(b > strategy.BICHAI_THRESHOLD for b in known),
        below=sum(b < -strategy.BICHAI_THRESHOLD for b in known),
        jumps_up=up, jumps_up_above=up_above, jumps_down=down, jumps_down_below=down_below,
        pairs=tuple(pairs),
        rise=rise if rise is not None and rise.change > 0 else None,
        drop=drop if drop is not None and drop.change < 0 else None,
    )


def _replay(events: Sequence[TxTrade | TxTradeCounts]) -> list[indicators.ClosedBar]:
    """照順序重播，和盤中程式組出同樣的 K 棒與筆差。最後一根和 13:20 收工時一樣直接結束。"""
    state = indicators.BarState()
    bars: list[indicators.ClosedBar] = []
    for event in events:
        closed = None
        if isinstance(event, TxTrade):
            state, closed = indicators.add_trade(state, event)
        else:
            state = indicators.add_counts(state, event)
        if closed is not None:
            bars.append(closed)
    state, closed = indicators.close_forming(state)
    if closed is not None:
        bars.append(closed)
    return bars


def _windows(bars: list[indicators.ClosedBar]) -> list[Window]:
    by_start = {b.bar.start: b for b in bars}
    windows = []
    for first in bars:
        last = by_start.get(first.bar.start + _WINDOW)
        if last is not None:
            windows.append(Window(first.bar.start + _MINUTE, last.bar.start + _MINUTE,
                                  last.bar.close - first.bar.close, first.bichai, last.bichai))
    return windows


# ── 印出來 ──────────────────────────────────────────────────────────────


def render(summary: DaySummary) -> list[str]:
    s = summary
    lines = [f"{s.day:%Y-%m-%d}（{_WEEKDAYS[s.day.weekday()]}）"]
    lines += ["  累計起點：" + _start_verdict(s)]
    if s.before_open is not None:
        c = s.before_open
        lines.append(f"    開盤前最後一次（{c.time:%H:%M:%S}）：買進 {c.buy_count:,}、"
                     f"賣出 {c.sell_count:,}")
    else:
        lines.append("    開盤前（08:45 之前）沒有收到累計筆數")
    if s.at_open is not None and s.at_close is not None:
        lines.append(f"    開盤後第一次（{s.at_open.time:%H:%M:%S}）：合計 {_total(s.at_open):,} 筆；"
                     f"到 {s.at_close.time:%H:%M:%S} 是 {_total(s.at_close):,} 筆")
    if s.pushes:
        gap = (f"間隔中位數 {s.median_gap:g} 秒，最長 {s.longest_gap[0]:g} 秒"
               f"（{s.longest_gap[1]:%H:%M:%S} 起）" if s.longest_gap else "只有一次")
        lines.append(f"  累計筆數推送：日盤裡 {s.pushes:,} 次，{gap}")
    lines.append(f"  1 分 K：{s.bars} 根，筆差未知 {s.unknown} 根")
    lines.append(f"  筆差 > +{_th()}：{s.above} 分鐘；< -{_th()}：{s.below} 分鐘")
    lines.append(f"  筆差跳增 ≥ +{_jump()}：{s.jumps_up} 次（其中筆差 > +{_th()} 時 "
                 f"{s.jumps_up_above} 次）；≤ -{_jump()}：{s.jumps_down} 次"
                 f"（其中筆差 < -{_th()} 時 {s.jumps_down_below} 次）")
    lines.append("  方向：" + _direction(s.pairs))
    lines.append("    " + (_window("漲最多", s.rise) if s.rise else "沒有上漲的 10 分鐘"))
    lines.append("    " + (_window("跌最多", s.drop) if s.drop else "沒有下跌的 10 分鐘"))
    return lines


def render_total(summaries: Sequence[DaySummary]) -> list[str]:
    n = len(summaries)
    above = sum(s.above for s in summaries)
    below = sum(s.below for s in summaries)
    up = sum(s.jumps_up_above for s in summaries)
    down = sum(s.jumps_down_below for s in summaries)
    pairs = [p for s in summaries for p in s.pairs]
    return [
        f"合計 {n} 天",
        f"  筆差 > +{_th()}：{above} 分鐘（平均每天 {above / n:.1f}）；"
        f"< -{_th()}：{below} 分鐘（平均每天 {below / n:.1f}）",
        f"  筆差過門檻又跳增（條件 3、4 的筆差部分）：多方 {up} 次、空方 {down} 次",
        f"  筆差未知：{sum(s.unknown for s in summaries)} / {sum(s.bars for s in summaries)} 根",
        "  方向：" + _direction(pairs),
    ]


def _start_verdict(s: DaySummary) -> str:
    """群益的累計筆數從什麼時候開始算。只是推測，數字印在下面讓人自己看。"""
    if s.at_open is None or s.at_close is None:
        return "日盤裡沒有收到累計筆數，判斷不了"
    opening = _total(s.at_open)
    if s.before_open is not None and opening < _total(s.before_open):
        return "開盤時歸零重算：從 08:45 起算（開盤前收到的是上一盤留下的數字）"
    growth = _total(s.at_close) - opening
    if growth <= 0:
        return "日盤裡累計筆數沒有增加，判斷不了"
    share = opening / growth
    if share < 0.05:
        return f"開盤時只累計了全天日盤的 {share:.0%}：看起來從 08:45 起算"
    if share > 0.2:
        return f"開盤時已經累計了全天日盤的 {share:.0%}：看起來含夜盤（或更早）"
    return f"開盤時累計了全天日盤的 {share:.0%}：看不出來，看下面的數字"


def _direction(pairs: Sequence[tuple[int, float]]) -> str:
    try:
        r = statistics.correlation([float(b) for b, _ in pairs], [p for _, p in pairs])
    except statistics.StatisticsError:          # 不到兩對，或其中一邊從頭到尾沒變
        return "資料不夠，算不出筆差變化和價格變化的關係"
    reading = ("價漲時筆差多半往上，和「正值是大戶在買」一致" if r > 0.2 else
               "價漲時筆差多半往下：和「正值是大戶在買」相反，筆差的正負可能定義反了" if r < -0.2 else
               "看不出明顯關係")
    return f"每分鐘筆差變化和價格變化的相關係數 {r:+.2f}，{reading}"


def _window(label: str, w: Window) -> str:
    return (f"{label}的 10 分鐘 {w.start:%H:%M}～{w.end:%H:%M} {'漲' if w.change > 0 else '跌'} "
            f"{abs(w.change):g} 點，筆差 {_bichai(w.bichai_from)} → {_bichai(w.bichai_to)}")


def _bichai(value: int | None) -> str:
    return "未知" if value is None else f"{value:+,}"


def _total(c: TxTradeCounts) -> int:
    return c.buy_count + c.sell_count


def _th() -> str:
    return f"{strategy.BICHAI_THRESHOLD:,}"


def _jump() -> str:
    return f"{strategy.BICHAI_JUMP:,}"


# ── 主程式 ──────────────────────────────────────────────────────────────


def recorded_days() -> list[date]:
    """原始行情目錄裡有記錄的交易日，由舊到新。"""
    try:
        names = os.listdir(paths.market_data_dir())
    except FileNotFoundError:
        return []
    return sorted(datetime.strptime(m.group(1), "%Y%m%d").date()
                  for m in map(_FILE.match, names) if m)


def _day(text: str) -> date:
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"日期要寫成 YYYYMMDD，例如 20261117：{text!r}") from None


def main(argv: list[str] | None = None, *, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description="從原始行情記錄算出 ticket 03 的筆差統計（不登入）")
    parser.add_argument("days", nargs="*", type=_day,
                        help="交易日，例如 20261117；不給就是 marketdata 裡的每一天")
    days = parser.parse_args(argv).days or recorded_days()
    if not days:
        print(f"{paths.market_data_dir()} 裡還沒有任何原始行情記錄", file=out)
        return 1
    summaries = []
    for day in days:
        events = raw_market.read(day)
        if not events:
            print(f"{day:%Y-%m-%d}：沒有原始行情記錄\n", file=out)
            continue
        summary = summarize(day, events)
        summaries.append(summary)
        print("\n".join(render(summary)) + "\n", file=out)
    if len(summaries) > 1:
        print("\n".join(render_total(summaries)), file=out)
    return 0 if summaries else 1


if __name__ == "__main__":
    raise SystemExit(main())
