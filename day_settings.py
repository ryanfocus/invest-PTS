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

寫法同部位檔：先寫暫存檔再置換（`state_files.write_json`）。

## 快照不見了

快照讀不懂（被改壞、被防毒鎖住），或今天稍早明明跑過（`_ran_earlier_today`）卻沒有快照（第一次
啟動時寫不進去）：今天第一次啟動時的自動下單開關不知道了。**不能照這一次的設定檔**——那正好是
快照要擋的事：模擬的早上、中午把開關打開，剛好重啟就開始送真單。改成保守的決定：只有部位檔裡
已經有今天的真實成交（部位檔只有真實下單會寫）才照常自動下單，否則今天其餘時間只模擬。
每次進場口數、加碼上限照這一次的設定檔（查不回來；部位檔在的話交易商品照部位檔）。快照照這個
決定重寫，之後再重啟不會又翻過去。這個決定和設定檔現在的開關不一樣時，呼叫端講一次
（`Today.lost`）：今天不照使用者寫的跑，一定要讓他知道。一樣的話只記 log——今天照設定檔跑，
沒有和使用者以為的不同，不必打擾（規格第 39 條）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from datetime import date

import heartbeat
import paths
import position_file
import raw_market
import settings
import sim_position
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


@dataclass(frozen=True)
class Today:
    """今天要用的設定。`lost` 平常是 None；快照不見了、保守的決定又和設定檔的開關不一樣時，
    是給人看的原因（要講一則）。"""

    config: settings.Config
    lost: str | None = None


def for_today(config: settings.Config, day: date) -> Today:
    """今天要用的設定。今天已經有快照：`order` 區段照快照，Discord 開關照 `config`（設定檔）。
    今天第一次啟動：`config` 就是今天的設定，寫成快照。快照不見了：見模組說明「快照不見了」。"""
    where = paths.shown_state(path_for(day))
    try:
        frozen = read(day)
        lost = None
    except Unreadable as exc:
        frozen, lost = None, f"當天的設定記錄 {where} 讀不懂（{exc}）"
    if frozen is not None:
        today = replace(frozen, discord_enabled=config.discord_enabled)
        if today != config:
            logger.warning("設定檔的 order 區段今天改過了（%s），今天照第一次啟動時的（%s），"
                           "新設定從下一個交易日起生效", config, today)
        return Today(today)
    if lost is None and _ran_earlier_today(day):
        lost = f"今天稍早盤中程式已經跑過，卻沒有當天的設定記錄 {where}（當時寫不進去？）"
    if lost is None:
        _write(config, day)
        return Today(config)
    live = os.path.exists(position_file.path_for(day))
    logger.error("%s。今天其餘時間%s", lost, "照常自動下單（部位檔裡有今天的真實成交）" if live
                 else "只模擬（部位檔裡沒有今天的真實成交）")
    safe = replace(config, auto_order_enabled=live)
    _write(safe, day)
    return Today(safe, lost if live != config.auto_order_enabled else None)


def _ran_earlier_today(day: date) -> bool:
    """今天稍早盤中程式跑過的痕跡。快照在登入之前就寫，所以沒有快照、又有這些痕跡，就是當時
    寫不進去。看好幾樣，因為寫不進快照的那個原因（防毒鎖住、`os.replace` 失敗）多半也讓其他
    JSON 記錄寫不進去：

      * 原始行情檔：直接接著寫、不經置換。收到今天日盤的成交才建——進場一定在那之後。
      * 心跳記錄（今天的；讀不懂也算，不知道就當成跑過）、部位檔、模擬部位。

    一樣都沒有的話，稍早那一輪（如果有）還沒收到今天的成交，什麼都沒判斷過：當成今天第一次
    啟動不會有兩套規則。
    """
    if any(os.path.exists(p) for p in (raw_market.path_for(day), position_file.path_for(day),
                                       sim_position.path_for(day))):
        return True
    try:
        beat = heartbeat.read()
    except heartbeat.Unreadable:
        return True
    return beat is not None and beat.trading_day == day


def _write(config: settings.Config, day: date) -> None:
    """寫不進去只記 log：這一輪照樣跑。重啟時看到「今天跑過、卻沒有快照」，照保守的決定走。"""
    try:
        state_files.write_json(path_for(day), _to_json(config, day))
    except Exception as exc:  # noqa: BLE001
        logger.error("當天的設定快照寫不進 %s（%s: %s）", path_for(day), type(exc).__name__, exc)


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
