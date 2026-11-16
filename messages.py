"""Discord 訊息的內容（純函式），以及收工指令印在主控台的內容（`stop_today_*`）。

規格要求訊息由純函式組裝：測試能直接斷言內容，送出的機制（`notifiers.discord`）另外一層。
用語照 GLOSSARY：做多／做空是方向，買進／賣出是委託。使用者在手機上看到的是中文，
不是 LONG。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from broker import MTX_CODE, TMF_CODE, TX_CODE
from calendar_tw import DAY_SESSION_CLOSE
import closing
import handoff
from handoff import DAY_TRADE_CLOSE, SETTLEMENT_CLOSE, Handoff, Who
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


def fill_unknown(*, at: datetime, on_file: position_file.PositionRecord, lots: int,
                 order_seq: str, known_filled: int, reason: str, file_error: str | None) -> str:
    """委託送出去了，但不知道成交幾口（規格第 41、50 條）：**可能有部位**。

    所有告警裡最需要人立刻動手的一則，所以講的是「去做什麼」：先看帳戶，再看誰會了結
    （`handoff.decide`，看的是部位檔裡的 `on_file`——出場程式只認它）。
    大台、小台標了當沖，券商 13:30 照樣強平。微台沒有當沖強平：部位檔記著「不確定」，出場程式
    看到它就**整個**不平（規格第 50 條），連部位檔裡確認過的口數也不平——要人平的是兩者加起來。

    部位檔在送單**之前**就記成「不確定」了（`intraday._Orders.enter`）。`file_error` 不是 None
    代表之後要補上這一筆的委託序號與已知口數時寫不進去：`on_file` 是送單前記下的「不確定」，
    少了序號，出場程式查不了、照樣不平倉——序號在這則告警裡，對帳用它。

    `known_filled` 是已知**至少**成交的口數：對帳時至少要看到這麼多。

    出場程式查得到時（`Who.EXIT_LOOKS_UP`），**請人等 13:25**：它會先用序號查券商紀錄，查到全部
    成交就照常平倉；查不到才再發一則。要人在那之前**不要自己平**——自己平了，13:25 查到成交又平
    一次，就是反向新倉。查不了的時候，要人自己平的那一句寫成**要送的單**：買賣別、商品，口數寫成
    算式與範圍（`handoff.entry_lots`）。帳戶 APP 看到的是淨部位，含使用者自己的長期部位與 OS 的
    口數，只寫「平掉 PTS 的部位」的話，使用者分不出要平幾口。
    """
    product, direction = on_file.product, on_file.direction
    name = _PRODUCT_TEXT[product]
    close = _CLOSE_SIDE_TEXT[direction]
    seq = f"委託序號 {order_seq}" if order_seq else "沒拿到委託序號"
    known = f"，已知至少成交 {known_filled} 口" if known_filled else ""
    missing = "" if file_error is None else f"（這筆的委託序號沒寫進去：{file_error}）"
    count = handoff.entry_lots(on_file.open_lots, requested=lots, known_filled=known_filled)
    h = handoff.decide(on_file, at=at, exit_done=False)
    if h.who is Who.EXIT_LOOKS_UP:
        on_file_text = "部位檔已記為「不確定」：盤中程式今天不再進場。"
        todo = (_exit_looked_up() if h.exit_started
                else f"{name}不必現在處理：{_exit_looks_up(name, close, count.high)}")
    else:
        on_file_text = (f"部位檔已記為「不確定」{missing}：盤中程式今天不再進場，"
                        "13:25 的出場程式也不會自動平倉。")
        if h.who is Who.BROKER:
            who_closes = _broker_closes_today(name)
        else:
            if count.held:
                how_many = (f"部位檔已確認的{_HOLDING_TEXT[direction]} {count.held} 口，"
                            f"加上這筆實際成交的口數，也就是 {_span(count.low, count.high)}")
            else:
                how_many = f"這筆實際成交幾口就平幾口（{_span(count.low, count.high)}）"
            who_closes = f"{name}沒有當沖強平。{_close_yourself(h, f'{close}{name}：{how_many}')}"
        todo = f"請到券商 APP 確認這筆成交了幾口。{who_closes}"
    return (
        "【要你處理】PTS 委託送出去了，但不知道成交幾口\n"
        f"這次：{_SIDE_TEXT[direction]}{name} {lots} 口（{seq}）{known}\n"
        f"原因：{reason}\n"
        f"{on_file_text}\n"
        f"{todo}\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def _exit_looks_up(name: str, close: str, lots: int, *, again: bool = False) -> str:
    """`Who.EXIT_LOOKS_UP`：等 13:25，不要自己平。`lots` 是查到全部成交時 13:25 要平的口數。"""
    how = "再" if again else "先"
    return (f"13:25 的出場程式會{how}用委託序號向券商查這筆成交了幾口，查到全部成交就照常平倉"
            f"（{close}{name} {lots} 口）；查不到確定的口數會發一則 Discord，寫明要平幾口。"
            "在那之前不要自己平：不然 13:25 會再平一次，開出反向新倉。")


def _exit_looked_up() -> str:
    """`Who.EXIT_LOOKS_UP`、但 13:25 已經到了（`Handoff.exit_started`）：出場程式已經起來，它要是
    查不到，已經（或就要）發 Discord 講要平幾口。不能再說「等 13:25」。"""
    return f"13:25 起由出場程式用委託序號查這筆、查到全部成交就平倉。{_exit_took_over()}"


def _close_yourself(h: Handoff, order: str) -> str:
    """要人自己平微台的那一句（`Who.USER`），期限照 `handoff.deadline`。`order` 是要送的單。

    結算日的近月 13:30 就停止交易，一般日到日盤 13:45 收盤；過了一般日的期限只能在夜盤平。
    結算日寫成 13:45 的話，使用者以為還有時間，其實 13:30 之後就賣不掉了。結算日過了 13:30
    就不是要人平了（`Who.EXCHANGE`）：不能再平，由交易所了結。
    """
    if h.who is Who.EXCHANGE:
        return (f"今天是結算日，近月 {SETTLEMENT_CLOSE:%H:%M} 已經停止交易，不能再平：PTS 還在的"
                "微台部位由交易所以最後結算價現金了結。結算後請到券商 APP 確認帳戶。")
    if h.settles_today:
        return (f"請在 {SETTLEMENT_CLOSE:%H:%M} 前自行{order}：今天是結算日，近月 "
                f"{SETTLEMENT_CLOSE:%H:%M} 停止交易，之後還在的口數由交易所以最後結算價現金了結。")
    if h.passed:
        return f"日盤 {DAY_SESSION_CLOSE:%H:%M} 已經收盤，請在夜盤自行{order}。"
    return f"請在日盤 {DAY_SESSION_CLOSE:%H:%M} 收盤前自行{order}。"


def _broker_closes_today(name: str) -> str:
    """大台、小台進場時標了當沖，券商 13:30 強平（ADR-0002）：不確定或沒記到的時候，也不必自己平。"""
    return f"{name}標了當沖，券商 {DAY_TRADE_CLOSE:%H:%M} 會強平 PTS 今天的{name}部位。"


def position_file_unreadable(*, day: date, reason: str, logs: handoff.FillLogs) -> str:
    """部位檔讀不懂（規格第 38 條）。盤中程式這一天不進場。

    刻意不說「今天沒有部位」：真相是「可能有部位，但讀不到記錄」。出場程式也讀同一個檔，
    同樣不會自動平倉——不講的話，使用者會以為 13:25 有人替他收尾。讀不懂就不知道是哪個商品、
    今天是不是結算日，兩種期限都講（`_close_micro_any_day`）；口數 PTS 自己也不知道，就講從
    哪裡算（`_count_from_logs`：13:20 前就可能被收工指令 `--close` 平過一部分）。
    `day` 而不是時刻：啟動時還沒有任何事件，盤中程式也只在日盤收盤前跑。
    """
    return (
        "【要你處理】PTS 部位檔讀不懂，盤中程式今天不進場\n"
        f"原因：{reason}\n"
        "今天 PTS 可能已經有部位。13:25 的出場程式同樣讀不懂這個檔，不會替你平倉："
        f"{_close_micro_any_day(False)}；大台、小台標了當沖，券商 {DAY_TRADE_CLOSE:%H:%M} 會強平。\n"
        f"{_count_from_logs(logs)}\n"
        f"{day:%Y/%m/%d}"
    )


def _count_from_logs(logs: handoff.FillLogs) -> str:
    """PTS 自己也不知道口數時（部位檔讀不懂、出場程式中止），要人平幾口從哪裡算（`handoff.FillLogs`）。

    帳戶 APP 看到的是淨部位，含使用者自己的長期部位與 OS 的口數，不能拿來當 PTS 的口數。
    PTS 的每一筆成交都寫在 log 裡：盤中程式的「進場成交」帶著累計口數，平倉的每一筆
    「平倉成交」帶著口數（查詢確認的也是這個字）。
    """
    closes = " 與 ".join(logs.closes)
    return (f"PTS 的口數：{logs.entry} 最後一筆「進場成交」的累計口數，減掉 {closes} 裡每一筆"
            "「平倉成交」的口數（有「不知道成交幾口」的委託時，另外到券商 APP 用委託序號查）。"
            "只平這個口數。")


def _span(low: int, high: int) -> str:
    """口數或口數的範圍：確定時只寫一個數字。"""
    return f"{low} 口" if low == high else f"{low}～{high} 口"


def day_settings_lost(*, day: date, reason: str, live: bool, log: str) -> str:
    """當天的設定記錄（`day_settings`）讀不到，今天第一次啟動時的自動下單開關不知道了。

    改設定最快下一個交易日才生效：照這一次的設定檔就可能今天中途開始送真單（或停掉）。所以只有
    部位檔裡已經有今天的真實成交才照常自動下單（`live`），否則今天其餘時間只模擬。講清楚今天
    怎麼跑、下一個交易日恢復正常；通常不必處理。`day` 而不是時刻：啟動時還沒有任何事件。
    """
    rest = "照常自動下單" if live else "只模擬"
    how = ("部位檔裡已經有今天的真實成交：今天其餘時間照常自動下單。" if live
           else "今天沒有真實成交：今天其餘時間只模擬，不送任何委託。")
    return (
        f"【要你處理】PTS 今天第一次啟動時的設定記錄讀不到，今天其餘時間{rest}\n"
        f"原因：{reason}\n"
        "改設定最快下一個交易日才生效，今天第一次啟動時的自動下單開關卻查不到。為了不在今天中途"
        f"改成真的下單，只有部位檔裡已經有今天的真實成交才照常自動下單。{how}\n"
        "每次進場口數、加碼上限照這一次讀到的設定檔。下一個交易日照設定檔正常啟動，不必處理；"
        f"想知道原因請看 {log}。\n"
        f"{day:%Y/%m/%d}"
    )


def login_failed(*, day: date, reason: str, marker: str) -> str:
    """盤中程式登入失敗（`broker.LoginFailed`：帳密、憑證、群益元件），今天不再自動登入。

    當下就講：等 13:25 出場程式的「盤中程式今天沒有跑」才知道的話，當天的進場時段已經過完了。
    講原因，也講排除之後怎麼讓它再登入（刪掉 `marker`，`paths.shown_state`）；以及為什麼不自己
    重試——帳密錯了還一直試，會把使用者自己也在用的帳號鎖住。`day` 而不是時刻：登入時還沒有
    任何事件（見 `intraday` 模組說明「時間」）。
    """
    return (
        "【要你處理】PTS 盤中程式登入失敗，今天不再自動登入\n"
        f"原因：{reason}\n"
        "今天不判斷進場，也不記錄原始行情；已經有的部位照常由 13:25 的出場程式處理。帳密錯了還一直"
        "重試，會把帳號鎖住，所以盤中程式今天不再自己登入。\n"
        f"排除原因（例如改好 .env 的密碼、更新憑證）之後，刪掉 {marker}，一分鐘內會再登入。\n"
        f"{day:%Y/%m/%d}"
    )


def fill_not_on_file(*, at: datetime, record: position_file.PositionRecord,
                     on_file: position_file.PositionRecord, fill: position_file.Fill,
                     reason: str) -> str:
    """成交了，部位檔卻寫不進去（規格第 38、41 條：「部位還在」）。盤中程式今天不再進場。

    `record` 是帳上的（含這一筆 `fill`），`on_file` 是檔裡的：停在送單**之前**記下的「不確定」
    （`intraday._Orders.enter`），沒有委託序號。出場程式看到它就整個不平倉，連檔裡已確認的口數也
    不平（規格第 50 條，`handoff.decide`）。所以微台要人自己平的是 PTS 今天**全部**的口數，不只
    沒記到的這幾口——只叫人平這幾口的話，檔裡那幾口沒人平，帶著過夜。帳上與檔裡的兩個數字都
    寫出來，對帳時才知道差在哪裡。大台、小台標了當沖，券商 13:30 照樣強平。
    """
    name = _PRODUCT_TEXT[record.product]
    direction = record.direction
    total = handoff.lots_to_close(record).high
    h = handoff.decide(on_file, at=at, exit_done=False)
    if h.who is Who.BROKER:
        who_closes = _broker_closes_today(name)
    else:
        order = f"{_CLOSE_SIDE_TEXT[direction]}{name} {total} 口（PTS 今天全部的口數）"
        who_closes = f"{name}沒有當沖強平。{_close_yourself(h, order)}"
    return (
        "【要你處理】PTS 部位還在，部位檔卻沒記到這次成交\n"
        f"這次：{_SIDE_TEXT[direction]}{name} {fill.lots} 口 @ {_points(fill.price)}"
        f"（委託序號 {fill.order_seq}）\n"
        f"帳上 PTS 共{_HOLDING_TEXT[direction]} {total} 口，部位檔只記到 {on_file.open_lots} 口，"
        "而且停在送單前記下的「不確定」\n"
        f"原因：{reason}\n"
        "13:25 的出場程式看到「不確定」不會自動平倉，盤中程式今天不再進場。"
        f"請到券商 APP 確認帳戶。{who_closes}\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def quote_outage(*, at: datetime, trade_at: datetime | None, counts_at: datetime | None,
                 outage: timedelta) -> str:
    """盤中報價中斷（規格第 38、53 條）。

    兩路各寫最後一次收到的時刻：哪一路斷了，使用者查原因時才有方向。今天還沒收到大台的成交，
    可能只是臨時休市（颱風假）——程式到 13:20 之前分不出來，所以講明那樣的話不必處理，
    免得使用者在休市日被叫起來查電腦。

    斷線之後群益會不會自己把報價接回來，還沒實機確認過（盤中程式不自己重新連線）。所以也講
    退路：先關掉、再重新執行——重啟會從部位檔與原始行情記錄接著做。「先關掉」要講：兩個盤中
    程式同時跑，同一分鐘就會進兩次場。
    """
    trade_text = "今天還沒有" if trade_at is None else f"{trade_at:%H:%M:%S}"
    counts_text = "今天還沒有" if counts_at is None else f"{counts_at:%H:%M:%S}"
    day_off = "今天如果臨時休市（例如颱風假），不必處理。\n" if trade_at is None else ""
    return (
        f"【要你處理】PTS 盤中報價中斷超過 {int(outage.total_seconds() // 60)} 分鐘\n"
        f"最後收到的大台成交：{trade_text}；累計成交筆數：{counts_text}\n"
        "報價恢復之前不會進場；恢復之後盤中程式自己接著判斷，不另外通知。已經有的部位照常了結。\n"
        f"{day_off}"
        "請檢查這台電腦的網路與群益的報價連線。網路正常、報價卻一直沒有回來的話，"
        "先關掉盤中程式再重新執行：它會從部位檔與原始行情記錄接著做。\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def entries_not_fully_judged(*, at: datetime, no_history: bool, no_settlement_day: bool,
                             no_contract_for: str | None, log: str) -> str:
    """到 08:50（條件 1、2 開始判斷）還有開盤前該查好的東西查不到（`intraday._Lookups`）。

    講清楚今天**哪些條件判斷不了**：不講的話，使用者分不出「今天沒有訊號」和「今天程式是瞎的」。
    通常什麼都不必做（多半是群益主機一時查不到，盤中程式每分鐘再查），所以先講這一點，再指出
    想查原因時去哪裡看（`log`）。`no_contract_for` 是查不到近月合約的交易商品（自動下單打開時）。
    """
    missing = []
    if no_history:
        missing.append("查不到前一個交易日的大台 1 分 K（昨收、RSI 暖身）：條件 1～4 都判斷不了。")
    if no_settlement_day:
        missing.append("查不到商品清單裡大台近月的結算日：不知道今天是不是結算週，條件 3、4 停用。")
    if no_contract_for is not None:
        missing.append(f"查不到{_PRODUCT_TEXT[no_contract_for]}的近月合約（下單代碼）："
                       "條件成立也不能下單。")
    return (
        f"【要你處理】PTS 到 {strategy.EARLY_START:%H:%M} 還有資料查不到，今天的進場判斷不完整\n"
        + "".join(f"{line}\n" for line in missing)
        + "盤中程式每分鐘再查一次，查到之後就照常判斷，不另外通知。原始行情照常記錄，"
        "已經有的部位照常了結。\n"
        f"通常不必處理（多半是群益主機一時查不到）；想知道原因請看 {log}。\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def _points(price: float) -> str:
    """台指期報價是整數點位：45150.0 寫成 45150。均價才可能有小數，留到兩位。"""
    return f"{round(price, 2):.2f}".rstrip("0").rstrip(".")


# ── 出場程式（13:25）─────────────────────────────────────────────────────


def exit_position_file_unreadable(*, at: datetime, reason: str, logs: handoff.FillLogs) -> str:
    """出場程式讀不懂部位檔（規格第 38 條）：一張單都不送。

    讀不懂就不知道是哪個商品，所以兩種處理都講（`_unknown_product`）：微台沒有人會替它平倉，
    大台、小台有券商的當沖強平。只寫「請平倉」的話，交易大台的人會自己平一次、券商 13:30 再平一次。
    口數也不知道，講從哪裡算（`_count_from_logs`：出場程式與收工指令的 log 都要減）。
    """
    return (
        "【要你處理】PTS 部位檔讀不懂，出場程式沒有平倉\n"
        f"原因：{reason}\n"
        f"PTS 今天可能還有部位：請到券商 APP 確認帳戶。{_unknown_product(at)}\n"
        f"{_count_from_logs(logs)}\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def _unknown_product(at: datetime) -> str:
    """不知道是哪個商品時（部位檔讀不懂），兩種商品各由誰了結的那一句。"""
    closed = handoff.day_closed(at)
    tx = (f"由券商 {DAY_TRADE_CLOSE:%H:%M} 強平" if closed
          else f"券商 {DAY_TRADE_CLOSE:%H:%M} 會強平")
    return f"{_close_micro_any_day(closed)}；大台、小台標了當沖，{tx}。"


def _close_micro_any_day(closed: bool) -> str:
    """不知道今天是不是結算日時（部位檔讀不懂、出場程式當掉），要人平微台的那一句。

    兩種期限都講：一般日到日盤 13:45 收盤，結算日到期的近月 13:30 就停止交易。只寫 13:45
    的話，結算日的使用者以為還有時間；只寫 13:30 的話，一般日又把時間講短了。
    `closed`：日盤已經收盤，只剩夜盤（規格第 55 條）。
    """
    if closed:
        return ("日盤 13:45 已經收盤，微台請在夜盤自行平掉（今天如果是結算日，到期的近月已經由"
                "交易所以最後結算價現金了結，不必平）")
    return ("微台要自行平掉（日盤 13:45 收盤；今天如果是結算日，到期的近月 13:30 就停止交易，"
            "之後由交易所以最後結算價現金了結）")


def exit_uncertain(*, at: datetime, record: position_file.PositionRecord,
                   lookup: str | None = None) -> str:
    """有一筆委託送出去了、卻不知道成交幾口（規格第 50 條）：出場程式一張單都不再送。

    兩種來源：部位檔早就記著一筆不確定的委託（進場或出場），或出場程式自己的平倉單剛剛
    不確定、向券商查成交紀錄也沒能確認全部成交——那時 `lookup` 是查到的結果（規格要求
    附上券商紀錄顯示的口數）。

    「委託送出去了，但…」：帳上**可能**比部位檔多（進場那筆成交了）或少（平倉那筆成交了）。
    兩個數字都寫出來——檔裡確定的口數與不確定那一筆的委託口數——使用者對帳戶時才知道
    要看到的範圍。要人做什麼看告警這一刻（`_check_account`）。
    """
    order, held = _uncertain_order_lines(record)
    looked_up = "" if lookup is None else f"券商的成交紀錄：{lookup}\n"
    return (
        "【要你處理】PTS 委託送出去了，但不知道成交幾口，出場程式不再送單\n"
        f"{order}\n"
        f"{looked_up}"
        f"{held}\n"
        f"{_check_account(record, at)}\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def _uncertain_order_lines(record: position_file.PositionRecord) -> tuple[str, str]:
    """部位檔記著一筆不確定的委託時，講那一筆與檔裡確定的部位的兩行（不含換行）。

    兩個數字都寫出來：檔裡確定的口數與不確定那一筆的委託口數，使用者對帳戶時才知道要看到的
    範圍（`exit_uncertain`、`exit_missed`）。
    """
    u = record.uncertain
    stage = "進場" if u.stage == position_file.ENTRY else "平倉"
    side = (_SIDE_TEXT if u.stage == position_file.ENTRY else _CLOSE_SIDE_TEXT)[record.direction]
    return (f"不確定的委託：{stage}{side}{_PRODUCT_TEXT[record.product]} {u.requested_lots} 口"
            f"（{_seq_text(u.order_seq)}），已知至少成交 {u.known_filled} 口",
            f"部位檔記到的部位（不含這一筆）：{_HOLDING_TEXT[record.direction]} "
            f"{record.open_lots} 口")


def _seq_text(order_seq: str) -> str:
    """委託序號；送出去卻沒拿到序號時照實講。"""
    return f"委託序號 {order_seq or '沒有拿到'}"


def _uncertain_close_order(record: position_file.PositionRecord) -> str:
    """部位檔記著一筆不確定的委託時，要人送的平倉單：買賣別、商品、下單代碼，口數寫成算式與範圍
    （`handoff.lots_to_close`）。

    帳戶 APP 看到的是淨部位（含使用者自己的、OS 的），只寫「平掉 PTS 的部位」的話分不出要平
    幾口。進場那一筆成交了幾口就多平幾口；平倉那一筆成交了幾口就少平幾口。
    """
    n = handoff.lots_to_close(record)
    how = "加上" if n.adds else "減掉"
    return (f"{_CLOSE_SIDE_TEXT[record.direction]}{_PRODUCT_TEXT[record.product]}"
            f"（{record.order_code}）：部位檔的{_HOLDING_TEXT[record.direction]} {n.held} 口，"
            f"{how}{_seq_text(n.uncertain.order_seq)} 實際成交的口數，也就是 {_span(n.low, n.high)}"
            "（到券商 APP 用委託序號查這筆成交了幾口）")


def _check_account(record: position_file.PositionRecord, at: datetime) -> str:
    """不確定時要人做的事（`handoff.decide`：出場程式不會再送單）。

    大台、小台叫人自己平的話，會和券商 13:30 的當沖強平撞在一起：兩邊都平，多平的
    就是反向新倉。所以只請他在強平之後確認。微台寫明要送的單（`_uncertain_close_order`），
    期限見 `_close_yourself`。
    """
    h = handoff.decide(record, at=at, exit_done=True)
    if h.who is Who.BROKER:
        return (f"大台、小台標了當沖，券商 {DAY_TRADE_CLOSE:%H:%M} 會強平："
                f"{DAY_TRADE_CLOSE:%H:%M} 之後請到券商 APP 確認帳戶裡已經沒有 PTS 的部位。")
    return _close_yourself(h, _uncertain_close_order(record))


def exit_incomplete(*, at: datetime, record: position_file.PositionRecord, reason: str) -> str:
    """微台沒有平完（規格第 34、55 條）：「部位還在」，確定還有這幾口。

    要人送的單寫完整（買賣別、口數、下單代碼）：只寫「請平倉」的話，使用者可能把帳上同商品
    的部位（自己的、OS 的）一起平掉。口數是**還沒平的**——已經成交的出場不算進去，
    叫他平全部的話，多平的就是反向新倉。

    期限看告警這一刻（`handoff.decide`）：一般日是日盤 13:45 收盤，過了只能在夜盤平；結算日的
    近月 13:30 就停止交易，過了就不能再平，由交易所以最後結算價現金了結。

    不講「不要重跑」：部位檔雖然還記著這幾口，出場程式同一天只處理一次（`exit_program.done_path`），
    使用者自己平掉之後再被執行也不會照檔再平。何況使用者多半人在外面，碰不到那台電腦。
    """
    lots = record.open_lots
    side = _CLOSE_SIDE_TEXT[record.direction]
    h = handoff.decide(record, at=at, exit_done=True)
    if h.who is Who.EXCHANGE:
        todo = (f"{record.order_code} 今天結算，{SETTLEMENT_CLOSE:%H:%M} 已經停止交易，不能再平：這 "
                f"{lots} 口由交易所以最後結算價現金了結，不會留到明天。結算後請到券商 APP 確認帳戶裡"
                f"已經沒有這 {lots} 口。")
    else:
        todo = _close_yourself(
            h, f"{side}{_PRODUCT_TEXT[record.product]} {lots} 口（{record.order_code}）")
    return (
        f"【要你處理】PTS 部位還在：微台平倉沒有完成，還剩{_HOLDING_TEXT[record.direction]} "
        f"{lots} 口\n"
        f"原因：{reason}\n"
        f"{todo}\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def exit_missed(*, at: datetime, record: position_file.PositionRecord) -> str:
    """出場程式在部位的那一天沒有跑完，`at` 這一次執行才發現（ticket 10：排程補跑落在隔天、
    週末，或那天跑到一半停了）。「部位還在」：部位檔記著這幾口還沒平。

    那一天的日盤早就收了，出場程式不送單，所以要人做的事講完整（買賣別、口數、下單代碼，
    理由見 `exit_incomplete`）。但他可能已經在夜盤、或看到別的告警自己平掉了——出場程式不知道，
    所以先請他確認帳戶。合約可能在這段時間結算掉了，最後交易日也一併講。
    不講「不要重跑」：出場程式對過去的交易日從來不送單。

    部位檔記著一筆不確定的委託時，改用「委託送出去了，但…」的說法（規格第 41 條），兩個數字
    都寫出來（理由見 `exit_uncertain`），只請他確認帳戶、自己平掉還在的。
    """
    day = f"{record.trading_day:%m/%d}"
    settled = (f"{record.order_code} 的最後交易日是 {record.last_trading_day:%m/%d}：過了那天 13:30 "
               "還在的口數由交易所以最後結算價現金了結，不必再平。")
    if record.uncertain is not None:
        order, held = _uncertain_order_lines(record)
        return (
            f"【要你處理】PTS 委託送出去了，但不知道成交幾口：{day} 的微台沒有平倉\n"
            f"{order}\n"
            f"{held}\n"
            f"{_missed_reason(day)}\n"
            "請先到券商 APP 確認帳戶（你可能已經自己平掉了），還在的話請自行"
            f"{_uncertain_close_order(record)}。{settled}\n"
            f"{at:%Y/%m/%d %H:%M:%S}"
        )
    lots = record.open_lots
    side = _CLOSE_SIDE_TEXT[record.direction]
    return (
        f"【要你處理】PTS 部位還在：{day} 的微台沒有平倉，還剩{_HOLDING_TEXT[record.direction]} "
        f"{lots} 口\n"
        f"{_missed_reason(day)}\n"
        "請先到券商 APP 確認帳戶（你可能已經自己平掉了），還在的話請自行"
        f"{side}{_PRODUCT_TEXT[record.product]} {lots} 口（{record.order_code}）。{settled}\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def exit_shortfall(closed: closing.Closed, *, started: datetime) -> str:
    """出場程式平不完的原因（`closing.Short`）。`started`：出場程式開始處理今天部位的時刻。

    過了期限：日盤收盤（之後只能在夜盤平），或結算日近月停止交易（之後由交易所以最後結算價
    現金了結，損益和回測有落差，規格第 35 條）。兩種都還剩口數，都要告警——結算日的「交易所會
    了結」是期交所規則，PTS 還沒有實機樣本，靠人在結算後確認。
    """
    if closed.short is not closing.Short.OUT_OF_TIME:
        return _shortfall(closed)
    if closed.record.settles_today:
        close = f"近月 {SETTLEMENT_CLOSE:%H:%M} 停止交易"
        stopped = f"今天結算的近月 {SETTLEMENT_CLOSE:%H:%M} 已經停止交易"
    else:
        close = f"日盤 {DAY_SESSION_CLOSE:%H:%M} 收盤"
        stopped = f"日盤 {DAY_SESSION_CLOSE:%H:%M} 已經收盤"
    if closed.rounds:
        return _not_all_sent(closed) + f"重試到{close}仍未完成"
    return f"出場程式 {started:%H:%M} 才執行，{stopped}，沒有送出平倉單"


def stop_shortfall(closed: closing.Closed) -> str:
    """收工指令平不完的原因（`closing.Short`）：它只送到 13:24，出場程式 13:25 啟動之前。"""
    if closed.short is not closing.Short.OUT_OF_TIME:
        return _shortfall(closed)
    until = handoff.STOP_COMMAND_UNTIL
    why = f"收工指令只送到 {handoff.EXIT_START:%H:%M} 出場程式啟動之前"
    if not closed.rounds:
        return f"已經過了 {until:%H:%M}，沒有送出平倉單（{why}）"
    return _not_all_sent(closed) + f"重試到 {until:%H:%M} 仍未完成（{why}）"


def _shortfall(closed: closing.Closed) -> str:
    """登入失敗、重試用完：誰平都一樣的說法。"""
    if closed.short is closing.Short.LOGIN_FAILED:
        return closed.problem
    if closed.problem:
        return f"平倉單重試 {closing.RETRIES} 次都沒能送出（最後一次：{closed.problem}）"
    return f"市價 IOC 沒有全部成交，重試 {closing.RETRIES} 次仍有剩餘"


def _not_all_sent(closed: closing.Closed) -> str:
    return (f"平倉單沒能送出（最後一次：{closed.problem}），" if closed.problem
            else "市價 IOC 沒有全部成交，")


def exit_missed_unreadable(*, at: datetime, day: date, reason: str,
                           logs: handoff.FillLogs) -> str:
    """出場程式在 `day` 沒有跑完，那天的部位檔又讀不懂（規格第 38 條）：不知道那天留下什麼。

    讀不懂就不知道是哪個商品：大台、小台那天 13:30 已經被券商強平，微台沒有人平。
    口數從那天的 log 算（`_count_from_logs`）。
    """
    return (
        f"【要你處理】PTS {day:%m/%d} 的部位檔讀不懂，出場程式沒有平倉\n"
        f"{_missed_reason(f'{day:%m/%d}')}。部位檔：{reason}\n"
        "PTS 那天可能留下微台部位：請到券商 APP 確認帳戶，還在的話請自行平掉。大台、小台標了當沖，"
        f"那天 {DAY_TRADE_CLOSE:%H:%M} 已經由券商強平。\n"
        f"{_count_from_logs(logs)}\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def _missed_reason(day: str) -> str:
    """出場程式那天為什麼沒平：排程補跑落在隔天或週末、那天跑到一半停了，或那天的告警沒有
    送出去（網路斷了，`exit_program.done_path`）——分不出是哪一種，都講。"""
    return (f"原因：出場程式 {day} 沒有跑完（電腦當時關機、睡著，出場程式中途停了，"
            "或告警沒有送出去），這一次執行才發現。那天的日盤已經收盤，不送單")


def exit_not_on_file(*, at: datetime, record: position_file.PositionRecord,
                     on_file: position_file.PositionRecord, reason: str) -> str:
    """出場程式平倉了，部位檔卻寫不進去。平倉照樣做完了：帳上以「實際」為準。

    檔裡還記著沒平的口數，但出場程式同一天只處理一次（`exit_program.done_path`），不會照它再平。
    要人做的是排除寫不進去的原因：盤中程式送單前寫不進部位檔就不送，原因不排除，下一個交易日
    就不會進場。

    有一筆平倉單不確定時（`record.uncertain`），「實際」只說得出範圍：那一筆成交了幾口不知道，
    帳上剩的介於「全部成交」與「只成交已知的口數」之間。講成確定的「還剩 N 口」的話，
    使用者照它去平，那筆其實已經成交的話就是反向新倉（規格第 41 條：「委託送出去了，但…」
    和「部位還在」要分開講）。
    """
    return (
        "【要你處理】PTS 部位檔沒記到出場程式這次的平倉\n"
        f"{_not_on_file_facts(record, on_file)}"
        f"原因：{reason}\n"
        "帳上以上面的「實際」為準：出場程式今天不會再照這份檔送單（一天只處理一次）。\n"
        "部位檔寫不進去的原因（例如防毒鎖住、磁碟滿）請在下一個交易日 08:40 之前排除："
        "盤中程式送單前寫不進部位檔就不會進場。\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def _not_on_file_facts(record: position_file.PositionRecord,
                       on_file: position_file.PositionRecord) -> str:
    """平倉了、部位檔卻沒記到時，實際與檔裡各是多少（每行都帶換行；`exit_not_on_file`）。

    有一筆委託不確定時（`record.uncertain`），「實際」只說得出範圍（`handoff.lots_to_close`）：
    平倉那一筆成交了幾口就少幾口，盤中留下的進場那一筆成交了幾口就多幾口。
    """
    holding = _HOLDING_TEXT[record.direction]
    n = handoff.lots_to_close(record)
    u = n.uncertain
    if u is None:
        actual = f"實際：平倉 {record.exited_lots} 口，還剩{holding} {n.held} 口"
    else:
        stage, side = (("進場", _SIDE_TEXT) if n.adds else ("平倉", _CLOSE_SIDE_TEXT))
        actual = (f"實際：確定的平倉 {record.exited_lots} 口，另有一筆{stage}"
                  f"{side[record.direction]}{_PRODUCT_TEXT[record.product]} {u.requested_lots} 口"
                  f"不知道成交幾口，帳上還剩{holding} {n.low}～{n.high} 口")
    lost = ""
    kept = on_file.uncertain
    if u is not None and (kept is None or (kept.stage, kept.order_seq) != (u.stage, u.order_seq)):
        lost = (f"部位檔也沒記到那筆不確定的{'進場' if n.adds else '平倉'}委託"
                f"（{_seq_text(u.order_seq)}）\n")
    return f"{actual}；部位檔記的是平倉 {on_file.exited_lots} 口\n{lost}"


def intraday_not_running(*, at: datetime, log: str, stopped_at: datetime | None = None) -> str:
    """出場程式發現盤中程式今天沒有跑（規格第 38、54 條），或跑到 `stopped_at` 就停了
    （當掉之後排程沒有把它重啟）。

    平常沒有例行訊息，這一則是使用者唯一會發現的管道。講清楚兩件事：今天少了什麼（進場的
    判斷），以及出場不受影響——不講的話，使用者會以為部位沒人平。要做的事是排除原因，
    所以指出去哪裡看：`log` 是盤中程式今天的 log（`paths.shown_log`）。
    """
    after_hours = _EXIT_GOES_ON
    deadline = _BEFORE_NEXT_OPEN
    if stopped_at is None:
        return (
            "【要你處理】PTS 盤中程式今天沒有跑\n"
            f"今天沒有判斷進場，也沒有記錄原始行情。{after_hours}\n"
            f"請檢查工作排程器裡盤中程式的排程，以及 {log}，{deadline}\n"
            f"{at:%Y/%m/%d %H:%M:%S}"
        )
    return (
        f"【要你處理】PTS 盤中程式今天 {stopped_at:%H:%M} 之後就沒有再跑\n"
        f"{stopped_at:%H:%M} 之後沒有判斷進場：盤中程式停了，排程沒有把它重啟，或重啟之後又停了。"
        f"{after_hours}\n"
        f"請看 {log} 最後幾行找原因，並檢查工作排程器裡盤中程式的排程，{deadline}\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def heartbeat_unreadable(*, at: datetime, reason: str, log: str) -> str:
    """出場程式讀不懂心跳記錄：不知道盤中程式今天有沒有跑完，照實講，請人去看 log（`log`）。"""
    return (
        "【要你處理】PTS 心跳記錄讀不懂，不知道盤中程式今天有沒有跑完\n"
        f"原因：{reason}\n"
        f"{_EXIT_GOES_ON}\n"
        f"請看 {log} 確認盤中程式今天有沒有跑完，{_BEFORE_NEXT_OPEN}\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def exit_settings_broken(*, at: datetime, error: str) -> str:
    """出場程式讀不懂設定檔，照部位檔處理了今天的微台（規格第 43、47 條）。

    出場程式用不到設定檔：要不要平只看部位檔，帳密在 `.env`。要人做的事是**修設定檔**——
    下一個交易日盤中程式一啟動就會被它擋下來（規格第 47 條）。微台沒平完的話另有一則
    「部位還在」，這裡不重複講部位。"""
    return (
        "【要你處理】PTS 設定檔有錯，出場程式照部位檔處理今天的微台\n"
        f"設定檔：{error}\n"
        "今天的微台照部位檔平倉，不看設定檔。設定檔不修好，下一個交易日盤中程式一啟動就會被擋下來："
        "請在下一個交易日 08:40 之前修好 config/settings.yaml。\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


_EXIT_GOES_ON = "出場程式照常處理部位檔裡的部位。"
_BEFORE_NEXT_OPEN = "下一個交易日 08:40 之前排除。"


def exit_crashed(*, at: datetime, reason: str, logs: handoff.FillLogs) -> str:
    """出場程式本身出錯中止。不知道做到哪裡，只能請人看帳戶。

    還剩幾口也不知道（可能平到一半），講從哪裡算（`_count_from_logs`）。
    不講「不要重跑」（理由見 `exit_incomplete`）。"""
    closed = handoff.day_closed(at)
    return (
        "【要你處理】PTS 出場程式意外中止\n"
        f"原因：{reason}\n"
        "今天 PTS 的微台部位可能沒有平完：請到券商 APP 確認帳戶。"
        f"{_close_micro_any_day(closed)}。大台、小台由券商 {DAY_TRADE_CLOSE:%H:%M} 當沖強平。\n"
        f"{_count_from_logs(logs)}\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


# ── 收工指令（tools/stop_today.py）：印在主控台，不發 Discord ─────────────────────
#
# 使用者自己下的指令，人就在電腦前，所以只印出來。講的仍是同一套說法（三分法、要送的單寫完整）：
# 他看完這裡，接著看到的可能就是 13:25 出場程式的告警。


def stop_today_position(record: position_file.PositionRecord | None, at: datetime) -> str:
    """部位檔的現況，以及誰會了結（收工指令不帶 `--close`，或已經過了進場截止；`handoff.decide`）。

    微台由 13:25 的出場程式平倉；大台、小台標了當沖，券商 13:30 強平（ADR-0002）。
    講出來，使用者才不會跑去券商 APP 自己平（APP 是淨部位，13:25 還會照部位檔再平一次）。
    13:25 之後就不能再講「出場程式會平掉」：它可能平完了、可能正等著這個收工指令，也可能平不完
    而告警了（`_exit_took_over`）。
    """
    if record is None:
        return "今天沒有真實部位（沒有部位檔，模擬模式也是這樣），沒有要了結的。"
    name = _PRODUCT_TEXT[record.product]
    if not record.open_lots:
        return f"PTS 今天的{name}部位已經平完，沒有要了結的。"
    h = handoff.decide(record, at=at, exit_done=False)
    if h.who is Who.BROKER:
        who = (f"標了當沖，由券商 {DAY_TRADE_CLOSE:%H:%M} 強平。" if h.passed
               else f"標了當沖，券商 {DAY_TRADE_CLOSE:%H:%M} 會強平。")
    elif h.exit_started:
        who = f"13:25 起由出場程式照部位檔平倉。{_exit_took_over()}"
    else:
        who = "13:25 的出場程式會平掉。"
    return (f"PTS 今天還沒平的部位：{name}{_HOLDING_TEXT[record.direction]} {record.open_lots} 口"
            f"（{record.order_code}），{who}")


def _exit_took_over() -> str:
    """13:25 之後收工指令講到微台時：出場程式已經起來（`handoff.Handoff.exit_started`）。它要是還沒
    處理今天的部位檔，會等這個收工指令結束（`exit_program._after_stop_command`）；處理過了、
    平不完或碰到不確定，就已經發了 Discord。兩種都一樣：照它的告警做，在那之前不要自己平。"""
    return ("Discord 收到「部位還在」或「委託送出去了，但…」的告警時，照那一則的口數處理。\n"
            "在那之前不要自己平：出場程式會照部位檔再平一次，開出反向新倉。")


def stop_today_not_confirmed(*, waited: int, close: bool, log: str) -> str:
    """盤中程式在跑，等了 `waited` 秒都沒有它的確認記錄（`intraday.stop_seen_path`）。

    記錄已經寫了，它一看到就不再進場。`--close` 這次不平倉：盤中程式要是正卡在一筆進場委託上，
    那一筆成交時它會用記憶體裡的部位整份覆寫部位檔，現在記下的平倉就不見了，13:25 的出場程式
    再照舊檔平一次。過一兩分鐘再執行一次就好。`log` 是盤中程式今天的 log。
    """
    rerun = ("\n--close 這次沒有平倉：請過一兩分鐘再執行一次 .\\.venv\\Scripts\\python.exe "
             "tools\\stop_today.py --close。" if close else "")
    return (
        f"【要你處理】盤中程式在跑，等了 {waited} 秒都沒有確認看到停止進場記錄\n"
        "記錄已經寫了，盤中程式一看到就不再進場。它每秒看一次：等不到通常是它正在等一筆委託的"
        f"成交回報，或卡住了（看 {log} 最後幾行）。{rerun}"
    )


def stop_today_position_file_unreadable(*, at: datetime, reason: str,
                                        logs: handoff.FillLogs) -> str:
    """收工指令讀不懂部位檔：不送單。說法同 `exit_position_file_unreadable`——13:25 的出場程式
    讀的是同一個檔，也不會平；口數從 log 算，收工指令自己平過的記在它的 log。"""
    return (
        "【要你處理】PTS 部位檔讀不懂，收工指令沒有平倉\n"
        f"原因：{reason}\n"
        f"PTS 今天可能還有部位：請到券商 APP 確認帳戶。{_unknown_product(at)}"
        "13:25 的出場程式同樣讀不懂這個檔，不會替你平倉。\n"
        f"{_count_from_logs(logs)}\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def stop_today_entry_unresolved(*, at: datetime, record: position_file.PositionRecord,
                                lookup: str | None) -> str:
    """盤中留下的微台進場不確定（有委託序號，`Who.EXIT_LOOKS_UP`），收工指令沒能確認
    （`lookup` 是查到的結果；沒有查就是 None）：一張單都不送。

    ⚠️ **不叫人自己平**，和 `exit_uncertain` 不同：部位檔還是這筆不確定的進場，13:25 的出場程式
       會再用委託序號查一次，查到全部成交就照常平倉——使用者先自己平掉的話，那時就是再平一次，
       開出反向新倉。說法同盤中那則 `fill_unknown`：等 13:25，查不到它會再講要平幾口。
       13:25 之後（只看部位、沒有平倉時）出場程式已經起來了，改講照它的告警做。
    """
    order, held = _uncertain_order_lines(record)
    looked_up = "" if lookup is None else f"券商的成交紀錄：{lookup}\n"
    name = _PRODUCT_TEXT[record.product]
    h = handoff.decide(record, at=at, exit_done=False)
    if h.exit_started:
        todo = _exit_looked_up()
    else:
        todo = (f"{name}不必現在處理："
                + _exit_looks_up(name, _CLOSE_SIDE_TEXT[record.direction],
                                 handoff.lots_to_close(record).high, again=lookup is not None))
    return (
        "PTS 委託送出去了，但不知道成交幾口，收工指令沒有平倉\n"
        f"{order}\n"
        f"{looked_up}"
        f"{held}\n"
        f"{todo}\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def stop_today_left(*, at: datetime, record: position_file.PositionRecord, lots: int,
                    reason: str) -> str:
    """`--close` 沒能平完（「部位還在」）：這次平了 `lots` 口，還剩的照常了結（`handoff.decide`）。

    ⚠️ **不叫人自己平**，和出場程式的 `exit_incomplete` 不同：部位檔記著已經平掉的口數，微台剩下的
       由 13:25 的出場程式平（它只平剩下的）——使用者自己平掉的話，出場程式再平一次就是反向新倉。
       大台、小台標了當沖，券商 13:30 強平帳上還在的，不必自己平。

    講這句話時 13:25 已經過了的話（最後一筆平倉單 13:24 前送出、回報或查詢拖過 13:25），出場程式
    這時已經起來、正等著收工指令做完（`exit_program._after_stop_command`），之後才照部位檔處理
    剩下的——它可能平完，也可能過了期限、碰到不確定而告警。所以不能再保證「照常由 13:25 平掉」，
    改講它會怎麼做，以及收到告警時照告警的口數。
    """
    left = record.open_lots
    h = handoff.decide(record, at=at, exit_done=False)
    if h.who is Who.BROKER:
        who = f"剩下的 {left} 口標了當沖，照常由券商 {DAY_TRADE_CLOSE:%H:%M} 強平，不必自己平。"
    elif h.exit_started:
        who = (f"13:25 的出場程式等這個收工指令結束，才照部位檔處理剩下的 {left} 口：部位檔記著"
               f"已經平掉的 {lots} 口，它只平剩下的。之後 {_exit_took_over()}")
    else:
        who = (f"剩下的 {left} 口照常由 13:25 的出場程式平掉：部位檔記著已經平掉的 {lots} 口，"
               "它只平剩下的。\n不要自己平：不然 13:25 會再平一次，開出反向新倉。")
    return (
        f"PTS 部位還在：收工指令平了 {lots} 口，還剩{_HOLDING_TEXT[record.direction]} {left} 口"
        f"（{record.order_code}）\n"
        f"原因：{reason}\n"
        f"{who}\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def stop_today_not_on_file(*, at: datetime, record: position_file.PositionRecord,
                           on_file: position_file.PositionRecord, reason: str) -> str:
    """`--close` 平倉了，部位檔卻寫不進去（`closing.Closed.file_behind`）。

    比出場程式那一則（`exit_not_on_file`）更急：13:25 的出場程式**一定會跑**，照舊檔把已經平掉的
    微台再平一次。所以要人在那之前停用它的排程，帳上還在的自己平。大台、小台出場程式本來就
    不送單，券商 13:30 照帳上還在的強平，不必停。兩種都不要再執行收工指令。
    """
    if handoff.left_to_broker(record.product):
        todo = (f"大台、小台出場程式不送單，券商 {DAY_TRADE_CLOSE:%H:%M} 照帳上還在的強平：不必處理，"
                "只是部位檔記錯了。")
    else:
        todo = ("13:25 的出場程式只認部位檔，會把已經平掉的口數再平一次，開出反向新倉：請在 13:25 前到"
                "工作排程器停用「PTS 出場程式」，帳上還在的 PTS 口數以上面的「實際」為準自己平掉，"
                "收盤後再啟用排程。")
    return (
        "【要你處理】PTS 部位檔沒記到收工指令這次的平倉\n"
        f"{_not_on_file_facts(record, on_file)}"
        f"原因：{reason}\n"
        f"{todo}\n"
        "也不要再執行收工指令：它一樣只認部位檔。\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def stop_today_after_cutoff() -> str:
    """進場截止之後的 `--close`：不送單。接著印 `stop_today_position`（由誰了結）。"""
    return (f"已經過了 {strategy.ENTRY_CUTOFF:%H:%M} 進場截止，收工指令不送單：出場程式 13:25、"
            "券商 13:30 就要接手，這時再送單可能和它們同時平倉。")


def stop_today_closed(*, at: datetime, record: position_file.PositionRecord, lots: int) -> str:
    """`--close` 平完了：這一次平掉的 `lots` 口記在部位檔裡，誰都不會再平一次。"""
    after = (f"券商 {DAY_TRADE_CLOSE:%H:%M} 的當沖強平也沒有 PTS 的部位可平。"
             if handoff.left_to_broker(record.product) else "13:25 的出場程式不會再送單。")
    return (
        f"收工指令平掉了 PTS 今天的{_PRODUCT_TEXT[record.product]}"
        f"{_HOLDING_TEXT[record.direction]} {lots} 口（{record.order_code}），部位已經了結：{after}\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )
