"""設定檔：缺鍵就啟動失敗、開關必須是真正的布林值、設定與程式不得漂移。

期望值來自規格第 42–47 條，不是從程式反推的。
"""

import pytest

import settings


def test_the_shipped_config_has_auto_ordering_switched_off():
    """**版控裡的設定檔，自動下單必須是關閉的**（規格第 42 條）。

    守的是 repo 的狀態而不是程式邏輯：任何人不小心把開著的設定 commit 上來，
    這裡就會紅。程式碼裡的預設值保護不了任何人，設定檔的實際內容才會。
    """
    assert settings.load().auto_order_enabled is False


# --- 缺鍵：啟動失敗並講明原因（規格第 47 條）---


def _shipped_without(dotted_key: str) -> dict:
    """從真實設定檔拿掉一個鍵。從真實檔長出來，設定形狀改了不必回來改這裡。"""
    raw = settings.read_raw()
    section, _, leaf = dotted_key.partition(".")
    del raw[section][leaf]
    return raw


@pytest.mark.parametrize("key", [
    "order.auto_enabled",
    "order.product",
    "order.lots_per_entry",
    "order.max_position",
    "discord.enabled",
])
def test_a_missing_key_stops_startup_and_names_the_key(key):
    """缺任何一個鍵都不可以套預設值——錯誤訊息要講出是**哪一個鍵**，
    使用者要去改的是 yaml，`KeyError: 'order'` 讓他無從下手。"""
    with pytest.raises(settings.SettingsError, match=key.replace(".", r"\.")):
        settings.build(_shipped_without(key))


def test_a_missing_section_also_names_the_key():
    raw = settings.read_raw()
    del raw["discord"]
    with pytest.raises(settings.SettingsError, match="discord"):
        settings.build(raw)


# --- 設定與程式不得漂移（規格 Testing Decisions「設定」）---


def test_every_key_in_the_shipped_config_is_read_by_the_program():
    """yaml 的鍵集合必須與程式實際讀取的集合完全相同。

    多出來 → 有設定沒人讀（使用者改了以為有效，其實沒有）
    少掉了 → 程式讀了 yaml 沒有的鍵（載入時就會失敗，上面那組測試會先紅）
    """
    raw = settings.read_raw()
    assert settings.consumed_keys(raw) == settings.flatten_keys(raw)


def test_a_key_nobody_reads_is_detected():
    """加一個沒人讀的鍵，漂移檢查必須抓得到——否則上面那條永遠是綠的。"""
    raw = settings.read_raw()
    raw["order"]["stop_loss"] = 50
    assert settings.consumed_keys(raw) != settings.flatten_keys(raw)


# --- 開關必須是真正的布林值（規格第 47 條）---


def _shipped_with(dotted_key: str, value) -> dict:
    raw = settings.read_raw()
    section, _, leaf = dotted_key.partition(".")
    raw[section][leaf] = value
    return raw


@pytest.mark.parametrize("key", ["order.auto_enabled", "discord.enabled"])
@pytest.mark.parametrize("value", ["false", "true", "no", 0, 1, None])
def test_a_switch_that_is_not_a_real_boolean_stops_startup(key, value):
    """**yaml 的 `auto_enabled: "false"` 是字串，而非空字串一律為真。**

    使用者以為開關關著，程式卻照常送出真單——這是設定錯誤裡代價最大的一種。
    `"true"` 也要擋：「剛好會動」會讓人以為加引號是合法寫法。
    """
    with pytest.raises(settings.SettingsError, match=key.replace(".", r"\.")):
        settings.build(_shipped_with(key, value))


def test_real_booleans_are_accepted():
    cfg = settings.build(_shipped_with("order.auto_enabled", True))
    assert cfg.auto_order_enabled is True


# --- 交易商品（規格第 8、43 條）---


@pytest.mark.parametrize("name, quote_code", [
    ("TX", "TX00AM"),
    ("MTX", "MTX00AM"),
    ("TMF", "TM0000AM"),
])
def test_the_product_name_becomes_the_day_session_quote_code(name, quote_code):
    """設定檔寫使用者認得的名字，程式拿到的是報價代碼。

    期望值是 invest-os 2026-08-07 實測確認的代碼。⚠️ 一定要帶 `AM`：不帶的是全盤
    （含夜盤）代碼，用錯不會報錯、數值也完全合理，但每天都會靜默失準。
    """
    assert settings.build(_shipped_with("order.product", name)).product == quote_code


@pytest.mark.parametrize("value", ["TX00", "TX00AM", "微台", "tmf ", "", None, 1])
def test_an_unknown_product_stops_startup_and_lists_the_choices(value):
    """打錯商品會下到別的東西上，必須在啟動時擋下。"""
    with pytest.raises(settings.SettingsError, match="order.product") as exc:
        settings.build(_shipped_with("order.product", value))
    for choice in ("TX", "MTX", "TMF"):
        assert choice in str(exc.value)


# --- 口數（規格第 25、33、44 條）---


def test_the_shipped_lot_settings_are_the_specs_defaults():
    """規格第 44 條：每次進場 1 口、加碼上限 20 口。"""
    cfg = settings.load()
    assert (cfg.lots_per_entry, cfg.max_position) == (1, 20)


@pytest.mark.parametrize("value", [0, -1, 11, 1.5, "1", True, None])
def test_lots_per_entry_outside_one_to_ten_stops_startup(value):
    """**期交所限制每筆市價委託最多 10 口**（一般交易時段）。

    超過會被退單，而退單訊息看不出是這個原因——每次進場都失敗一次。
    `True` 也要擋：yaml 的 `yes` 是布林值，而 Python 的 True 剛好等於 1。
    """
    with pytest.raises(settings.SettingsError, match="order.lots_per_entry"):
        settings.build(_shipped_with("order.lots_per_entry", value))


def test_ten_lots_per_entry_is_the_cap_itself_and_is_accepted():
    assert settings.build(_shipped_with("order.lots_per_entry", 10)).lots_per_entry == 10


@pytest.mark.parametrize("value", [0, -5, "20", 20.0, False, None])
def test_a_max_position_that_is_not_a_positive_whole_number_stops_startup(value):
    with pytest.raises(settings.SettingsError, match="order.max_position"):
        settings.build(_shipped_with("order.max_position", value))


def test_a_max_position_smaller_than_one_entry_stops_startup():
    """上限比一次進場還小，等於永遠進不了場——那不會是使用者想要的。"""
    raw = _shipped_with("order.lots_per_entry", 3)
    raw["order"]["max_position"] = 2
    with pytest.raises(settings.SettingsError, match="order.max_position"):
        settings.build(raw)
