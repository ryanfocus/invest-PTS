"""建立、查看、移除 PTS 的兩個排程（ticket 10）。

用法（在專案根目錄，**用虛擬環境的 python 執行**——排程記下的就是它旁邊的 pythonw.exe）：

    .venv\\Scripts\\python.exe tools\\schedule.py install    建立（或覆蓋）兩個排程
    .venv\\Scripts\\python.exe tools\\schedule.py show       查看現況，不改任何東西
    .venv\\Scripts\\python.exe tools\\schedule.py remove     兩個都移除，PTS 從此不再自己跑

不需要系統管理員。重跑 `install` 會覆蓋成同樣的兩個排程，不會越積越多。換機器、移動專案資料夾、
重建虛擬環境之後都要重跑一次：排程記的是絕對路徑。

## 兩個排程

| 排程 | 什麼時候 | 做什麼 |
|------|---------|--------|
| PTS 盤中程式 | 每天 08:40 起每分鐘一次，到 13:20 為止；已經在跑就不啟動新的 | `scheduled.py intraday` |
| PTS 出場程式 | 每天 13:25 一次 | `scheduled.py exit` |

**每天都叫醒，假日也是**：是不是交易日由程式自己判斷（台灣假日套件；颱風假靠報價的交易日），
排程不重複一份假日表。**排程也不管自動下單的開關**：要不要真的下單，只看 `config/settings.yaml`
的 `order.auto_enabled`。

### 盤中程式：「失敗時自動重啟」為什麼是每分鐘叫醒一次

工作排程器自己的「工作失敗時重新啟動」只管得到**啟動失敗**（找不到執行檔、帳號無法登入）；
程式跑起來之後回傳錯誤、被砍掉、電腦重開，它都不管【推測，未在這台機器實測；網路上的實務經驗
一致】。所以改成從 08:40 起每分鐘叫醒一次，直到 13:20 進場截止，加上「已經在跑就不啟動新的」：

  * 當掉、被關掉、電腦重開（自動登入回桌面）之後，一分鐘內就回來。盤中重啟是安全的：部位、
    停止進場記錄、今天的 K 棒都從檔案讀回（ticket 09）。
  * 同一時間只會有一個。兩個同時跑，同一分鐘會進兩次場；盤中程式自己也用檔案鎖再擋一次。
  * 正常結束之後還是會被叫醒：盤中程式自己知道今天已經收工（心跳記錄）或登入失敗過，什麼都
    不做就結束。非交易日每分鐘起來看一眼日曆就結束，不登入。
  * **網路不通時不叫醒**：開機後網路還沒好的那幾秒不必去登入。群益回網路、主機類代碼的登入
    下一分鐘照樣再試（`broker.LoginUnavailable`）；帳密、憑證與分不清的代碼今天就不再試（帳密錯了
    每分鐘再試會把帳號鎖住，群益代碼 307）——分不清的網路錯誤因此也會賠掉一天，這一項替它擋一點。

### 出場程式：不重跑

出場程式**不設任何重跑**（ticket 08）：告警之後使用者可能已經自己平掉剩下的口數，再跑一次，
它照部位檔再平一次，就是反向新倉。網路不通也照樣叫醒：它是微台唯一的出場，連不上就會告警。

### 兩個排程共同的設定

  * **錯過就補跑**（電腦當時關機或睡著）：盤中程式補跑照常執行；出場程式 13:45 之後才補跑時
    不送單、發 Discord 請你在夜盤處理（ticket 08）。
  * **用電池也照跑**：工作排程器預設用電池時不啟動、拔電就砍掉。筆電沒插電的那天，出場程式
    不跑，微台就過夜。
  * **可以喚醒電腦**（電源選項的「喚醒計時器」也要啟用）。
  * **只在使用者登入時執行**：群益的下單憑證裝在使用者帳號底下（invest-os 的經驗）。代價是電腦
    要保持登入（鎖定畫面可以），Windows Update 重開機之後要能自動登入回桌面。
  * **用 pythonw，沒有視窗**（理由見 `scheduled.py`）。看它有沒有在跑：`show`、工作管理員、
    或 `logs/`。
  * 正常的優先順序（工作排程器的預設是「低於正常」）。
  * 執行時間上限：盤中程式 14 小時、出場程式 1 小時。卡住的那一個不會讓隔天的排程以為它還在跑。
    盤中程式的上限從它啟動時算：凌晨開機補跑起來的那一個要常駐到 13:20，不能在盤中被砍掉
    （`_INTRADAY_TIME_LIMIT`）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paths  # noqa: E402
import strategy  # noqa: E402

_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"

# 盤中程式 08:40 啟動（規格「兩支程式」一節）：開盤 08:45 之前登入、訂閱好，累計筆數從開盤前
# 就記（ticket 03）。出場程式 13:25：13:20 進場截止之後、券商 13:30 當沖強平之前（ADR-0002）。
INTRADAY_START = time(8, 40)
EXIT_START = time(13, 25)


@dataclass(frozen=True)
class Task:
    name: str
    program: str                    # `scheduled.py` 的參數
    start: time
    description: str
    time_limit: timedelta
    restart_until: time | None = None   # 從 start 起每分鐘叫醒一次，直到這個時刻
    needs_network: bool = False


# 盤中程式的執行上限，從**這一個**行程啟動時算起，不是從 08:40。「錯過就補跑」會在開機當下把它
# 叫起來：週末整個關機、週一 00:30 開機，它就從 00:30 常駐到 13:20 收工，將近 13 小時（前一天
# 晚上補跑起來的那一個，日期還是前一天，一啟動就過了 13:20，馬上收工）。上限比這短，工作排程器
# 就會在盤中砍掉它，等於排程自己製造一次當機。也不能長過 19 小時 20 分：13:20 才起來又卡住的
# 那一個，要在隔天 08:40 之前被砍掉，「已經在跑就不啟動新的」才不會讓隔天整天都叫不醒。
_INTRADAY_TIME_LIMIT = timedelta(hours=14)

TASKS = (
    Task(name="PTS 盤中程式", program="intraday", start=INTRADAY_START,
         restart_until=strategy.ENTRY_CUTOFF, needs_network=True,
         time_limit=_INTRADAY_TIME_LIMIT,
         description=(f"PTS 盤中程式。每天 {INTRADAY_START:%H:%M} 起每分鐘叫醒一次，到"
                      f" {strategy.ENTRY_CUTOFF:%H:%M} 進場截止為止；已經在跑就不啟動新的，"
                      "當掉之後一分鐘內就會回來。是不是交易日由程式自己判斷。"
                      "由 tools/schedule.py 建立。")),
    Task(name="PTS 出場程式", program="exit", start=EXIT_START,
         time_limit=timedelta(hours=1),
         description=(f"PTS 出場程式。每天 {EXIT_START:%H:%M} 一次：平掉微台、檢查盤中程式今天"
                      "有沒有跑。刻意不設重跑（告警之後再跑一次會把你自己平掉的口數再平一次）。"
                      "由 tools/schedule.py 建立。")),
)


def main(argv: list[str] | None = None, *,
         schtasks: Callable[[list[str]], int] | None = None,
         python: str = sys.executable,
         now: Callable[[], datetime] = datetime.now,
         user: str | None = None) -> int:
    """`schtasks` 執行一次 `schtasks.exe`、回傳結束代碼（測試換掉它）；`python` 是排程要用的
    直譯器（預設是執行這支工具的那一個）；`user` 是排程掛在誰底下（預設是目前登入的使用者）。"""
    argv = sys.argv[1:] if argv is None else argv
    schtasks = schtasks or _schtasks
    if argv == ["install"]:
        return _install(schtasks, python=python, now=now(), user=user or _current_user())
    if argv == ["show"]:
        return _show(schtasks)
    if argv == ["remove"]:
        return _remove(schtasks)
    print("\n\n".join(__doc__.split("\n\n")[1:3]))     # 用法
    return 2


def _install(schtasks: Callable[[list[str]], int], *, python: str, now: datetime,
             user: str) -> int:
    pythonw = os.path.join(os.path.dirname(os.path.abspath(python)), "pythonw.exe")
    if not os.path.isfile(pythonw):
        print(f"找不到 {pythonw}。請用虛擬環境的 python 執行這支工具（見 docs/DEPLOY.md）。")
        return 1
    print(f"排程要用的 Python：{pythonw}\n專案資料夾：{paths.app_root()}\n")
    failed = []
    for task in TASKS:
        if _register(schtasks, task, task_xml(task, pythonw=pythonw, now=now, user=user)) != 0:
            failed.append(task.name)
    if failed:
        print(f"\n建立失敗：{'、'.join(failed)}。上面是 schtasks 的訊息。")
        return 1
    print("\n兩個排程都建好了。")
    return _show(schtasks)


def _register(schtasks: Callable[[list[str]], int], task: Task, definition: str) -> int:
    """schtasks 只吃檔案形式的排程定義。暫存檔用完就刪。"""
    fd, path = tempfile.mkstemp(prefix="pts-task-", suffix=".xml")
    try:
        # 工作排程器匯出的就是 UTF-16；schtasks 對 UTF-8 的定義會回「XML 格式不正確」
        with os.fdopen(fd, "w", encoding="utf-16") as fh:
            fh.write(definition)
        return schtasks(["/Create", "/TN", task.name, "/XML", path, "/F"])
    finally:
        os.unlink(path)


def _show(schtasks: Callable[[list[str]], int]) -> int:
    status = 0
    for task in TASKS:
        print(f"\n=== {task.name} ===", flush=True)
        if schtasks(["/Query", "/TN", task.name, "/V", "/FO", "LIST"]) != 0:
            status = 1
    return status


def _remove(schtasks: Callable[[list[str]], int]) -> int:
    status = 0
    for task in TASKS:
        if schtasks(["/Delete", "/TN", task.name, "/F"]) != 0:
            status = 1
    return status


def _schtasks(args: list[str]) -> int:
    """真的執行 `schtasks.exe`。輸出直接印在畫面上（它的編碼跟著系統，不經過 Python 解碼）。"""
    return subprocess.run(["schtasks.exe", *args]).returncode


def _current_user() -> str:
    domain, name = os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", "")
    return f"{domain}\\{name}" if domain else name


# ── 排程定義 ─────────────────────────────────────────────────────────────


def task_xml(task: Task, *, pythonw: str, now: datetime, user: str) -> str:
    """工作排程器的排程定義（Task Scheduler schema 1.2），元素順序照它自己匯出的樣子。"""
    ET.register_namespace("", _NAMESPACE)
    root = _el(None, "Task", version="1.2")
    _el(_el(root, "RegistrationInfo"), "Description", task.description)

    trigger = _el(_el(root, "Triggers"), "CalendarTrigger")
    if task.restart_until is not None:
        repetition = _el(trigger, "Repetition")
        _el(repetition, "Interval", "PT1M")
        _el(repetition, "Duration", _duration(_between(task.start, task.restart_until)))
        _el(repetition, "StopAtDurationEnd", "false")
    _el(trigger, "StartBoundary", f"{_first_day(task.start, now):%Y-%m-%d}T{task.start:%H:%M:%S}")
    _el(trigger, "Enabled", "true")
    _el(_el(trigger, "ScheduleByDay"), "DaysInterval", "1")

    principal = _el(_el(root, "Principals"), "Principal", id="Author")
    _el(principal, "UserId", user)
    _el(principal, "LogonType", "InteractiveToken")
    _el(principal, "RunLevel", "LeastPrivilege")

    settings = _el(root, "Settings")
    for tag, value in (
            ("MultipleInstancesPolicy", "IgnoreNew"),
            ("DisallowStartIfOnBatteries", "false"),
            ("StopIfGoingOnBatteries", "false"),
            ("AllowHardTerminate", "true"),
            ("StartWhenAvailable", "true"),
            ("RunOnlyIfNetworkAvailable", "true" if task.needs_network else "false")):
        _el(settings, tag, value)
    idle = _el(settings, "IdleSettings")
    _el(idle, "StopOnIdleEnd", "false")
    _el(idle, "RestartOnIdle", "false")
    for tag, value in (
            ("AllowStartOnDemand", "true"),
            ("Enabled", "true"),
            ("Hidden", "false"),
            ("RunOnlyIfIdle", "false"),
            ("WakeToRun", "true"),
            ("ExecutionTimeLimit", _duration(task.time_limit)),
            ("Priority", "5")):     # 一般（4～6）；預設的 7 是「低於正常」
        _el(settings, tag, value)

    command = _el(_el(root, "Actions", Context="Author"), "Exec")
    _el(command, "Command", pythonw)
    _el(command, "Arguments", f'"{os.path.join(paths.app_root(), "scheduled.py")}" {task.program}')
    _el(command, "WorkingDirectory", paths.app_root())

    return ('<?xml version="1.0" encoding="UTF-16"?>\n'
            + ET.tostring(root, encoding="unicode"))


def _first_day(start: time, now: datetime) -> date:
    """第一次執行的日期：今天的那個時刻還沒到就是今天，否則明天。

    起點寫成已經過去的時刻的話，「錯過就補跑」可能一建好排程就把它當成錯過的那一次補跑——
    下午建排程，出場程式就立刻跑起來，講一句「盤中程式今天沒有跑」。
    """
    return now.date() if now.time() < start else now.date() + timedelta(days=1)


def _between(start: time, end: time) -> timedelta:
    return datetime.combine(datetime.min, end) - datetime.combine(datetime.min, start)


def _duration(span: timedelta) -> str:
    """ISO 8601 的時間長度，例如 PT4H40M。"""
    minutes = int(span.total_seconds()) // 60
    hours, minutes = divmod(minutes, 60)
    return "PT" + (f"{hours}H" if hours else "") + (f"{minutes}M" if minutes or not hours else "")


def _el(parent: ET.Element | None, tag: str, text: str | None = None, **attrib: str) -> ET.Element:
    name = f"{{{_NAMESPACE}}}{tag}"
    node = ET.Element(name, attrib) if parent is None else ET.SubElement(parent, name, attrib)
    if text is not None:
        node.text = text
    return node


if __name__ == "__main__":
    raise SystemExit(main())
