"""這個程式住在哪裡。

設定檔、不進版控的帳密檔、log 的位置全部從這裡推導。**只有這一個地方回答這個問題。**

沿用 invest-os 的教訓（2026-08-31）：`os.path.dirname(__file__)` 原本散在四個檔案，
打包成 exe 之後答案變了，而四份就是四個各自會漏掉的地方——漏掉的那個不會報錯，
只會讀不到設定，或把部位寫進一個開機就消失的資料夾。

其他模組要的是路徑，就呼叫這裡的函式；**不要在模組載入時把結果存成常數**，
否則測試導開路徑時，已經被複製出去的那份不會跟著改。
"""

from __future__ import annotations

import os
import sys


def app_root() -> str:
    """程式的根目錄。設定與 log 都掛在它底下。

    ⚠️ 判斷依據是 `sys.frozen`，不是檔名。改成看「檔名是不是 .exe」的話，
       用 `python.exe` 跑原始碼也會被當成打包版，根目錄會指到 Python 的安裝資料夾。
    """
    if getattr(sys, "frozen", False):
        # 打包版：使用者的檔案放在 exe 旁邊，不在 exe 裡面
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def config_path() -> str:
    """使用者可調的設定檔（進版控）。"""
    return os.path.join(app_root(), "config", "settings.yaml")


def env_path() -> str:
    """帳號、密碼、期貨帳號、webhook（**不進版控**，見 .gitignore）。"""
    return os.path.join(app_root(), ".env")


def logs_dir() -> str:
    """執行紀錄與券商回覆的原始存檔。含未遮罩的期貨帳號，30 天後清除（ticket 10）。"""
    return os.path.join(app_root(), "logs")


def capital_log_dir() -> str:
    """群益元件自己寫的 log（含身分證字號）。

    放在 `logs/` 底下，所以和其他 log 一起定期清除（規格第 60 條）。
    不設定的話元件會寫進「執行檔所在目錄」的 `CapitalLog/`——用 venv 跑時
    那是 Python 的安裝目錄，invest-os 從 2026-08 起累積在那裡、從未被清過。

    ⚠️ 元件可能在這個目錄底下再建子資料夾（預設的 `CapitalLog` 就是），
       所以清除時要往下找，不能只掃最上層（ticket 10）。
    """
    return os.path.join(logs_dir(), "capital")
