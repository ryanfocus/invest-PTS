"""實機唯讀檢查：登入群益，印出大台、小台、微台的近月下單代碼與最後交易日。

用法（在專案根目錄）：

    python tools/show_contracts.py

用途是在新機器或換版之後，確認「登入 → 連報價主機 → 查商品清單」這條路是通的，
而且查到的下單代碼與結算日看起來合理。順便可以看一次 `logs/capital/`，
確認群益元件的 log 有寫到那裡（規格第 60 條）。

## 為什麼它保證不會下單

1. **啟動的第一件事是檢查自己的原始碼**：裡面出現任何送單函式（`place_order`、
   `Send...Order...`），就拒絕執行。這發生在 import 專案的任何模組之前。
2. 建立 broker 時**刻意不給期貨帳號**——就算有人繞過第 1 點，送單也會在
   送出之前就以「未設定期貨帳號」失敗。

需要 `.env` 裡的 `CAPITAL_USER_ID` 與 `CAPITAL_PASSWORD`。
"""

from __future__ import annotations

import ast
import os
import re
import sys

# 送單函式：broker 的 `place_order`，以及群益所有 `Send…Order…` 系列
# （SendFutureOrderCLR、SendOptionOrder、SendOverseaFutureOrder…）。
_ORDER_FUNCTION = re.compile(r"^(place_order|Send\w*Order\w*)$")


def order_functions_in(source: str) -> list[str]:
    """原始碼裡用到的送單函式名稱。看的是**程式碼**，不是說明文字。

    識別字（呼叫、屬性、函式定義、import）全部算；字串只在**整個字串剛好是**
    函式名稱時才算——那是 `getattr(broker, "place_order")` 的寫法。
    句子裡提到函式名稱（像這段說明）不算。
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            names = [node.id]
        elif isinstance(node, ast.Attribute):
            names = [node.attr]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = [node.name]
        elif isinstance(node, ast.alias):
            names = [node.name.rsplit(".", 1)[-1], node.asname or ""]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names = [node.value]
        else:
            continue
        found += [n for n in names if _ORDER_FUNCTION.match(n)]
    return found


def refuse_if_it_can_order(path: str = __file__) -> None:
    with open(path, encoding="utf-8") as fh:
        found = order_functions_in(fh.read())
    if found:
        raise SystemExit(
            f"拒絕執行：這支唯讀工具的程式碼裡出現送單函式 {sorted(set(found))}。"
            "它只能查詢，不可以下單。")


_NAMES = (("大台", "TX"), ("小台", "MTX"), ("微台", "TMF"))


def format_contracts(contracts: dict) -> str:
    from broker import PRODUCT_NAMES

    lines = []
    for label, name in _NAMES:
        contract = contracts[PRODUCT_NAMES[name]]
        lines.append(f"{label} {name:<4} 下單代碼 {contract.order_code:<8} "
                     f"最後交易日 {contract.last_trading_day:%Y/%m/%d}")
    return "\n".join(lines)


def main() -> int:
    refuse_if_it_can_order()

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import paths
    import settings
    from broker.capital import CapitalBroker

    secrets = settings.load_secrets(None)
    # 刻意不給期貨帳號：見模組說明「為什麼它保證不會下單」第 2 點
    broker = CapitalBroker(secrets.user_id, secrets.password)
    broker.login()
    print(format_contracts(broker.get_contracts()))
    print(f"\n群益元件的 log 應該寫在：{paths.capital_log_dir()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
