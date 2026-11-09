"""設定載入。

兩個設計原則，沿用 invest-os：

1. **缺鍵就大聲失敗，不套用預設值。** 靜默的預設值會讓「設定沒生效」變成
   要跑到線上才發現的問題，而這個系統的線上代表真錢。
2. **設定與程式不得漂移，而且由結構保證、不靠人工維護清單。** `build()` 讀了哪些鍵
   是被追蹤出來的（`consumed_keys`），不是抄一份在旁邊——抄的那份遲早會忘記更新。

策略常數（±1000、RSI 70／30、08:50、13:20…）**不在這裡**：改常數等於換了一個策略，
不是調整使用方式。它們只出現在策略判斷一處（規格「設定」一節）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import yaml

import paths
from broker import MARKET_ORDER_LOT_CAP, PRODUCT_NAMES


@dataclass(frozen=True)
class Config:
    """使用者可調的五個設定（規格第 42–45 條）。"""

    # 自動下單。**關閉時完全不呼叫下單 API**，只做模擬。
    auto_order_enabled: bool
    # 交易商品的報價代碼（broker.PRODUCT_CODES 之一）。設定檔裡寫的是 TX／MTX／TMF。
    product: str
    lots_per_entry: int
    # 加碼上限：PTS 一天的部位最多幾口，等待中的突破進場也算在內。
    max_position: int
    discord_enabled: bool


class SettingsError(ValueError):
    """設定寫錯。程式一啟動就擋下來，訊息要講使用者看得到的那個鍵名（規格第 47 條）。"""


class _Tracked:
    """唯讀的 mapping：缺鍵時講出完整鍵名，並記下自己被讀過哪些葉節點。

    兩件事放在一起，是因為它們都需要「完整鍵名」：錯誤訊息要講 `order.max_position`
    而不是 `KeyError: 'max_position'`；漂移檢查要問出 `build()` 到底讀了哪些鍵，
    不必手寫一份清單擺在旁邊等著過期。
    """

    def __init__(self, data, prefix: str = "", seen: set | None = None):
        self._data = data
        self._prefix = prefix
        self.seen = set() if seen is None else seen

    def __getitem__(self, key: str):
        full = f"{self._prefix}{key}"
        if not isinstance(self._data, dict) or key not in self._data:
            raise SettingsError(f"設定檔缺少 {full}")
        value = self._data[key]
        if isinstance(value, dict):
            return _Tracked(value, prefix=f"{full}.", seen=self.seen)
        self.seen.add(full)
        return value


def read_raw(path: str | None = None) -> dict:
    with open(path or paths.config_path(), encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def build(raw: dict) -> Config:
    """從原始 mapping 組出 Config。缺鍵會拋 SettingsError，不靜默補預設值。"""
    return _build(_Tracked(raw))


def _build(raw: _Tracked) -> Config:
    order = raw["order"]
    lots = _whole_number(order["lots_per_entry"], "order.lots_per_entry")
    if lots > MARKET_ORDER_LOT_CAP:
        raise SettingsError(
            f"order.lots_per_entry 不可超過 {MARKET_ORDER_LOT_CAP}，目前是 {lots}。"
            "期交所限制每筆市價委託口數（一般交易時段 10 口），超過會被退單。"
        )
    max_position = _whole_number(order["max_position"], "order.max_position")
    if max_position < lots:
        raise SettingsError(
            f"order.max_position（{max_position}）比每次進場的口數（{lots}）還小，"
            "這樣永遠進不了場"
        )
    return Config(
        auto_order_enabled=_switch(order["auto_enabled"], "order.auto_enabled"),
        product=_product(order["product"]),
        lots_per_entry=lots,
        max_position=max_position,
        discord_enabled=_switch(raw["discord"]["enabled"], "discord.enabled"),
    )


def _whole_number(value, key: str) -> int:
    """≥ 1 的整數。

    ⚠️ `bool` 是 `int` 的子類：yaml 的 `yes` 會變成 True，而 True == 1。
       不先排除的話，`lots_per_entry: yes` 會被當成 1 口放行。
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SettingsError(f"{key} 必須是 ≥ 1 的整數，目前是 {value!r}")
    return value


def _product(value) -> str:
    """設定檔的商品名稱 → 報價代碼。只收這三個名字，大小寫與空白都不寬容——
    猜對使用者的意思沒有好處，猜錯的代價是下到別的商品上。"""
    if not isinstance(value, str) or value not in PRODUCT_NAMES:
        raise SettingsError(
            f"order.product 必須是 {'、'.join(PRODUCT_NAMES)} 其中之一，目前是 {value!r}"
        )
    return PRODUCT_NAMES[value]


def _switch(value, key: str) -> bool:
    """開關必須是真正的布林值。

    ⚠️ yaml 的 `auto_enabled: "false"` 會解析成字串 "false"，而非空字串一律為真——
       使用者以為開關關著，程式卻照常送出真單。數字 0／1 也擋：要的是打得出來的
       true／false，不是「剛好能轉成布林」的東西。
    """
    if not isinstance(value, bool):
        raise SettingsError(
            f"{key} 必須是 true 或 false（不加引號），"
            f"目前是 {value!r}（型別 {type(value).__name__}）"
        )
    return value


def flatten_keys(data: dict, prefix: str = "") -> set:
    """把巢狀 dict 的鍵攤平成 {"a.b", "a.c"}。"""
    keys = set()
    for key, value in data.items():
        full = f"{prefix}{key}"
        if isinstance(value, dict):
            keys |= flatten_keys(value, prefix=f"{full}.")
        else:
            keys.add(full)
    return keys


def consumed_keys(raw: dict) -> set:
    """`build()` 實際讀到的設定鍵，供漂移檢查使用。"""
    tracked = _Tracked(raw)
    _build(tracked)
    return tracked.seen


def load(path: str | None = None) -> Config:
    return build(read_raw(path))


# ── 不進版控的帳密（規格第 46 條）────────────────────────────────────────


@dataclass(frozen=True)
class Secrets:
    """登入、下單、通知要用的四個值。

    ⚠️ 密碼與 webhook 不出現在 repr 裡：設定物件常被印進 log 或錯誤訊息，
       密碼等於下單權限，webhook 的 token 等於發訊權限
       （invest-os 2026-09 曾經把 webhook token 寫進日誌）。
    """

    user_id: str
    password: str = field(repr=False)
    # 期貨帳號由這裡明確指定，不從回報裡自動挑——有多個帳號時「自動挑一個」
    # 會把單下到使用者沒預期的帳戶上。
    futures_account: str
    discord_webhook: str = field(repr=False)


# `.env` 裡的名字 → `Secrets` 的欄位。`.env.example` 必須剛好列出這些（有測試守著）。
SECRET_NAMES = {
    "CAPITAL_USER_ID": "user_id",
    "CAPITAL_PASSWORD": "password",
    "CAPITAL_FUTURES_ACCOUNT": "futures_account",
    "DISCORD_WEBHOOK_URL": "discord_webhook",
}


def load_secrets(config: Config | None, path: str | None = None) -> Secrets:
    """從不進版控的 `.env` 讀帳密，並檢查**這次執行**需要的值都在。

    需要哪些取決於設定：登入一定要；期貨帳號只有自動下單時要（模擬模式不碰下單 API）；
    webhook 只有開 Discord 時要。`config` 為 None 代表只需要登入（唯讀檢查工具）。

    缺的一次列完，不是修好一個才發現還有下一個。
    """
    path = path or paths.env_path()
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        raise SettingsError(
            f"找不到 {path}。把 .env.example 複製成 .env，填入帳號密碼"
        ) from None

    values = dict.fromkeys(SECRET_NAMES.values(), "")
    for line in lines:
        name, sep, value = line.partition("=")
        name = name.strip()
        if sep and name in SECRET_NAMES and not name.startswith("#"):
            values[SECRET_NAMES[name]] = value.strip()
    secrets = Secrets(**values)

    required = ["CAPITAL_USER_ID", "CAPITAL_PASSWORD"]
    if config is not None and config.auto_order_enabled:
        required.append("CAPITAL_FUTURES_ACCOUNT")
    if config is not None and config.discord_enabled:
        required.append("DISCORD_WEBHOOK_URL")
    missing = [n for n in required if not getattr(secrets, SECRET_NAMES[n])]
    if missing:
        raise SettingsError(f"{path} 缺少：{'、'.join(missing)}")
    return secrets
