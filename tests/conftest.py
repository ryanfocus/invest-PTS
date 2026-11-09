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


@pytest.fixture(autouse=True)
def market_dir(tmp_path_factory, monkeypatch):
    """**測試絕不碰正式的原始行情目錄。** 那裡是長期保留的真實行情——ticket 03 靠它
    回答筆差的事實問題，作者日後也要拿它回測。混進一份假的劇本，就污染了那一天。"""
    directory = tmp_path_factory.mktemp("marketdata")
    monkeypatch.setattr(paths, "market_data_dir", lambda: str(directory))
    return directory


@pytest.fixture(autouse=True)
def state_dir(tmp_path_factory, monkeypatch):
    """**測試絕不碰正式的部位檔。** 出場程式 13:25 照著它平倉；混進一份假劇本寫的部位，
    它就會去平一個不存在的部位——那一筆「平倉」單是開出來的反向新倉。"""
    directory = tmp_path_factory.mktemp("state")
    monkeypatch.setattr(paths, "state_dir", lambda: str(directory))
    return directory


class _RealComForbidden:
    def __init__(self, *args, **kwargs):
        raise AssertionError(
            "測試試圖建立真的群益 COM 元件。測試絕不連真實券商——"
            "請用 CapitalBroker(com=FakeSkcom(...)) 或 FakeBroker。"
        )


@pytest.fixture(autouse=True)
def never_touch_the_real_broker(monkeypatch):
    """**測試絕不連真實券商。** 這台機器裝著群益元件與憑證，忘了換掉 COM 的測試
    會真的登入——而 `CapitalBroker` 是唯一會送出真單的程式。在這裡把那條路堵死，
    忘了的那一天會是一個看得懂的失敗，而不是一次真的登入。"""
    import broker.capital

    monkeypatch.setattr(broker.capital, "_RealCom", _RealComForbidden)
