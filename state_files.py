"""`state/` 底下的小檔怎麼取名、怎麼寫、怎麼讀。

`state/`（`paths.state_dir`）放的是程式之間、程式與自己的下一次重啟之間留下的記錄。它刻意
不在 `logs/` 底下：30 天清除的範圍外，清掉任何一個都會讓程式做錯事。裡面有這些：

| 檔案 | 寫的 | 讀的 | 說明在 |
|------|------|------|--------|
| `position-YYYYMMDD.json` | 盤中程式（進場）、出場程式與收工指令（出場） | 三者 | `position_file` |
| `heartbeat.json` | 盤中程式 | 出場程式；盤中程式（今天收工了沒） | `heartbeat` |
| `entries-stopped-YYYYMMDD.txt` | 盤中程式、收工指令 | 盤中程式（啟動時與每個時鐘事件） | `intraday.stop_path` |
| `stop-seen-YYYYMMDD.txt` | 盤中程式（看到停止進場記錄） | 收工指令 | `intraday.stop_seen_path` |
| `login-failed-YYYYMMDD.txt` | 盤中程式 | 盤中程式（下一次被叫醒） | `intraday.login_failed_path` |
| `exit-done-YYYYMMDD.txt` | 出場程式 | 出場程式（之後每一次執行） | `exit_program.done_path` |
| `day-settings-YYYYMMDD.json` | 盤中程式（當天第一次啟動） | 盤中程式（重啟） | `day_settings` |
| `sim-position-YYYYMMDD.json` | 盤中程式（模擬模式） | 盤中程式（重啟）；出場程式**絕不讀** | `sim_position` |
| `intraday.lock` | 盤中程式 | 收工指令（盤中程式在不在跑） | `intraday._alone` |
| `stop_today.lock` | 收工指令、出場程式（讀部位檔之前等收工指令做完） | — | `exit_program.stop_today_lock_path` |

兩支程式之間只靠部位檔與心跳記錄溝通（規格「兩支程式」一節）。使用者盤中下的收工指令
（`tools/stop_today.py`）另外靠停止進場記錄與確認記錄和盤中程式說好「不再進場」；其他的都只有
寫它的那支程式讀。

兩種寫法，各有理由：

  * **JSON 記錄**（`write_json`）：先寫暫存檔再置換。內容整份都要對（部位口數、心跳的時刻、
    當天的設定），寫到一半斷電只能留下舊的或新的，不能是半個（規格第 52 條）。
  * **標記檔**（`write_marker`／`read_marker`）：**檔案存在就算數**，內容只是寫給人看的原因。
    直接寫、不經置換：寫到一半也一樣算數；部位檔被防毒鎖住、`os.replace` 失敗的時候，這裡照樣
    寫得進去——停止進場記錄最需要的正是那種時候。

`.lock` 檔是「同一時間只有一個」的鎖（`exclusive`），內容不重要。
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from datetime import date
from typing import Iterator

import paths


def daily(name: str, day: date, suffix: str = ".txt") -> str:
    """`state/` 底下那個交易日的檔：`daily("exit-done", day)` → `state/exit-done-YYYYMMDD.txt`。"""
    return os.path.join(paths.state_dir(), f"{name}-{day:%Y%m%d}{suffix}")


def read_marker(path: str) -> str | None:
    """標記檔的內容；不存在是 None。

    **讀不懂也算存在**（寫到一半斷電、被改壞）：回傳一段說明，不是 None——和部位檔同一個道理，
    讀不懂不能當成「沒事」。
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except (OSError, ValueError) as exc:     # UTF-8 的解碼錯誤是 ValueError
        return f"（記錄在 {path}，但讀不懂：{type(exc).__name__}: {exc}）"


def write_marker(path: str, line: str, *, append: bool = False) -> None:
    """寫一行到標記檔（`append` 是接在後面，否則覆寫）。寫完就落到磁碟：緊接著可能就是當機重啟。

    寫不進去就拋 `OSError`：要不要因此中止、log 怎麼講，由呼叫端決定——三種記錄寫不進去的
    後果各不相同。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a" if append else "w", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def write_json(path: str, data: object) -> None:
    """原子寫入一份 JSON 記錄。寫不進去就拋例外，正式檔維持寫之前的樣子。

    暫存檔刻意建在**目標檔案的同一個目錄**：跨磁碟的 `os.replace` 不是原子的，而系統暫存目錄
    與程式目錄很可能不在同一個磁碟上。暫存檔的名字以 `.` 開頭、以 `.tmp` 結尾，認檔名的程式
    （`position_file.days`）不會把它當成記錄。
    """
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{os.path.basename(path)}-",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())       # 真的落到磁碟，不只是進了作業系統的快取
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_json(path: str) -> object | None:
    """一份 JSON 記錄的內容（還沒驗證過）；不存在是 None。讀不了、不是 JSON 拋 `ValueError`
    或 `OSError`（JSON 與 UTF-8 的解碼錯誤都是 ValueError），由呼叫端換成它自己的「讀不懂」。"""
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@contextlib.contextmanager
def exclusive(path: str) -> Iterator[bool]:
    """鎖住 `path`（作業系統的檔案鎖，不等）：交出 True；別的行程拿著、鎖不到就交出 False。

    用檔案鎖、不用「檔案在不在」：行程不管怎麼死，鎖都跟著放掉；留下一個檔案的話，當掉之後的
    每一次都會以為還有人在跑。鎖檔本身留著，不刪。同一個行程裡另開一次也鎖不到（Windows 的鎖
    跟著檔案代碼走）。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        try:
            _lock(fd, True)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            _lock(fd, False)
    finally:
        os.close(fd)


def _lock(fd: int, on: bool) -> None:
    """鎖住（或放掉）檔案的第一個位元組，鎖不到立刻拋 OSError、不等。"""
    if sys.platform == "win32":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)        # msvcrt 鎖的是「目前位置起」的位元組
        msvcrt.locking(fd, msvcrt.LK_NBLCK if on else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, (fcntl.LOCK_EX | fcntl.LOCK_NB) if on else fcntl.LOCK_UN)
