"""盤中程式 —— 從最上層打進去：餵一個交易日的劇本，斷言產出的 1 分 K 與原始行情檔。

接縫只有 broker（`FakeBroker` 照劇本回放）；時間全部取自劇本裡的事件。
期望值是手算的字面值，不是用程式的算法重算一次。
"""

from datetime import date

from broker import TMF_CODE
from broker.fake import FakeBroker
from day_script import at, clock, counts, product_trade, trade, whole_morning
from recording import RecordingNotifier
import intraday
import settings

TRADING_DAY = date(2026, 11, 9)      # 週一
SATURDAY = date(2026, 11, 7)


def _config(product: str = TMF_CODE) -> settings.Config:
    return settings.Config(auto_order_enabled=False, product=product, lots_per_entry=1,
                           max_position=20, discord_enabled=False)


def _run(broker: FakeBroker, today: date = TRADING_DAY) -> intraday.SessionOutcome:
    return intraday.run(broker, _config(), today=today, notify=RecordingNotifier())


# --- 一整個上午 ---


def test_a_whole_morning_yields_one_bar_per_minute_from_0845_to_1319(market_dir):
    """08:45 到 13:20 是 275 分鐘，所以是 275 根，最後一根是 13:19。"""
    outcome = _run(FakeBroker(events=whole_morning()))

    assert outcome.ending is intraday.Ending.CUTOFF
    assert len(outcome.bars) == 275
    ohlc = _ohlc(outcome)
    assert ohlc[0] == ("08:45", 45000, 45005, 45000, 45005)
    assert ohlc[15] == ("09:00", 45015, 45020, 45015, 45020)
    assert ohlc[-1] == ("13:19", 45274, 45279, 45274, 45279)

    raw = _raw_lines(market_dir)
    assert raw[1] == "2026-11-09T08:40:00.000,,1,2", "開盤前的筆數也要記，ticket 03 要看累計起點"
    assert raw[-1] == "2026-11-09T13:19:50.000,45279,,"


# --- 非交易日 ---


def test_on_a_non_trading_day_it_does_not_log_in_and_does_nothing(market_dir):
    broker = FakeBroker(
        login_error=AssertionError("非交易日不該登入"),
        events=[clock(at("08:45:00", SATURDAY)), trade(at("08:45:00", SATURDAY), 45000)],
    )
    outcome = _run(broker, today=SATURDAY)
    assert outcome.ending is intraday.Ending.NOT_A_TRADING_DAY
    assert outcome.bars == ()
    assert list(market_dir.iterdir()) == []


# --- 1 分 K（規格第 1、2 條）---


def _ohlc(outcome: intraday.SessionOutcome) -> list[tuple]:
    return [(f"{b.bar.start:%H:%M}", b.bar.open, b.bar.high, b.bar.low, b.bar.close)
            for b in outcome.bars]


def test_trades_are_grouped_into_one_minute_bars_by_their_minute():
    outcome = _run(FakeBroker(events=[
        trade(at("08:45:00"), 45000), trade(at("08:45:20"), 45010),
        trade(at("08:45:40"), 44990), trade(at("08:45:59"), 45005),
        trade(at("08:46:00"), 45020), trade(at("08:46:30"), 45030),
        trade(at("08:47:00"), 45025),
    ]))
    assert _ohlc(outcome) == [
        ("08:45", 45000, 45010, 44990, 45005),
        ("08:46", 45020, 45030, 45020, 45030),
    ]


def test_only_day_session_trades_make_bars_starting_with_the_0845_bar():
    """作者的回測只用日盤 1 分 K（規格第 2 條）。夜盤的成交日期算在次日，所以清晨 05:00 以前
    的夜盤成交帶的是今天的日期——只看日期擋不掉，要看時刻。"""
    outcome = _run(FakeBroker(events=[
        trade(at("04:59:00"), 44800),          # 夜盤
        trade(at("08:44:59"), 44950),          # 開盤前
        trade(at("08:45:00"), 45000),
        trade(at("08:45:30"), 45010),
        trade(at("08:46:00"), 45020),
    ]))
    assert _ohlc(outcome) == [("08:45", 45000, 45010, 45000, 45010)]


def test_a_trade_from_a_minute_already_closed_does_not_reopen_it():
    """K 棒一結束就交出去了（之後的策略判斷用的就是它），不能回頭改，也不能因為一筆
    遲到的成交再開一根舊的。"""
    outcome = _run(FakeBroker(events=[
        trade(at("08:45:00"), 45000),
        trade(at("08:46:00"), 45020),
        trade(at("08:45:59"), 44000),          # 遲到
        trade(at("08:46:30"), 45030),
        trade(at("08:47:00"), 45025),
    ]))
    assert _ohlc(outcome) == [
        ("08:45", 45000, 45000, 45000, 45000),
        ("08:46", 45020, 45030, 45020, 45030),
    ]


def test_the_trading_products_trades_neither_make_bars_nor_are_recorded(market_dir):
    """串流裡也有交易商品（預設是微台）的成交，價格和大台不同。1 分 K 和原始行情都只是
    大台的（規格第 1、58 條）：混進來的話，開高低收、之後的 RSI 與條件判斷都不再是大台的，
    要長期保留的原始行情也被污染。交易商品的現價只給「部位不虧」用（ticket 04）。"""
    outcome = _run(FakeBroker(events=[
        trade(at("08:45:00"), 45000),
        product_trade(at("08:45:00"), 44990),
        product_trade(at("08:45:30"), 45050),
        trade(at("08:45:40"), 45010),
        product_trade(at("08:46:00"), 44900),
        trade(at("08:46:10"), 45020),
        trade(at("08:47:00"), 45030),
    ]))
    assert _ohlc(outcome) == [
        ("08:45", 45000, 45010, 45000, 45010),
        ("08:46", 45020, 45020, 45020, 45020),
    ]
    assert _raw_lines(market_dir) == [
        "time,price,buy_count,sell_count",
        "2026-11-09T08:45:00.000,45000,,",
        "2026-11-09T08:45:40.000,45010,,",
        "2026-11-09T08:46:10.000,45020,,",
        "2026-11-09T08:47:00.000,45030,,",
    ]


def _bichai(outcome: intraday.SessionOutcome) -> list[tuple]:
    return [(f"{b.bar.start:%H:%M}", b.bichai) for b in outcome.bars]


def test_each_bar_records_the_bichai_as_of_its_end():
    """筆差 = 成交賣出筆數 − 成交買進筆數，取 K 棒結束那一刻最新的累計值（規格第 1、7 條）。
    每一根都取它結束前最後收到的那一次（這裡兩根都在 :50），之前收到的被蓋掉。"""
    outcome = _run(FakeBroker(events=[
        counts(at("08:44:50"), buy=100, sell=100),
        trade(at("08:45:00"), 45000),
        counts(at("08:45:10"), buy=150, sell=300),
        trade(at("08:45:30"), 45010),
        counts(at("08:45:50"), buy=200, sell=420),
        trade(at("08:46:00"), 45020),
        counts(at("08:46:50"), buy=260, sell=400),
        trade(at("08:47:00"), 45000),
    ]))
    assert _bichai(outcome) == [("08:45", 220), ("08:46", 140)]


def _raw_lines(market_dir, day: date = TRADING_DAY) -> list[str]:
    path = market_dir / f"TX-{day:%Y%m%d}.csv"
    return path.read_text(encoding="utf-8").splitlines()


# --- 原始行情（規格第 58 條）---


def test_the_days_raw_market_file_records_each_trade_and_count_in_the_order_received(
        market_dir):
    """欄位只有時間、價格、買進筆數、賣出筆數——沒有任何帳號資訊，所以能長期保留。
    成交的時間是交易所的，筆數的時間是收到的時刻（群益的筆數事件不帶時間）。"""
    _run(FakeBroker(events=[
        counts(at("08:44:50"), buy=100, sell=100),
        trade(at("08:45:00"), 45000),
        counts(at("08:45:10.250"), buy=150, sell=300),
        trade(at("08:46:00"), 45020.5),
    ]))
    assert _raw_lines(market_dir) == [
        "time,price,buy_count,sell_count",
        "2026-11-09T08:44:50.000,,100,100",
        "2026-11-09T08:45:00.000,45000,,",
        "2026-11-09T08:45:10.250,,150,300",
        "2026-11-09T08:46:00.000,45020.5,,",
    ]


def test_a_restart_on_the_same_day_appends_to_that_days_raw_file(market_dir):
    """盤中程式當掉被重啟時（ticket 09），重啟前的行情要留著：重啟後的筆差只讀得回那裡。"""
    _run(FakeBroker(events=[trade(at("09:00:00"), 45000)]))
    _run(FakeBroker(events=[trade(at("09:30:00"), 45100)]))
    assert _raw_lines(market_dir) == [
        "time,price,buy_count,sell_count",
        "2026-11-09T09:00:00.000,45000,,",
        "2026-11-09T09:30:00.000,45100,,",
    ]


# --- 報價交易日（規格第 10 條）---

FRIDAY = date(2026, 11, 6)           # TRADING_DAY 的前一個交易日


def test_quotes_from_the_previous_trading_day_end_the_session_without_bars_or_a_raw_file(
        market_dir):
    """臨時休市（颱風假）：日曆說今天要開盤，訂閱回補的卻是前一個交易日的成交，
    之後再也沒有新的。到 13:20 都沒有今天的成交，這一天就沒有屬於它的報價。

    結論要到 13:20 才下：開盤時報價中斷的話，前面看起來一模一樣（見下一個測試）。
    等的代價是零——別的交易日的成交本來就不組 K 棒、不會被拿去判斷。"""
    outcome = _run(FakeBroker(events=[
        clock(at("08:40:00")),
        trade(at("08:45:00", FRIDAY), 44000),
        trade(at("13:44:59", FRIDAY), 44100),
        counts(at("08:40:01"), buy=52000, sell=51000),
        clock(at("08:45:00")),
        clock(at("08:46:00")),
        clock(at("10:00:00")),
        clock(at("13:19:59")),
        clock(at("13:20:00")),
    ]))
    assert outcome.ending is intraday.Ending.QUOTES_FROM_ANOTHER_DAY
    assert outcome.bars == ()
    assert list(market_dir.iterdir()) == [], "連筆數也不能寫：那一天沒有屬於它的行情"


def test_yesterdays_ticks_replayed_before_the_open_are_neither_traded_on_nor_recorded(
        market_dir):
    """一般交易日 08:40 訂閱時，回補的也可能是前一個交易日的成交。它們不屬於今天：
    不組 K 棒、不寫進今天的原始行情。開盤前收到的筆數則是今天的，照樣記下。"""
    outcome = _run(FakeBroker(events=[
        trade(at("13:44:59", FRIDAY), 44100),
        counts(at("08:40:01"), buy=0, sell=0),
        clock(at("08:44:59")),
        trade(at("08:45:00"), 45000),
        trade(at("08:46:00"), 45020),
        clock(at("08:46:00")),
        clock(at("08:46:01")),
    ]))
    assert outcome.ending is not intraday.Ending.QUOTES_FROM_ANOTHER_DAY
    assert _ohlc(outcome) == [("08:45", 45000, 45000, 45000, 45000)]
    assert _raw_lines(market_dir) == [
        "time,price,buy_count,sell_count",
        "2026-11-09T08:40:01.000,,0,0",
        "2026-11-09T08:45:00.000,45000,,",
        "2026-11-09T08:46:00.000,45020,,",
    ]


def test_a_quote_outage_at_the_open_after_yesterdays_backfill_does_not_cost_the_day(market_dir):
    """一般交易日 08:40 訂閱時回補前一個交易日的成交是常態；開盤時報價又斷了幾分鐘的話，
    08:46 過了還是沒有今天的成交——到這裡為止，和臨時休市一模一樣。差別只在今天的成交
    之後會不會來，所以不能在那時收工：今天的成交一到，照常組 K 棒、寫原始行情。"""
    outcome = _run(FakeBroker(events=[
        clock(at("08:40:00")),
        trade(at("13:44:59", FRIDAY), 44100),
        counts(at("08:40:01"), buy=0, sell=0),
        clock(at("08:45:00")),
        clock(at("08:46:00")),
        clock(at("08:50:00")),
        trade(at("08:50:10"), 45000),
        trade(at("08:51:00"), 45020),
        clock(at("08:51:01")),
    ]))
    assert _ohlc(outcome) == [("08:50", 45000, 45000, 45000, 45000)]
    assert _raw_lines(market_dir) == [
        "time,price,buy_count,sell_count",
        "2026-11-09T08:40:01.000,,0,0",
        "2026-11-09T08:50:10.000,45000,,",
        "2026-11-09T08:51:00.000,45020,,",
    ]


def test_no_quotes_at_all_is_not_taken_as_yesterdays_quotes():
    """什麼都沒收到是報價中斷（ticket 09），不是「報價屬於前一個交易日」——沒有證據
    就不能收工，否則一次開盤時的斷線就賠掉一整天。"""
    outcome = _run(FakeBroker(events=[
        clock(at("08:45:00")),
        clock(at("08:46:00")),
        clock(at("08:47:00")),
        trade(at("08:47:10"), 45000),
        trade(at("08:48:00"), 45020),
    ]))
    assert _ohlc(outcome) == [("08:47", 45000, 45000, 45000, 45000)]


# --- 13:20 結束 ---


def test_it_ends_at_1320_with_the_1319_bar_as_the_last_one(market_dir):
    """13:20 是進場截止，盤中程式在那一刻收工。13:19 那根到 13:20 就結束了，照樣交出來；
    13:20 之後的事件不組 K 棒、也不寫進原始行情。"""
    outcome = _run(FakeBroker(events=[
        trade(at("13:18:30"), 45100),
        counts(at("13:18:40"), buy=1000, sell=1500),
        trade(at("13:19:00"), 45110),
        trade(at("13:19:30"), 45090),
        counts(at("13:19:50"), buy=1100, sell=1550),
        clock(at("13:19:59")),
        clock(at("13:20:00")),
        trade(at("13:20:01"), 45200),
        counts(at("13:20:02"), buy=1200, sell=1700),
    ]))
    assert outcome.ending is intraday.Ending.CUTOFF
    assert _ohlc(outcome) == [("13:18", 45100, 45100, 45100, 45100),
                              ("13:19", 45110, 45110, 45090, 45090)]
    assert _bichai(outcome) == [("13:18", 500), ("13:19", 450)]
    assert _raw_lines(market_dir)[-1] == "2026-11-09T13:19:50.000,,1100,1550"


def test_a_bar_that_ends_before_any_count_arrives_has_no_bichai_rather_than_zero():
    """筆差 0 是「買賣筆數相等」，是一個會被拿去判斷的值；沒收到就是不知道。"""
    outcome = _run(FakeBroker(events=[
        trade(at("08:45:00"), 45000),
        trade(at("08:46:00"), 45020),
    ]))
    assert _bichai(outcome) == [("08:45", None)]
