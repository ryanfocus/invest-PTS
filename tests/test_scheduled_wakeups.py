"""排程每分鐘叫醒盤中程式（ticket 10）——從最上層打進去，同一天跑好幾輪 `intraday.run`。

盤中程式的排程從 08:40 起每分鐘叫醒它一次，直到 13:20 進場截止；已經在跑就不啟動新的。
這就是「失敗時自動重啟」：當掉、被關掉、電腦重開之後，一分鐘內就會被叫回來（規格第 48 條）。
代價是**正常結束之後也會再被叫醒**，所以盤中程式自己要知道今天是不是已經不必再跑：

  * 今天已經走到結局（13:20 收工）：不登入、不再告警。
  * 今天登入失敗過：不再登入。帳密錯了的話，每分鐘再試一次就是一天兩百多次錯誤的登入。

斷言送出的委託、發出的訊息，以及有沒有登入（假 broker 的登入一被呼叫就讓測試失敗）。
"""

import pytest

from broker import LoginFailed, LoginUnavailable
from broker.fake import FakeBroker
from day_script import (DAY, PREVIOUS_DAY, at, main_env, make_config, run_day, whole_morning,
                        write_position_file)
from recording import RecordingNotifier
import exit_program
import heartbeat
import intraday


def _wake(events=(), *, day=DAY, **broker_options):
    """排程叫醒盤中程式一次（`run_day`）。回傳 (發出的訊息, 結果)。"""
    _, sent, outcome = run_day(events, today=day, **broker_options)
    return sent, outcome


def _must_not_log_in(why: str) -> AssertionError:
    return AssertionError(why)


def test_an_unreadable_position_file_is_reported_once_however_often_the_program_restarts():
    """部位檔讀不懂：盤中程式告警、今天不進場（ticket 06），行情照記。它在 10:00 當掉，排程一分鐘
    後又叫醒它，檔案還是讀不懂——再告警一次的話，當掉幾次就講幾次同一件事。部位檔只有真實下單
    會寫：自動下單打開的一天。"""
    write_position_file("{寫到一半")
    morning = whole_morning()
    live = make_config(auto_order_enabled=True)
    sent, _ = _wake([e for e in morning if e.time <= at("10:00:00")], config=live)
    assert [m.splitlines()[0] for m in sent] == ["【要你處理】PTS 部位檔讀不懂，盤中程式今天不進場"]

    sent, outcome = _wake([e for e in morning if e.time >= at("10:01:00")], config=live)
    assert sent == []
    assert outcome.ending == intraday.Ending.CUTOFF


def test_after_the_1320_cutoff_the_next_wake_up_does_not_log_in_again():
    """排程最後一次叫醒剛好碰上收工的那一刻（或電腦下午才開機、排程補跑）：今天已經收工，
    不必再登入一次。"""
    _wake(whole_morning())
    sent, outcome = _wake(login_error=_must_not_log_in("今天已經收工，不該再登入"))
    assert sent == []
    assert outcome.ending == intraday.Ending.FINISHED_EARLIER


def test_what_the_previous_trading_day_concluded_does_not_stop_today():
    """心跳記錄只有一個檔，昨天收工時寫的「收工了」今天早上還在。那是昨天的結局。"""
    heartbeat.write(heartbeat.Heartbeat(PREVIOUS_DAY, at("13:20:00", PREVIOUS_DAY), True))
    _, outcome = _wake(whole_morning())
    assert outcome.ending == intraday.Ending.CUTOFF


# --- 登入失敗：今天不再登入 ---


def test_a_failed_login_is_reported_at_once_with_how_to_get_going_again():
    """登入失敗（帳密、憑證、群益元件）當下就講一則：等 13:25 出場程式的「盤中程式今天沒有跑」
    才知道的話，當天的進場時段已經過完了。講原因，也講排除之後怎麼讓它再登入——今天不再自動
    重登，是怕把帳號鎖住。下一次被叫醒不再講（也不再登入）。"""
    notifier = RecordingNotifier()
    with pytest.raises(LoginFailed):
        intraday.run(FakeBroker(login_error=LoginFailed("群益登入失敗，代碼 1001：密碼錯誤")),
                     make_config(), today=DAY, notify=notifier)
    assert notifier.sent == [
        "【要你處理】PTS 盤中程式登入失敗，今天不再自動登入\n"
        "原因：群益登入失敗，代碼 1001：密碼錯誤\n"
        "今天不判斷進場，也不記錄原始行情；已經有的部位照常由 13:25 的出場程式處理。帳密錯了還一直"
        "重試，會把帳號鎖住，所以盤中程式今天不再自己登入。\n"
        "排除原因（例如改好 .env 的密碼、更新憑證）之後，刪掉 state/login-failed-20261109.txt，"
        "一分鐘內會再登入。\n"
        "2026/11/09",
    ]

    sent, _ = _wake(login_error=_must_not_log_in("今天登入失敗過，不該再登入"))
    assert sent == []


def test_after_a_failed_login_the_next_wake_ups_do_not_log_in_again():
    """登入失敗要人處理（帳密、憑證、群益元件），重試沒有用（`broker.LoginFailed`）。帳密錯了
    還每分鐘再試，一天就是兩百多次錯誤的登入，可能把帳號鎖住——連使用者自己的 APP 也登不進去。"""
    try:
        _wake(login_error=LoginFailed("群益登入失敗，代碼 1001：密碼錯誤"))
    except LoginFailed:
        pass
    sent, outcome = _wake(login_error=_must_not_log_in("今天登入失敗過，不該再登入"))
    assert sent == []
    assert outcome.ending == intraday.Ending.LOGIN_FAILED_EARLIER


def test_a_login_lost_to_the_network_is_tried_again_at_the_next_wake_up():
    """開盤時碰上一次斷線、群益主機忙（`broker.LoginUnavailable`）：沒有驗證到密碼，不會把帳號
    鎖住。下一分鐘排程叫醒時照常登入、照常跑完——一次網路異常不能賠掉一整天。"""
    try:
        _wake(login_error=LoginUnavailable("群益登入沒有成功（網路或群益主機），代碼 1108"))
    except LoginUnavailable:
        pass
    _, outcome = _wake(whole_morning())
    assert outcome.ending == intraday.Ending.CUTOFF


def test_a_failed_login_still_shows_up_at_1325_as_the_intraday_program_not_running():
    """登入失敗當下講過一則之後，13:25 出場程式照樣講「盤中程式今天沒有跑」（規格第 54 條）：
    使用者可能還沒排除——「不再登入」不能被寫成「收工了」。"""
    try:
        _wake(login_error=LoginFailed("群益登入失敗，代碼 1001：密碼錯誤"))
    except LoginFailed:
        pass
    _wake(login_error=_must_not_log_in("今天登入失敗過，不該再登入"))

    notifier = RecordingNotifier()
    exit_program.run(FakeBroker(), make_config(), notify=notifier, clock=lambda: at("13:25:00"),
                     sleep=lambda seconds: None)
    assert [m.splitlines()[0] for m in notifier.sent] == ["【要你處理】PTS 盤中程式今天沒有跑"]


def test_a_failed_login_on_the_previous_trading_day_does_not_stop_today():
    """排除原因（改好 `.env`、展延憑證）之後，下一個交易日照常登入。"""
    try:
        _wake(day=PREVIOUS_DAY, login_error=LoginFailed("群益登入失敗，代碼 1001：密碼錯誤"))
    except LoginFailed:
        pass
    _, outcome = _wake(whole_morning())
    assert outcome.ending == intraday.Ending.CUTOFF


# --- 同一時間只有一個盤中程式 ---


def test_a_second_intraday_program_started_while_one_is_running_does_nothing(tmp_path,
                                                                             monkeypatch):
    """兩個盤中程式同時跑，同一分鐘就會進兩次場（ticket 09）。排程設了「已經在跑就不啟動新的」，
    但那只管得到排程自己叫醒的；有人在命令列手動再跑一次、或排程以為它已經停了（只砍掉了
    外層的行程），第二個就起來了。所以盤中程式自己也要擋：第一個還在跑，第二個什麼都不做——
    不登入（它的 broker 一登入就讓測試失敗），正常結束。第一個結束之後再啟動就照常登入。"""
    main_env(tmp_path, monkeypatch)
    second = []

    class _FirstOne(FakeBroker):
        def login(self) -> None:
            super().login()
            # 第一個還在跑的時候，又被啟動一次
            locked_out = FakeBroker(login_error=AssertionError("另一個盤中程式在跑，不該登入"))
            second.append(intraday.main(connect=lambda secrets: locked_out, today=lambda: DAY))

    assert intraday.main(connect=lambda secrets: _FirstOne(), today=lambda: DAY) == 0
    assert second == [0]

    # 第一個結束之後再啟動：照常登入（這次的登入因網路失敗，`main()` 回 1，下一分鐘再試）
    offline = FakeBroker(login_error=LoginUnavailable("群益登入沒有成功（網路或群益主機），代碼 1108"))
    assert intraday.main(connect=lambda secrets: offline, today=lambda: DAY) == 1
