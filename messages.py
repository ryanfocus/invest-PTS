"""Discord 訊息的內容（純函式）。

規格要求訊息由純函式組裝：測試能直接斷言內容，送出的機制（`notifiers.discord`）另外一層。
用語照 GLOSSARY：做多／做空是方向，買進／賣出是委託。使用者在手機上看到的是中文，
不是 LONG。
"""

from __future__ import annotations

from datetime import date, datetime

from broker import MTX_CODE, TMF_CODE, TX_CODE
import position_file
import strategy

_PRODUCT_TEXT = {TX_CODE: "大台", MTX_CODE: "小台", TMF_CODE: "微台"}
_DIRECTION_TEXT = {strategy.LONG: "做多", strategy.SHORT: "做空"}
_SIDE_TEXT = {strategy.LONG: "買進", strategy.SHORT: "賣出"}
# 平倉的委託：多單賣出、空單買進
_CLOSE_SIDE_TEXT = {strategy.LONG: "賣出", strategy.SHORT: "買進"}
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


# ── 出場程式（13:25）─────────────────────────────────────────────────────


def exit_position_file_unreadable(*, at: datetime, reason: str) -> str:
    """出場程式讀不懂部位檔（規格第 38 條）：一張單都不送。

    讀不懂就不知道是哪個商品，所以兩種處理都講：微台沒有人會替它平倉，大台、小台有券商的
    當沖強平。只寫「請平倉」的話，交易大台的人會自己平一次、券商 13:30 再平一次。
    """
    return (
        "【要你處理】PTS 部位檔讀不懂，出場程式沒有平倉\n"
        f"原因：{reason}\n"
        "PTS 今天可能還有部位：請到券商 APP 確認帳戶。"
        "微台要自行平掉（日盤 13:45 收盤）；大台、小台標了當沖，券商 13:30 會強平。\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def exit_uncertain(*, at: datetime, record: position_file.PositionRecord,
                   lookup: str | None = None) -> str:
    """有一筆委託送出去了、卻不知道成交幾口（規格第 50 條）：出場程式一張單都不再送。

    兩種來源：部位檔早就記著一筆不確定的委託（進場或出場），或出場程式自己的平倉單剛剛
    不確定、向券商查成交紀錄也沒能確認全部成交——那時 `lookup` 是查到的結果（規格要求
    附上券商紀錄顯示的口數）。

    「委託送出去了，但…」：帳上**可能**比部位檔多（進場那筆成交了）或少（平倉那筆成交了）。
    兩個數字都寫出來——檔裡確定的口數與不確定那一筆的委託口數——使用者對帳戶時才知道
    要看到的範圍。
    """
    u = record.uncertain
    stage = "進場" if u.stage == position_file.ENTRY else "平倉"
    side = (_SIDE_TEXT if u.stage == position_file.ENTRY else _CLOSE_SIDE_TEXT)[record.direction]
    looked_up = "" if lookup is None else f"券商的成交紀錄：{lookup}\n"
    return (
        "【要你處理】PTS 委託送出去了，但不知道成交幾口，出場程式不再送單\n"
        f"不確定的委託：{stage}{side}{_PRODUCT_TEXT[record.product]} {u.requested_lots} 口"
        f"（委託序號 {u.order_seq or '沒有拿到'}），已知至少成交 {u.known_filled} 口\n"
        f"{looked_up}"
        f"部位檔記到的部位（不含這一筆）：{_HOLDING_TEXT[record.direction]} {record.open_lots} 口\n"
        f"{_check_account(record.product)}\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def _check_account(product: str) -> str:
    """不確定時要人做的事，依商品而定（ADR-0002）。

    大台、小台叫人自己平的話，會和券商 13:30 的當沖強平撞在一起：兩邊都平，多平的
    就是反向新倉。所以只請他在強平之後確認。
    """
    if product == TMF_CODE:
        return "請到券商 APP 確認帳戶，自行平掉 PTS 的微台部位（日盤 13:45 收盤）。"
    return ("大台、小台標了當沖，券商 13:30 會強平：13:30 之後請到券商 APP 確認帳戶裡"
            "已經沒有 PTS 的部位。")


def exit_incomplete(*, at: datetime, record: position_file.PositionRecord, reason: str,
                    night: bool = False) -> str:
    """微台沒有平完（規格第 34、55 條）：「部位還在」，確定還有這幾口。

    要人送的單寫完整（買賣別、口數、下單代碼）：只寫「請平倉」的話，使用者可能把帳上同商品
    的部位（自己的、OS 的）一起平掉。口數是**還沒平的**——已經成交的出場不算進去，
    叫他平全部的話，多平的就是反向新倉。`night`：日盤已經收盤，只能在夜盤處理。
    """
    lots = record.open_lots
    when = "請在夜盤" if night else "請"
    until = "。" if night else "，日盤 13:45 收盤。"
    return (
        f"【要你處理】PTS 部位還在：微台平倉沒有完成，還剩{_HOLDING_TEXT[record.direction]} "
        f"{lots} 口\n"
        f"原因：{reason}\n"
        f"{when}自行{_CLOSE_SIDE_TEXT[record.direction]}{_PRODUCT_TEXT[record.product]} {lots} 口"
        f"（{record.order_code}）{until}\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def exit_not_on_file(*, at: datetime, record: position_file.PositionRecord,
                     on_file: position_file.PositionRecord, reason: str) -> str:
    """出場程式平倉了，部位檔卻寫不進去。平倉照樣做完了，要人做的只有一件事：**不要重跑**。

    出場程式只認部位檔：檔裡還記著沒平的口數，再執行一次就會把已經平掉的再平一次。
    """
    exited = record.entered_lots - record.open_lots
    on_file_exited = on_file.entered_lots - on_file.open_lots
    lost = ""
    if record.uncertain is not None and record.uncertain != on_file.uncertain:
        lost = (f"部位檔也沒記到那筆不確定的平倉委託（委託序號 "
                f"{record.uncertain.order_seq or '沒有拿到'}）\n")
    return (
        "【要你處理】PTS 部位檔沒記到出場程式這次的平倉\n"
        f"實際：平倉 {exited} 口，還剩{_HOLDING_TEXT[record.direction]} {record.open_lots} 口；"
        f"部位檔記的是平倉 {on_file_exited} 口\n"
        f"{lost}"
        f"原因：{reason}\n"
        "不要再執行出場程式：它只認部位檔，會把已經平掉的口數再平一次，開出反向新倉。\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def exit_crashed(*, at: datetime, reason: str) -> str:
    """出場程式本身出錯中止。不知道做到哪裡，只能請人看帳戶。"""
    return (
        "【要你處理】PTS 出場程式意外中止\n"
        f"原因：{reason}\n"
        "今天 PTS 的微台部位可能沒有平完：請到券商 APP 確認帳戶，自行平掉 PTS 還在的部位"
        "（日盤 13:45 收盤）。大台、小台由券商 13:30 當沖強平。\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )
