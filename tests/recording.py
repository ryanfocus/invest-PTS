"""通知的替身：記下被要求送出的每一則訊息（規格 Testing Decisions：通知是兩個接縫之一）。

測試斷言的是**內容**與**有沒有送**，不是送出的機制。
"""

from __future__ import annotations


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def __call__(self, text: str) -> bool:
        self.sent.append(text)
        return True
