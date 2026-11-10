"""模擬部位：自動下單關閉時（模擬模式，規格第 42 條），今天模擬進場累計的口數與均價。

模擬模式要「照常判斷」：上線前拿它核對模擬出來的進場和作者的判斷一致（Further Notes 第 1 點）。
只放在記憶體的話，盤中程式一重啟（規格第 48 條）就歸零：累計口數從 1 重算、加碼上限被突破、
條件 3、4 要的「已持有部位」不成立，那一天模擬的路就和實盤不同，最新一則訊息的累計口數也不是
目前的部位（第 37 條）。所以每次模擬成交後寫成 `state/sim-position-YYYYMMDD.json`，重啟時讀回來
接著累計。

**和部位檔分開，出場程式絕不讀它**：出場程式照部位檔平倉，部位檔「確定沒有部位就是沒有部位檔」；
模擬的口數混進去，13:25 那筆「平倉」單就是開出來的反向新倉。當天的設定在第一次啟動時就凍結
（`day_settings`），同一天不會在模擬與真實之間切換。

## 格式

    {
      "trading_day": "2026-11-09",
      "direction": "LONG",          LONG／SHORT
      "lots": 2,                    模擬累計的口數
      "avg_price": 45155.0          模擬成交均價
    }

寫法同部位檔：先寫暫存檔再置換（`state_files.write_json`）。讀不懂就拋 `Unreadable`。
"""

from __future__ import annotations

from datetime import date

import state_files
import strategy

_FIELDS = ("trading_day", "direction", "lots", "avg_price")


class Unreadable(Exception):
    """模擬部位存在但讀不懂。"""


def path_for(day: date) -> str:
    """那個交易日的模擬部位。"""
    return state_files.daily("sim-position", day, ".json")


def read(day: date) -> strategy.Position | None:
    """那個交易日模擬累計的部位；今天還沒有模擬進場是 None。"""
    path = path_for(day)
    try:
        data = state_files.read_json(path)
        if data is None:
            return None
        position, trading_day = _from_json(data)
    except (ValueError, OSError) as exc:     # JSON 與 UTF-8 的解碼錯誤也是 ValueError
        raise Unreadable(f"{path}：{exc}") from exc
    if trading_day != day.isoformat():
        raise Unreadable(f"{path}：檔名是 {day} 的模擬部位，內容卻是 {trading_day} 的")
    return position


def write(day: date, position: strategy.Position) -> None:
    """原子寫入（`state_files.write_json`）。寫不進去就拋例外。"""
    state_files.write_json(path_for(day), {
        "trading_day": day.isoformat(), "direction": position.direction,
        "lots": position.lots, "avg_price": position.avg_price})


def _from_json(data: object) -> tuple[strategy.Position, str]:
    if not isinstance(data, dict) or set(data) != set(_FIELDS):
        raise ValueError(f"模擬部位應該正好有 {'、'.join(_FIELDS)} 這幾個欄位，目前是 {data!r}")
    day, direction, lots, price = (data["trading_day"], data["direction"], data["lots"],
                                   data["avg_price"])
    if not isinstance(day, str):
        raise ValueError(f"trading_day 應該是 YYYY-MM-DD 格式的日期，目前是 {day!r}")
    if direction not in (strategy.LONG, strategy.SHORT):
        raise ValueError(f"direction 應該是 {strategy.LONG} 或 {strategy.SHORT}，目前是 {direction!r}")
    if isinstance(lots, bool) or not isinstance(lots, int) or lots < 1:
        raise ValueError(f"lots 應該是 ≥ 1 的整數，目前是 {lots!r}")
    if isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0:
        raise ValueError(f"avg_price 應該是正數，目前是 {price!r}")
    return strategy.Position(direction, lots, float(price)), day
