"""盤中程式當掉被重啟（規格第 48 條）——從最上層打進去，跑兩輪 `intraday.run`。

第一輪的劇本在某個時刻就沒了：程式在那裡停了。第二輪是重啟之後 broker 送來的樣子：
先一個本機時鐘，接著群益第一次訂閱時回補的今天的成交（`replayed`，只有成交、帶交易所
當時的時間），然後才是即時的行情。兩輪之間只靠檔案：部位檔、停止進場記錄、原始行情檔。

斷言送出的委託、發出的訊息、部位檔，以及 `run()` 交回的 1 分 K（每根結束時的筆差）。
"""

import json
import os

import pytest

from broker import MTX_CODE, TMF_CODE
from broker.fake import FakeBroker
from day_script import (DAY, at, clock, counts, make_config, minute, rally, restarted_at,
                        run_day, trade, until)
from recording import RecordingNotifier
import day_settings
import exit_program
import position_file


def _bichai(outcome) -> list[tuple]:
    return [(f"{b.bar.start:%H:%M}", b.bichai) for b in outcome.bars]


def test_after_a_restart_the_bichai_of_each_bar_comes_back_from_the_raw_market_file():
    """08:45 起每根的筆差是 1000、1010、1020…。第一輪在 08:50:40 之後當掉，08:54:10 重啟。

    群益的筆差沒有歷史回補：重啟後回補的只有成交。08:45–08:49 每根結束時的筆差只能從
    PTS 自己的原始行情檔讀回來。08:50 那根在當掉時還沒結束，08:51–08:53 程式不在：
    它們結束那一刻的筆數都沒有記到，筆差是「不知道」——不是當掉前最後的那個值，
    也不是重啟後收到的值。重啟之後的 08:54 起照常。"""
    day = rally("08:45", 12, bichai=1000, bichai_step=10) + [trade(at("08:57:00"), 45230)]
    run_day(until(day, at("08:50:40")))
    _, _, outcome = run_day(restarted_at(day, at("08:54:10")))

    assert _bichai(outcome) == [
        ("08:45", 1000), ("08:46", 1010), ("08:47", 1020), ("08:48", 1030), ("08:49", 1040),
        ("08:50", None), ("08:51", None), ("08:52", None), ("08:53", None),
        ("08:54", 1090), ("08:55", 1100), ("08:56", 1110)]


@pytest.mark.parametrize("restart", ["08:52:50", "08:55:10"],
                         ids=["當掉時還沒結束的那一根", "程式不在的那幾分鐘"])
def test_counts_that_piled_up_while_the_program_was_down_are_not_a_bichai_jump(restart):
    """自動下單打開，08:50 條件 1 進場做多一口，之後大台橫盤（開盤價沒有比上一根高，不是紅K），
    筆差 1500。08:51:40 之後當掉，最後記到的累計筆數是 08:51:20 的 1500。當機期間筆數照樣在
    累積，重啟後收到的已經是 1700。

    拿兩者相減，重啟後的第一根就是 +200 的「筆差跳增」，條件 3 立刻追多加碼——那是當機前後
    的累積，不是一分鐘裡突然大量買進（ticket 09）。兩種重啟時機都不可以：

      * 08:52:50 重啟（排程重啟加上登入，一分鐘多是常態）：08:51 那根在當掉時還沒結束，
        結束那一刻的筆數沒有記到；08:52:55 收到第一次筆數 1700，08:52 那根和它相減。
      * 08:55:10 重啟：08:52–08:54 程式不在，那幾根結束時手上的還是當掉前的 1500。

    之後 08:56 那根真的 +100，條件 3 照常加碼，證明重啟後照常在判斷。"""
    flat = []
    for i, bichai in enumerate([1500, 1500, 1600, 1700, 1700, 1700, 1800]):
        flat += minute(f"08:{50 + i}", 45140, 45140, bichai=bichai)
        if i == 2:
            flat.append(counts(at("08:52:55"), buy=20000, sell=21700))
    day = rally("08:45", 5) + flat + [trade(at("08:57:00"), 45140)]
    on = make_config(auto_order_enabled=True)
    first, _, _ = run_day(until(day, at("08:51:40")), config=on)
    assert [o.lots for o in first.orders] == [1]

    _, sent, _ = run_day(restarted_at(day, at(restart)), config=on)
    assert [(m.splitlines()[1], m.splitlines()[-1]) for m in sent] == [
        ("觸發：條件 3（追多加碼）", "2026/11/09 08:57:00")]


def test_bars_with_trades_missing_before_the_crash_are_still_not_used_after_a_restart():
    """強勢的早上，條件 1 每分鐘成立。大台成交在 08:50:00 之後斷了 70 秒，08:51:10 才接回來：
    08:50、08:51 兩根缺了成交（見 test_quote_outage）。08:52:40 當掉，08:53:10 重啟。

    重啟後手上只有原始行情檔，記憶體裡「哪幾根缺了成交」跟著當掉的那一輪一起沒了；從檔裡的
    成交時間重新認出來，它們還在最近 6 根裡時照樣不判斷——和沒當掉的那一天一樣，08:58:00
    才又進場（模擬模式，重啟前那一輪的模擬部位不留）。"""
    morning = (rally("08:45", 5)
               + [clock(at("08:50:00")), trade(at("08:50:00"), 45150),
                  counts(at("08:50:20"), buy=20000, sell=21500)]
               + [clock(at("08:51:00"))]
               + [trade(at("08:51:10"), 45160), counts(at("08:51:20"), buy=20000, sell=21500),
                  trade(at("08:51:30"), 45170)]
               + rally("08:52", 7, open_=45180) + [trade(at("08:59:00"), 45260)])
    run_day(until(morning, at("08:52:40")))

    _, sent, _ = run_day(restarted_at(morning, at("08:53:10")))

    assert [m.splitlines()[-1] for m in sent] == ["2026/11/09 08:58:00", "2026/11/09 08:59:00"]


# --- 重啟得很快：當掉前一刻剛判斷過的那一根不再判斷一次 ---


def test_a_quick_restart_does_not_judge_the_last_bar_again():
    """自動下單打開，08:50–08:53 每分鐘條件 1 都成立。08:50:00 那筆成交讓 08:49 那根結束、
    進場一口，08:50:05 當掉，08:50:20 就重啟了（手動重開、或排程立刻重啟）。

    群益回補的成交裡有 08:50:00 那一筆，離重啟才 20 秒，還算即時；累計筆數一訂閱就推來
    現在的值（比回補的成交先到）。再拿那筆成交結束一次 08:49 那根，條件 1 照樣成立，
    就是同一分鐘判斷兩次、多送一口（ticket 04 留下的問題）。重啟前就處理過的成交不再處理。
    08:50 那根在當掉時還沒結束，筆差當成不知道，08:51 不進場；08:52 起照常，累計接在
    部位檔原來的口數後面。"""
    day = rally("08:45", 8) + [trade(at("08:53:00"), 45180)]
    on = make_config(auto_order_enabled=True)
    first, _, _ = run_day(until(day, at("08:50:05")), config=on)
    assert len(first.orders) == 1

    restart = restarted_at(day, at("08:50:20"))
    restart.insert(1, counts(at("08:50:20"), buy=20000, sell=21500))
    second, sent, _ = run_day(restart, config=on)
    assert len(second.orders) == 2
    assert [(m.splitlines()[2], m.splitlines()[-1]) for m in sent] == [
        ("累計：多單 2 口，均價 45160", "2026/11/09 08:52:00"),
        ("累計：多單 3 口，均價 45166.67", "2026/11/09 08:53:00")]
    assert [e["lots"] for e in _position()["entries"]] == [1, 1, 1]


def _position() -> dict:
    with open(position_file.path_for(DAY), encoding="utf-8") as fh:
        return json.load(fh)


def test_the_raw_market_file_keeps_each_trade_once_across_a_restart(market_dir):
    """重啟後群益回補當天的成交，裡面大半重啟前就記過了。原始行情檔每一筆成交只記一次、
    依時間排好——ticket 03 拿它看筆差，作者日後拿它做 tick 等級的回測，重複的成交會讓
    兩者都失真。當機期間的成交（08:51–08:54）由回補補上。"""
    day = rally("08:45", 12, bichai=1000, bichai_step=10) + [trade(at("08:57:00"), 45230)]
    run_day(until(day, at("08:50:40")))
    run_day(restarted_at(day, at("08:54:10")))

    rows = (market_dir / "TX-20261109.csv").read_text(encoding="utf-8").splitlines()[1:]
    trades = [r.split(",")[0][11:19] for r in rows if r.split(",")[1]]
    assert trades == [f"08:{m:02d}:{s:02d}" for m in range(45, 57) for s in (0, 30)] + ["08:57:00"]


# --- 送出委託、還沒記下成交就當掉（規格第 48、50 條）---


class _Killed(BaseException):
    """盤中程式被硬性中止（被關掉、COM 存取違規）：不是程式裡的例外處理接得住的那種。"""


def test_an_order_left_waiting_for_its_fill_by_a_crash_is_uncertain_after_the_restart():
    """自動下單打開、交易微台，08:50–08:53 每分鐘條件 1 都成立。08:50 送出進場單之後，在等
    成交回報的那段時間（最長 30 秒）盤中程式被硬性中止，部位檔來不及寫；08:51:10 重啟。

    那張單可能已經成交了，而盤中程式只認自己這一輪的委託序號（規格第 56 條），它的回報重啟後
    不會再被認領。所以送單**之前**部位檔就要記下「送出去了、還不知道成交」：

      * 重啟後今天不再進場：當成沒送過的話，同一個條件下一分鐘再送一次。告警由重啟後的這一輪
        發——當掉的那一輪來不及發（規格第 50 條）。
      * 13:25 出場程式看到「不確定」就不平倉、告警：照部位檔裡的口數平的話，多出來的那一口
        沒人平，微台就過夜。"""
    morning = rally("08:45", 8) + [trade(at("08:53:00"), 45180)]
    on = make_config(auto_order_enabled=True)
    with pytest.raises(_Killed):
        run_day(until(morning, at("08:50:40")), config=on, order_errors=[_Killed()])

    second, sent, _ = run_day(restarted_at(morning, at("08:51:10")), config=on)

    assert second.orders == []
    assert sent == [
        "【要你處理】PTS 委託送出去了，但不知道成交幾口\n"
        "這次：買進微台 1 口（沒拿到委託序號）\n"
        "原因：盤中程式送出這筆委託之後、記下成交之前就停了（當掉或被關掉），"
        "重啟後收不到它的回報\n"
        "部位檔已記為「不確定」：盤中程式今天不再進場，13:25 的出場程式也不會自動平倉。\n"
        "請到券商 APP 確認這筆成交了幾口。微台沒有當沖強平：PTS 今天的微台部位要你自己在 "
        "13:30 前平掉。\n"
        "2026/11/09 08:51:10"]

    exit_broker = FakeBroker(fill_price=45200.0)
    notifier = RecordingNotifier()
    exit_program.run(exit_broker, on, notify=notifier, clock=lambda: at("13:25:00"),
                     sleep=lambda seconds: None)
    assert exit_broker.orders == []
    assert ("【要你處理】PTS 委託送出去了，但不知道成交幾口，出場程式不再送單"
            in [m.splitlines()[0] for m in notifier.sent])


def test_after_the_restart_that_reported_it_a_further_restart_stays_quiet():
    """當掉留下的「不確定」，重啟後那一輪已經告警、寫下停止進場記錄。之後再重啟一次（又當掉、
    或使用者手動重開）：照樣不進場，但不再告警——同一件事講一次就夠。"""
    morning = rally("08:45", 8) + [trade(at("08:53:00"), 45180)]
    on = make_config(auto_order_enabled=True)
    with pytest.raises(_Killed):
        run_day(until(morning, at("08:50:40")), config=on, order_errors=[_Killed()])
    run_day(until(restarted_at(morning, at("08:51:10")), at("08:51:40")), config=on)

    third, sent, _ = run_day(restarted_at(morning, at("08:52:10")), config=on)

    assert third.orders == []
    assert sent == []


# --- 交易商品與口數以部位檔為準（規格第 43、48 條）---

# 08:50 起條件 1 每分鐘成立。08:51:40 當掉、08:52:50 重啟的話，08:52 那根在當掉時還沒結束
# （筆差不知道，不判斷），重啟後 08:54、08:55 各判斷一次。
_STRONG = rally("08:45", 10) + [trade(at("08:55:00"), 45200)]


def test_after_a_restart_the_lots_and_product_on_file_carry_on_even_if_the_settings_changed():
    """自動下單打開、交易小台：08:50、08:51 各進一口，08:51:40 當掉。使用者趁這時把設定改成
    微台，08:52:50 盤中程式被重啟。今天已經有小台的部位：照樣看小台的行情、下小台的單，
    累計接在原來的 2 口後面（加碼上限也是）；新設定從下一個交易日起才生效。"""
    first, _, _ = run_day(until(_STRONG, at("08:51:40")),
                          config=make_config(auto_order_enabled=True, product=MTX_CODE))
    assert [o.contract.order_code for o in first.orders] == ["MTX11", "MTX11"]

    second, sent, _ = run_day(restarted_at(_STRONG, at("08:52:50")),
                              config=make_config(auto_order_enabled=True, product=TMF_CODE))
    assert second.streamed == [MTX_CODE]
    assert [o.contract.order_code for o in second.orders] == ["MTX11", "MTX11"]
    assert [m.splitlines()[0::2] for m in sent] == [
        ["PTS 做多：買進小台 1 口 @ 45190", "累計：多單 3 口，均價 45166.67"],
        ["PTS 做多：買進小台 1 口 @ 45200", "累計：多單 4 口，均價 45175"]]
    assert _position()["product"] == MTX_CODE


# --- 當天的設定在第一次啟動時就定了（規格「設定」一節：改設定最快下一個交易日才生效）---


def test_switching_auto_order_off_during_the_day_takes_effect_the_next_trading_day():
    """自動下單打開，08:50、08:51 真實成交各 1 口，08:51:40 當掉。使用者趁這時把自動下單關掉，
    08:52:50 被重啟。今天的設定在第一次啟動時就定了：照樣真實下單。重讀設定的話，之後的「模擬」
    進場會疊在帳上的 2 口真單上，訊息講的累計口數就不是帳上的部位（規格第 37 條）。"""
    on = make_config(auto_order_enabled=True)
    run_day(until(_STRONG, at("08:51:40")), config=on)

    second, sent, _ = run_day(restarted_at(_STRONG, at("08:52:50")), config=make_config())

    assert len(second.orders) == 2
    assert [m.splitlines()[0::2] for m in sent] == [
        ["PTS 做多：買進微台 1 口 @ 45190", "累計：多單 3 口，均價 45166.67"],
        ["PTS 做多：買進微台 1 口 @ 45200", "累計：多單 4 口，均價 45175"]]
    assert len(_position()["entries"]) == 4


def test_switching_auto_order_on_during_the_day_does_not_send_real_orders_today():
    """反過來：早上是模擬模式，08:51:40 當掉時使用者把自動下單打開。重啟後照樣模擬、一張真單都
    不送——使用者以為明天才開始的真實下單，不能因為剛好當掉就今天開始。"""
    run_day(until(_STRONG, at("08:51:40")))

    second, sent, _ = run_day(restarted_at(_STRONG, at("08:52:50")),
                              config=make_config(auto_order_enabled=True))

    assert second.orders == []
    assert sent and all(m.startswith("【模擬】") for m in sent)


def test_lots_and_the_position_limit_set_in_the_morning_hold_all_day():
    """早上每次進場 1 口、加碼上限 3。當掉時使用者改成每次 2 口、上限 20：今天照早上的，
    重啟前 2 口、重啟後再 1 口就到上限。"""
    run_day(until(_STRONG, at("08:51:40")),
            config=make_config(auto_order_enabled=True, max_position=3))

    second, _, _ = run_day(restarted_at(_STRONG, at("08:52:50")),
                           config=make_config(auto_order_enabled=True, lots_per_entry=2))

    assert [o.lots for o in second.orders] == [1]


def test_a_settings_snapshot_that_cannot_be_read_is_rewritten_from_the_settings_read_now():
    """當天的設定快照被改壞（或寫到一半）：當成今天還沒有快照，照這一次讀到的設定檔，並重寫快照
    ——和心跳記錄一樣，讀不懂不讓盤中程式停下來。之後再重啟就照重寫的那一份。"""
    os.makedirs(os.path.dirname(day_settings.path_for(DAY)), exist_ok=True)
    with open(day_settings.path_for(DAY), "w", encoding="utf-8") as fh:
        fh.write("{寫到一半")
    first, _, _ = run_day(until(_STRONG, at("08:51:40")),
                          config=make_config(auto_order_enabled=True))
    assert len(first.orders) == 2

    second, _, _ = run_day(restarted_at(_STRONG, at("08:52:50")), config=make_config())
    assert len(second.orders) == 2


def test_the_discord_switch_is_not_frozen():
    """Discord 開關不影響部位：使用者盤中關掉通知，重啟之後就不發。"""
    on = make_config(auto_order_enabled=True)
    run_day(until(_STRONG, at("08:51:40")), config=on)

    second, sent, _ = run_day(restarted_at(_STRONG, at("08:52:50")),
                              config=make_config(auto_order_enabled=True, discord_enabled=False))

    assert len(second.orders) == 2
    assert sent == []


# --- 等待中的突破進場不恢復（規格第 48 條）---


def test_a_breakout_waiting_before_the_crash_is_not_restored():
    """10:00 那根筆差從 500 跳到 1600，10:01:00 設下突破進場（觸發價 45180）；10:01:05 當掉，
    10:01:20 就重啟了。等待中的突破進場只在記憶體裡，重啟後不恢復：10:02:30 大台碰到 45180，
    不進場。10:00 那根重啟前就判斷過了，重啟後不再判斷，所以也不會把同一筆突破進場又設一次。"""
    day = (rally("08:45", 5) + rally("08:50", 70, open_=45150, step=0, bichai=500)
           + minute("10:00", 45150, 45180, bichai=1600)
           + minute("10:01", 45170, 45175, bichai=1600)
           + minute("10:02", 45175, 45180, bichai=1600)
           + [trade(at("10:03:00"), 45185)])
    on = make_config(auto_order_enabled=True)
    first, _, _ = run_day(until(day, at("10:01:05")), config=on)
    assert len(first.orders) == 1             # 08:50 條件 1

    second, sent, _ = run_day(restarted_at(day, at("10:01:20")), config=on)
    assert second.orders == []
    assert sent == []


# --- 原始行情檔的最後一行只寫了一半 ---


def test_a_raw_market_line_cut_off_by_the_crash_is_skipped_and_new_lines_start_fresh(market_dir):
    """當掉那一刻最後一行只寫了一半。重啟後讀不懂的那一行略過，其餘照樣讀回；新的行情另起
    一行寫，不黏在半行後面（黏上去的話，兩行一起讀不懂）。"""
    day = rally("08:45", 12, bichai=1000, bichai_step=10) + [trade(at("08:57:00"), 45230)]
    run_day(until(day, at("08:50:40")))
    path = market_dir / "TX-20261109.csv"
    with open(path, "a", encoding="utf-8", newline="") as fh:
        fh.write("2026-11-09T08:50:4")

    _, _, outcome = run_day(restarted_at(day, at("08:54:10")))

    assert _bichai(outcome)[:5] == [("08:45", 1000), ("08:46", 1010), ("08:47", 1020),
                                    ("08:48", 1030), ("08:49", 1040)]
    lines = path.read_text(encoding="utf-8").splitlines()
    cut = lines.index("2026-11-09T08:50:4")
    assert lines[cut + 1] == "2026-11-09T08:51:00.000,45160,,"
