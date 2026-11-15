"""Discord 訊息的內容（純函式），以及收工指令印在主控台的內容（`stop_today_*`）。

規格要求訊息由純函式組裝：測試能直接斷言內容，送出的機制（`notifiers.discord`）另外一層。
用語照 GLOSSARY：做多／做空是方向，買進／賣出是委託。使用者在手機上看到的是中文，
不是 LONG。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from broker import DAY_TRADE_CODES, MTX_CODE, TMF_CODE, TX_CODE
import calendar_tw
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


def fill_unknown(*, at: datetime, product: str, direction: str, lots: int, order_seq: str,
                 known_filled: int, reason: str, held_lots: int, file_error: str | None) -> str:
    """委託送出去了，但不知道成交幾口（規格第 41、50 條）：**可能有部位**。

    所有告警裡最需要人立刻動手的一則，所以講的是「去做什麼」：先看帳戶，再看誰會了結。
    大台、小台標了當沖，券商 13:30 照樣強平。微台沒有當沖強平：部位檔記著「不確定」，出場程式
    看到它就**整個**不平（規格第 50 條），連部位檔裡確認過的 `held_lots` 口也不平——要人平的是
    兩者加起來。

    部位檔在送單**之前**就記成「不確定」了（`intraday._Orders.enter`）。`file_error` 不是 None
    代表之後要補上這一筆的委託序號與已知口數時寫不進去：檔裡還是「不確定」，只是少了序號，
    出場程式照樣不平倉——序號在這則告警裡，對帳用它。

    `known_filled` 是已知**至少**成交的口數：對帳時至少要看到這麼多。

    微台有委託序號、序號也寫進部位檔時，**請人等 13:25**：出場程式會先用序號查券商紀錄，
    查到全部成交就照常平倉（`exit_program._MicroExit._look_up_entry`）；查不到才再發一則。
    要人在那之前**不要自己平**——自己平了，13:25 查到成交又平一次，就是反向新倉。
    出場程式查不了的時候（沒拿到序號、序號沒寫進去），要人自己平的那一句寫成**要送的單**：
    買賣別、商品，口數寫成算式與範圍（確認的口數加上這筆實際成交的）。帳戶 APP 看到的是淨部位，
    含使用者自己的長期部位與 OS 的口數，只寫「平掉 PTS 的部位」的話，使用者分不出要平幾口。
    """
    name = _PRODUCT_TEXT[product]
    holding = _HOLDING_TEXT[direction]
    close = _CLOSE_SIDE_TEXT[direction]
    seq = f"委託序號 {order_seq}" if order_seq else "沒拿到委託序號"
    known = f"，已知至少成交 {known_filled} 口" if known_filled else ""
    missing = "" if file_error is None else f"（這筆的委託序號沒寫進去：{file_error}）"
    exit_looks_up = product not in DAY_TRADE_CODES and order_seq and file_error is None
    if exit_looks_up:
        on_file = "部位檔已記為「不確定」：盤中程式今天不再進場。"
        todo = (f"{name}不必現在處理：13:25 的出場程式會先用委託序號向券商查這筆成交了幾口，"
                f"查到全部成交就照常平倉（{close}{name} {held_lots + lots} 口）；查不到確定的口數會"
                "再發一則，寫明要平幾口。在那之前不要自己平：不然 13:25 會再平一次，開出反向新倉。")
    else:
        on_file = (f"部位檔已記為「不確定」{missing}：盤中程式今天不再進場，"
                   "13:25 的出場程式也不會自動平倉。")
        if product in DAY_TRADE_CODES:
            who_closes = _broker_closes_today(name)
        else:
            if held_lots:
                count = (f"部位檔已確認的{holding} {held_lots} 口，加上這筆實際成交的口數，也就是 "
                         f"{_span(held_lots + known_filled, held_lots + lots)}")
            else:
                count = f"這筆實際成交幾口就平幾口（{_span(known_filled, lots)}）"
            who_closes = f"{name}沒有當沖強平，要你自己在 13:30 前{close}{name}：{count}。"
        todo = f"請到券商 APP 確認這筆成交了幾口。{who_closes}"
    return (
        "【要你處理】PTS 委託送出去了，但不知道成交幾口\n"
        f"這次：{_SIDE_TEXT[direction]}{name} {lots} 口（{seq}）{known}\n"
        f"原因：{reason}\n"
        f"{on_file}\n"
        f"{todo}\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def _broker_closes_today(name: str) -> str:
    """大台、小台進場時標了當沖，券商 13:30 強平（ADR-0002）：不確定或沒記到的時候，也不必自己平。"""
    return f"{name}標了當沖，券商 13:30 會強平 PTS 今天的{name}部位。"


def position_file_unreadable(*, day: date, reason: str, entry_log: str, stop_log: str) -> str:
    """部位檔讀不懂（規格第 38 條）。盤中程式這一天不進場。

    刻意不說「今天沒有部位」：真相是「可能有部位，但讀不到記錄」。出場程式也讀同一個檔，
    同樣不會自動平倉——不講的話，使用者會以為 13:25 有人替他收尾。口數 PTS 自己也不知道，
    就講從哪裡算（`_count_from_logs`）：盤中程式的 log `entry_log`，減掉收工指令平過的
    （`stop_log`，13:20 前就可能 `--close` 過）。出場程式這時還沒跑。
    """
    return (
        "【要你處理】PTS 部位檔讀不懂，盤中程式今天不進場\n"
        f"原因：{reason}\n"
        "今天 PTS 可能已經有部位。13:25 的出場程式同樣讀不懂這個檔，不會替你平倉：交易微台的話，"
        "請在日盤 13:45 收盤前自行平掉；大台、小台標了當沖，券商 13:30 會強平。\n"
        f"{_count_from_logs(entry_log, stop_log)}\n"
        f"{day:%Y/%m/%d}"
    )


def _count_from_logs(entry_log: str, *close_logs: str) -> str:
    """PTS 自己也不知道口數時（部位檔讀不懂、出場程式中止），要人平幾口從哪裡算。

    帳戶 APP 看到的是淨部位，含使用者自己的長期部位與 OS 的口數，不能拿來當 PTS 的口數。
    PTS 的每一筆成交都寫在 log 裡：盤中程式的「進場成交」帶著累計口數，平倉的每一筆
    「平倉成交」帶著口數（查詢確認的也是這個字）。`close_logs` 是記著平倉的 log：出場程式的，
    以及收工指令的（`tools/stop_today.py --close` 用的是出場程式同一段平倉，log 也寫成「平倉成交」，
    但寫在它自己的 log 裡）。**每一個都要列**：漏掉收工指令的，算出來就多了它平掉的口數，
    使用者照著平就是反向新倉。
    """
    closes = " 與 ".join(close_logs)
    minus = f"，減掉 {closes} 裡每一筆「平倉成交」的口數" if close_logs else ""
    return (f"PTS 的口數：{entry_log} 最後一筆「進場成交」的累計口數{minus}"
            "（有「不知道成交幾口」的委託時，另外到券商 APP 用委託序號查）。只平這個口數。")


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


def fill_not_on_file(*, at: datetime, product: str, direction: str, lots: int, price: float,
                     order_seq: str, total_lots: int, on_file_lots: int, reason: str) -> str:
    """成交了，部位檔卻寫不進去（規格第 38、41 條：「部位還在」）。盤中程式今天不再進場。

    部位檔停在送單**之前**記下的「不確定」（`intraday._Orders.enter`）：出場程式看到它就整個
    不平倉，連檔裡已確認的口數也不平（規格第 50 條）。所以微台要人自己平的是 PTS 今天**全部**的
    口數，不只沒記到的這幾口——只叫人平這幾口的話，檔裡那幾口沒人平，帶著過夜。帳上與檔裡的
    兩個數字都寫出來，對帳時才知道差在哪裡。大台、小台標了當沖，券商 13:30 照樣強平。
    """
    name = _PRODUCT_TEXT[product]
    if product in DAY_TRADE_CODES:
        who_closes = _broker_closes_today(name)
    else:
        who_closes = (f"{name}沒有當沖強平，要你自己在 13:30 前"
                      f"{_CLOSE_SIDE_TEXT[direction]}{name} {total_lots} 口（PTS 今天全部的口數）。")
    return (
        "【要你處理】PTS 部位還在，部位檔卻沒記到這次成交\n"
        f"這次：{_SIDE_TEXT[direction]}{name} {lots} 口 @ {_points(price)}"
        f"（委託序號 {order_seq}）\n"
        f"帳上 PTS 共{_HOLDING_TEXT[direction]} {total_lots} 口，部位檔只記到 {on_file_lots} 口，"
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


def exit_position_file_unreadable(*, at: datetime, reason: str, entry_log: str, exit_log: str,
                                  stop_log: str, closed: bool = False) -> str:
    """出場程式讀不懂部位檔（規格第 38 條）：一張單都不送。

    讀不懂就不知道是哪個商品，所以兩種處理都講：微台沒有人會替它平倉，大台、小台有券商的
    當沖強平。只寫「請平倉」的話，交易大台的人會自己平一次、券商 13:30 再平一次。
    也不知道今天是不是結算日，所以微台的兩種期限都講（`closed`：日盤已經收盤）。
    口數也不知道，講從哪裡算（`_count_from_logs`：出場程式與收工指令的 log 都要減）。
    """
    tx = "由券商 13:30 強平" if closed else "券商 13:30 會強平"
    return (
        "【要你處理】PTS 部位檔讀不懂，出場程式沒有平倉\n"
        f"原因：{reason}\n"
        f"PTS 今天可能還有部位：請到券商 APP 確認帳戶。{_close_micro_any_day(closed)}；"
        f"大台、小台標了當沖，{tx}。\n"
        f"{_count_from_logs(entry_log, exit_log, stop_log)}\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


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
                   lookup: str | None = None, closed: bool = False) -> str:
    """有一筆委託送出去了、卻不知道成交幾口（規格第 50 條）：出場程式一張單都不再送。

    兩種來源：部位檔早就記著一筆不確定的委託（進場或出場），或出場程式自己的平倉單剛剛
    不確定、向券商查成交紀錄也沒能確認全部成交——那時 `lookup` 是查到的結果（規格要求
    附上券商紀錄顯示的口數）。

    「委託送出去了，但…」：帳上**可能**比部位檔多（進場那筆成交了）或少（平倉那筆成交了）。
    兩個數字都寫出來——檔裡確定的口數與不確定那一筆的委託口數——使用者對帳戶時才知道
    要看到的範圍。`closed` 同 `exit_incomplete`：告警這一刻已經過了微台的期限。
    """
    order, held = _uncertain_order_lines(record)
    looked_up = "" if lookup is None else f"券商的成交紀錄：{lookup}\n"
    return (
        "【要你處理】PTS 委託送出去了，但不知道成交幾口，出場程式不再送單\n"
        f"{order}\n"
        f"{looked_up}"
        f"{held}\n"
        f"{_check_account(record, closed)}\n"
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
    """部位檔記著一筆不確定的委託時，要人送的平倉單：買賣別、商品、下單代碼，口數寫成算式與範圍。

    帳戶 APP 看到的是淨部位（含使用者自己的、OS 的），只寫「平掉 PTS 的部位」的話分不出要平
    幾口。進場那一筆成交了幾口就多平幾口；平倉那一筆成交了幾口就少平幾口（檔裡的口數還沒扣掉
    它，同 `exit_not_on_file`）。
    """
    u = record.uncertain
    held = record.open_lots
    if u.stage == position_file.ENTRY:
        how, low, high = "加上", held + u.known_filled, held + u.requested_lots
    else:
        how, low, high = "減掉", max(0, held - u.requested_lots), held - u.known_filled
    return (f"{_CLOSE_SIDE_TEXT[record.direction]}{_PRODUCT_TEXT[record.product]}"
            f"（{record.order_code}）：部位檔的{_HOLDING_TEXT[record.direction]} {held} 口，"
            f"{how}{_seq_text(u.order_seq)} 實際成交的口數，也就是 {_span(low, high)}"
            "（到券商 APP 用委託序號查這筆成交了幾口）")


def _check_account(record: position_file.PositionRecord, closed: bool) -> str:
    """不確定時要人做的事，依商品而定（ADR-0002）。

    大台、小台叫人自己平的話，會和券商 13:30 的當沖強平撞在一起：兩邊都平，多平的
    就是反向新倉。所以只請他在強平之後確認。微台寫明要送的單（`_uncertain_close_order`），
    期限見 `exit_incomplete`。
    """
    if record.product in DAY_TRADE_CODES:
        return ("大台、小台標了當沖，券商 13:30 會強平：13:30 之後請到券商 APP 確認帳戶裡"
                "已經沒有 PTS 的部位。")
    if record.settles_today and closed:
        return ("今天是結算日，近月 13:30 已經停止交易，不能再平：PTS 還在的微台部位由交易所"
                "以最後結算價現金了結。結算後請到券商 APP 確認帳戶。")
    order = _uncertain_close_order(record)
    if record.settles_today:
        return (f"請在 13:30 前自行{order}。今天是結算日，"
                "近月 13:30 停止交易，之後還在的口數由交易所以最後結算價現金了結。")
    if closed:
        return f"日盤 13:45 已經收盤，請在夜盤自行{order}。"
    return f"請在日盤 13:45 收盤前自行{order}。"


def exit_incomplete(*, at: datetime, record: position_file.PositionRecord, reason: str,
                    closed: bool = False) -> str:
    """微台沒有平完（規格第 34、55 條）：「部位還在」，確定還有這幾口。

    要人送的單寫完整（買賣別、口數、下單代碼）：只寫「請平倉」的話，使用者可能把帳上同商品
    的部位（自己的、OS 的）一起平掉。口數是**還沒平的**——已經成交的出場不算進去，
    叫他平全部的話，多平的就是反向新倉。

    期限依結算日而不同（`closed`：告警這一刻已經過了期限）：一般日是日盤 13:45 收盤，
    過了只能在夜盤平；結算日的近月 13:30 就停止交易，過了就不能再平，由交易所以最後結算價
    現金了結。結算日寫成 13:45 的話，使用者以為還有時間，其實 13:30 之後就賣不掉了。

    期限之前還要講**不要重跑**：部位檔原封不動，還是「這幾口沒平」。使用者自己平掉之後，
    出場程式只要再被執行一次（手動、排程補跑），就照檔再平一次，開出反向新倉過夜。
    過了期限的重跑不會送單，就不講。
    """
    lots = record.open_lots
    side = _CLOSE_SIDE_TEXT[record.direction]
    order = f"{side}{_PRODUCT_TEXT[record.product]} {lots} 口（{record.order_code}）"
    if record.settles_today and closed:
        todo = (f"{record.order_code} 今天結算，13:30 已經停止交易，不能再平：這 {lots} 口由交易所"
                f"以最後結算價現金了結，不會留到明天。結算後請到券商 APP 確認帳戶裡已經沒有"
                f"這 {lots} 口。")
    elif record.settles_today:
        todo = (f"請在 13:30 前自行{order}：今天是結算日，近月 13:30 停止交易，"
                "之後還在的口數由交易所以最後結算價現金了結。")
    elif closed:
        todo = f"請在夜盤自行{order}。"
    else:
        todo = f"請自行{order}，日盤 13:45 收盤。"
    if not closed:
        todo += (f"\n自己平掉之後，不要再執行出場程式：它只認部位檔，會把這 {lots} 口再{side}一次，"
                 "開出反向新倉。")
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


def exit_missed_unreadable(*, at: datetime, day: date, reason: str, entry_log: str,
                           exit_log: str, stop_log: str) -> str:
    """出場程式在 `day` 沒有跑完，那天的部位檔又讀不懂（規格第 38 條）：不知道那天留下什麼。

    讀不懂就不知道是哪個商品：大台、小台那天 13:30 已經被券商強平，微台沒有人平。
    口數從那天的 log 算（`_count_from_logs`）。
    """
    return (
        f"【要你處理】PTS {day:%m/%d} 的部位檔讀不懂，出場程式沒有平倉\n"
        f"{_missed_reason(f'{day:%m/%d}')}。部位檔：{reason}\n"
        "PTS 那天可能留下微台部位：請到券商 APP 確認帳戶，還在的話請自行平掉。大台、小台標了當沖，"
        "那天 13:30 已經由券商強平。\n"
        f"{_count_from_logs(entry_log, exit_log, stop_log)}\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def _missed_reason(day: str) -> str:
    """出場程式那天為什麼沒平：排程補跑落在隔天或週末、那天跑到一半停了，或那天的告警沒有
    送出去（網路斷了，`exit_program.done_path`）——分不出是哪一種，都講。"""
    return (f"原因：出場程式 {day} 沒有跑完（電腦當時關機、睡著，出場程式中途停了，"
            "或告警沒有送出去），這一次執行才發現。那天的日盤已經收盤，不送單")


def exit_not_on_file(*, at: datetime, record: position_file.PositionRecord,
                     on_file: position_file.PositionRecord, reason: str) -> str:
    """出場程式平倉了，部位檔卻寫不進去。平倉照樣做完了，要人做的只有一件事：**不要重跑**。

    出場程式只認部位檔：檔裡還記著沒平的口數，再執行一次就會把已經平掉的再平一次。

    有一筆平倉單不確定時（`record.uncertain`），「實際」只說得出範圍：那一筆成交了幾口不知道，
    帳上剩的介於「全部成交」與「只成交已知的口數」之間。講成確定的「還剩 N 口」的話，
    使用者照它去平，那筆其實已經成交的話就是反向新倉（規格第 41 條：「委託送出去了，但…」
    和「部位還在」要分開講）。
    """
    return (
        "【要你處理】PTS 部位檔沒記到出場程式這次的平倉\n"
        f"{_not_on_file_facts(record, on_file)}"
        f"原因：{reason}\n"
        "不要再執行出場程式：它只認部位檔，會把已經平掉的口數再平一次，開出反向新倉。\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def _not_on_file_facts(record: position_file.PositionRecord,
                       on_file: position_file.PositionRecord) -> str:
    """平倉了、部位檔卻沒記到時，實際與檔裡各是多少（每行都帶換行；`exit_not_on_file`）。"""
    exited = record.entered_lots - record.open_lots
    on_file_exited = on_file.entered_lots - on_file.open_lots
    holding = _HOLDING_TEXT[record.direction]
    u = record.uncertain
    if u is None:
        actual = f"實際：平倉 {exited} 口，還剩{holding} {record.open_lots} 口"
    else:
        actual = (f"實際：確定的平倉 {exited} 口，另有一筆平倉{_CLOSE_SIDE_TEXT[record.direction]}"
                  f"{_PRODUCT_TEXT[record.product]} {u.requested_lots} 口不知道成交幾口，"
                  f"帳上還剩{holding} {record.open_lots - u.requested_lots}～"
                  f"{record.open_lots - u.known_filled} 口")
    lost = ""
    if u is not None and u != on_file.uncertain:
        lost = f"部位檔也沒記到那筆不確定的平倉委託（{_seq_text(u.order_seq)}）\n"
    return f"{actual}；部位檔記的是平倉 {on_file_exited} 口\n{lost}"


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


def exit_crashed(*, at: datetime, reason: str, entry_log: str, exit_log: str, stop_log: str,
                 closed: bool = False) -> str:
    """出場程式本身出錯中止。不知道做到哪裡，只能請人看帳戶。`closed`：日盤已經收盤。

    還剩幾口也不知道（可能平到一半），講從哪裡算（`_count_from_logs`）。
    收盤前同樣要講不要重跑（理由見 `exit_incomplete`）。"""
    rerun = ("" if closed else
             "自己平掉之後，不要再執行出場程式：它只認部位檔，會把你平掉的口數再平一次，"
             "開出反向新倉。\n")
    return (
        "【要你處理】PTS 出場程式意外中止\n"
        f"原因：{reason}\n"
        "今天 PTS 的微台部位可能沒有平完：請到券商 APP 確認帳戶。"
        f"{_close_micro_any_day(closed)}。大台、小台由券商 13:30 當沖強平。\n"
        f"{_count_from_logs(entry_log, exit_log, stop_log)}\n"
        f"{rerun}"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


# ── 收工指令（tools/stop_today.py）：印在主控台，不發 Discord ─────────────────────
#
# 使用者自己下的指令，人就在電腦前，所以只印出來。講的仍是同一套說法（三分法、要送的單寫完整）：
# 他看完這裡，接著看到的可能就是 13:25 出場程式的告警。


def stop_today_position(record: position_file.PositionRecord | None) -> str:
    """部位檔的現況，以及誰會了結（收工指令不帶 `--close`，或已經過了進場截止）。

    微台由 13:25 的出場程式平倉；大台、小台標了當沖，券商 13:30 強平（ADR-0002）。
    講出來，使用者才不會跑去券商 APP 自己平（APP 是淨部位，13:25 還會照部位檔再平一次）。
    """
    if record is None:
        return "今天沒有真實部位（沒有部位檔，模擬模式也是這樣），沒有要了結的。"
    name = _PRODUCT_TEXT[record.product]
    if not record.open_lots:
        return f"PTS 今天的{name}部位已經平完，沒有要了結的。"
    who = ("標了當沖，券商 13:30 會強平。" if record.product in DAY_TRADE_CODES
           else "13:25 的出場程式會平掉。")
    return (f"PTS 今天還沒平的部位：{name}{_HOLDING_TEXT[record.direction]} {record.open_lots} 口"
            f"（{record.order_code}），{who}")


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


def stop_today_position_file_unreadable(*, at: datetime, reason: str, entry_log: str,
                                        exit_log: str, stop_log: str) -> str:
    """收工指令讀不懂部位檔：不送單。說法同 `exit_position_file_unreadable`——13:25 的出場程式
    讀的是同一個檔，也不會平；口數從 log 算，收工指令自己平過的記在 `stop_log`。"""
    closed = at.time() >= calendar_tw.DAY_SESSION_CLOSE
    tx = "由券商 13:30 強平" if closed else "券商 13:30 會強平"
    return (
        "【要你處理】PTS 部位檔讀不懂，收工指令沒有平倉\n"
        f"原因：{reason}\n"
        f"PTS 今天可能還有部位：請到券商 APP 確認帳戶。{_close_micro_any_day(closed)}；"
        f"大台、小台標了當沖，{tx}。13:25 的出場程式同樣讀不懂這個檔，不會替你平倉。\n"
        f"{_count_from_logs(entry_log, exit_log, stop_log)}\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def stop_today_entry_unresolved(*, at: datetime, record: position_file.PositionRecord,
                                lookup: str | None) -> str:
    """盤中留下的微台進場不確定（有委託序號，`exit_program.entry_to_look_up`），收工指令沒能確認
    （`lookup` 是查到的結果；沒有查就是 None）：一張單都不送。

    ⚠️ **不叫人自己平**，和 `exit_uncertain` 不同：部位檔還是這筆不確定的進場，13:25 的出場程式
       會再用委託序號查一次，查到全部成交就照常平倉——使用者先自己平掉的話，那時就是再平一次，
       開出反向新倉。說法同盤中那則 `fill_unknown`：等 13:25，查不到它會再講要平幾口。
    """
    order, held = _uncertain_order_lines(record)
    looked_up = "" if lookup is None else f"券商的成交紀錄：{lookup}\n"
    name = _PRODUCT_TEXT[record.product]
    lots = record.open_lots + record.uncertain.requested_lots
    how = "先" if lookup is None else "再"
    return (
        "PTS 委託送出去了，但不知道成交幾口，收工指令沒有平倉\n"
        f"{order}\n"
        f"{looked_up}"
        f"{held}\n"
        f"{name}不必現在處理：13:25 的出場程式會{how}用委託序號向券商查一次，查到全部成交就照常平倉"
        f"（{_CLOSE_SIDE_TEXT[record.direction]}{name} {lots} 口）；查不到確定的口數會發 Discord，"
        "寫明要平幾口。在那之前不要自己平：不然 13:25 會再平一次，開出反向新倉。\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )


def stop_today_left(*, at: datetime, record: position_file.PositionRecord, lots: int,
                    reason: str) -> str:
    """`--close` 沒能平完（「部位還在」）：這次平了 `lots` 口，還剩的照常了結。

    ⚠️ **不叫人自己平**，和出場程式的 `exit_incomplete` 不同：部位檔記著已經平掉的口數，微台剩下的
       由 13:25 的出場程式平（它只平剩下的）——使用者自己平掉的話，13:25 再平一次就是反向新倉。
       大台、小台標了當沖，券商 13:30 強平帳上還在的，不必自己平。
    """
    left = record.open_lots
    if record.product in DAY_TRADE_CODES:
        who = f"剩下的 {left} 口標了當沖，照常由券商 13:30 強平，不必自己平。"
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
    """`--close` 平倉了，部位檔卻寫不進去（`exit_program.Closing`）。

    比出場程式那一則（`exit_not_on_file`）更急：13:25 的出場程式**一定會跑**，照舊檔把已經平掉的
    微台再平一次。所以要人在那之前停用它的排程，帳上還在的自己平。大台、小台出場程式本來就
    不送單，券商 13:30 照帳上還在的強平，不必停。兩種都不要再執行收工指令。
    """
    if record.product in DAY_TRADE_CODES:
        todo = ("大台、小台出場程式不送單，券商 13:30 照帳上還在的強平：不必處理，"
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
    after = ("券商 13:30 的當沖強平也沒有 PTS 的部位可平。" if record.product in DAY_TRADE_CODES
             else "13:25 的出場程式不會再送單。")
    return (
        f"收工指令平掉了 PTS 今天的{_PRODUCT_TEXT[record.product]}"
        f"{_HOLDING_TEXT[record.direction]} {lots} 口（{record.order_code}），部位已經了結：{after}\n"
        f"{at:%Y/%m/%d %H:%M:%S}"
    )
