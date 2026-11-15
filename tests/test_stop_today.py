"""收工指令（`tools/stop_today.py`）——從最上層打進去：`intraday.run`、`stop_today.main`，
以及之後 13:25 的 `exit_program.run`。

使用者盤中想讓 PTS 今天收工：不再進場，帶 `--close` 時現在就平掉 PTS 自己的口數。斷言的是
外部效果：送出的委託（假 broker）、寫下的檔（部位檔、停止進場記錄與確認記錄、log）、
主控台印出的內容，以及**沒有** Discord（使用者就在電腦前）。

收工指令和盤中程式是兩個行程，只靠 `state/` 底下的檔與 `state/intraday.lock` 溝通：
「盤中程式在跑」就是測試自己鎖住那個鎖；盤中程式看到記錄的那一刻，是在收工指令等待的
那一秒裡（`sleep`）真的跑一段 `intraday.run`。

收工指令讀一次系統時鐘，之後只有「等一秒」「等一分鐘再重試」會讓時間往前走。`_Time` 同時
扮演時鐘與睡眠（同 test_exit_program）。
"""

import contextlib
import os
import sys
from datetime import date, datetime, timedelta

import pytest

from broker import SELL, FillQuery, LoginFailed, OrderFailed
from broker.fake import FakeBroker
from conftest import REPO_ROOT
from day_script import (DAY, PREVIOUS_DAY, at, calm_history, clock, contracts,
                        lock_position_file, main_env, make_config, minute, position_content,
                        position_on_file, rally, restarted_at, trade, until, write_position_file)
from recording import RecordingNotifier
import exit_program
import heartbeat
import intraday
import paths
import raw_market
import state_files

sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
import stop_today  # noqa: E402


class _Time:
    """收工指令的時鐘與睡眠。睡多久，時鐘就往前走多久；`during_sleep` 是每次睡的時候
    「同時發生的事」（例如盤中程式這一秒在做什麼）。"""

    def __init__(self, start: datetime, during_sleep=None):
        self.now = start
        self.slept: list[float] = []
        self._during_sleep = during_sleep

    def clock(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)
        if self._during_sleep is not None:
            self._during_sleep(self.now)


def _at(hhmmss: str, day: date = DAY) -> datetime:
    fmt = "%H:%M:%S" if hhmmss.count(":") == 2 else "%H:%M"
    return datetime.combine(day, datetime.strptime(hhmmss, fmt).time())


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """每個測試都有 `.env` 與 Discord 的替身（`main_env`）：收工指令要平倉時讀的是這份假的帳密，
    不是這台機器上真的那一份。回傳 Discord 的記錄器：收工指令一則都不該發。"""
    return main_env(tmp_path, monkeypatch)


def _stop(*args: str, at: str = "10:00:00", time: _Time | None = None,
          broker: FakeBroker | None = None, wait: int = 5,
          **broker_options) -> tuple[int, list[FakeBroker], _Time]:
    """使用者在 `at` 執行 `tools/stop_today.py <args>`。回傳 (結束代碼, 收工指令建立的 broker, 時間)。
    成交價預設 45200。"""
    time = time or _Time(_at(at))
    broker_options.setdefault("fill_price", 45200.0)
    connected: list[FakeBroker] = []

    def connect(secrets) -> FakeBroker:
        connected.append(broker or FakeBroker(**broker_options))
        return connected[-1]
    code = stop_today.main(list(args), connect=connect, clock=time.clock, sleep=time.sleep,
                           wait_seconds=wait)
    return code, connected, time


def _on_file(**overrides) -> dict:
    """盤中程式今天寫下的部位檔：預設微台多單 3 口（2 口 @ 45100、1 口 @ 45150）。"""
    content = position_content(DAY, **{
        "entries": [{"order_seq": "2315609990001", "lots": 2, "price": 45100.0},
                    {"order_seq": "2315609990002", "lots": 1, "price": 45150.0}],
        **overrides})
    write_position_file(content)
    return content


def _lines(capsys) -> list[str]:
    return capsys.readouterr().out.splitlines()


# --- 不帶 --close：今天不再進場，部位照常了結 ---


def test_without_close_entries_stop_nothing_is_sent_and_it_says_who_closes_the_position(
        capsys, env):
    """盤中程式沒在跑（例如剛好當掉、等排程叫醒）。寫下停止進場記錄，不登入、不送單，
    印出部位檔的現況與誰會了結：微台由 13:25 的出場程式。不發 Discord。"""
    _on_file()
    code, connected, _ = _stop()

    assert code == 0
    assert connected == []
    assert env.sent == []
    assert os.path.exists(intraday.stop_path(DAY))
    assert _lines(capsys) == [
        "已寫下停止進場記錄（state/entries-stopped-20261109.txt）：PTS 今天不再進場。",
        "盤中程式沒在跑：之後啟動的盤中程式一開始就會讀到這份記錄，不再進場。",
        "PTS 今天還沒平的部位：微台多單 3 口（TM2611），13:25 的出場程式會平掉。",
    ]


def test_without_close_an_uncertain_micro_entry_is_left_for_1325_to_look_up(capsys):
    """部位檔記著盤中留下的微台進場不確定（有序號）：13:25 的出場程式會先用序號查，查到全部成交
    就照常平倉。現在沒有事要做，回 0；講清楚不要自己平（不然 13:25 會再平一次）。"""
    _on_file(status="UNCERTAIN", uncertain={"stage": "ENTRY", "order_seq": "2315609990003",
                                            "requested_lots": 1, "known_filled": 0})

    code, connected, _ = _stop()

    assert code == 0
    assert connected == []
    assert _lines(capsys)[-2] == (
        "微台不必現在處理：13:25 的出場程式會先用委託序號向券商查一次，查到全部成交就照常平倉"
        "（賣出微台 4 口）；查不到確定的口數會發 Discord，寫明要平幾口。在那之前不要自己平："
        "不然 13:25 會再平一次，開出反向新倉。")


def test_on_a_non_trading_day_it_says_so_and_does_nothing(capsys, env):
    code, connected, _ = _stop("--close", time=_Time(_at("10:00:00", date(2026, 11, 8))))

    assert code == 0
    assert connected == []
    assert not os.path.exists(intraday.stop_path(date(2026, 11, 8)))
    assert _lines(capsys) == ["2026/11/08 不是交易日，收工指令什麼都不做。"]


def test_a_stop_record_that_cannot_be_written_stops_nothing_and_says_so(capsys):
    """停止進場記錄寫不進去：盤中程式不會知道要停。帶 `--close` 也不平倉——盤中程式還會進場，
    進場時用記憶體裡的部位整份覆寫部位檔，平掉的就不見了。回 1。"""
    _on_file()
    os.makedirs(intraday.stop_path(DAY))        # 擋在那裡、寫不進去

    code, brokers, _ = _stop("--close")

    assert code == 1
    assert brokers == []
    assert _lines(capsys)[-1].startswith(
        "【要你處理】停止進場記錄寫不進 state/entries-stopped-20261109.txt，盤中程式不會知道要停，"
        "也沒有平倉：")


def test_without_close_an_intraday_program_that_never_confirms_is_reported(capsys):
    _on_file()

    with _intraday_running():
        code, _, _ = _stop(wait=3)

    assert code == 1
    assert _lines(capsys)[-2:] == [
        "【要你處理】盤中程式在跑，等了 3 秒都沒有確認看到停止進場記錄",
        "記錄已經寫了，盤中程式一看到就不再進場。它每秒看一次：等不到通常是它正在等一筆委託的"
        "成交回報，或卡住了（看 logs/intraday-20261109.log 最後幾行）。",
    ]


def test_the_console_output_is_also_kept_in_the_days_stop_log(capsys, env):
    """結果印在主控台，也寫進 `logs/stop-YYYYMMDD.log`（跟著 `logs/` 30 天清除）。"""
    _on_file()
    _stop()

    with open(paths.daily_log("stop", DAY), encoding="utf-8") as fh:
        kept = fh.read()
    for line in _lines(capsys):
        assert line in kept


# --- 盤中程式：每個時鐘事件都看一次停止進場記錄 ---


class _Meanwhile(FakeBroker):
    """劇本走到 `at` 時（那個事件送出之前），盤中程式以外的誰做了 `do()`。"""

    def __init__(self, at: datetime, do, **options):
        options.setdefault("minute_bars", {PREVIOUS_DAY: calm_history(45000)})
        options.setdefault("contracts", contracts())
        super().__init__(**options)
        self._at = at
        self._do = do
        self.done = False

    def events(self, product):
        for event in super().events(product):
            if not self.done and event.time >= self._at:
                self.done = True
                self._do()
            yield event


def _UserStopsAt(at: datetime, **options) -> _Meanwhile:
    """劇本走到 `at` 時，使用者執行收工指令（不帶 --close）。

    `intraday.run` 本身不拿 `intraday.lock`（那是 `intraday.main` 的事），所以收工指令看到的是
    「盤中程式沒在跑」、不等確認——這裡要看的是盤中程式那一邊。"""
    def user_stops() -> None:
        assert _stop(time=_Time(at))[0] == 0
    return _Meanwhile(at, user_stops, **options)


def _intraday(broker: FakeBroker, *, live: bool = True) -> list[str]:
    """盤中程式跑一輪（自動下單預設打開），回傳發出的 Discord。"""
    notifier = RecordingNotifier()
    intraday.run(broker, make_config(auto_order_enabled=live), today=DAY, notify=notifier)
    return notifier.sent


def _times(sent: list[str]) -> list[str]:
    return [text.splitlines()[-1] for text in sent]


def test_after_the_stop_record_appears_the_intraday_program_enters_no_more(market_dir):
    """08:50 起每分鐘條件 1 都成立。08:50、08:51 各進一口之後，使用者 08:51:30 執行收工指令。
    盤中程式在下一個時鐘事件（08:52:00）看到停止進場記錄：之後條件照樣成立，也不再進場。

    看到的那一刻寫下確認記錄（收工指令等的就是它），只記 log、不發 Discord。原始行情照記到
    劇本結束：每個交易日都要記（規格第 58 條）。"""
    day = rally("08:45", 8) + [trade(at("08:53:00"), 45180)]
    broker = _UserStopsAt(at("08:51:30"), events=day)

    sent = _intraday(broker)

    assert [o.lots for o in broker.orders] == [1, 1]
    assert _times(sent) == ["2026/11/09 08:50:00", "2026/11/09 08:51:00"]
    assert os.path.exists(intraday.stop_seen_path(DAY))
    assert raw_market.read(DAY)[-1] == trade(at("08:53:00"), 45180)


@pytest.mark.parametrize("content", [b"", b"\xff\xfe\xfa"], ids=["空檔", "讀不懂"])
def test_a_stop_record_caught_half_written_still_stops_entries_without_crashing(content,
                                                                             market_dir):
    """收工指令寫停止進場記錄時，檔案先建出來、那一行才寫進去：盤中程式剛好在中間看到的是空檔
    （寫到一半被砍掉、或寫壞了也一樣）。存在就算數：照樣不再進場、寫確認記錄，原因當成不知道。
    每秒一次的檢查不能讓盤中程式當掉——它一當，原始行情就斷到排程把它叫回來（規格第 58 條）。"""
    def half_written() -> None:
        with open(intraday.stop_path(DAY), "wb") as fh:
            fh.write(content)
    day = rally("08:45", 8) + [trade(at("08:53:00"), 45180)]
    broker = _Meanwhile(at("08:51:30"), half_written, events=day)

    outcome_sent = _intraday(broker)

    assert [o.lots for o in broker.orders] == [1, 1]
    assert _times(outcome_sent) == ["2026/11/09 08:50:00", "2026/11/09 08:51:00"]
    assert os.path.exists(intraday.stop_seen_path(DAY))
    assert raw_market.read(DAY)[-1] == trade(at("08:53:00"), 45180)


def test_a_confirmation_that_could_not_be_written_is_written_again_later():
    """停止進場記錄一開始就在（稍早執行過收工指令），盤中程式起來時確認記錄卻寫不進去（防毒鎖住）。
    不是看過就算了：之後的時鐘事件再寫，寫進去為止——不然收工指令那天怎麼等都等不到確認，
    `--close` 一次都做不成。08:50:30 鎖放掉，下一次重試就寫進去了。"""
    _on_file()
    assert _stop(at="08:40:00")[0] == 0
    os.makedirs(intraday.stop_seen_path(DAY))        # 擋在那裡、寫不進去
    broker = _Meanwhile(at("08:50:30"), lambda: os.rmdir(intraday.stop_seen_path(DAY)),
                        events=rally("08:45", 8))

    _intraday(broker)

    assert broker.orders == []
    assert os.path.isfile(intraday.stop_seen_path(DAY))


def test_a_waiting_breakout_is_cancelled_when_the_stop_record_appears():
    """08:50 條件 1 進一口（模擬模式），之後平盤到 10:00。10:00 那根筆差跳增，10:01 設下突破進場
    （觸發價 45180，同 test_chase_entry）。使用者 10:01:45 執行收工指令；10:02:30 大台碰到
    45180，也不進場：等待中的那一筆在盤中程式看到記錄時就取消了，不恢復。"""
    day = (rally("08:45", 5) + rally("08:50", 70, open_=45150, step=0, bichai=500)
           + minute("10:00", 45150, 45180, bichai=1600)
           + minute("10:01", 45170, 45175, bichai=1600)
           + minute("10:02", 45175, 45180, bichai=1600)
           + [trade(at("10:03:00"), 45185)])
    broker = _UserStopsAt(at("10:01:45"), events=day)

    sent = _intraday(broker, live=False)

    assert _times(sent) == ["2026/11/09 08:50:00"]
    assert broker.orders == []


def test_a_restarted_intraday_program_reads_the_stop_record_and_confirms_it():
    """08:50 進一口之後盤中程式當掉。使用者 08:50:40 執行收工指令（盤中程式不在，不必等）。
    08:51:10 排程把它叫起來：一開始就讀到停止進場記錄，條件照樣成立也不進場，並寫下確認記錄
    ——之後再執行 `--close` 的收工指令，等的就是這一份。"""
    day = rally("08:45", 8) + [trade(at("08:53:00"), 45180)]
    first = FakeBroker(events=until(day, at("08:50:30")),
                       minute_bars={PREVIOUS_DAY: calm_history(45000)}, contracts=contracts())
    _intraday(first)
    assert len(first.orders) == 1

    assert _stop(at="08:50:40")[0] == 0
    assert not os.path.exists(intraday.stop_seen_path(DAY))

    second = FakeBroker(events=restarted_at(day, at("08:51:10")),
                        minute_bars={PREVIOUS_DAY: calm_history(45000)}, contracts=contracts())
    sent = _intraday(second)

    assert second.orders == []
    assert sent == []
    assert os.path.exists(intraday.stop_seen_path(DAY))


# --- --close：現在就平掉 PTS 自己的口數 ---


def _orders(broker: FakeBroker) -> list[tuple]:
    return [(o.contract.order_code, o.side, o.lots, o.day_trade) for o in broker.orders]


def _lots(*sizes: int) -> list[dict]:
    """進場明細：每一筆委託成交的口數，價格不重要。"""
    return [{"order_seq": f"23156099900{i:02d}", "lots": n, "price": 45100.0}
            for i, n in enumerate(sizes, 1)]


def _exit_at_1325() -> tuple[FakeBroker, list[str]]:
    """13:25 排程叫醒出場程式（盤中程式今天照常跑完了）。回傳 (它的 broker, 它發的 Discord)。"""
    heartbeat.write(heartbeat.Heartbeat(DAY, at("13:20:00"), True))
    broker = FakeBroker(fill_price=45200.0)
    notifier = RecordingNotifier()
    time = _Time(_at("13:25:00"))
    exit_program.run(broker, make_config(), notify=notifier, clock=time.clock, sleep=time.sleep)
    return broker, notifier.sent


def test_close_sells_ptss_own_micro_lots_now_and_leaves_nothing_for_1325(capsys, env):
    """盤中程式沒在跑。部位檔記著微台多單 12 口：現在就賣出 10 口與 2 口（每筆最多 10 口，
    規格第 33 條），微台不標當沖。每筆成交寫進部位檔的出場明細，帳記在 PTS 自己的檔裡——
    所以 13:25 的出場程式看到已經平完，一張單都不送。全程不發 Discord。"""
    _on_file(entries=_lots(10, 2))

    code, brokers, _ = _stop("--close")

    assert code == 0
    assert _orders(brokers[0]) == [("TM2611", SELL, 10, False), ("TM2611", SELL, 2, False)]
    assert position_on_file()["exits"] == [
        {"order_seq": "2315610000001", "lots": 10, "price": 45200.0},
        {"order_seq": "2315610000002", "lots": 2, "price": 45200.0},
    ]
    assert _lines(capsys)[-2:] == [
        "收工指令平掉了 PTS 今天的微台多單 12 口（TM2611），部位已經了結：13:25 的出場程式"
        "不會再送單。",
        "2026/11/09 10:00:00",
    ]
    assert env.sent == []

    exit_broker, exit_sent = _exit_at_1325()
    assert exit_broker.orders == []
    assert exit_sent == []


def test_after_a_close_the_1325_alarms_count_the_stop_commands_closes_too():
    """10:00 `--close` 賣掉 PTS 的 3 口微台，記在 `logs/stop-….log` 的「平倉成交」。13:25 時部位檔壞了：
    出場程式不知道口數，叫人從 log 算。只減 `logs/exit-….log` 的話算出來是 3 口，使用者照著再賣 3 口，
    就是空單 3 口過夜。所以兩個 log 的「平倉成交」都要減。"""
    _on_file()
    assert _stop("--close")[0] == 0
    with open(paths.daily_log("stop", DAY), encoding="utf-8") as fh:
        assert [line for line in fh if "平倉成交" in line][0].count("3 口") == 1
    write_position_file("{ 寫到一半")

    exit_broker, exit_sent = _exit_at_1325()

    assert exit_broker.orders == []
    assert exit_sent[0].splitlines()[3] == (
        "PTS 的口數：logs/intraday-20261109.log 最後一筆「進場成交」的累計口數，減掉 "
        "logs/exit-20261109.log 與 logs/stop-20261109.log 裡每一筆「平倉成交」的口數"
        "（有「不知道成交幾口」的委託時，另外到券商 APP 用委託序號查）。只平這個口數。")


@pytest.mark.parametrize("product, order_code, name", [("TX00AM", "TX11", "大台"),
                                                       ("MTX00AM", "MTX11", "小台")],
                         ids=["大台", "小台"])
def test_close_also_closes_tx_and_mtx_with_the_day_trade_flag(product, order_code, name, capsys,
                                                              env):
    """大台、小台平時交給券商 13:30 當沖強平；使用者要的是**現在**收工，所以也平。平倉單照
    `OrderRequest` 依商品標當沖（和進場時一樣，券商才會把它和進場那幾口沖掉）。平完之後
    13:30 也沒有東西可強平。"""
    _on_file(product=product, order_code=order_code)

    code, brokers, _ = _stop("--close")

    assert code == 0
    assert _orders(brokers[0]) == [(order_code, SELL, 3, True)]
    assert sum(fill["lots"] for fill in position_on_file()["exits"]) == 3
    assert _lines(capsys)[-2] == (
        f"收工指令平掉了 PTS 今天的{name}多單 3 口（{order_code}），部位已經了結："
        "券商 13:30 的當沖強平也沒有 PTS 的部位可平。")


@contextlib.contextmanager
def _intraday_running():
    """盤中程式正在跑：它拿著 `state/intraday.lock`（`intraday.main` 拿的同一把鎖）。"""
    with intraday._alone() as alone:
        assert alone
        yield


def _intraday_second(now: datetime) -> None:
    """正在跑的盤中程式過了一秒：收到一個時鐘事件。它在這一秒看停止進場記錄。"""
    _intraday(FakeBroker(events=[clock(now)], minute_bars={PREVIOUS_DAY: calm_history(45000)},
                         contracts=contracts()))


def test_while_the_intraday_program_runs_close_waits_for_it_to_confirm_and_then_closes(
        capsys, env):
    """盤中程式在跑：它可能正在等一筆進場委託的成交回報（最長 30 秒以上），那一筆成交時它會用
    記憶體裡的部位整份覆寫部位檔。所以收工指令先等它確認看到停止進場記錄，才平倉。
    下一秒盤中程式就看到了，照常賣出 3 口。"""
    _on_file()
    time = _Time(_at("10:00:00"), during_sleep=_intraday_second)

    with _intraday_running():
        code, brokers, _ = _stop("--close", time=time)

    assert code == 0
    assert time.slept[:1] == [1]
    assert _orders(brokers[0]) == [("TM2611", SELL, 3, False)]
    lines = _lines(capsys)
    assert "盤中程式在跑：等它確認看到這份記錄（最多 5 秒）。" in lines
    assert "盤中程式確認了：今天不再進場，等待中的突破進場也取消了。" in lines


def test_while_the_intraday_program_runs_and_never_confirms_close_sends_nothing(capsys, env):
    """盤中程式在跑，等了 5 秒（正式是 120 秒）都沒有確認（卡在一筆委託上，或整個卡住了）。
    不登入、不送單、回 1：這時平倉，它那一筆成交時就把收工指令記下的平倉覆寫掉，13:25 的出場程式
    再照舊檔平一次。記錄已經寫了，它一看到就不再進場；請使用者過一兩分鐘再執行一次 `--close`。"""
    content = _on_file()

    with _intraday_running():
        code, brokers, time = _stop("--close", wait=5)

    assert code == 1
    assert brokers == []
    assert time.slept == [1] * 5
    assert position_on_file() == content
    assert _lines(capsys)[-3:] == [
        "【要你處理】盤中程式在跑，等了 5 秒都沒有確認看到停止進場記錄",
        "記錄已經寫了，盤中程式一看到就不再進場。它每秒看一次：等不到通常是它正在等一筆委託的"
        "成交回報，或卡住了（看 logs/intraday-20261109.log 最後幾行）。",
        "--close 這次沒有平倉：請過一兩分鐘再執行一次 .\\.venv\\Scripts\\python.exe "
        "tools\\stop_today.py --close。",
    ]
    assert env.sent == []


# 收工指令的鎖被拿著時講的話：拿著的可能是另一個收工指令，也可能是 13:25 的出場程式
BUSY = ("另一個收工指令或 13:25 的出場程式正在處理 PTS 的部位，這一個什麼都不做："
        "照它印出的結果或 Discord 的告警處理。")


def test_a_second_stop_command_while_the_first_still_runs_does_nothing(capsys, env):
    """第一個 `--close` 還在等盤中程式確認時，使用者又執行了一次。兩個都平的話就是平兩次：
    第二個只講有人正在處理 PTS 的部位，不寫記錄、不登入、回 1。第一個照常做完。"""
    _on_file()
    lock = _intraday_running()
    lock.__enter__()
    second: list[tuple] = []

    def meanwhile(now: datetime) -> None:
        if not second:
            second.append(_stop("--close", time=_Time(now)))
        elif len(second) == 1:
            second.append("盤中程式結束")
            lock.__exit__(None, None, None)

    code, brokers, _ = _stop("--close", time=_Time(_at("10:00:00"), during_sleep=meanwhile))

    assert second[0][:2] == (1, [])
    assert BUSY in _lines(capsys)
    with open(intraday.stop_path(DAY), encoding="utf-8") as fh:
        assert len(fh.read().splitlines()) == 1
    assert code == 0
    assert _orders(brokers[0]) == [("TM2611", SELL, 3, False)]


def test_an_intraday_program_that_ends_while_close_waits_needs_no_confirmation(env):
    """等的時候盤中程式結束了（例如它這一輪本來就要結束、或當掉了）：鎖跟著放掉。它沒在等
    任何成交回報，之後被排程叫醒的那一個一開始就會讀到停止進場記錄——不必再等，照常平倉。"""
    _on_file()
    lock = _intraday_running()
    lock.__enter__()
    time = _Time(_at("10:00:00"), during_sleep=lambda now: lock.__exit__(None, None, None))

    code, brokers, _ = _stop("--close", time=time)

    assert code == 0
    assert time.slept == [1]
    assert _orders(brokers[0]) == [("TM2611", SELL, 3, False)]


# --- --close 沒能一次平完：和出場程式同樣重試，剩下的照常交給 13:25 ---


def test_a_close_order_that_was_not_sent_is_retried_a_minute_later(env):
    """「委託沒有送出去」確定沒有成交，再送是安全的：10:00 被拒，10:01 成交，平完。"""
    _on_file()

    code, brokers, time = _stop("--close", order_errors=[OrderFailed("保證金不足"), None])

    assert code == 0
    assert [(o.side, o.lots) for o in brokers[0].orders] == [(SELL, 3), (SELL, 3)]
    assert time.slept == [60]
    assert position_on_file()["exits"] == [
        {"order_seq": "2315610000002", "lots": 3, "price": 45200.0}]


class _SlowReplies(FakeBroker):
    """每筆委託要等 90 秒才知道結果（成交回報慢）：送出之後，收工指令的時鐘多走 90 秒。"""

    def __init__(self, time: _Time, **options):
        super().__init__(fill_price=45200.0, **options)
        self._time = time

    def place_order(self, request):
        self._time.now += timedelta(seconds=90)
        return super().place_order(request)


def test_lots_still_open_at_1324_are_left_to_the_1325_exit_program(capsys, env):
    """13:19 執行 `--close`，市價 IOC 每次只成交 1 口、回報又慢：13:19 送出（13:20:30 才知道成交 1 口）、
    13:21:30 再送（13:23 成交 1 口），等到 13:24 就到了期限——出場程式 13:25 起來之前一分鐘，
    兩邊不會同時送單。

    還剩 1 口：**不叫人自己平**。部位檔記著已經平掉的 2 口，13:25 的出場程式只平剩下的；
    使用者自己平的話，13:25 再平一次就是反向新倉。回 1。"""
    _on_file()
    time = _Time(_at("13:19:00"))

    code, _, _ = _stop("--close", time=time, broker=_SlowReplies(time, fills=[1, 1]))

    assert code == 1
    out = _lines(capsys)
    assert out[-5:] == [
        "PTS 部位還在：收工指令平了 2 口，還剩多單 1 口（TM2611）",
        "原因：市價 IOC 沒有全部成交，重試到 13:24 仍未完成（收工指令只送到 13:25 出場程式啟動之前）",
        "剩下的 1 口照常由 13:25 的出場程式平掉：部位檔記著已經平掉的 2 口，它只平剩下的。",
        "不要自己平：不然 13:25 會再平一次，開出反向新倉。",
        "2026/11/09 13:24:00",
    ]
    assert not any("請自行" in line for line in out)

    exit_broker, exit_sent = _exit_at_1325()
    assert [(o.side, o.lots) for o in exit_broker.orders] == [(SELL, 1)]
    assert exit_sent == []


def test_lots_still_open_after_1325_are_not_promised_to_the_exit_program(capsys):
    """回報更慢（每筆 150 秒）：13:22:30 送出的那一筆 13:25 才知道成交，收工指令 13:26 才講還剩幾口。
    這時 13:25 的出場程式已經起來、正等著收工指令做完——不能再說「照常由 13:25 的出場程式平掉」。
    照實講：它等這個收工指令結束，才照部位檔處理剩下的；Discord 收到「部位還在」或「委託送出去了，
    但…」的告警時，照那一則的口數處理。"""
    _on_file()
    time = _Time(_at("13:19:00"))

    class _SlowerReplies(_SlowReplies):
        def place_order(self, request):
            self._time.now += timedelta(seconds=60)
            return super().place_order(request)

    code, _, _ = _stop("--close", time=time, broker=_SlowerReplies(time, fills=[1, 1]))

    assert code == 1
    out = _lines(capsys)
    assert out[-5:] == [
        "PTS 部位還在：收工指令平了 2 口，還剩多單 1 口（TM2611）",
        "原因：市價 IOC 沒有全部成交，重試到 13:24 仍未完成（收工指令只送到 13:25 出場程式啟動之前）",
        "13:25 的出場程式等這個收工指令結束，才照部位檔處理剩下的 1 口：部位檔記著已經平掉的 2 口，"
        "它只平剩下的。之後 Discord 收到「部位還在」或「委託送出去了，但…」的告警時，照那一則的口數處理。",
        "在那之前不要自己平：出場程式會照部位檔再平一次，開出反向新倉。",
        "2026/11/09 13:26:00",
    ]
    assert not any("照常由 13:25 的出場程式平掉" in line for line in out)


def test_lots_of_tx_still_open_are_left_to_the_brokers_day_trade_close(capsys, env):
    """大台還剩的口數標了當沖，照常由券商 13:30 強平。登入失敗（帳密）時一口都沒平，也一樣。"""
    _on_file(product="TX00AM", order_code="TX11")

    code, brokers, _ = _stop("--close", login_error=LoginFailed("代碼 300 帳號密碼錯誤"))

    assert code == 1
    assert brokers[0].orders == []
    assert _lines(capsys)[-4:] == [
        "PTS 部位還在：收工指令平了 0 口，還剩多單 3 口（TX11）",
        "原因：代碼 300 帳號密碼錯誤",
        "剩下的 3 口標了當沖，照常由券商 13:30 強平，不必自己平。",
        "2026/11/09 10:00:00",
    ]


def test_a_close_that_cannot_be_written_to_the_position_file_says_so(monkeypatch, capsys):
    """平倉成交了，部位檔卻寫不進去（防毒鎖住）：照樣平完（帳上已經動了），但 13:25 的出場程式
    只認部位檔，會照舊檔再賣 3 口、開出反向新倉。所以講明實際與檔裡各是多少，請使用者在 13:25
    前停用出場程式的排程，回 1。"""
    _on_file()
    lock_position_file(monkeypatch)

    code, brokers, _ = _stop("--close")

    assert code == 1
    assert _orders(brokers[0]) == [("TM2611", SELL, 3, False)]
    out = _lines(capsys)
    assert out[-6:-2] == [
        "【要你處理】PTS 部位檔沒記到收工指令這次的平倉",
        "實際：平倉 3 口，還剩多單 0 口；部位檔記的是平倉 0 口",
        "原因：PermissionError: [Errno 13] 存取被拒",
        "13:25 的出場程式只認部位檔，會把已經平掉的口數再平一次，開出反向新倉：請在 13:25 前到"
        "工作排程器停用「PTS 出場程式」，帳上還在的 PTS 口數以上面的「實際」為準自己平掉，"
        "收盤後再啟用排程。",
    ]
    assert out[-2] == "也不要再執行收工指令：它一樣只認部位檔。"


def test_without_a_futures_account_close_does_not_log_in(tmp_path, monkeypatch, capsys):
    """`.env` 少了期貨帳號就送不出平倉單：不登入，講明缺什麼，回 1。部位照常由 13:25 了結。"""
    _on_file()
    secrets = tmp_path / "no-account.env"
    secrets.write_text("CAPITAL_USER_ID=A100000000\nCAPITAL_PASSWORD=pw\n", encoding="utf-8")
    monkeypatch.setattr(paths, "env_path", lambda: str(secrets))

    code, brokers, _ = _stop("--close")

    assert code == 1
    assert brokers == []
    assert _lines(capsys)[-2:] == [
        f"【要你處理】讀不到平倉要用的帳密，沒有平倉：{secrets} 缺少：CAPITAL_FUTURES_ACCOUNT",
        "部位照常了結：微台由 13:25 的出場程式平倉，大台、小台由券商 13:30 當沖強平"
        "（出場程式用的是同一份 .env）。",
    ]


class _Breaks(FakeBroker):
    def login(self) -> None:
        raise RuntimeError("意料之外的錯誤")


def test_when_the_command_itself_breaks_it_says_where_to_look(capsys):
    """收工指令本身出錯：不知道做到哪裡。回 1，講明去哪裡看、部位照常由誰了結。"""
    _on_file()

    code, _, _ = _stop("--close", broker=_Breaks())

    assert code == 1
    assert _lines(capsys)[-1] == (
        "【要你處理】收工指令意外中止（RuntimeError: 意料之外的錯誤），詳情在 "
        "logs/stop-20261109.log。部位檔記到的口數照常由 13:25 的出場程式（微台）、券商 13:30 "
        "當沖強平（大台、小台）了結；它要是已經送出平倉單，請到券商 APP 對照 log 裡的「平倉成交」"
        "確認帳戶。")


# --- 13:25 的出場程式等收工指令做完，才讀部位檔 ---


@contextlib.contextmanager
def _stop_command_running():
    """收工指令正在跑：它拿著 `state/stop_today.lock`。"""
    with state_files.exclusive(exit_program.stop_today_lock_path()) as alone:
        assert alone
        yield


def _exit_program_at_1325(time: _Time) -> tuple[FakeBroker, list[str]]:
    heartbeat.write(heartbeat.Heartbeat(DAY, at("13:20:00"), True))
    broker = FakeBroker(fill_price=45200.0)
    notifier = RecordingNotifier()
    exit_program.run(broker, make_config(), notify=notifier, clock=time.clock, sleep=time.sleep)
    return broker, notifier.sent


def test_the_1325_exit_program_waits_for_a_stop_command_still_closing():
    """收工指令 13:23:50 送出最後一筆平倉單（2 口），回報 30 秒沒來、改用委託序號查（又要 5 秒以上）：
    13:25 出場程式起來時它還沒做完，部位檔記著「這筆平倉不確定」。這時讀，出場程式會發「委託送出去了，
    但…」叫人自己平——收工指令一秒後就查到全部成交。所以出場程式先等收工指令結束，讀它留下的最後那份
    部位檔：已經平完，不送單、不發訊息。"""
    entries = [{"order_seq": "2315609990001", "lots": 10, "price": 45100.0},
               {"order_seq": "2315609990002", "lots": 2, "price": 45100.0}]
    first_close = {"order_seq": "2315610000001", "lots": 10, "price": 45200.0}
    _on_file(entries=entries, exits=[first_close], status="UNCERTAIN",
             uncertain={"stage": "EXIT", "order_seq": "2315610000002", "requested_lots": 2,
                        "known_filled": 0})
    lock = _stop_command_running()
    lock.__enter__()

    def stop_command_finishes(now: datetime) -> None:
        _on_file(entries=entries, exits=[
            first_close, {"order_seq": "2315610000002", "lots": 2, "price": 45200.0}])
        lock.__exit__(None, None, None)
    time = _Time(_at("13:25:00"), during_sleep=stop_command_finishes)

    broker, sent = _exit_program_at_1325(time)

    assert time.slept == [1]
    assert broker.orders == []
    assert sent == []


def test_a_stop_command_stuck_for_3_minutes_is_not_waited_for_any_longer():
    """收工指令卡住了（鎖一直沒放）：出場程式最多等 3 分鐘，之後照現在的部位檔照常平倉。再等下去，
    結算日的近月 13:30 就停止交易了。"""
    _on_file()

    with _stop_command_running():
        time = _Time(_at("13:25:00"))
        broker, sent = _exit_program_at_1325(time)

    assert sum(time.slept) == 180
    assert _orders(broker) == [("TM2611", SELL, 3, False)]
    assert sent == []


def test_a_stop_command_while_the_exit_program_works_does_nothing(capsys):
    """13:26 出場程式正在平倉（拿著同一把鎖）時執行收工指令：不寫記錄、不登入、回 1，講明有人正在
    處理——不是講成「另一個收工指令還在跑」。"""
    _on_file()

    with _stop_command_running():
        code, brokers, _ = _stop("--close", at="13:26:00")

    assert code == 1
    assert brokers == []
    assert _lines(capsys) == [BUSY]


# --- --close 只看部位檔，和出場程式同一條規則 ---


def _uncertain(stage: str = "ENTRY", *, seq: str = "2315609990003") -> dict:
    return {"stage": stage, "order_seq": seq, "requested_lots": 1, "known_filled": 0}


def test_an_uncertain_micro_entry_confirmed_by_the_lookup_is_closed_as_usual(capsys, env):
    """盤中留下一筆不確定的進場單（有委託序號）。和 13:25 的出場程式一樣，先用序號查券商紀錄：
    全部成交，補進部位檔、回到已確認，照常平掉 PTS 全部的 4 口——講的也是 4 口，不是查之前檔裡
    確定的 3 口。"""
    _on_file(status="UNCERTAIN", uncertain=_uncertain())

    code, brokers, _ = _stop("--close", fill_queries=[FillQuery(lots=1, avg_price=45160.0)])

    assert code == 0
    assert brokers[0].queries == [("2315609990003", DAY, 1)]
    assert _orders(brokers[0]) == [("TM2611", SELL, 4, False)]
    assert position_on_file()["status"] == "CONFIRMED"
    assert _lines(capsys)[-2] == (
        "收工指令平掉了 PTS 今天的微台多單 4 口（TM2611），部位已經了結：13:25 的出場程式"
        "不會再送單。")


def test_after_the_lookup_confirms_an_entry_the_lots_left_are_counted_from_the_new_total(capsys):
    """查到那筆不確定的進場全部成交（共 4 口），之後的平倉單每一次都被拒：平了 0 口、還剩 4 口。
    拿查之前的 3 口去減，就會講成「平了 -1 口」。"""
    _on_file(status="UNCERTAIN", uncertain=_uncertain())

    code, brokers, _ = _stop("--close", fill_queries=[FillQuery(lots=1, avg_price=45160.0)],
                             order_errors=[OrderFailed("保證金不足")] * 4)

    assert code == 1
    assert len(brokers[0].orders) == 4
    assert "PTS 部位還在：收工指令平了 0 口，還剩多單 4 口（TM2611）" in _lines(capsys)


def test_an_uncertain_micro_entry_the_lookup_cannot_confirm_is_left_for_1325(capsys, env):
    """查不到：不送單、回 1，講明知道的與要平幾口。**但不叫人自己平**：部位檔還是這筆不確定的
    進場，13:25 的出場程式會再用序號查一次，查到全部成交就照常平倉——使用者先自己平掉的話，
    那時就是再平一次，開出反向新倉（同盤中那則「委託送出去了，但…」）。"""
    content = _on_file(status="UNCERTAIN", uncertain=_uncertain())

    code, brokers, _ = _stop("--close", fill_queries=[FillQuery(lots=None)])

    assert code == 1
    assert brokers[0].orders == []
    assert position_on_file() == content
    out = _lines(capsys)
    assert out[-6:] == [
        "PTS 委託送出去了，但不知道成交幾口，收工指令沒有平倉",
        "不確定的委託：進場買進微台 1 口（委託序號 2315609990003），已知至少成交 0 口",
        "券商的成交紀錄：查不到這筆委託的成交",
        "部位檔記到的部位（不含這一筆）：多單 3 口",
        "微台不必現在處理：13:25 的出場程式會再用委託序號向券商查一次，查到全部成交就照常平倉"
        "（賣出微台 4 口）；查不到確定的口數會發 Discord，寫明要平幾口。在那之前不要自己平："
        "不然 13:25 會再平一次，開出反向新倉。",
        "2026/11/09 10:00:00",
    ]
    assert not any("請在" in line or "自行" in line for line in out)


@pytest.mark.parametrize("overrides, first_line", [
    (dict(product="TX00AM", order_code="TX11", uncertain=_uncertain()),
     "不確定的委託：進場買進大台 1 口（委託序號 2315609990003），已知至少成交 0 口"),
    (dict(uncertain=_uncertain("EXIT")),
     "不確定的委託：平倉賣出微台 1 口（委託序號 2315609990003），已知至少成交 0 口"),
    (dict(uncertain=_uncertain(seq="")),
     "不確定的委託：進場買進微台 1 口（委託序號 沒有拿到），已知至少成交 0 口"),
], ids=["大台", "平倉單", "沒有序號"])
def test_other_uncertain_positions_send_nothing_and_say_what_the_exit_program_would(
        overrides, first_line, capsys, env):
    """大台小台、平倉單、沒有序號的不確定：13:25 的出場程式也不查、不送。收工指令一樣不登入、
    不送單、回 1，印出出場程式那一則同樣的內容。"""
    content = _on_file(status="UNCERTAIN", **overrides)

    code, brokers, _ = _stop("--close")

    assert code == 1
    assert brokers == []
    assert position_on_file() == content
    out = _lines(capsys)
    assert out[-5:-3] == ["【要你處理】PTS 委託送出去了，但不知道成交幾口，出場程式不再送單",
                          first_line]


def test_without_a_position_file_close_sends_nothing(capsys, env):
    """沒有部位檔：今天沒有真實部位（模擬模式也是這樣，自動下單關閉時完全不呼叫下單 API，
    規格第 42 條）。不登入、不送單，回 0。"""
    code, brokers, _ = _stop("--close")

    assert code == 0
    assert brokers == []
    assert _lines(capsys)[-1] == "今天沒有真實部位（沒有部位檔，模擬模式也是這樣），沒有要了結的。"


def test_a_position_already_closed_is_not_closed_again(capsys, env):
    content = _on_file(exits=[{"order_seq": "2315609990009", "lots": 3, "price": 45180.0}])

    code, brokers, _ = _stop("--close")

    assert code == 0
    assert brokers == []
    assert position_on_file() == content
    assert _lines(capsys)[-1] == "PTS 今天的微台部位已經平完，沒有要了結的。"


def test_a_position_file_that_cannot_be_read_sends_nothing_and_says_how_to_count(capsys, env):
    """讀不懂就不知道帳上有幾口、是哪個商品。不送單、回 1，講法同出場程式：13:25 的出場程式
    同樣讀不懂、不會平；口數從 log 算——收工指令稍早平過的，記在它自己的 log 裡。"""
    write_position_file("{ 寫到一半")

    code, brokers, _ = _stop("--close")

    assert code == 1
    assert brokers == []
    out = _lines(capsys)
    assert out[-5] == "【要你處理】PTS 部位檔讀不懂，收工指令沒有平倉"
    assert out[-4].startswith("原因：")
    assert out[-3:] == [
        "PTS 今天可能還有部位：請到券商 APP 確認帳戶。微台要自行平掉（日盤 13:45 收盤；今天如果是"
        "結算日，到期的近月 13:30 就停止交易，之後由交易所以最後結算價現金了結）；大台、小台標了"
        "當沖，券商 13:30 會強平。13:25 的出場程式同樣讀不懂這個檔，不會替你平倉。",
        "PTS 的口數：logs/intraday-20261109.log 最後一筆「進場成交」的累計口數，減掉 "
        "logs/exit-20261109.log 與 logs/stop-20261109.log 裡每一筆「平倉成交」的口數"
        "（有「不知道成交幾口」的委託時，另外到券商 APP 用委託序號查）。只平這個口數。",
        "2026/11/09 10:00:00",
    ]


def test_after_the_entry_cutoff_close_sends_nothing_and_says_who_closes(capsys, env):
    """13:20 之後的 `--close` 不送任何單：出場程式 13:25、券商 13:30 就要接手，收工指令再送單
    就可能和它們同時平倉。講明由誰處理，回 0。"""
    _on_file()

    code, brokers, _ = _stop("--close", at="13:20:00")

    assert code == 0
    assert brokers == []
    assert _lines(capsys)[-2:] == [
        "已經過了 13:20 進場截止，收工指令不送單：出場程式 13:25、券商 13:30 就要接手，"
        "這時再送單可能和它們同時平倉。",
        "PTS 今天還沒平的部位：微台多單 3 口（TM2611），13:25 的出場程式會平掉。",
    ]
