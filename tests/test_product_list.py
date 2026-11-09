"""商品清單 —— 近月的下單代碼與最後交易日，都來自群益的商品清單，不由日期推算。

結算日由最後交易日判定（規格第 24 條）：「每月第三個週三」在颱風假順延時會失準。
樣本取自 invest-os 2026-08-07 實際抓到的商品清單（獨立的事實來源）。
"""

from datetime import date

import pytest

from broker import MTX_CODE, TMF_CODE, TX_CODE, ProductListUnavailable
from broker.capital_wire import parse_product_list

# 實際回傳格式：「%類別碼%類別名%」標頭，接著以「;」分隔的
# 「商品代碼,名稱,最後交易日,交易所代碼」
REAL_SAMPLE = (
    "%201%期指數%"
    "TX00AM,台指近,20260819,TXFH600;"
    "TX08AM,台指08,20260819,TXFH6;"
    "TX09AM,台指09,20260916,TXFI6;"
    "MTX00AM,小台近,20260819,MXFH600;"
    "MTX08AM,小台08,20260819,MXFH6;"
    "TM0000AM,微台近,20260819,TMFH600;"
    "TM2608AM,微台2608,20260819,TMFH6;"
    "TX00,台指近,20260819,TXFH600;"
)


def test_each_product_gets_its_month_specific_order_code():
    """下單要帶指名月份的代碼，近月連續代碼（TX00AM）是報價用的。

    三個商品的格式互不相同——大台 TX08、小台 MTX08、微台 TM2608（多了年份）——
    自己組字串一定會在某一個上錯，所以只能從清單讀。"""
    contracts = parse_product_list(REAL_SAMPLE)
    assert contracts[TX_CODE].order_code == "TX08"
    assert contracts[MTX_CODE].order_code == "MTX08"
    assert contracts[TMF_CODE].order_code == "TM2608"


def test_the_last_trading_day_is_read_from_the_list():
    contracts = parse_product_list(REAL_SAMPLE)
    for code in (TX_CODE, MTX_CODE, TMF_CODE):
        assert contracts[code].last_trading_day == date(2026, 8, 19)
    assert contracts[TX_CODE].contract_month == "202608"


def test_a_later_month_is_not_mistaken_for_the_near_month():
    """清單裡同時有 TX08 與 TX09，靠最後交易日分開。挑錯就交易到下個月的合約。"""
    assert parse_product_list(REAL_SAMPLE)[TX_CODE].order_code != "TX09"


def test_a_category_header_in_the_middle_is_stripped_too():
    sample = REAL_SAMPLE.replace("TM0000AM,", "%202%期指數二%TM0000AM,")
    assert parse_product_list(sample)[TMF_CODE].order_code == "TM2608"


def test_broken_entries_are_skipped_not_fatal():
    """清單裡混了殘缺列很正常，只要三個商品都在就該通過。"""
    noisy = REAL_SAMPLE + "壞掉的一列;,,,;X;TX10AM,台指10,2026101,TXFJ6;"
    assert parse_product_list(noisy)[TX_CODE].order_code == "TX08"


def test_a_missing_product_raises_rather_than_returning_half():
    without_micro = REAL_SAMPLE.replace("TM0000AM,微台近,20260819,TMFH600;", "")
    with pytest.raises(ProductListUnavailable, match=TMF_CODE):
        parse_product_list(without_micro)


def test_a_missing_month_code_raises_rather_than_guessing():
    """找不到指名月份的代碼時，絕不退而求其次用連續代碼——那能不能下單沒有文件保證。"""
    without_month = REAL_SAMPLE.replace("TX08AM,台指08,20260819,TXFH6;", "")
    with pytest.raises(ProductListUnavailable, match="TX"):
        parse_product_list(without_month)


def test_an_empty_list_raises():
    with pytest.raises(ProductListUnavailable):
        parse_product_list("")
