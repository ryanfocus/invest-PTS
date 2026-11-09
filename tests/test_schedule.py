"""排程（ticket 10）：工作排程器叫醒程式的入口 `scheduled.py`，以及建立、查看、移除排程的工具
`tools/schedule.py`。

兩者都**絕不真的動到這台電腦的工作排程器**：這台機器裝著群益元件與憑證，一個真的排程明天
08:40 就會登入券商。排程工具的外部效果是它交給 `schtasks` 的命令與排程定義，測試換掉的就是
那一個接縫，斷言排程定義裡寫了什麼。
"""

import os
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime

import pytest

import paths
import scheduled
from conftest import REPO_ROOT

sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
import schedule  # noqa: E402


def _log(name: str) -> str:
    """當天的 log。出場程式 13:25 告警「盤中程式今天沒有跑」時叫人去看的就是這個檔
    （`logs/intraday-YYYYMMDD.log`）。"""
    path = os.path.join(paths.logs_dir(), f"{name}-{date.today():%Y%m%d}.log")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# --- 排程叫醒程式的入口 ---


def test_a_program_that_cannot_even_be_loaded_leaves_the_reason_in_its_log(monkeypatch):
    """排程用 pythonw 執行（沒有主控台視窗），traceback 無處可去。程式在設定好 log 之前就死掉
    （少裝了套件、語法錯誤）時，排程只記下「結果 1」，log 一個字都沒有——invest-os 2026-08-23
    就是這樣，症狀是「排程跑了但什麼都沒發生」，最難查的一種。"""
    monkeypatch.setitem(sys.modules, "intraday", None)       # import intraday 會失敗

    assert scheduled.main(["scheduled.py", "intraday"]) == 1
    log = _log("intraday")
    assert "盤中程式沒有正常結束" in log
    assert log.splitlines()[-1].startswith("ModuleNotFoundError: import of intraday")


def test_without_a_console_a_crash_after_start_is_also_in_the_log(monkeypatch):
    """pythonw 的 `sys.stdout`／`sys.stderr` 是 None：直接寫它們的程式碼（第三方套件也算）不能
    因此出錯；程式跑到一半意外中止，原因照樣留在當天的 log。"""
    import intraday

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    def crash():
        sys.stderr.write("某個套件直接寫 stderr\n")
        raise RuntimeError("意料之外的錯誤")
    monkeypatch.setattr(intraday, "main", crash)

    assert scheduled.main(["scheduled.py", "intraday"]) == 1
    assert _log("intraday").splitlines()[-1] == "RuntimeError: 意料之外的錯誤"


def test_the_scheduler_records_the_programs_own_result(monkeypatch):
    """出場程式回 1（程式出錯、設定有錯）或 0，排程的「上次執行結果」照實記下。"""
    import exit_program

    monkeypatch.setattr(exit_program, "main", lambda: 1)
    assert scheduled.main(["scheduled.py", "exit"]) == 1
    monkeypatch.setattr(exit_program, "main", lambda: 0)
    assert scheduled.main(["scheduled.py", "exit"]) == 0


# --- 建立、查看、移除排程的工具 ---

_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
INTRADAY, EXIT = "PTS 盤中程式", "PTS 出場程式"


class _Schtasks:
    """代替 `schtasks.exe`：記下每一次的參數，以及 /Create 時交給它的排程定義。"""

    def __init__(self, result: int = 0):
        self.calls: list[list[str]] = []
        self.definitions: dict[str, ET.Element] = {}
        self._result = result

    def __call__(self, args: list[str]) -> int:
        self.calls.append(list(args))
        if args[0] == "/Create":
            with open(args[args.index("/XML") + 1], encoding="utf-16") as fh:
                self.definitions[args[args.index("/TN") + 1]] = ET.fromstring(fh.read())
        return self._result


@pytest.fixture
def venv(tmp_path):
    """一個假的虛擬環境：`python.exe` 旁邊有 `pythonw.exe`。"""
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    for name in ("python.exe", "pythonw.exe"):
        (scripts / name).write_bytes(b"")
    return scripts


def _install(venv, at: str = "2026-11-10 07:00", schtasks=None) -> _Schtasks:
    schtasks = schtasks or _Schtasks()
    code = schedule.main(["install"], schtasks=schtasks, python=str(venv / "python.exe"),
                         now=lambda: datetime.strptime(at, "%Y-%m-%d %H:%M"), user=r"PC\User")
    assert code == 0
    return schtasks


def test_the_intraday_program_is_woken_from_0840_every_minute_until_the_1320_cutoff(venv):
    """「08:40 啟動，失敗時自動重啟」：工作排程器的「失敗時重新啟動」只管得到啟動失敗，程式
    跑起來之後回傳錯誤、被砍掉、電腦重開，它都不管。所以改成從 08:40 起每分鐘叫醒一次，直到
    13:20 進場截止，**已經在跑就不啟動新的**——當掉之後一分鐘內就回來，而同一時間只會有一個
    （兩個同時跑，同一分鐘會進兩次場）。正常結束之後被叫醒，盤中程式自己知道今天不必再跑。"""
    task = _install(venv).definitions[INTRADAY]

    trigger = task.find("t:Triggers/t:CalendarTrigger", _NS)
    assert trigger.find("t:StartBoundary", _NS).text == "2026-11-10T08:40:00"
    assert trigger.find("t:ScheduleByDay/t:DaysInterval", _NS).text == "1"   # 每天，含假日
    assert trigger.find("t:Repetition/t:Interval", _NS).text == "PT1M"
    assert trigger.find("t:Repetition/t:Duration", _NS).text == "PT4H40M"   # 到 13:20
    assert task.find("t:Settings/t:MultipleInstancesPolicy", _NS).text == "IgnoreNew"

    command = task.find("t:Actions/t:Exec", _NS)
    assert command.find("t:Command", _NS).text == str(venv / "pythonw.exe")
    assert command.find("t:Arguments", _NS).text == (
        f'"{os.path.join(paths.app_root(), "scheduled.py")}" intraday')
    assert command.find("t:WorkingDirectory", _NS).text == paths.app_root()


def test_the_exit_program_is_woken_once_at_1325_and_never_rerun(venv):
    """出場程式不設任何重跑（ticket 08）：告警之後使用者可能已經自己平掉剩下的口數，再跑一次，
    它照部位檔再平一次，就是反向新倉。"""
    task = _install(venv).definitions[EXIT]

    trigger = task.find("t:Triggers/t:CalendarTrigger", _NS)
    assert trigger.find("t:StartBoundary", _NS).text == "2026-11-10T13:25:00"
    assert trigger.find("t:ScheduleByDay/t:DaysInterval", _NS).text == "1"
    assert trigger.find("t:Repetition", _NS) is None
    assert task.find("t:Settings/t:RestartOnFailure", _NS) is None
    assert task.find("t:Settings/t:MultipleInstancesPolicy", _NS).text == "IgnoreNew"
    assert task.find("t:Actions/t:Exec/t:Arguments", _NS).text.endswith('scheduled.py" exit')


@pytest.mark.parametrize("name", [INTRADAY, EXIT])
def test_both_programs_catch_up_after_a_missed_start_and_run_on_battery(venv, name):
    """電腦當時關機或睡著：開機後補跑（盤中程式照常執行；出場程式 13:45 之後才補跑就不送單、
    告警，ticket 08）。工作排程器預設用電池時不啟動、拔電就砍掉——筆電沒插電的那天出場程式
    不跑，微台就過夜，而且一則通知都沒有。可以喚醒睡著的電腦。"""
    settings = _install(venv).definitions[name].find("t:Settings", _NS)
    assert settings.find("t:StartWhenAvailable", _NS).text == "true"
    assert settings.find("t:DisallowStartIfOnBatteries", _NS).text == "false"
    assert settings.find("t:StopIfGoingOnBatteries", _NS).text == "false"
    assert settings.find("t:WakeToRun", _NS).text == "true"


@pytest.mark.parametrize("name", [INTRADAY, EXIT])
def test_both_programs_run_as_the_logged_in_user_who_holds_the_certificate(venv, name):
    """群益的下單憑證裝在使用者帳號底下：排程用「只在使用者登入時執行」，掛在建排程的那個人
    底下（invest-os 的經驗）。"""
    principal = _install(venv).definitions[name].find("t:Principals/t:Principal", _NS)
    assert principal.find("t:UserId", _NS).text == r"PC\User"
    assert principal.find("t:LogonType", _NS).text == "InteractiveToken"


def test_the_intraday_program_is_not_woken_while_the_network_is_down(venv):
    """登入失敗今天就不再試（帳密錯了每分鐘再試會把帳號鎖住，群益代碼 307）。開機自動登入之後
    網路還沒好的那幾秒去登入，登入失敗就賠掉一整天；網路不通時不叫醒，下一分鐘再看。
    出場程式照樣叫醒：它是微台唯一的出場，連不上也要留下記錄。"""
    definitions = _install(venv).definitions
    assert definitions[INTRADAY].find("t:Settings/t:RunOnlyIfNetworkAvailable", _NS).text == "true"
    assert definitions[EXIT].find("t:Settings/t:RunOnlyIfNetworkAvailable", _NS).text == "false"


FIRST_RUNS = {
    # 建排程的時刻 → (盤中程式第一次, 出場程式第一次)
    "開盤前建": ("2026-11-10 07:00", "2026-11-10T08:40:00", "2026-11-10T13:25:00"),
    "盤中建": ("2026-11-10 10:00", "2026-11-11T08:40:00", "2026-11-10T13:25:00"),
    "收盤後建": ("2026-11-10 15:00", "2026-11-11T08:40:00", "2026-11-11T13:25:00"),
}


@pytest.mark.parametrize("at, intraday, exit_", FIRST_RUNS.values(), ids=FIRST_RUNS.keys())
def test_a_new_schedule_starts_at_the_next_time_that_has_not_passed(venv, at, intraday, exit_):
    """起點寫成今天已經過去的時刻，「錯過就補跑」可能一建好就把它當成錯過的那一次：下午建排程，
    出場程式立刻跑起來，告警「盤中程式今天沒有跑」。"""
    definitions = _install(venv, at=at).definitions
    start = "t:Triggers/t:CalendarTrigger/t:StartBoundary"
    assert definitions[INTRADAY].find(start, _NS).text == intraday
    assert definitions[EXIT].find(start, _NS).text == exit_


def test_installing_again_replaces_the_two_tasks_instead_of_adding_more(venv):
    """換機器、搬資料夾、重建虛擬環境之後要重跑一次（排程記的是絕對路徑）。重跑要收斂到同樣的
    兩個排程，不是越積越多。"""
    schtasks = _install(venv)
    created = [c for c in schtasks.calls if c[0] == "/Create"]
    assert [c[c.index("/TN") + 1] for c in created] == [INTRADAY, EXIT]
    assert all("/F" in c for c in created)


def test_show_and_remove_cover_both_tasks():
    shown, removed = _Schtasks(), _Schtasks()
    assert schedule.main(["show"], schtasks=shown) == 0
    assert schedule.main(["remove"], schtasks=removed) == 0
    assert [c[:3] for c in shown.calls] == [["/Query", "/TN", INTRADAY], ["/Query", "/TN", EXIT]]
    assert removed.calls == [["/Delete", "/TN", INTRADAY, "/F"], ["/Delete", "/TN", EXIT, "/F"]]


def test_a_task_that_schtasks_refuses_makes_the_tool_fail(venv):
    """schtasks 的錯誤訊息直接印在畫面上；工具要回非 0，不能說「建好了」。"""
    code = schedule.main(["install"], schtasks=_Schtasks(result=1),
                         python=str(venv / "python.exe"), user=r"PC\User")
    assert code == 1


def test_without_pythonw_next_to_the_interpreter_nothing_is_registered(tmp_path):
    """排程記下的是執行這支工具的那個 Python 旁邊的 pythonw.exe。找不到就什麼都不建，
    而不是建一個每天都啟動失敗、又沒有任何記錄的排程。"""
    lonely = tmp_path / "python.exe"
    lonely.write_bytes(b"")
    schtasks = _Schtasks()
    assert schedule.main(["install"], schtasks=schtasks, python=str(lonely), user="u") == 1
    assert schtasks.calls == []


@pytest.mark.skipif(sys.platform != "win32", reason="工作排程器只在 Windows 上")
@pytest.mark.parametrize("task", schedule.TASKS, ids=lambda t: t.program)
def test_this_machines_task_scheduler_understands_both_definitions(task):
    """排程定義的格式只有工作排程器自己說了算：拿這台機器的工作排程器**解析**一次（只建立
    記憶體裡的定義，不註冊任何排程），寫錯一個元素名稱或值，它就拒絕。"""
    client = pytest.importorskip("win32com.client", reason="pywin32 沒裝，見 requirements.txt")
    service = client.Dispatch("Schedule.Service")
    service.Connect()
    definition = service.NewTask(0)
    definition.XmlText = schedule.task_xml(task, pythonw=r"C:\venv\Scripts\pythonw.exe",
                                           now=datetime(2026, 11, 10, 7, 0), user=r"PC\User")
    assert definition.Settings.MultipleInstances == 2          # TASK_INSTANCES_IGNORE_NEW
    assert definition.Settings.StartWhenAvailable
