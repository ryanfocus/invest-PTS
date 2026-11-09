"""守住**測試執行不到、或測試環境看不出來**的東西。"""

import io
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


def _tracked(pattern: str):
    """版控裡的檔案。「要守哪些檔案」與「哪些檔案進版控」是同一件事，不另外維護清單。"""
    out = subprocess.run(["git", "ls-files", pattern], cwd=ROOT,
                         capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        pytest.skip("這裡不是 git 工作區，無法決定哪些檔案該守")
    return [ROOT / name for name in out.stdout.splitlines() if (ROOT / name).is_file()]


# --- 規格第 59 條：沒有群益元件、沒有帳號的機器上也能跑 ---


def test_every_module_imports_on_a_machine_without_the_capital_component(tmp_path):
    """群益元件只裝得起來在 Windows 上，而 broker 以外的程式碼都不需要它。

    在 `comtypes`、`pythoncom` 一 import 就爆的環境裡，開一個子行程把每個模組都載入。
    刻意不用 `importorskip` 或 monkeypatch——那兩種寫法在沒裝套件的機器上會直接跳過，
    而那正是這條要守的機器。
    """
    for name in ("comtypes", "pythoncom", "win32com"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "__init__.py").write_text(
            f"raise ImportError('模擬沒有安裝 {name}')", encoding="utf-8")
    (tmp_path / "comtypes" / "client.py").write_text(
        "raise ImportError('模擬沒有安裝 comtypes')", encoding="utf-8")

    modules = _production_modules()
    assert "broker.capital" in modules and "intraday" in modules, modules
    code = "import " + ", ".join(modules)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(tmp_path), str(ROOT)])}
    result = subprocess.run([sys.executable, "-c", code], env=env, cwd=tmp_path,
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=60)
    assert result.returncode == 0, (
        f"有模組在載入時就需要群益元件——COM 的初始化必須延遲到函式內。\n{result.stderr}")


def _production_modules() -> list[str]:
    """版控裡的正式模組（`tests/`、`tools/` 以外的 .py），寫成 import 用的名字。

    從版控推導，不手寫清單：新模組在載入時就碰 COM、又忘了加進清單的話，這條照樣綠燈，
    而它要守的正是沒有群益元件的機器。"""
    names = []
    for path in _tracked("*.py"):
        rel = path.relative_to(ROOT).with_suffix("")
        if rel.parts[0] in ("tests", "tools"):
            continue
        parts = rel.parts[:-1] if rel.name == "__init__" else rel.parts
        names.append(".".join(parts))
    return sorted(names)


# --- 測試跑不到的程式碼 ---


def test_no_code_refers_to_a_name_that_does_not_exist():
    """`broker/capital.py` 的 `_RealCom` 只在真的連上群益時才會跑，測試永遠執行不到。
    用到不存在的名字 = 那一行一跑就 NameError，而那時使用者在上班。
    （invest-os 2026-08-23：搬檔時漏掉一個 import，全部測試綠燈。）"""
    api = pytest.importorskip("pyflakes.api", reason="pyflakes 沒裝，見 requirements.txt")
    from pyflakes import messages
    from pyflakes.reporter import Reporter

    crashers = (messages.UndefinedName, messages.UndefinedLocal, messages.UndefinedExport)
    found: list[str] = []

    class Collect(Reporter):
        def __init__(self):
            super().__init__(io.StringIO(), io.StringIO())

        def flake(self, message):
            if isinstance(message, crashers):
                found.append(str(message))

        def syntaxError(self, filename, msg, lineno, offset, text):
            found.append(f"{filename}:{lineno}: 語法錯誤 {msg}")

    for path in _tracked("*.py"):
        api.checkPath(str(path), Collect())
    assert found == [], "以下的名字不存在，執行到那一行就會 NameError：\n" + "\n".join(found)


def test_only_one_module_asks_where_the_program_lives():
    """**`__file__` 只准出現在 `paths.py`。** invest-os 原本散在四個檔案，打包之後答案變了，
    漏掉的那個不會報錯，只會讀不到設定，或把檔案寫進一個開機就消失的資料夾。
    `tools/` 與 `tests/` 是開發用的，不在此限。"""
    offenders = [
        path.relative_to(ROOT).as_posix() for path in _tracked("*.py")
        if not path.relative_to(ROOT).as_posix().startswith(("tools/", "tests/"))
        and path.name != "paths.py"
        and "__file__" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"這些檔案自己去問「程式在哪」：{offenders}。改用 paths 的函式。"


# --- 進版控的真實樣本不可以帶帳戶識別資訊 ---


def test_every_reply_sample_has_its_account_fields_masked():
    """`fixtures/` 收的是券商回來的真實字串——那正是它們的價值。代價是一不小心就會把
    帳戶識別資訊一起帶進版控。invest-os 2026-09-04 的稽核發現某份樣本的檔頭寫著
    「已置換」，實際上沒有；三次都不是不知道要遮，是沒有東西在檢查。

    `[4]` 期貨商代號、`[5]` 帳號、`[26]` 子帳號、`[27]` 營業員代號，在每一列都只能是
    0／F 組成的假值。委託序號、書號、成交價、時間要留著：比對靠的就是它們。
    """
    rows = 0
    for path in sorted(FIXTURES.glob("onnewdata-*.txt")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            fields = line.split(",")
            for i in (4, 5, 26, 27):
                value = fields[i].strip()
                assert set(value) <= {"0", "F"}, f"{path.name} 的欄位 [{i}] 不是假值：{value!r}"
            rows += 1
    assert rows >= 6, "樣本不見了，這條檢查等於沒有在跑"


def test_every_report_sample_has_its_account_fields_masked():
    """委託查詢與成交查詢的真實回傳（出場單不確定時的兩段查詢用）同樣帶著帳戶識別資訊，
    位置和推播不同：委託查詢 `[3]` 期貨商代號、`[5]` `[6]` 帳號、`[39]` 營業員代號；成交查詢
    `[3]` `[5]` `[6]`、`[32]` 營業員代號。委託書號、序號、成交價、時間要留著。
    兩種查詢用欄位數分：委託查詢一列 77 欄、成交查詢 52 欄。"""
    rows = 0
    for path in sorted(FIXTURES.glob("reports-*.txt")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.startswith("TF,"):
                continue
            fields = line.split(",")
            masked = (3, 5, 6, 39) if len(fields) > 55 else (3, 5, 6, 32)
            for i in masked:
                value = fields[i].strip()
                assert set(value) <= {"0", "F"}, f"{path.name} 的欄位 [{i}] 不是假值：{value!r}"
            rows += 1
    assert rows >= 10, "樣本不見了，這條檢查等於沒有在跑"


# --- 策略常數只出現在策略判斷一處（規格「策略判斷」一節）---


def test_strategy_times_and_thresholds_appear_only_in_the_strategy_module():
    """改常數等於換了一個策略。散在流程裡的那一份，改的時候一定會漏——漏掉的不會報錯，
    只會讓 13:20 收工和 13:20 進場截止變成兩個時間。

    只掃認得出來、不會誤傷的幾個：08:50、09:45、13:20 這三個時刻，以及筆差門檻 1000、
    RSI 門檻 70。30、5、100 在別處太常見（逾時秒數、保留天數、價格倍數），不掃。
    """
    import ast

    times = {(8, 50), (9, 45), (13, 20)}
    numbers = {1000, 70}
    offenders: list[str] = []
    for path in _tracked("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", "tools/")) or rel == "strategy.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "time"
                    and tuple(getattr(a, "value", None) for a in node.args[:2]) in times):
                offenders.append(f"{rel}:{node.lineno} time{tuple(a.value for a in node.args)}")
            elif (isinstance(node, ast.Constant) and type(node.value) is int
                    and node.value in numbers):
                offenders.append(f"{rel}:{node.lineno} {node.value}")
    assert offenders == [], f"策略常數出現在策略判斷以外：{offenders}"
