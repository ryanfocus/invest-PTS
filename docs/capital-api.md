# 群益 API：PTS 行情事件的出處

程式說明裡「細節與出處見 docs/capital-api.md 第 N 節」指的就是這份。內容是 2026-11-09 開工前
整理的事實筆記，2026-11-10 把被程式引用的第 1–4 節搬進 repo（第 0 節只留和這四節有關的前四點）。
只查文件、範例與 invest-os 留下的實機證據，**沒有連券商**；標【推測】的都還是未知。
實機之後的結論記在各 ticket 的 Comments（ticket 03、04、11），不回頭改這份。

## 出處標記

| 標記 | 意思 |
|---|---|
| 【文件 4-4-9】 | 群益 SDK `CapitalAPI_2.13.58_PythonExample` 裡《策略王COM元件使用說明_V2.13.58》的章節編號 |
| 【文件 版本 2.13.xx】 | 同一份文件開頭「版本控管」表的那一列 |
| 【V2文件 13】／【V2文件 4】 | SDK `PythonExampleV2` 的說明文件《13.國內報價》／《4.下單準備介紹》 |
| 【看我】 | SDK 裡的《群益PythonAPI使用前看我看我》 |
| 【包裝:行】 | comtypes 依 2.13.58 型別庫產生的介面檔（`comtypes/gen/_75AAD71C_8F4F_4F1F_9AEE_3D41A8C9BA5E_0_1_0.py`）的行號。COM 的真實參數方向與事件名稱以它為準 |
| 【範例 檔:行】 | SDK 的範例程式，路徑相對於 `CapitalAPI_2.13.58_PythonExample` |
| 【OS 檔:行】 | invest-os 裡的檔案（程式、fixture、工具） |
| 【實測】 | invest-os 留下的真機證據（fixture、樣本檔） |
| 【推測】 | 推論，**沒有文件或實測支持**，實作前要當成未知 |

引用文件請用章節編號：docx 轉成純文字之後行號沒有意義。

---

## 0. 先讀這段

1. **筆差的來源事件沒有日期也沒有時間。** `OnNotifyFutureTradeInfoLONG` 只有商品代碼和六個累計數字。時間要由 adapter 自己蓋，交易日檢查只能靠別的事件（逐筆成交的 `nDate`）。文件也沒說它從何時開始累計、多久推一次、收不收 `TX00AM`。這些都要靠 ticket 03 實機回答。
2. **筆差沒有歷史回補。** 逐筆成交第一次訂閱時會補送當天的 tick，K 線也有查詢函式，但筆差沒有任何回補管道。盤中重啟之後，重啟前每根 K 棒結束時的筆差，**只能從 PTS 自己的原始行情檔讀回來**。這直接影響 ticket 09，也影響重啟後第一根 K 棒的筆差跳增。
3. **API 沒有每秒一次的時鐘。** `OnTimer` 每分鐘一次，`OnNotifyServerTime` 只在呼叫查詢後回來。每秒的時鐘事件只能由 adapter 在訊息幫浦迴圈裡用本機時鐘產生。
4. **開盤前訂閱逐筆成交，補送的可能是上一個交易日的 tick。** 另外 08:30 起有試撮 tick（`nSimulate=1`）。兩者都要濾掉：`nDate` 必須是今天，`nSimulate` 必須是 0。

---

## 1. 筆差：`SKQuoteLib_RequestFutureTradeInfo` 與 `OnNotifyFutureTradeInfoLONG`

### 訂閱

- 宣告：`Long SKQuoteLib_RequestFutureTradeInfo([in] SHORT* psPageNo, [in] BSTR bstrStockNo)`【文件 4-4-9】
- `psPageNo` 是 `[in]` 指標，不是 `[in,out]`，所以 comtypes **只回傳一個整數**，不是 tuple【包裝:632-636】。這和 `RequestTicks`、`RequestStocks` 回 `(page, code)` 不同。
- 呼叫寫法照範例，傳 `ctypes.c_short`：`code = quote.SKQuoteLib_RequestFutureTradeInfo(ctypes.c_short(0), "TX00AM")`【範例 PythonExampleV2\Quote\Quote\Quote.py:1885-1887】【範例 PythonExample\Quote_Service\Quote.py:1096】
- 一個 page 只能訂一檔【文件 4-4-9】。
- 沒簽期貨 API 下單同意書時回 3031【文件 4-4-9】。
- 事件只在用 `SKQuoteLib_EnterMonitorLONG` 連線時才會觸發【文件 4-4-q 備註；4-4-25 備註】。
- **文件沒有取消訂閱的函式。** 舊範例的「取消」按鈕仿照 MACD 的慣例用 page 50，但程式實際傳的是 0，等於重新訂閱【範例 PythonExample\Quote_Service\Quote.py:1118-1120】。不要依賴取消。

### 事件

`void OnNotifyFutureTradeInfoLONG(BSTR bstrStockNo, SHORT sMarketNo, LONG nStockidx, LONG nBuyTotalCount, LONG nSellTotalCount, LONG nBuyTotalQty, LONG nSellTotalQty, LONG nBuyDealTotalCount, LONG nSellDealTotalCount)`【文件 4-4-q】【包裝:1182】

| 參數 | 文件說法 | PTS 用不用 |
|---|---|---|
| `bstrStockNo` | 商品代號 | 用來分辨是哪一個訂閱回來的 |
| `sMarketNo`、`nStockidx` | 市場別、系統索引 | 可以拿去 `GetStockByIndexLONG` 查商品資料 |
| `nBuyTotalCount`／`nSellTotalCount` | 總**委託**買進／賣出筆數 | **不用**（是委託，不是成交） |
| `nBuyTotalQty`／`nSellTotalQty` | 總委託買進／賣出口數 | 不用 |
| `nBuyDealTotalCount` | 總**成交**買進筆數 | 筆差的減數 |
| `nSellDealTotalCount` | 總**成交**賣出筆數 | 筆差的被減數 |

⚠️ **結構欄位是 `nStockIdx`（I 大寫），不是文件與範例事件參數寫的 `nStockidx`。** `SKSTOCKLONG._fields_` 只有 `nStockIdx`【包裝:1846-1847】；事件參數照位置傳，名字不算數，結構讀錯名字則是 AttributeError（ticket 02 審查發現，實機會在第一個行情事件前當掉）。`tests/fake_skcom.py` 的 `SKSTOCKLONG`／`FUTUREORDER` 只認型別庫裡的欄位名。

- 依 `GLOSSARY.md`：**筆差 = `nSellDealTotalCount − nBuyDealTotalCount`**。
- 事件**沒有日期、沒有時間**。

### 文件沒有回答的事（全部要實機驗證，ticket 03）

| 問題 | 現況 |
|---|---|
| 是不是累計值 | 欄位名都帶「總」，文件沒有定義。【推測】是當盤累計 |
| 從什麼時候開始累計，含不含夜盤 | 文件沒寫 |
| 多久推一次 | 文件沒寫。【推測】有變動才推 |
| 能不能用 `TX00AM` | `RequestStocks`、`RequestTicks` 的備註明寫可以加 `AM` 取純日盤行情【文件 4-4-2、4-4-3】；`RequestFutureTradeInfo` **完全沒提**。範例的商品代號是使用者輸入的，看不出來 |
| 開盤前（08:40）推來的是什麼 | 文件沒寫。【推測】可能是上一盤的累計值，或試撮期間的數字 |

【推測】建議 ticket 02 錄原始行情時，`TX00AM` 和 `TX00` 各開一個 page 一起訂，連同 `bstrStockNo` 與本機收到時刻一起寫檔。一週後就能看出哪個代碼有回應、兩者差在哪裡、什麼時候歸零。

---

## 2. 逐筆成交價

### 三條路

| 函式 | 事件 | 歷史回補 | 說明 |
|---|---|---|---|
| `SKQuoteLib_RequestTicks([in,out] SHORT* psPageNo, BSTR bstrStockNo)` | `OnNotifyHistoryTicksLONG`（回補）＋`OnNotifyTicksLONG`（即時）＋`OnNotifyBest5LONG` | **有**：「當首次索取個股成交明細，此事件會回補當天Tick」【文件 4-4-l】 | 【文件 4-4-3】 |
| `SKQuoteLib_RequestLiveTick([in,out] SHORT* psPageNo, BSTR bstrStockNo)` | 只有 `OnNotifyTicksLONG` | **沒有** | 「與SKQuoteLib_RequestTicks不同，不會回補歷史成交明細資料，請擇一使用」【文件 4-4-18】 |
| `SKQuoteLib_RequestStocks` ＋ `OnNotifyQuoteLONG(sMarketNo, nIndex)` ＋ `GetStockByIndexLONG` | 報價快照 | — | 快照有 `nClose`、`nDealTime`（hhmmss）、`nTickQty`、`nTradingDay`【文件 5-21】。【推測】快照可能合併多筆成交，不保證每一筆都看得到 |

- comtypes：`RequestTicks`／`RequestLiveTick`／`RequestStocks` 都回 `(psPageNo, retCode)`【包裝:550-554、726-730、534-538】。invest-os 的寫法是 `page_no, ret = quote.SKQuoteLib_RequestTicks(0, code)`【OS tools/compare_open.py:70-71】。
- `RequestTicks`／`RequestLiveTick` 的 page「請從0開始」，一個 page 一檔【文件 4-4-3、4-4-18】。
- `RequestStocks` 的 page 自 2.13.54 起「請固定帶1」，「一般用戶目前PageNo上限為1」【文件 4-4-2 修改比較表】。invest-os 一直傳 0，實際上也拿得到報價【OS broker/capital.py:663-664】。
- 數量限制：「單一SKQuoteLib物件，僅一個訂閱Tick報價功能(10檔商品)及一個RequestStocks訂閱即時報價功能(100檔)」【文件 4-4-1 備註】。
- 不要在 `OnNotifyTicksLONG`／`OnNotifyHistoryTicksLONG` 裡呼叫 `GetTickLONG` 或 `GetStockByIndexLONG`【文件 4-4-l、4-4-m、4-4-28 備註】。
- 不要在 `OnConnection` 裡直接訂閱【文件 4-4-a 備註】。

### 事件簽章

`OnNotifyTicksLONG(SHORT sMarketNo, LONG nIndex, LONG nPtr, LONG nDate, LONG nTimehms, LONG nTimemillismicros, LONG nBid, LONG nAsk, LONG nClose, LONG nQty, LONG nSimulate)`。`OnNotifyHistoryTicksLONG` 的參數完全相同【文件 4-4-l、4-4-m】【包裝:1177-1178】。

| 欄位 | 格式 | 出處 |
|---|---|---|
| `nPtr` | 資料位址（key），由 0 開始 | 【文件 4-4-m、5-6】。【推測】可以拿來去除回補與即時重複的那幾筆 |
| `nDate` | 交易日 `YYYYMMDD` | 【文件 4-4-m】 |
| `nTimehms` | 整數 hhmmss。08:45:00 是 `84500`，**前導零會不見** | 【文件 4-4-m】【實測 OS state/open-price-samples.jsonl：`first_real_tick_time: 84500`】 |
| `nTimemillismicros` | 毫秒＋微秒。文件註明「目前solace只提供證券商品」 | 【文件 4-4-m】。【推測】期貨是 0 |
| `nClose` | 成交價，整數、放大 100 倍（`sDecimal` 位小數） | 【範例 PythonExampleV2\Quote\Quote\Quote.py:246-253 用 `/100.0`】【看我：`nOpen/math.pow(10,pStock.sDecimal)`】【實測 OS tools/compare_open.py:58 用 `/100.0` 且與期交所一致】 |
| `nQty` | 成交量 | 【文件 4-4-m】 |
| `nSimulate` | 0 一般揭示、1 試算揭示（試撮）。「未進行揭示處理，開發人員需自行接手處理」 | 【文件 4-4-m】 |

⚠️ V2 範例用 `str(nTimehms)[:2]` 取小時【範例 PythonExampleV2\Quote\Quote\Quote.py:259-261】，10 點以前會切錯（`84500` 變成 84 時）。要用整數除法。

### 日盤、夜盤與試撮

- 代碼加 `AM`（`TX00AM`）取純 AM 盤行情；下單不能用帶 `AM` 的代碼【文件 4-4-2、4-4-3 備註】。invest-os 的三個報價代碼與實測見 `invest-os\docs\adr\0005-use-am-session-quote-codes.md`。
- 「T盤切換T+1盤，不保留前一盤資料，開發者需自行清除前一盤資料」【文件 4-4-l、4-4-m 備註】。
- 【實測】08:30 起就有成交明細，但那是試撮。2026-08-10 最早的試撮價 44981，真正的開盤價 44985 在 08:45:00【OS tools/compare_open.py:44-49】。
- 【實測】`TX00AM` 用 `RequestTicks` 取得的第一筆非試撮 tick 時間是 `84500`，價格與 `nOpen`、期交所開盤價一致（2026-08-16、08-17、08-19 三筆）【OS state/open-price-samples.jsonl】。

### 開盤前訂閱會補送什麼

- 【實測】同一份樣本的第一筆是 **2026-08-16（週日）22:25** 抓的，仍然拿到 `TX00AM` 時間 `84500` 的非試撮 tick。休市時回補的是**最近一個 AM 盤**（週五）的 tick【OS state/open-price-samples.jsonl 第 1 行】。
- 【實測】2026-08-17 08:45:11 抓的那筆，`nOpen` 是 0，也還沒有非試撮 tick【同檔第 2 行】。
- 【推測】08:40 啟動時，回補內容可能是前一交易日的 AM 盤，也可能是當天的試撮。**一律以 `nDate == 今天` 且 `nSimulate == 0` 過濾**；這也是規格第 10 條「報價交易日不是今天就不判斷」最直接的資料來源，因為筆差事件本身沒有日期。
- invest-os 另外在 `SKSTOCKLONG.nTradingDay` 上觀察到同一件事：休市時給上一交易日的資料（「當日非交易日時, 資料為前一交易日」【文件 5-21】；2026-08-09 週日拿到 08-07 的價格【OS broker/__init__.py:249-255】）。

### 建議（推測）

- 大台：`RequestTicks("TX00AM")`。回補可以在盤中重啟時重建當天的 1 分 K（見 3 節），即時事件給突破進場盯價。
- 交易商品（`MTX00AM`／`TM0000AM`）：同樣用 `RequestTicks`，或用 `RequestStocks` 快照取現價。部位不虧只需要現價，不需要每一筆。

---

## 3. 1 分 K 歷史：`SKQuoteLib_RequestKLineAMByDate`

### 呼叫

`Long SKQuoteLib_RequestKLineAMByDate(BSTR bstrStockNo, SHORT sKLineType, SHORT sOutType, SHORT sTradeSession, BSTR bstrStartDate, BSTR bstrEndDate, SHORT sMinuteNumber)`【文件 4-4-24】【包裝:768-777，全部是 `[in]`，回傳一個整數】

| 參數 | PTS 要填的值 | 文件說明 |
|---|---|---|
| `bstrStockNo` | 見下方「用哪個代碼」 | 商品代號 |
| `sKLineType` | `0` | 0 分線、4 日線、5 週線、6 月線 |
| `sOutType` | `1` | 0 舊版、1 新版輸出格式 |
| `sTradeSession` | `1` | 僅國內期權有效。0 全盤、1 AM 盤。「國內期選—AM盤:當日T盤」 |
| `bstrStartDate`／`bstrEndDate` | `"YYYYMMDD"` | 例 `20201001`／`20201010` |
| `sMinuteNumber` | `1` | 幾分 K，`sKLineType=0` 時才有意義 |

- 全盤的範例：「指定日期區間為2023/9/5全盤K LINE，將包含2023/9/4 17:25開盤後~2023/9/5 05:00，及2023/9/5 8:45~16:15」【文件 4-4-24 備註】。
- 標題寫明「（僅提供歷史資料）」【文件 4-4-24】。【推測】盤中查今天的 K 棒可能不完整或沒有。重啟時補今天的 K 棒，較可靠的來源是 `RequestTicks` 的當日回補（2 節），而不是這個函式。
- 「目前僅提供1分鐘K，其他5分鐘、30分鐘K Line可自行以1分鐘K Line為基礎組成」【文件 4-4-16 備註】。
- 用哪個代碼：文件的例子只有股票代號。【推測】用 `TX00` 加 `sTradeSession=1`；`TX00AM` 收不收文件沒說。實機確認一次。
- 沒有期貨帳戶就拿不到期選 K 線【文件 4-4-24 備註】。

### 回傳：`OnNotifyKLineData(BSTR bstrStockNo, BSTR bstrData)`【文件 4-4-b】【包裝:1163】

- 新版 1 分線格式：`(年/月/日, 時:分, 開盤價, 最高價, 最低價, 收盤價, 成交量)`。文件的例子是 `2021/8/2 09:01 12.95,12.95,12.95.12.95,5`。
- ⚠️ 文件的例子本身就不一致：日期與時間之間是空白不是逗號、月日沒有補零、有一個逗號打成句點。**解析要寬鬆，並且用第一次實機收到的字串做 fixture 把格式釘死。**
- 新版的價格「已進行過小數點處理」，不用除 100；舊版要除 100【文件 4-4-b】。
- 沒有請求序號，只能用 `bstrStockNo` 對應。【推測】一次只送一個查詢，等收完再送下一個。

### 怎麼知道送完了

- `OnKLineComplete(BSTR bstrEndString)`，「收完歷史KLine，等收到此事件通知後表示回補完成」，參數是結尾字串 `"##"`【文件 4-4-t】【包裝:1185】。
- V2 範例沒有實作這個事件。

### 文件沒有回答的事

- **K 棒的時間標的是起點還是終點**（`09:01` 是 09:00–09:01 那根，還是 09:01–09:02）。這決定程式自己組的 K 棒和券商 K 棒怎麼對齊，也決定 08:45 那根叫 `08:45` 還是 `08:46`。要用實機資料和看盤軟體核對（ticket 02 的實機項目）。
- 一個交易日 AM 盤有幾根（應該是 08:45–13:45，300 根左右）。

### 昨收

- 規格要的是前一交易日 AM 盤最後一根 K 棒的收盤價。
- `SKSTOCKLONG.nRef` 的說明是「昨收、參考價」【文件 5-21】。規格明確排除參考價，**不要用 `nRef`**。
- 要和期交所核對時，invest-os 的 `taifex.fetch_ohlc` 能取「一般」時段的 OHLC【OS taifex.py:31】。

---

## 4. 每秒時鐘

| 來源 | 頻率 | 出處 |
|---|---|---|
| `SKCenterLib` 的 `OnTimer(LONG nTime)` | **每分鐘一次**。`nTime` 是 hhmmss 整數，例 `133525` | 【文件 4-1-a】【包裝:228】 |
| `OnNotifyServerTime(SHORT sHour, SHORT sMinute, SHORT sSecond, LONG nTotal)` | 呼叫 `SKQuoteLib_RequestServerTime()` 之後回來。「報價連線成功後，即會回傳主機時間」 | 【文件 4-4-5、4-4-c】【看我「主機時間」節】 |
| 逐筆成交的 `nTimehms` | 每筆成交 | 2 節 |

- `RequestServerTime` 的說明：「為避免收盤後無報價資料傳送，導致連線被防火牆切斷，目前solace固定每五秒會自動更新時間，請固定每十五秒呼叫此函式，確保連線正常」【文件 4-4-5】。
- **結論：API 沒有每秒一次的事件。** 規格要的「每秒一次的時鐘事件」只能由 adapter 在訊息幫浦迴圈裡用本機時鐘產生。invest-os 的幫浦迴圈每 50 毫秒醒一次【OS broker/capital.py:288-298】，在裡面每跨過一個整秒送一個時鐘事件即可。
- 【推測】順便每 15 秒呼叫一次 `RequestServerTime`，當作保持連線，也能拿主機時間檢查本機時鐘有沒有漂。
