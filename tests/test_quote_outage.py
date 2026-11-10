"""盤中報價中斷：中斷期間不進場；連續中斷 3 分鐘發 Discord（規格第 53 條）——從最上層打進去。

報價中斷的樣子：本機時鐘照走（群益 broker 在訊息幫浦裡自己產生時鐘事件），大台的成交與
累計成交筆數卻不來了。劇本就照這個樣子寫：中斷期間只有時鐘。

斷言送出的委託與發出的訊息。
"""

from datetime import timedelta

from broker.fake import FakeBroker
from day_script import (PREVIOUS_DAY, at, clock, counts, make_config, minute, product_trade,
                        rally, run_day, trade)

def _run(events, *, config=None, **broker_options) -> tuple[FakeBroker, list[str]]:
    """跑一天（`run_day`，預設自動下單打開），回傳 (broker, 發出的訊息)。"""
    broker, sent, _ = run_day(events, config=config or make_config(auto_order_enabled=True),
                              **broker_options)
    return broker, sent


def _silence(start: str, end: str, step: int = 10) -> list:
    """`start` 到 `end`（含）之間只有本機時鐘，每 `step` 秒一個：報價斷了，程式還活著。"""
    t, stop = at(start), at(end)
    events = []
    while t <= stop:
        events.append(clock(t))
        t += timedelta(seconds=step)
    return events


def _heads(sent: list[str]) -> list[str]:
    """每一則訊息的第一行。"""
    return [m.splitlines()[0] for m in sent]


_OUTAGE = "【要你處理】PTS 盤中報價中斷超過 3 分鐘"


def test_no_entries_while_quotes_are_down_and_one_alert_after_three_minutes():
    """強勢的早上，條件 1 每分鐘成立。08:50 進一口之後，大台的報價在 08:50:30 那筆成交之後
    就斷了（筆數最後一次是 08:50:20），一直到 08:55 才恢復。

      * 中斷期間沒有任何進場。
      * 連續中斷 3 分鐘（從兩路裡比較早斷的那一路算起，08:53:20）發一則告警，只發一次。
      * 恢復之後照常判斷，只是要等資料完整：08:50 那根少了 08:50:30 之後的成交，它還在
        最近 6 根裡時不判斷（見 test_bars_with_trades_missing_from_an_outage_are_not_used_to_enter）。
        08:55 起的 6 根都是完整的，09:00 那根結束時（09:01:00）才又進場。"""
    events = (rally("08:45", 6) + _silence("08:50:40", "08:54:50")
              + rally("08:55", 7, open_=45200) + [trade(at("09:02:00"), 45280)])
    broker, sent = _run(events)

    assert [o.lots for o in broker.orders] == [1, 1, 1]
    assert [m.splitlines()[-1] for m in sent] == [
        "2026/11/09 08:50:00", "2026/11/09 08:53:20", "2026/11/09 09:01:00",
        "2026/11/09 09:02:00"]
    assert sent[1] == (
        "【要你處理】PTS 盤中報價中斷超過 3 分鐘\n"
        "最後收到的大台成交：08:50:30；累計成交筆數：08:50:20\n"
        "報價恢復之前不會進場；恢復之後盤中程式自己接著判斷，不另外通知。已經有的部位照常了結。\n"
        "請檢查這台電腦的網路與群益的報價連線。網路正常、報價卻一直沒有回來的話，"
        "先關掉盤中程式再重新執行：它會從部位檔與原始行情記錄接著做。\n"
        "2026/11/09 08:53:20")


def test_an_outage_shorter_than_three_minutes_is_not_reported():
    """報價斷了 2 分 50 秒又回來：中斷期間照樣不進場，但不發告警——規格說連續 3 分鐘。"""
    events = (rally("08:45", 6) + _silence("08:50:40", "08:53:10")
              + rally("08:53", 3, open_=45200) + [trade(at("08:56:00"), 45240)])
    _, sent = _run(events, config=make_config())
    assert _OUTAGE not in _heads(sent)


def test_counts_that_stop_while_trades_go_on_are_an_outage_too():
    """大台成交照常，累計成交筆數卻在 09:00:20 之後不來了。沒有筆數就沒有筆差，判斷少了一條腿：
    一樣是報價中斷。告警寫出兩路各自最後一次收到的時刻，查原因時才知道是哪一路斷了。
    （這份劇本每分鐘只在 :00 有一個時鐘，所以 09:03:20 到期的告警在 09:04:00 發出。）"""
    events = rally("08:45", 16, step=1)
    events += [e for i in range(5) for e in minute(f"09:0{i + 1}", 45200, 45201)]
    events += [trade(at("09:06:00"), 45201)]
    _, sent = _run(events, config=make_config())
    outages = [m for m in sent if m.startswith(_OUTAGE)]
    assert [m.splitlines()[1:2] + m.splitlines()[-1:] for m in outages] == [
        ["最後收到的大台成交：09:03:30；累計成交筆數：09:00:20", "2026/11/09 09:04:00"]]


def test_a_waiting_breakout_is_cancelled_when_an_outage_is_reported():
    """自動下單打開。08:50 條件 1 進一口；10:00 那根筆差從 500 跳到 1600、最高 45180，10:01:00
    設突破進場，觸發價 45180。累計成交筆數在 10:00:20 之後就不來了，大台成交照常。

    10:04:00 告警「報價恢復之前不會進場」——說了就要做到：10:04:30 大台碰到 45180，不進場。
    筆數 10:05:20 恢復之後，10:05:30 再碰到 45180 也不進場：等待中的那一筆在告警時就取消了，
    和重啟一樣不恢復（規格第 48 條；ADR-0001「報價中斷時那一筆就不會進場」）。它的觸發價是
    中斷之前的 5 根算的，恢復之後的價格可能早就遠遠超過，那時才進場就是追高。"""
    events = (rally("08:45", 5) + rally("08:50", 70, open_=45150, step=0, bichai=500)
              + minute("10:00", 45150, 45180, bichai=1600)
              + minute("10:01", 45170, 45175) + minute("10:02", 45170, 45175)
              + minute("10:03", 45170, 45175) + minute("10:04", 45175, 45180)
              + [clock(at("10:05:00")), trade(at("10:05:00"), 45175),
                 counts(at("10:05:20"), buy=20000, sell=21700), trade(at("10:05:30"), 45180),
                 product_trade(at("10:05:40"), 45180), trade(at("10:06:00"), 45175)])
    broker, sent = _run(events)

    assert [o.lots for o in broker.orders] == [1]
    assert [(m.splitlines()[0], m.splitlines()[-1]) for m in sent] == [
        ("PTS 做多：買進微台 1 口 @ 45150", "2026/11/09 08:50:00"),
        (_OUTAGE, "2026/11/09 10:04:00")]


def test_each_separate_outage_is_reported_once():
    """一次中斷只發一則（中斷 4 分鐘也是一則）；恢復之後又斷了 3 分鐘，是新的一次，再發一則。
    兩次都從比較早斷的筆數（:20）算起。"""
    events = (rally("08:45", 2) + _silence("08:46:40", "08:51:00")
              + rally("08:51", 2) + _silence("08:52:40", "08:56:00")
              + rally("08:56", 2))
    _, sent = _run(events, config=make_config())
    assert [m.splitlines()[-1] for m in sent if m.startswith(_OUTAGE)] == [
        "2026/11/09 08:49:20", "2026/11/09 08:55:20"]


def test_no_quotes_after_the_open_may_be_a_day_off_and_the_alert_says_so():
    """08:45 開盤之後一筆今天的成交都沒有（訂閱回補的是前一個交易日的）。可能是開盤時報價
    斷了，也可能是臨時休市（颱風假）：13:20 之前分不出來。照樣在 08:48 告警，講明休市的話
    不必處理——不告警的話，真的斷線時一整天沒有人知道。"""
    events = ([clock(at("08:40:00")), trade(at("13:44:59", PREVIOUS_DAY), 44100),
               counts(at("08:40:01"), buy=0, sell=0)] + _silence("08:44:00", "08:49:00"))
    _, sent = _run(events, config=make_config())
    assert sent == [
        "【要你處理】PTS 盤中報價中斷超過 3 分鐘\n"
        "最後收到的大台成交：今天還沒有；累計成交筆數：08:40:01\n"
        "報價恢復之前不會進場；恢復之後盤中程式自己接著判斷，不另外通知。已經有的部位照常了結。\n"
        "今天如果臨時休市（例如颱風假），不必處理。\n"
        "請檢查這台電腦的網路與群益的報價連線。網路正常、報價卻一直沒有回來的話，"
        "先關掉盤中程式再重新執行：它會從部位檔與原始行情記錄接著做。\n"
        "2026/11/09 08:48:00"]


def test_after_a_late_start_the_three_minutes_count_from_the_start():
    """盤中程式 10:00 才啟動（晚啟動或重啟）：08:45 到 10:00 的空白不是這一輪看到的中斷，
    那是心跳記錄的事。3 分鐘從這一輪的第一個時鐘算起：10:02 之前報價來了就不告警；
    一直沒來的話，10:03 告警，而不是一啟動就告警。"""
    _, sent = _run(_silence("10:00:00", "10:02:00") + rally("10:02", 3),
                   config=make_config())
    assert _OUTAGE not in _heads(sent)

    _, sent = _run(_silence("10:00:00", "10:04:00"), config=make_config())
    assert [m.splitlines()[-1] for m in sent] == ["2026/11/09 10:03:00"]


def test_no_alert_outside_the_day_session():
    """08:45 開盤之前沒有成交是正常的；13:20 收工之後盤中程式也不在了。"""
    _, sent = _run(_silence("08:40:00", "08:44:50") + rally("08:45", 2),
                   config=make_config())
    assert sent == []


def test_counts_that_stall_for_a_while_are_not_taken_for_a_bichai_jump():
    """自動下單打開。08:50 條件 1 進場做多一口，之後大台橫盤（開盤價沒有比上一根高）。交易所
    那邊筆差每分鐘穩定多 85——不到 100，不是筆差跳增。大台成交照常，累計成交筆數卻在
    08:55:01 那次之後停了 79 秒，08:56:20 才又來（不到 3 分鐘，不告警）。

    08:55 那根結束時手上的筆數是 08:55:01 的，08:56 那根是 08:56:20 的：兩者相減是 79 秒的
    累積（112），不是一分鐘的。拿它當筆差跳增，08:57:00 條件 3 就送出一張追多的市價單——
    把報價停住前後的差距誤判成「突然大量買進」（ticket 09）。

    筆數恢復正常之後照常判斷：08:59 那根真的多了 100，09:00:00 條件 3 追多加碼。"""
    def at_counts(t, bichai):
        return counts(at(t), buy=20000, sell=20000 + bichai)

    flat = []
    for i, bichai in enumerate([1500, 1585, 1670, 1755, 1840]):       # 08:50–08:54，:20 的筆數
        flat += minute(f"08:5{i}", 45140, 45140, bichai=bichai)
    stalled = [clock(at("08:55:00")), trade(at("08:55:00"), 45140), at_counts("08:55:01", 1898),
               trade(at("08:55:30"), 45140), product_trade(at("08:55:40"), 45140),
               clock(at("08:56:00")), trade(at("08:56:00"), 45140), at_counts("08:56:20", 2010),
               trade(at("08:56:30"), 45140), product_trade(at("08:56:40"), 45140)]
    after = (minute("08:57", 45140, 45140, bichai=2095) + minute("08:58", 45140, 45140, bichai=2180)
             + minute("08:59", 45140, 45140, bichai=2280) + [trade(at("09:00:00"), 45140)])
    broker, sent = _run(rally("08:45", 5) + flat + stalled + after)

    assert [(m.splitlines()[1], m.splitlines()[-1]) for m in sent] == [
        ("觸發：條件 1（早盤做多）", "2026/11/09 08:50:00"),
        ("觸發：條件 3（追多加碼）", "2026/11/09 09:00:00")]
    assert [o.lots for o in broker.orders] == [1, 1]


def test_bars_with_trades_missing_from_an_outage_are_not_used_to_enter():
    """強勢的早上，條件 1 每分鐘成立。大台成交在 08:50:00 之後斷了 70 秒（筆數照常），
    08:51:10 才又有成交。缺了成交的有兩根：

      * 08:50 那根只收到第一筆，收盤價、高點少了後面 60 秒。
      * 08:51 那根少了開頭 10 秒：開盤價其實是 08:51:10 那筆的價格。紅K 正是拿開盤價和
        上一根比（規格第 6 條）。

    拿它們判斷，就是在中斷期間的資料上進場（規格第 53 條）。一次判斷讀到的不只最後一根：
    紅K 與筆差跳增看最後兩根，RSI(5) 看最近 6 根的收盤價，突破進場的觸發價看最近 5 根的
    高低點。所以這兩根還在最近 6 根裡的時候都不判斷：08:52 起的 K 棒完整，到 08:57 那根
    結束（08:58:00）最近 6 根才全是完整的，照常進場。"""
    def bar_counts(t):
        return counts(at(t), buy=20000, sell=21500)

    events = (rally("08:45", 5)
              + [clock(at("08:50:00")), trade(at("08:50:00"), 45150), bar_counts("08:50:20")]
              + _silence("08:50:10", "08:51:10")
              + [trade(at("08:51:10"), 45160), bar_counts("08:51:20"),
                 trade(at("08:51:30"), 45170), product_trade(at("08:51:40"), 45170)]
              + rally("08:52", 7, open_=45180) + [trade(at("08:59:00"), 45260)])
    _, sent = _run(events, config=make_config())
    assert [m.splitlines()[-1] for m in sent] == [
        "2026/11/09 08:50:00", "2026/11/09 08:58:00", "2026/11/09 08:59:00"]


def test_a_gap_inside_one_minute_leaves_the_next_bar_whole():
    """大台成交在 08:50:05 到 08:50:50 之間斷了 45 秒，同一分鐘裡就接回來：只有 08:50 那根缺了
    成交，08:51 那根是完整的。等 08:50 那根離開最近 6 根（08:56 那根結束、08:57:00）才又進場——
    不會因為一次分鐘內的中斷，多停一分鐘。"""
    events = (rally("08:45", 5)
              + [clock(at("08:50:00")), trade(at("08:50:00"), 45150),
                 counts(at("08:50:20"), buy=20000, sell=21500)]
              + _silence("08:50:05", "08:50:50")
              + [trade(at("08:50:50"), 45160), product_trade(at("08:50:55"), 45160)]
              + rally("08:51", 7, open_=45170) + [trade(at("08:58:00"), 45250)])
    _, sent = _run(events, config=make_config())
    assert [m.splitlines()[-1] for m in sent] == [
        "2026/11/09 08:50:00", "2026/11/09 08:57:00", "2026/11/09 08:58:00"]
