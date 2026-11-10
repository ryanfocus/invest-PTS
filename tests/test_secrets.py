"""帳號、密碼、期貨帳號、Discord webhook：從不進版控的設定檔讀取（規格第 46 條）。

⚠️ 這裡的測試**只讀暫存資料夾裡的假檔案**，從不碰專案的 `.env`。
"""

import os
import subprocess

import pytest

import settings
from conftest import REPO_ROOT
from day_script import make_config

# 自動下單打開：期貨帳號也要
LIVE = make_config(auto_order_enabled=True)

FULL = (
    "# 註解\n"
    "\n"
    "CAPITAL_USER_ID=A100000000\n"
    "CAPITAL_PASSWORD=not-a-real-password\n"
    "CAPITAL_FUTURES_ACCOUNT=F0000000000000\n"
    "DISCORD_WEBHOOK_URL=https://discord.example/api/webhooks/1/token\n"
)


def _env(tmp_path, text: str) -> str:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_all_four_values_are_read(tmp_path):
    secrets = settings.load_secrets(LIVE, path=_env(tmp_path, FULL))
    assert secrets.user_id == "A100000000"
    assert secrets.password == "not-a-real-password"
    assert secrets.futures_account == "F0000000000000"
    assert secrets.discord_webhook == "https://discord.example/api/webhooks/1/token"


def test_every_missing_value_is_named_at_once(tmp_path):
    """一次列完，不是修好一個才發現還有下一個。"""
    with pytest.raises(settings.SettingsError) as exc:
        settings.load_secrets(LIVE, path=_env(tmp_path, "CAPITAL_USER_ID=A100000000\n"))
    for name in ("CAPITAL_PASSWORD", "CAPITAL_FUTURES_ACCOUNT", "DISCORD_WEBHOOK_URL"):
        assert name in str(exc.value)
    assert "CAPITAL_USER_ID" not in str(exc.value)


def test_an_empty_value_counts_as_missing(tmp_path):
    """`.env.example` 複製過來沒填，每一行都是 `NAME=`。"""
    with pytest.raises(settings.SettingsError, match="CAPITAL_PASSWORD"):
        settings.load_secrets(LIVE, path=_env(tmp_path, FULL.replace(
            "CAPITAL_PASSWORD=not-a-real-password", "CAPITAL_PASSWORD=")))


def test_simulation_mode_does_not_need_a_futures_account(tmp_path):
    """模擬模式完全不碰下單 API，沒有期貨帳號也能跑——上線前就是這樣驗證的。"""
    text = FULL.replace("CAPITAL_FUTURES_ACCOUNT=F0000000000000\n", "")
    secrets = settings.load_secrets(make_config(), path=_env(tmp_path, text))
    assert secrets.futures_account == ""


def test_no_webhook_is_needed_when_discord_is_off(tmp_path):
    text = FULL.replace("DISCORD_WEBHOOK_URL=https://discord.example/api/webhooks/1/token\n", "")
    secrets = settings.load_secrets(make_config(auto_order_enabled=True, discord_enabled=False), path=_env(tmp_path, text))
    assert secrets.discord_webhook == ""


def test_the_read_only_tool_only_needs_the_login(tmp_path):
    """唯讀檢查工具不下單、不發通知，所以只要登入用的兩個值。"""
    text = "CAPITAL_USER_ID=A100000000\nCAPITAL_PASSWORD=pw\n"
    secrets = settings.load_secrets(None, path=_env(tmp_path, text))
    assert (secrets.user_id, secrets.password) == ("A100000000", "pw")


def test_a_missing_file_says_how_to_create_it(tmp_path):
    with pytest.raises(settings.SettingsError, match=r"\.env\.example"):
        settings.load_secrets(LIVE, path=str(tmp_path / ".env"))


def test_a_name_that_is_only_a_prefix_is_not_matched(tmp_path):
    """讀 CAPITAL_USER_ID 不可以撈到 CAPITAL_USER_ID_OLD 的值。"""
    text = "CAPITAL_USER_ID_OLD=wrong\n" + FULL
    assert settings.load_secrets(LIVE, path=_env(tmp_path, text)).user_id == "A100000000"


def test_the_password_and_webhook_never_show_up_in_a_printout(tmp_path):
    """設定物件常被印進 log 或錯誤訊息。webhook 的 token 等於發訊權限，
    密碼等於下單權限——invest-os 曾經把 webhook token 寫進日誌（2026-09）。"""
    secrets = settings.load_secrets(LIVE, path=_env(tmp_path, FULL))
    shown = repr(secrets) + str(secrets)
    assert "not-a-real-password" not in shown
    assert "token" not in shown


# --- repo 的狀態：帳密檔不進版控、範本與程式不漂移 ---


def test_the_secrets_file_is_ignored_by_git():
    result = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=REPO_ROOT)
    assert result.returncode == 0, ".env 沒有被 .gitignore 排除"


def test_the_example_file_lists_exactly_the_names_the_program_reads():
    """`.env.example` 是使用者建立 `.env` 的唯一指引。少一個，他要到啟動失敗才知道；
    多一個，他會以為那個值有作用。"""
    with open(os.path.join(REPO_ROOT, ".env.example"), encoding="utf-8") as fh:
        names = {line.split("=", 1)[0].strip() for line in fh
                 if "=" in line and not line.lstrip().startswith("#")}
    assert names == set(settings.SECRET_NAMES)
