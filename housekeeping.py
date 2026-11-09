"""收尾：`logs/` 保留 30 天（規格第 60 條）。

## 為什麼這件事比「省空間」重要

`logs/` 裡有三種來源的東西，只有一種的內容是我們決定的：

| 來源 | 例子 | 敏感資訊 |
|------|------|---------|
| 盤中程式、出場程式的執行紀錄 | `intraday-20261109.log` | 無 |
| 券商回覆的原始存檔 | `replies-20261109-134301-2315610000001.txt` | **未遮罩的期貨帳號** |
| 群益元件自己寫的 log（`logs/capital/`） | `20261109_Center.log` | **身分證字號** |

所以清理的目的是**限制個資留在磁碟上的時間**。invest-os 沒把群益元件的 log 導進來、
清理又只掃一層，那些檔從 2026-08 起累積在 Python 的安裝目錄，從沒被清過。

## 範圍

**整個 `logs/` 往下找**：群益元件可能在 `logs/capital/` 底下再建一層 `CapitalLog`
（文件沒講，見 `paths.capital_log_dir`），只掃一層就清不到。

**只碰 `logs/`。** 原始行情記錄（`marketdata/`，長期保留，規格第 58 條）、部位檔與心跳記錄
（`state/`，兩支程式之間的溝通管道）都是 `logs/` 的兄弟目錄，不在底下——`paths` 刻意這樣放。

## 兩條原則（沿用 invest-os）

**只刪檔案，不刪資料夾。** 群益元件建的資料夾不是我們的；空資料夾留著沒有代價。

**絕不拋例外。** 這是收尾動作，跑在出場程式做完正事之後；為它讓出場程式以錯誤收場不成比例。
Windows 上檔案被別的行程開著就刪不掉（群益元件正是會這樣），略過、下次再清。
"""

from __future__ import annotations

import logging
import os
import time

import paths

logger = logging.getLogger(__name__)

# 保留天數。超過一個月的個資留著沒有用途；出事回頭查也很少查超過一個月。
# 邊界留著不刪：「保留 30 天」的自然理解是「30 天內的都在」。
LOG_RETENTION_DAYS = 30

_SECONDS_PER_DAY = 86400


def purge_old_logs(*, retention_days: int = LOG_RETENTION_DAYS, now: float | None = None) -> int:
    """刪掉 `logs/` 底下（含子資料夾）最後修改超過保留期限的檔案，回傳刪了幾個。永遠不拋例外。"""
    directory = paths.logs_dir()
    cutoff = (time.time() if now is None else now) - retention_days * _SECONDS_PER_DAY
    removed = 0
    for folder, _, names in os.walk(directory):
        for name in names:
            path = os.path.join(folder, name)
            try:
                if os.lstat(path).st_mtime < cutoff:
                    os.remove(path)
                    removed += 1
            except Exception as exc:  # noqa: BLE001
                logger.info("清理 log：%s 刪不掉（%s），略過", path, type(exc).__name__)
    if removed:
        logger.info("清理 log：刪除 %d 個超過 %d 天的檔案", removed, retention_days)
    return removed
