import os
import sys

import pytest

# 讓測試能 import 專案根目錄的模組，不必在每個測試檔重複 sys.path 手續
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import paths  # noqa: E402


@pytest.fixture(autouse=True)
def logs_never_touch_the_real_directory(tmp_path_factory, monkeypatch):
    """**測試絕不碰正式的 `logs/`。**

    那個目錄放的是真實交易的鑑識記錄：券商的原始回覆、群益元件自己寫的 log。
    invest-os 2026-08-23 發現測試在那裡累積了 46 個假的回覆檔，和真的混在一起。

    ⚠️ 不放在 `tmp_path` 底下：有測試會斷言 `tmp_path` 裡有哪些東西，
       多一個 `logs/` 會讓它們無緣無故變紅。
    """
    logs = tmp_path_factory.mktemp("logs")
    monkeypatch.setattr(paths, "logs_dir", lambda: str(logs))
    return logs
