"""Discord 訊息的內容（純函式）。

規格要求訊息由純函式組裝：測試能直接斷言內容，送出的機制（`notifiers.discord`）另外一層。
用語照 GLOSSARY：做多／做空是方向，買進／賣出是委託。使用者在手機上看到的是中文，
不是 LONG。
"""

from __future__ import annotations

from datetime import date, datetime

from broker import DAY_TRADE_CODES, MTX_CODE, TMF_CODE, TX_CODE
import strategy

_PRODUCT_TEXT = {TX_CODE: "大台", MTX_CODE: "小台", TMF_CODE: "微台"}
_DIRECTION_TEXT = {strategy.LONG: "做多", strategy.SHORT: "做空"}
_SIDE_TEXT = {strategy.LONG: "買進", strategy.SHORT: "賣出"}
_HOLDING_TEXT = {strategy.LONG: "多單", strategy.SHORT: "空單"}


def entry(*, at: datetime, product: str, direction: str, condition: strategy.Condition,
          lots: int, price: float, total_lots: int, avg_price: float, simulated: bool,
          trigger: float | None = None) -> str:
    """一次進場成交（規格第 37 條）：方向、觸發條件、這次口數與成交價、累計口數與均價。

    每一則都帶累計：看最新的一則就知道目前的部位。模擬模式在最前面標「模擬」（規格第 42 條），
    使用者不會把它當成真的成交。突破進場另外寫出觸發價（`trigger`）：觸發價是大台的價格，
    成交價是交易商品的，兩個數字擺在一起，使用者才對得出是碰到哪個價位才進場。
    """
    head = "【模擬】" if simulated else ""
    how = "" if trigger is None else f"，突破進場（觸發價 {_points(trigger)}）"
    return (
        f"{head}PTS {_DIRECTION_TEXT[direction]}："
        f"{_SIDE_TEXT[direction]}{_PRODUCT_TEXT[product]} {lots} 口 @ {_points(price)}\n"
        f"觸發：{condition.value}{how}\n"
        f"累計：{_HOLDING_TEXT[direction]} {total_lots} 口，均價 {_points(avg_price)}\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def order_not_sent(*, at: datetime, product: str, direction: str, lots: int,
                   reason: str, held_lots: int) -> str:
    """委託沒有送出去（規格第 41、51 條）：**確定沒有部位**。盤中程式今天不再進場。

    刻意叫人**不必**看帳戶（invest-os 2026-08-25 的教訓）：這一則對應的是「確定沒有送出去」，
    叫人白跑幾趟券商 APP 之後，真的要看帳戶的那一則（`fill_unknown`）就沒人看了。
    要做的事是排除原因：保證金不足、憑證過期這類原因明天還在，明天會再被拒一次。
    `held_lots` 是部位檔裡今天稍早已確認的口數：它們照常了結，講出來使用者才不會起疑。
    """
    earlier = (f"今天稍早的{_HOLDING_TEXT[direction]} {held_lots} 口不受影響，照常了結。\n"
               if held_lots else "")
    return (
        "【要你處理】PTS 委託沒有送出去，今天不再進場\n"
        f"這次：{_SIDE_TEXT[direction]}{_PRODUCT_TEXT[product]} {lots} 口\n"
        f"原因：{reason}\n"
        f"{earlier}"
        "帳上不會因為這筆多出部位，不必看帳戶。"
        "請依原因處理（例如補足保證金、更新憑證），明天開盤前排除。\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def fill_unknown(*, at: datetime, product: str, direction: str, lots: int, order_seq: str,
                 known_filled: int, reason: str, held_lots: int, file_error: str | None) -> str:
    """委託送出去了，但不知道成交幾口（規格第 41、50 條）：**可能有部位**。

    所有告警裡最需要人立刻動手的一則，所以講的是「去做什麼」：先看帳戶，再看誰會了結。
    大台、小台標了當沖，券商 13:30 照樣強平。微台沒有當沖強平，要人平幾口取決於部位檔：

      * 已記成「不確定」（`file_error` 是 None）：出場程式看到它就**整個**不平（規格第 50 條），
        連部位檔裡確認過的 `held_lots` 口也不平——要人平的是兩者加起來。
      * 寫不進去：檔裡還是之前「已確認」的那一份，出場程式 13:25 照樣平那 `held_lots` 口，
        要人平的**只有這一筆**。這時叫人全部平掉，出場程式再平一次就是反向新倉過夜。

    `known_filled` 是已知**至少**成交的口數：對帳時至少要看到這麼多。
    """
    name = _PRODUCT_TEXT[product]
    holding = _HOLDING_TEXT[direction]
    seq = f"委託序號 {order_seq}" if order_seq else "沒拿到委託序號"
    known = f"，已知至少成交 {known_filled} 口" if known_filled else ""
    if file_error is None:
        on_file = "部位檔已記為「不確定」：盤中程式今天不再進場，13:25 的出場程式也不會自動平倉。"
    else:
        on_file = (f"部位檔寫不進「不確定」（{file_error}），還是之前的樣子："
                   "出場程式不知道這筆委託。盤中程式今天不再進場。")
    if product in DAY_TRADE_CODES:
        who_closes = f"{name}標了當沖，券商 13:30 會強平 PTS 今天的{name}部位。"
    elif file_error is None:
        held = f"（部位檔已確認的{holding} {held_lots} 口，加上這筆實際成交的）" if held_lots else ""
        who_closes = f"{name}沒有當沖強平：PTS 今天的{name}部位{held}要你自己在 13:30 前平掉。"
    else:
        held = f"出場程式只會平部位檔裡的{holding} {held_lots} 口，" if held_lots else ""
        who_closes = f"{name}沒有當沖強平：{held}這筆實際成交的要你自己在 13:30 前平掉。"
    return (
        "【要你處理】PTS 委託送出去了，但不知道成交幾口\n"
        f"這次：{_SIDE_TEXT[direction]}{name} {lots} 口（{seq}）{known}\n"
        f"原因：{reason}\n"
        f"{on_file}\n"
        f"請到券商 APP 確認這筆成交了幾口。{who_closes}\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def position_file_unreadable(*, day: date, reason: str) -> str:
    """部位檔讀不懂（規格第 38 條）。盤中程式這一天不進場。

    刻意不說「今天沒有部位」：真相是「可能有部位，但讀不到記錄」。要人做的事寫在前面：
    看帳戶。出場程式也讀同一個檔，同樣不會自動平倉——不講的話，使用者會以為 13:25
    有人替他收尾。
    """
    return (
        "【要你處理】PTS 部位檔讀不懂，盤中程式今天不進場\n"
        f"原因：{reason}\n"
        "今天 PTS 可能已經有部位：請到券商 APP 確認帳戶。"
        "13:25 的出場程式同樣讀不懂這個檔，不會替你平倉。\n"
        f"{day:%Y/%m/%d}"
    )


def fill_not_on_file(*, at: datetime, product: str, direction: str, lots: int, price: float,
                     order_seq: str, total_lots: int, on_file_lots: int, reason: str) -> str:
    """成交了，部位檔卻寫不進去（規格第 38、41 條：「部位還在」）。盤中程式今天不再進場。

    出場程式只照部位檔平倉，所以要人自己平的**只有沒記到的這幾口**——檔裡的口數照樣會被
    平掉。兩個數字都寫出來：只說「請平倉」的話，使用者把全部平掉，出場程式 13:25 再平一次
    檔裡的口數，就是反向新倉。
    """
    return (
        "【要你處理】PTS 部位還在，部位檔卻沒記到這次成交\n"
        f"這次：{_SIDE_TEXT[direction]}{_PRODUCT_TEXT[product]} {lots} 口 @ {_points(price)}"
        f"（委託序號 {order_seq}）\n"
        f"帳上 PTS 共{_HOLDING_TEXT[direction]} {total_lots} 口，部位檔只記到 {on_file_lots} 口\n"
        f"原因：{reason}\n"
        f"出場程式和盤中程式都只認部位檔，不知道這 {lots} 口：請到券商 APP 確認帳戶，"
        f"自行平掉這 {lots} 口。盤中程式今天不再進場。\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def _points(price: float) -> str:
    """台指期報價是整數點位：45150.0 寫成 45150。均價才可能有小數，留到兩位。"""
    return f"{round(price, 2):.2f}".rstrip("0").rstrip(".")
