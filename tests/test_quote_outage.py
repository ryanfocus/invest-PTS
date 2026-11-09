"""盤中報價中斷：中斷期間不進場；連續中斷 3 分鐘發 Discord（規格第 53 條）——從最上層打進去。

報價中斷的樣子：本機時鐘照走（群益 broker 在訊息幫浦裡自己產生時鐘事件），大台的成交與
累計成交筆數卻不來了。劇本就照這個樣子寫：中斷期間只有時鐘。

斷言送出的委託與發出的訊息。
"""

from datetime import date, timedelta

from broker import TMF_CODE, ContractInfo
from broker.fake import FakeBroker
from day_script import (DAY, PREVIOUS_DAY, at, calm_history, clock, counts, minute,
                        product_trade, rally, trade)
from recording import RecordingNotifier
import intraday
import settings

CONTRACTS = {TMF_CODE: ContractInfo(TMF_CODE, date(2026, 11, 18), "TM2611")}


def _config(**overrides) -> settings.Config:
    values = dict(auto_order_enabled=True, product=TMF_CODE, lots_per_entry=1,
                  max_position=20, discord_enabled=True)
    values.update(overrides)
    return settings.Config(**values)


def _run(events, *, config=None, **broker_options) -> tuple[FakeBroker, list[str]]:
    broker = FakeBroker(contracts=CONTRACTS, events=events,
                        minute_bars={PREVIOUS_DAY: calm_history(45000)}, **broker_options)
    notifier = RecordingNotifier()
    intraday.run(broker, config or _config(), today=DAY, notify=notifier)
    return broker, notifier.sent


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
      * 恢復之後照常判斷：08:55 那根的上一根是 08:50，不是上一分鐘，不判斷；08:56 那根
        結束時（08:57:00）才又進場。"""
    events = (rally("08:45", 6) + _silence("08:50:40", "08:54:50")
              + rally("08:55", 3, open_=45200) + [trade(at("08:58:00"), 45240)])
    broker, sent = _run(events)

    assert [o.lots for o in broker.orders] == [1, 1, 1]
    assert [m.splitlines()[-1] for m in sent] == [
        "2026/11/09 08:50:00", "2026/11/09 08:53:20", "2026/11/09 08:57:00",
        "2026/11/09 08:58:00"]
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
    _, sent = _run(events, config=_config(auto_order_enabled=False))
    assert _OUTAGE not in _heads(sent)


def test_counts_that_stop_while_trades_go_on_are_an_outage_too():
    """大台成交照常，累計成交筆數卻在 09:00:20 之後不來了。沒有筆數就沒有筆差，判斷少了一條腿：
    一樣是報價中斷。告警寫出兩路各自最後一次收到的時刻，查原因時才知道是哪一路斷了。
    （這份劇本每分鐘只在 :00 有一個時鐘，所以 09:03:20 到期的告警在 09:04:00 發出。）"""
    events = rally("08:45", 16, step=1)
    events += [e for i in range(5) for e in minute(f"09:0{i + 1}", 45200, 45201)]
    events += [trade(at("09:06:00"), 45201)]
    _, sent = _run(events, config=_config(auto_order_enabled=False))
    outages = [m for m in sent if m.startswith(_OUTAGE)]
    assert [m.splitlines()[1:2] + m.splitlines()[-1:] for m in outages] == [
        ["最後收到的大台成交：09:03:30；累計成交筆數：09:00:20", "2026/11/09 09:04:00"]]


def test_each_separate_outage_is_reported_once():
    """一次中斷只發一則（中斷 4 分鐘也是一則）；恢復之後又斷了 3 分鐘，是新的一次，再發一則。
    兩次都從比較早斷的筆數（:20）算起。"""
    events = (rally("08:45", 2) + _silence("08:46:40", "08:51:00")
              + rally("08:51", 2) + _silence("08:52:40", "08:56:00")
              + rally("08:56", 2))
    _, sent = _run(events, config=_config(auto_order_enabled=False))
    assert [m.splitlines()[-1] for m in sent if m.startswith(_OUTAGE)] == [
        "2026/11/09 08:49:20", "2026/11/09 08:55:20"]


def test_no_quotes_after_the_open_may_be_a_day_off_and_the_alert_says_so():
    """08:45 開盤之後一筆今天的成交都沒有（訂閱回補的是前一個交易日的）。可能是開盤時報價
    斷了，也可能是臨時休市（颱風假）：13:20 之前分不出來。照樣在 08:48 告警，講明休市的話
    不必處理——不告警的話，真的斷線時一整天沒有人知道。"""
    events = ([clock(at("08:40:00")), trade(at("13:44:59", PREVIOUS_DAY), 44100),
               counts(at("08:40:01"), buy=0, sell=0)] + _silence("08:44:00", "08:49:00"))
    _, sent = _run(events, config=_config(auto_order_enabled=False))
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
                   config=_config(auto_order_enabled=False))
    assert _OUTAGE not in _heads(sent)

    _, sent = _run(_silence("10:00:00", "10:04:00"), config=_config(auto_order_enabled=False))
    assert [m.splitlines()[-1] for m in sent] == ["2026/11/09 10:03:00"]


def test_no_alert_outside_the_day_session():
    """08:45 開盤之前沒有成交是正常的；13:20 收工之後盤中程式也不在了。"""
    _, sent = _run(_silence("08:40:00", "08:44:50") + rally("08:45", 2),
                   config=_config(auto_order_enabled=False))
    assert sent == []


def test_a_bar_with_trades_missing_from_an_outage_is_not_judged():
    """大台成交在 08:50:00 之後斷了 70 秒（筆數照常），08:51:10 才又有成交。08:50 那根只收到
    第一筆：收盤價少了後面 50 秒，RSI 跟著錯——拿它判斷，就是在中斷期間的資料上進場。
    所以那一根不判斷；08:51 那根是完整的，08:52 照常判斷、進場。"""
    def bar_counts(t):
        return counts(at(t), buy=20000, sell=21500)

    events = (rally("08:45", 5)
              + [clock(at("08:50:00")), trade(at("08:50:00"), 45150), bar_counts("08:50:20")]
              + _silence("08:50:10", "08:51:10")
              + [trade(at("08:51:10"), 45160), bar_counts("08:51:20"),
                 trade(at("08:51:30"), 45170), product_trade(at("08:51:40"), 45170)]
              + minute("08:52", 45180, 45190, bichai=1500) + [trade(at("08:53:00"), 45200)])
    _, sent = _run(events, config=_config(auto_order_enabled=False))
    assert [m.splitlines()[-1] for m in sent] == [
        "2026/11/09 08:50:00", "2026/11/09 08:52:00", "2026/11/09 08:53:00"]
