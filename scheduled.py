"""排程叫醒程式的入口（ticket 10）。工作排程器執行的是：

    pythonw.exe scheduled.py intraday     盤中程式：08:40 起每分鐘叫醒一次，直到 13:20
    pythonw.exe scheduled.py exit         出場程式：13:25

排程由 `tools/schedule.py` 建立。平常手動執行時照舊用 `python intraday.py`／`python exit_program.py`。

## 為什麼不讓排程直接執行那兩支程式

1. **用 pythonw，沒有主控台視窗。** 盤中程式每分鐘被叫醒一次（這就是失敗時的自動重啟，見
   `tools/schedule.py`）：用 python.exe 的話，非交易日每分鐘跳出一個黑色視窗、搶走鍵盤焦點；
   交易日那個視窗開一整個早上，被順手關掉就等於當掉。
2. **pythonw 沒有 stderr。** 程式在設定好 log 之前就死掉（少裝套件、語法錯誤、設定檔讀不懂）時，
   traceback 無處可去，排程只記下「結果 1」，log 一個字都沒有——invest-os 2026-08-23 就是這樣，
   症狀是「排程跑了但什麼都沒發生」。這裡把它寫進那支程式當天的 log：13:25 出場程式告警
   「盤中程式今天沒有跑」時，叫人去看的就是那個檔。

所以這裡只 import 標準函式庫與 `paths`，那兩支程式在 `try` 裡面才載入。
"""

from __future__ import annotations

import importlib
import os
import sys
import traceback
from datetime import datetime

import paths

# 排程的參數 → (模組, 當天 log 的檔名開頭, 給人看的名字)。log 檔名與兩支程式的 `_setup_logging` 一致。
PROGRAMS = {
    "intraday": ("intraday", "intraday", "盤中程式"),
    "exit": ("exit_program", "exit", "出場程式"),
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in PROGRAMS:
        print(f"用法：scheduled.py {'|'.join(PROGRAMS)}", file=sys.stderr or sys.__stderr__)
        return 2
    module, log_name, label = PROGRAMS[argv[1]]
    for stream in ("stdout", "stderr"):
        if getattr(sys, stream) is None:
            # pythonw：直接寫 sys.stdout／stderr 的程式碼（第三方套件也算）不能因此出錯
            setattr(sys, stream, open(os.devnull, "w", encoding="utf-8"))
    try:
        return importlib.import_module(module).main()
    except Exception:  # noqa: BLE001
        _record_crash(log_name, label, traceback.format_exc())
        return 1


def _record_crash(log_name: str, label: str, trace: str) -> None:
    """把 traceback 附加到那支程式當天的 log。程式自己的 log 可能還沒設定好（死在 import），
    所以不經過 logging。寫不進去也不拋：已經沒有別的地方能講了。"""
    now = datetime.now()
    path = os.path.join(paths.logs_dir(), f"{log_name}-{now:%Y%m%d}.log")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{now:%Y-%m-%d %H:%M:%S} [CRITICAL] scheduled: {label}沒有正常結束\n{trace}")
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
