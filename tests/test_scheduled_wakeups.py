"""排程每分鐘叫醒盤中程式（ticket 10）——從最上層打進去，同一天跑好幾輪 `intraday.run`。

盤中程式的排程從 08:40 起每分鐘叫醒它一次，直到 13:20 進場截止；已經在跑就不啟動新的。
這就是「失敗時自動重啟」：當掉、被關掉、電腦重開之後，一分鐘內就會被叫回來（規格第 48 條）。
代價是**正常結束之後也會再被叫醒**，所以盤中程式自己要知道今天是不是已經不必再跑：

  * 今天已經走到結局（13:20 收工、部位檔讀不懂而不進場）：不登入、不再告警。
  * 今天登入失敗過：不再登入。帳密錯了的話，每分鐘再試一次就是一天兩百多次錯誤的登入。

斷言送出的委託、發出的訊息，以及有沒有登入（假 broker 的登入一被呼叫就讓測試失敗）。
"""

import os

from broker import TMF_CODE, LoginFailed, LoginUnavailable
from broker.fake import FakeBroker
from day_script import DAY, PREVIOUS_DAY, at, calm_history, contracts, whole_morning
from recording import RecordingNotifier
import exit_program
import heartbeat
import intraday
import position_file
import settings


def _config(**overrides) -> settings.Config:
    values = dict(auto_order_enabled=False, product=TMF_CODE, lots_per_entry=1,
                  max_position=20, discord_enabled=True)
    values.update(overrides)
    return settings.Config(**values)


def _wake(events=(), *, day=DAY, **broker_options):
    """排程叫醒盤中程式一次。回傳 (發出的訊息, 結果)。"""
    broker = FakeBroker(contracts=contracts(), events=events,
                        minute_bars={PREVIOUS_DAY: calm_history(45000)}, **broker_options)
    notifier = RecordingNotifier()
    outcome = intraday.run(broker, _config(), today=day, notify=notifier)
    return notifier.sent, outcome


def _must_not_log_in(why: str) -> AssertionError:
    return AssertionError(why)


def _write_position_file(text: str) -> None:
    path = position_file.path_for(DAY)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def test_after_giving_up_on_an_unreadable_position_file_the_next_wake_up_stays_quiet():
    """部位檔讀不懂：盤中程式告警、今天不進場（ticket 06）。一分鐘後排程又叫醒它，檔案
    還是讀不懂——再告警一次的話，到 13:20 就是兩百多則同樣的訊息。"""
    _write_position_file("{寫到一半")
    sent, _ = _wake(login_error=_must_not_log_in("部位檔讀不懂時不該登入"))
    assert [m.splitlines()[0] for m in sent] == ["【要你處理】PTS 部位檔讀不懂，盤中程式今天不進場"]

    sent, _ = _wake(login_error=_must_not_log_in("今天已經下過結論，不該再登入"))
    assert sent == []


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
    """登入失敗時盤中程式不發 Discord（沒有進場、也沒有部位），使用者是從 13:25 出場程式的
    「盤中程式今天沒有跑」知道的（規格第 54 條）——「不再登入」不能被寫成「收工了」。"""
    try:
        _wake(login_error=LoginFailed("群益登入失敗，代碼 1001：密碼錯誤"))
    except LoginFailed:
        pass
    _wake(login_error=_must_not_log_in("今天登入失敗過，不該再登入"))

    notifier = RecordingNotifier()
    exit_program.run(FakeBroker(), _config(), notify=notifier, clock=lambda: at("13:25:00"),
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


def _main_env(tmp_path, monkeypatch) -> None:
    """`intraday.main()` 的外部環境：暫存的 `.env`、版控裡的設定檔、不寫 log 檔。"""
    import paths

    env = tmp_path / ".env"
    env.write_text("CAPITAL_USER_ID=A100000000\nCAPITAL_PASSWORD=pw\n"
                   "DISCORD_WEBHOOK_URL=https://discord.invalid/api/webhooks/1/x\n",
                   encoding="utf-8")
    monkeypatch.setattr(paths, "env_path", lambda: str(env))
    monkeypatch.setattr(intraday, "_setup_logging", lambda today: None)


def test_a_second_intraday_program_started_while_one_is_running_does_nothing(tmp_path,
                                                                             monkeypatch):
    """兩個盤中程式同時跑，同一分鐘就會進兩次場（ticket 09）。排程設了「已經在跑就不啟動新的」，
    但那只管得到排程自己叫醒的；有人在命令列手動再跑一次、或排程以為它已經停了（只砍掉了
    外層的行程），第二個就起來了。所以盤中程式自己也要擋：第一個還在跑，第二個什麼都不做。"""
    _main_env(tmp_path, monkeypatch)
    sessions, second = [], []

    def session(broker, config, *, today, notify):
        sessions.append(today)
        if len(sessions) == 1:
            second.append(intraday.main())     # 第一個還在跑的時候，又被啟動一次
        return intraday.SessionOutcome(intraday.Ending.CUTOFF)
    monkeypatch.setattr(intraday, "run", session)

    assert intraday.main() == 0
    assert second == [0]
    assert len(sessions) == 1

    intraday.main()                             # 第一個結束之後再啟動：照常跑
    assert len(sessions) == 2
