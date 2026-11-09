"""部位檔：PTS 今天開了什麼部位。盤中程式每次成交後寫，出場程式 13:25 照著它平倉。

兩支程式是不同的行程，中間隔著幾個小時，唯一的溝通管道就是這個檔案（規格「兩支程式」
一節）。所以它的正確性直接等於「13:25 會不會去平倉、平幾口」。

三個設計選擇，沿用 invest-os 的 `state.py`：

1. **JSON 純文字。** 出事時使用者要能自己打開來看、必要時手改。
2. **先寫暫存檔再置換。** 同一個磁碟上 `os.replace` 是原子的：寫到一半斷電只會留下暫存檔，
   正式檔要嘛是舊的、要嘛是新的，不會是半個（規格第 52 條）。
3. **讀不懂就拋例外（`Unreadable`），絕不當成「沒有部位」。**

## 和 invest-os 不同的地方

* **每一筆委託各記一筆**（序號、實際口數、成交價），均價由明細算出，不另外存。
  OS 只有一個序號欄位，出場時會被覆蓋，所以出場不確定時查不了（規格「出場流程」）。
* **一個交易日一個檔**（`state/position-YYYYMMDD.json`）。今天的檔不存在就是今天沒有部位；
  前一天沒了結的檔不會被今天的成交覆蓋掉，也不會被當成今天的部位去平。

## 格式

    {
      "trading_day": "2026-11-09",          交易日
      "product": "TM0000AM",                交易商品（報價代碼）
      "order_code": "TM2611",               下單代碼
      "contract_month": "202611",           契約月份
      "direction": "LONG",                  方向：LONG／SHORT
      "last_trading_day": "2026-11-18",     最後交易日
      "status": "CONFIRMED",                CONFIRMED（已確認）／UNCERTAIN（不確定）
      "entries": [{"order_seq": "2315610000001", "lots": 1, "price": 45150.0}],
      "exits": [],                          出場委託，格式同 entries（出場程式寫）。
                                            口數加起來等於 entries 就是**已了結**
      "uncertain": null                     不確定的那一筆：
                                            {"stage": "ENTRY"／"EXIT", "order_seq": "…",
                                             "requested_lots": 2, "known_filled": 0}
    }

「確定沒有部位」就是沒有部位檔，或部位檔裡沒有成交（規格「部位檔」一節）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date

import paths
import strategy
from broker import MARKET_ORDER_LOT_CAP, PRODUCT_CODES, ContractInfo

logger = logging.getLogger(__name__)

# 部位的可信度（規格第 50 條）。
#
# `CONFIRMED`  每一筆委託都知道實際成交幾口。
# `UNCERTAIN`  **有一筆委託送出去了，但不知道成交幾口**（`uncertain` 記的就是那一筆）。
#
# ⚠️ 「不確定」與「確定沒有部位」長得很像，處理卻完全相反：不確定時當天不再進場，
#    出場程式也不自動平倉，改發 Discord 請人看帳戶。
CONFIRMED = "CONFIRMED"
UNCERTAIN = "UNCERTAIN"

# 不確定的是**哪一筆**（invest-os 2026-08-19 的教訓）：進場單不確定，出場時查得到就能照常
# 處理；出場單不確定，**絕對不可以再送一次**——它很可能已經成交，再送就是反向新倉。
ENTRY = "ENTRY"
EXIT = "EXIT"


class Unreadable(Exception):
    """部位檔存在但讀不懂。**不可以當成「沒有部位」**：它可能記著今天已經成交的口數。"""


@dataclass(frozen=True)
class Fill:
    """一筆委託的**實際**成交（規格第 29 條）：IOC 只成交一部分時，口數就是成交的那部分。

    只有成交了的委託才有這一筆；0 口的委託不記（沒有部位，也沒有價格）。
    """

    order_seq: str          # 群益的 13 碼委託序號：人工對帳、出場的兩段查詢都靠它
    lots: int               # 實際成交口數
    price: float            # 這筆委託的成交均價（分好幾個價位成交時依口數加權）

    def __post_init__(self) -> None:
        if not self.order_seq:
            raise ValueError("成交缺少委託序號：認不出是哪一筆，出場時也查不了")
        if self.lots < 1:
            raise ValueError(f"成交口數必須 ≥ 1，目前是 {self.lots}（0 口的委託不記）")
        if self.price <= 0:
            raise ValueError(f"成交價必須 > 0，目前是 {self.price}")


@dataclass(frozen=True)
class UncertainOrder:
    """送出去了、卻不知道成交幾口的那一筆（ticket 07 寫進場的、ticket 08 寫出場的）。"""

    stage: str              # ENTRY / EXIT：不確定的是進場單還是出場單
    order_seq: str          # 沒拿到序號時是空字串：連查都沒辦法查
    requested_lots: int     # 委託口數：曝險的上界，「可能有 1 口」與「可能有 10 口」差很多
    known_filled: int       # 目前已知**至少**成交了幾口（`FillUnknown.known_filled`）

    def __post_init__(self) -> None:
        if self.stage not in (ENTRY, EXIT):
            raise ValueError(f"不確定的委託必須註明是 {ENTRY} 或 {EXIT}，目前是 {self.stage!r}")
        if not 1 <= self.requested_lots <= MARKET_ORDER_LOT_CAP:
            raise ValueError(
                f"委託口數必須是 1～{MARKET_ORDER_LOT_CAP}，目前是 {self.requested_lots}")
        if not 0 <= self.known_filled <= self.requested_lots:
            raise ValueError(f"已知成交 {self.known_filled} 口，不在 0 到委託的 "
                             f"{self.requested_lots} 口之間")


@dataclass(frozen=True)
class PositionRecord:
    """一個交易日的部位檔。"""

    trading_day: date
    product: str                    # 報價代碼（broker.PRODUCT_CODES 之一）
    # 下單代碼（`TM2611`）、契約月份、最後交易日：**出場照這裡送，不重查商品清單**。
    # 進場時已經知道，出場再查一次只是多一個會失敗的地方。
    order_code: str
    contract_month: str             # yyyymm
    direction: str                  # strategy.LONG / strategy.SHORT
    last_trading_day: date
    status: str                     # CONFIRMED / UNCERTAIN
    entries: tuple[Fill, ...]       # 進場委託，依時間
    exits: tuple[Fill, ...] = ()    # 出場委託（出場程式寫）
    uncertain: UncertainOrder | None = None

    def __post_init__(self) -> None:
        """殘缺或自相矛盾的記錄比沒有記錄更糟：出場程式會拿著它去下單。

        寫的時候擋，讀的時候也擋（`read` 把這裡的 ValueError 變成 `Unreadable`）——
        手改出錯的檔、舊版程式寫的檔，都不能被當成可信的部位。
        """
        if self.product not in PRODUCT_CODES:
            raise ValueError(f"交易商品必須是 {'、'.join(PRODUCT_CODES)} 其中之一，"
                             f"目前是 {self.product!r}")
        if not self.order_code:
            raise ValueError("缺少下單代碼，出場時無法送出委託")
        if self.contract_month != f"{self.last_trading_day:%Y%m}":
            raise ValueError(f"契約月份 {self.contract_month} 和最後交易日 "
                             f"{self.last_trading_day} 對不上")
        if self.direction not in (strategy.LONG, strategy.SHORT):
            raise ValueError(f"方向必須是 {strategy.LONG} 或 {strategy.SHORT}，"
                             f"目前是 {self.direction!r}")
        if self.status not in (CONFIRMED, UNCERTAIN):
            raise ValueError(f"狀態必須是 {CONFIRMED} 或 {UNCERTAIN}，目前是 {self.status!r}")
        # 狀態和「哪一筆不確定」必須同進同退：只說不確定卻不說是哪一筆，出場單會被當成
        # 進場單處理；掛著不確定的委託卻說已確認，出場程式就會照著不完整的口數平倉。
        if (self.status == UNCERTAIN) != (self.uncertain is not None):
            raise ValueError(f"狀態是 {self.status}，不確定的委託卻是 {self.uncertain}")
        exited = sum(f.lots for f in self.exits)
        if exited > self.entered_lots:
            raise ValueError(f"平倉 {exited} 口，比進場的 {self.entered_lots} 口還多")

    @property
    def contract(self) -> ContractInfo:
        """記在檔裡的合約。當天已經有部位時一律照它下單，不看設定檔（規格第 43 條）。"""
        return ContractInfo(code=self.product, last_trading_day=self.last_trading_day,
                            order_code=self.order_code)

    @property
    def settles_today(self) -> bool:
        """這一天就是合約的最後交易日（結算日）：近月 13:30 就停止交易，還在的部位由交易所
        以最後結算價現金了結。出場程式送單的期限、告警裡要人在幾點前平倉，都看這一個。"""
        return self.last_trading_day == self.trading_day

    @property
    def entered_lots(self) -> int:
        return sum(f.lots for f in self.entries)

    @property
    def open_lots(self) -> int:
        """還沒平掉的口數：進場減出場。出場程式平的就是這幾口，不多也不少（規格第 32 條）。

        **0 就是已了結**：出場明細的口數等於進場，部位檔不另外存一個「已了結」旗標——
        兩份記載同一件事，總有一天會互相矛盾，而出場程式要拿著它去下單。
        """
        return self.entered_lots - sum(f.lots for f in self.exits)

    @property
    def avg_price(self) -> float | None:
        """進場的成交均價，依口數加權。還沒有成交時是 None。"""
        lots = self.entered_lots
        if not lots:
            return None
        return sum(f.lots * f.price for f in self.entries) / lots


def path_for(day: date) -> str:
    """那個交易日的部位檔。"""
    return os.path.join(paths.state_dir(), f"position-{day:%Y%m%d}.json")


def read(day: date) -> PositionRecord | None:
    """那個交易日的部位檔。

    檔案不存在是 None：**確定**那天沒有部位。存在卻讀不懂——寫到一半、少了欄位、
    多了欄位、型別不對、內容自相矛盾、記的是別天——一律拋 `Unreadable`，訊息講得出
    是哪裡不對，給人工處理時對照。
    """
    path = path_for(day)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        record = _from_json(data)
    except (ValueError, OSError) as exc:     # JSON 與 UTF-8 的解碼錯誤也是 ValueError
        raise Unreadable(f"{path}：{exc}") from exc
    if record.trading_day != day:
        raise Unreadable(f"{path}：檔名是 {day} 的部位檔，內容卻是 {record.trading_day} 的")
    return record


def write(record: PositionRecord) -> None:
    """原子寫入。

    暫存檔刻意建在**目標檔案的同一個目錄**：跨磁碟的 `os.replace` 不是原子的，
    而系統暫存目錄與程式目錄很可能不在同一個磁碟上。
    """
    path = path_for(record.trading_day)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".position-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(_to_json(record), fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())       # 真的落到磁碟，不只是進了作業系統的快取
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    logger.info("部位檔已更新：%s %s 進場 %d 口、還沒平 %d 口（%s）", record.order_code,
                record.direction, record.entered_lots, record.open_lots, record.status)


# ── JSON 格式（和出場程式、和人之間的約定）─────────────────────────────────

_FIELDS = ("trading_day", "product", "order_code", "contract_month", "direction",
           "last_trading_day", "status", "entries", "exits", "uncertain")
_FILL_FIELDS = ("order_seq", "lots", "price")
_UNCERTAIN_FIELDS = ("stage", "order_seq", "requested_lots", "known_filled")
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _to_json(record: PositionRecord) -> dict:
    return {
        "trading_day": record.trading_day.isoformat(),
        "product": record.product,
        "order_code": record.order_code,
        "contract_month": record.contract_month,
        "direction": record.direction,
        "last_trading_day": record.last_trading_day.isoformat(),
        "status": record.status,
        "entries": [_fill_json(f) for f in record.entries],
        "exits": [_fill_json(f) for f in record.exits],
        "uncertain": None if record.uncertain is None else {
            "stage": record.uncertain.stage,
            "order_seq": record.uncertain.order_seq,
            "requested_lots": record.uncertain.requested_lots,
            "known_filled": record.uncertain.known_filled,
        },
    }


def _fill_json(fill: Fill) -> dict:
    return {"order_seq": fill.order_seq, "lots": fill.lots, "price": fill.price}


def _from_json(data) -> PositionRecord:
    """逐欄檢查型別再組回記錄。欄位之間的關係由 `PositionRecord` 自己檢查。

    ⚠️ 型別要嚴格：`true` 在 Python 裡等於 1、`"2"` 看起來也像 2——寬容地收下的話，
       出場程式平的口數就是猜出來的。
    """
    data = _object(data, _FIELDS, "部位檔")
    uncertain = data["uncertain"]
    if uncertain is not None:
        u = _object(uncertain, _UNCERTAIN_FIELDS, "不確定的委託")
        uncertain = UncertainOrder(
            stage=_text(u["stage"], "stage"),
            order_seq=_text(u["order_seq"], "order_seq", empty_ok=True),
            requested_lots=_whole(u["requested_lots"], "requested_lots"),
            known_filled=_whole(u["known_filled"], "known_filled"))
    return PositionRecord(
        trading_day=_day(data["trading_day"], "trading_day"),
        product=_text(data["product"], "product"),
        order_code=_text(data["order_code"], "order_code"),
        contract_month=_text(data["contract_month"], "contract_month"),
        direction=_text(data["direction"], "direction"),
        last_trading_day=_day(data["last_trading_day"], "last_trading_day"),
        status=_text(data["status"], "status"),
        entries=_fills(data["entries"], "entries"),
        exits=_fills(data["exits"], "exits"),
        uncertain=uncertain,
    )


def _object(value, fields: tuple[str, ...], where: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{where}應該是一個物件（{{…}}），目前是 {type(value).__name__}")
    missing = [f for f in fields if f not in value]
    if missing:
        raise ValueError(f"{where}缺少 {'、'.join(missing)}")
    unknown = sorted(set(value) - set(fields))
    if unknown:
        raise ValueError(f"{where}有不認得的欄位 {'、'.join(unknown)}")
    return value


def _fills(value, key: str) -> tuple[Fill, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{key} 應該是一個清單（[…]），目前是 {type(value).__name__}")
    fills = []
    for item in value:
        f = _object(item, _FILL_FIELDS, f"{key} 裡的成交")
        fills.append(Fill(order_seq=_text(f["order_seq"], "order_seq"),
                          lots=_whole(f["lots"], "lots"),
                          price=_number(f["price"], "price")))
    return tuple(fills)


def _text(value, key: str, *, empty_ok: bool = False) -> str:
    if not isinstance(value, str) or not (value or empty_ok):
        raise ValueError(f"{key} 應該是一段文字，目前是 {value!r}")
    return value


def _whole(value, key: str) -> int:
    # bool 是 int 的子類：不先排除的話，`true` 會被當成 1 口
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} 應該是整數，目前是 {value!r}")
    return value


def _number(value, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} 應該是數字，目前是 {value!r}")
    return float(value)


def _day(value, key: str) -> date:
    if not isinstance(value, str) or not _ISO_DAY.fullmatch(value):
        raise ValueError(f"{key} 應該是 YYYY-MM-DD 格式的日期，目前是 {value!r}")
    return date.fromisoformat(value)
