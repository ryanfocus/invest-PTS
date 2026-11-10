"""實機唯讀檢查工具 `tools/show_contracts.py`。

它會真的登入群益，所以測試**絕不執行它的 main**。測的是兩件不需要連線的事：
啟動時的自我檢查（程式碼裡沒有任何送單函式），以及印出來的內容。
"""

import os
import shutil
import subprocess
import sys

import pytest

from day_script import contracts
from conftest import REPO_ROOT

TOOL = os.path.join(REPO_ROOT, "tools", "show_contracts.py")
sys.path.insert(0, os.path.dirname(TOOL))

import show_contracts  # noqa: E402


def test_the_tool_itself_contains_no_order_function():
    with open(TOOL, encoding="utf-8") as fh:
        assert show_contracts.order_functions_in(fh.read()) == []


@pytest.mark.parametrize("source", [
    "broker.place_order(request)",
    "message, code = order.SendFutureOrderCLR(user, False, o)",
    "from broker.capital import place_order as p",
    "def SendFutureOrder():\n    pass",
    "getattr(broker, 'place_order')(request)",
    "lib.SendOverseaFutureOrder(x)",
])
def test_the_self_check_finds_any_way_of_reaching_an_order_function(source):
    assert show_contracts.order_functions_in(source) != []


def test_mentioning_an_order_function_in_prose_is_not_calling_it():
    """說明文字裡可以講「這支工具不呼叫 place_order」，那不是程式碼。"""
    source = '"""這支工具不呼叫 place_order。"""\n# SendFutureOrderCLR 也不會\nx = 1\n'
    assert show_contracts.order_functions_in(source) == []


def test_a_tampered_tool_refuses_to_start_before_doing_anything_else(tmp_path):
    """有人在工具裡加了一行送單，它要在**登入之前**就拒絕執行。

    跑的是一份放在空資料夾裡的副本：那裡沒有專案的其他模組、也沒有帳密檔，
    就算自我檢查沒擋下來，它也連不到券商——只會死在 import，錯誤訊息就不一樣。
    """
    copy = tmp_path / "tools" / "show_contracts.py"
    copy.parent.mkdir()
    shutil.copyfile(TOOL, copy)
    with open(copy, "a", encoding="utf-8") as fh:
        fh.write("\n\ndef _sneaky(broker, request):\n    return broker.place_order(request)\n")

    # -I：不吃環境變數與使用者的 site-packages；-X utf8：輸出固定用 UTF-8
    result = subprocess.run([sys.executable, "-I", "-X", "utf8", str(copy)], cwd=tmp_path,
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode != 0
    assert "place_order" in result.stdout + result.stderr
    assert "ModuleNotFoundError" not in result.stderr, "自我檢查必須是第一件事"


def test_the_printout_shows_each_products_order_code_and_last_trading_day():
    lines = show_contracts.format_contracts(contracts()).splitlines()
    for name, code in (("大台", "TX11"), ("小台", "MTX11"), ("微台", "TM2611")):
        line = next(ln for ln in lines if name in ln)
        assert code in line and "2026/11/18" in line
