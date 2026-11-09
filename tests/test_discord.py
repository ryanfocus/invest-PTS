"""Discord 送出 —— 從 invest-os 的 `notifiers/discord.py` 搬來的 `send` 與 webhook 遮罩。

Discord 掛掉不該讓策略本身失敗（訊息只是傳遞），但也不能靜默：每條失敗路徑都回 False、
記一行 log。**那一行 log 裡不可以有 webhook**：它的最後一段就是發訊權限的 token，
而 log 正是出事時使用者會傳出去的檔案（invest-os 2026-09-02 查驗發現）。

`requests.post` 是這裡唯一換掉的東西：測試絕不發真實的 Discord。
"""

import logging

import pytest
import requests

from notifiers.discord import send

URL = "https://discord.com/api/webhooks/1234567890/TOKEN-abcdef"


class _Resp:
    def __init__(self, ok: bool, status_code: int = 200):
        self.ok = ok
        self.status_code = status_code


@pytest.fixture
def posts(monkeypatch):
    """記下每一次 `requests.post`，回成功。"""
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return _Resp(ok=True)

    monkeypatch.setattr(requests, "post", fake_post)
    return calls


def test_the_text_is_posted_as_the_message_content_with_a_timeout(posts):
    assert send("PTS 測試", URL) is True
    assert posts == [{"url": URL, "json": {"content": "PTS 測試"}, "timeout": posts[0]["timeout"]}]
    assert posts[0]["timeout"] is not None, "沒有 timeout 的話，Discord 掛住會拖住整個盤中程式"


def test_without_a_webhook_no_request_is_made(posts):
    assert send("PTS 測試", "") is False
    assert posts == []


def test_an_http_error_is_reported_as_not_sent(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(ok=False, status_code=500))
    assert send("PTS 測試", URL) is False


def test_a_network_failure_is_not_sent_and_the_log_does_not_contain_the_webhook(
        monkeypatch, caplog):
    """`requests` 的例外字串含完整的請求路徑。主機名稱與失敗原因要留著（排查的唯一線索），
    webhook 的 id 與 token 要遮掉。"""
    def boom(*a, **k):
        raise requests.ConnectionError(
            "HTTPSConnectionPool(host='discord.com', port=443): Max retries exceeded with "
            "url: /api/webhooks/1234567890/TOKEN-abcdef (Caused by NameResolutionError)")

    monkeypatch.setattr(requests, "post", boom)
    with caplog.at_level(logging.DEBUG):
        assert send("PTS 測試", URL) is False
    assert "TOKEN-abcdef" not in caplog.text
    assert "1234567890" not in caplog.text
    assert "NameResolutionError" in caplog.text
    assert "discord.com" in caplog.text


@pytest.mark.parametrize("webhook", ["not-a-url", "https://x/"])
def test_an_odd_webhook_does_not_break_the_failure_path(monkeypatch, webhook):
    """這條路本來就是失敗路徑，在上面再炸一次，「發不出訊息」就變成盤中程式中止。"""
    def boom(*a, **k):
        raise requests.ConnectionError("something went wrong")

    monkeypatch.setattr(requests, "post", boom)
    assert send("PTS 測試", webhook) is False
