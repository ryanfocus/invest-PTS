"""當天的設定快照：盤中程式當天第一次啟動時讀到的 `order` 區段，整天不變（規格「設定」一節）。

規格答應使用者「改設定最快下一個交易日才生效」。盤中程式一天可能被重啟好幾次（排程每分鐘叫醒，
規格第 48 條），每次重讀設定檔的話，這個承諾就看「有沒有剛好當掉」：

  * 早上真實進場、中午把自動下單關掉，剛好當掉重啟 → 下午的「模擬」進場疊在帳上的真實口數上，
    訊息的累計口數不是帳上的部位（第 37 條）。
  * 反過來，早上模擬、中午打開 → 剛好當掉重啟的話，當天就開始送真單。
  * 每次進場口數、加碼上限在重啟後變了，一天的部位就照兩套規則加碼。

所以第一次成功讀到設定時，把 `order` 區段（自動下單開關、交易商品、每次進場口數、加碼上限）寫成
`state/day-settings-YYYYMMDD.json`，同一天重啟時照它，不重讀設定檔的這些值。交易商品仍以部位檔
為準（盤中程式自己處理）。**Discord 開關不在快照裡**：它不影響部位，使用者想關掉通知，下一次
啟動（含重啟）就照新的。

只有盤中程式讀寫它。出場程式不看：它決定要不要平微台只看部位檔（部位檔只有真實下單會寫）。

## 格式

    {
      "trading_day": "2026-11-09",
      "auto_order_enabled": true,
      "product": "TM0000AM",          報價代碼
      "lots_per_entry": 1,
      "max_position": 20
    }

寫法同部位檔：先寫暫存檔再置換（`state_files.write_json`）。讀不懂（被手改壞、被防毒鎖住）時當成
沒有快照：照這一次讀到的設定檔，並覆寫它——和心跳記錄一樣，讀不懂不會讓盤中程式停下來；
要同時碰上「快照壞了」與「當天改了設定」才會照新設定走。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date

import settings
import state_files
from broker import MARKET_ORDER_LOT_CAP, PRODUCT_CODES

logger = logging.getLogger(__name__)

# 快照裡的欄位：`settings.Config` 的 `order` 區段（Discord 開關不在裡面）
_FIELDS = ("trading_day", "auto_order_enabled", "product", "lots_per_entry", "max_position")


class Unreadable(Exception):
    """快照存在但讀不懂。"""


def path_for(day: date) -> str:
    """那個交易日的設定快照。"""
    return state_files.daily("day-settings", day, ".json")


def for_today(config: settings.Config, day: date) -> settings.Config:
    """今天要用的設定。今天已經有快照：`order` 區段照快照，Discord 開關照 `config`（設定檔）。
    還沒有：`config` 就是今天的設定，寫成快照。寫不進去只記 log：這一輪照樣跑，代價是重啟時
    會重讀設定檔。"""
    try:
        frozen = read(day)
    except Unreadable as exc:
        logger.error("當天的設定快照讀不懂，照這一次讀到的設定檔，並重寫快照：%s", exc)
        frozen = None
    if frozen is not None:
        today = replace(frozen, discord_enabled=config.discord_enabled)
        if today != config:
            logger.warning("設定檔的 order 區段今天改過了（%s），今天照第一次啟動時的（%s），"
                           "新設定從下一個交易日起生效", config, today)
        return today
    try:
        state_files.write_json(path_for(day), _to_json(config, day))
    except Exception as exc:  # noqa: BLE001
        logger.error("當天的設定快照寫不進 %s（%s: %s）：盤中程式若重啟，會重讀設定檔",
                     path_for(day), type(exc).__name__, exc)
    return config


def read(day: date) -> settings.Config | None:
    """那個交易日的設定快照（Discord 開關是 True，由呼叫端換掉）；沒有快照是 None。"""
    path = path_for(day)
    try:
        data = state_files.read_json(path)
        if data is None:
            return None
        frozen = _from_json(data)
    except (ValueError, OSError) as exc:     # JSON 與 UTF-8 的解碼錯誤也是 ValueError
        raise Unreadable(f"{path}：{exc}") from exc
    if data["trading_day"] != day.isoformat():
        raise Unreadable(f"{path}：檔名是 {day} 的快照，內容卻是 {data['trading_day']} 的")
    return frozen


def _to_json(config: settings.Config, day: date) -> dict:
    return {"trading_day": day.isoformat(),
            "auto_order_enabled": config.auto_order_enabled,
            "product": config.product,
            "lots_per_entry": config.lots_per_entry,
            "max_position": config.max_position}


def _from_json(data: object) -> settings.Config:
    """逐欄檢查型別：寬容地收下 `"true"`、`"2"` 的話，自動下單與口數就是猜出來的。"""
    if not isinstance(data, dict) or set(data) != set(_FIELDS):
        raise ValueError(f"設定快照應該正好有 {'、'.join(_FIELDS)} 這幾個欄位，目前是 {data!r}")
    auto, product = data["auto_order_enabled"], data["product"]
    lots, cap = data["lots_per_entry"], data["max_position"]
    if not isinstance(auto, bool):
        raise ValueError(f"auto_order_enabled 應該是 true 或 false，目前是 {auto!r}")
    if product not in PRODUCT_CODES:
        raise ValueError(f"product 應該是 {'、'.join(PRODUCT_CODES)} 其中之一，目前是 {product!r}")
    for key, value in (("lots_per_entry", lots), ("max_position", cap)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{key} 應該是 ≥ 1 的整數，目前是 {value!r}")
    if lots > MARKET_ORDER_LOT_CAP or cap < lots:
        raise ValueError(f"lots_per_entry {lots}、max_position {cap} 不是設定檔寫得出來的值")
    return settings.Config(auto_order_enabled=auto, product=product, lots_per_entry=lots,
                           max_position=cap, discord_enabled=True)
