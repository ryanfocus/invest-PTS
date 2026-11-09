"""Discord webhook 送出。從 invest-os 的 `notifiers/discord.py` 搬來 `send` 與 `redact_webhook`。

訊息內容不在這裡（`messages` 是純函式）；Discord 開關也不在這裡（盤中流程把關，
關著就根本不呼叫）。這裡只負責「送出去，失敗了講得出原因，而且不洩漏 webhook」。
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

_REDACTED = "<webhook 已遮蔽>"


def redact_webhook(text: str, webhook_url: str) -> str:
    """把 webhook 從文字裡拿掉，但**留下主機名稱與失敗原因**。

    `requests` 的例外字串含完整的請求路徑，而 Discord webhook 的最後一段就是 token：

        HTTPSConnectionPool(host='discord.com', port=443):
        Max retries exceeded with url: /api/webhooks/<id>/<token> (Caused by ...)

    任何 DNS 失敗、逾時、斷線都會走到那裡，而那行字會進當天的 log——正是出事時
    使用者會傳出來的檔案（invest-os 2026-09-02 查驗發現）。

    ⚠️ 只遮網址，不遮原因。「為什麼發不出去」是排查唯一的線索。
    """
    if not webhook_url:
        return text
    out = text.replace(webhook_url, _REDACTED)
    # 例外訊息裡通常只有路徑那一段（不含 scheme 與主機），所以也要換掉它
    path = webhook_url.split("://", 1)[-1]
    if "/" in path:
        path = path[path.index("/"):]
        if len(path) > 1:
            out = out.replace(path, _REDACTED)
    return out


def send(text: str, webhook_url: str, timeout: tuple[float, float] = (5, 10)) -> bool:
    """送出一則訊息。回傳是否成功。

    失敗只記 log、不拋例外：Discord 掛掉不該讓盤中程式中止——判斷與記錄行情比通知重要。
    有 timeout（連線、讀取各幾秒）：沒有的話，Discord 掛住就拖住整個行情迴圈。
    """
    if not webhook_url:
        logger.warning("Discord webhook 沒有設定，這則訊息沒有送出")
        return False
    try:
        resp = requests.post(webhook_url, json={"content": text}, timeout=timeout)
    except requests.RequestException as exc:
        # ⚠️ 一定要遮：例外字串含完整的請求路徑，見 `redact_webhook`
        logger.error("Discord 發送失敗：%s", redact_webhook(str(exc), webhook_url))
        return False
    if resp.ok:
        logger.info("Discord 已發送")
        return True
    logger.error("Discord 發送失敗：HTTP %s", resp.status_code)
    return False
