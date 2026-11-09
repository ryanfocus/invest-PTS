"""心跳記錄：盤中程式今天有沒有跑、跑到幾點（規格第 54 條）。

盤中程式啟動後每分鐘寫一次，13:25 的出場程式讀它。平常沒有例行訊息，盤中程式沒跑
（排程壞了、當掉之後沒被重啟）的話，使用者不會發現——出場程式是當天唯一一定會被叫醒、
又能發 Discord 的東西，所以由它來看。兩支程式之間只靠部位檔與心跳記錄溝通。

## 格式（`state/heartbeat.json`，只有一個檔，每次整個覆寫）

    {
      "trading_day": "2026-11-09",              交易日
      "last_alive": "2026-11-09T10:00:00",      最後存活時刻：盤中程式最後一次寫心跳時，
                                                收到的本機時鐘事件。還沒收到任何事件是 null
      "finished": false                         盤中程式自己走到了結局（13:20 收工、臨時休市、
                                                部位檔讀不懂而不進場）。false 而且出場程式
                                                已經在跑，就是半路停了（當掉、被關掉）
    }

寫入方式同部位檔：先寫暫存檔再置換，讀不懂就拋 `Unreadable`——讀不懂不能當成「有跑」。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime

import paths


class Unreadable(Exception):
    """心跳記錄存在但讀不懂。不可以當成「今天有跑」，也不能當成「沒跑」：不知道就要講。"""


@dataclass(frozen=True)
class Heartbeat:
    trading_day: date
    last_alive: datetime | None
    finished: bool


def path() -> str:
    """心跳記錄。和部位檔放在一起，不在 `logs/`：30 天清除的範圍外。"""
    return os.path.join(paths.state_dir(), "heartbeat.json")


def read() -> Heartbeat | None:
    """心跳記錄；從來沒寫過（或被刪掉）是 None。記的是別天的照樣回傳，由呼叫端比對交易日。"""
    target = path()
    if not os.path.exists(target):
        return None
    try:
        with open(target, encoding="utf-8") as fh:
            return _from_json(json.load(fh))
    except (ValueError, OSError) as exc:     # JSON 與 UTF-8 的解碼錯誤也是 ValueError
        raise Unreadable(f"{target}：{exc}") from exc


def write(beat: Heartbeat) -> None:
    """原子寫入。暫存檔建在同一個目錄（跨磁碟的 `os.replace` 不是原子的）。"""
    target = path()
    directory = os.path.dirname(target)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".heartbeat-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(_to_json(beat), fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


_FIELDS = ("trading_day", "last_alive", "finished")
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_ISO_MOMENT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _to_json(beat: Heartbeat) -> dict:
    return {
        "trading_day": beat.trading_day.isoformat(),
        "last_alive": None if beat.last_alive is None
        else beat.last_alive.isoformat(timespec="seconds"),
        "finished": beat.finished,
    }


def _from_json(data) -> Heartbeat:
    if not isinstance(data, dict) or set(data) != set(_FIELDS):
        raise ValueError(f"心跳記錄應該正好有 {'、'.join(_FIELDS)} 這幾個欄位，目前是 {data!r}")
    day, alive, finished = data["trading_day"], data["last_alive"], data["finished"]
    if not isinstance(day, str) or not _ISO_DAY.fullmatch(day):
        raise ValueError(f"trading_day 應該是 YYYY-MM-DD 格式的日期，目前是 {day!r}")
    if alive is not None and (not isinstance(alive, str) or not _ISO_MOMENT.fullmatch(alive)):
        raise ValueError(f"last_alive 應該是 YYYY-MM-DDTHH:MM:SS 格式的時刻，目前是 {alive!r}")
    if not isinstance(finished, bool):
        raise ValueError(f"finished 應該是 true 或 false，目前是 {finished!r}")
    if alive is None and not finished:
        # 盤中程式在第一個時鐘事件才寫心跳；沒有存活時刻的只有「還沒登入就結束」那一種
        raise ValueError("last_alive 是 null，finished 卻是 false：盤中程式不會寫出這樣的記錄")
    return Heartbeat(trading_day=date.fromisoformat(day),
                     last_alive=None if alive is None else datetime.fromisoformat(alive),
                     finished=finished)
