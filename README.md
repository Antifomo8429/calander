# TWSE 競價拍賣公告自動更新行事曆

這個專案會把臺灣證交所「競價拍賣公告」自動轉成可訂閱的行事曆（`.ics` 檔）。

資料來源頁面：
<https://www.twse.com.tw/zh/announcement/auction.html>

---

## 你會得到什麼

- 一個可訂閱的日曆檔：`calendar/twse-auction.ics`
- 每天自動更新（GitHub Actions 排程）
- 事件內容包含：
  - 投標開始日
  - 投標結束日
  - 開標日期
  - 撥券/掛牌日期
  - **拆解日**（可轉債專用，掛牌日起算第 6 個交易日）
- 可轉債的事件標題會直接帶上轉換價，例如
  `[TWSE競拍] 博智三(81553) 投標開始｜轉換價 318`

---

## 轉換價是怎麼來的

證交所的競拍 API **沒有轉換價欄位**，轉換價只存在於承銷商公會（twsa.org.tw）
的公告 PDF 裡，兩邊沒有共同識別碼，所以要自己抓 PDF 再比對回來。

本專案抓兩種公告，先到先用：

| 來源 | 上架時間 | 比對方式 |
| --- | --- | --- |
| 承銷公告（UnderwritingNotice） | 承銷商申報時，比投標日早約 10~14 天 | 公司簡稱 + 主辦券商 + 申報日在投標日之前 |
| 競拍公告（Auction） | 幾乎在投標開始當天 | 投標開始日 + 投標結束日（同期間多檔時再比公司與券商） |

PDF 內文的寫法是：

> 係以 115 年 7 月 24 日為轉換價格基準日，取其前一、三、五個營業日普通股收盤價之
> 簡單算術平均數(324.5 元、311.2 元與 303.5 元)擇一者(經選定為 311.2 元)，
> 乘以轉換溢價率 102.19% 後即為**每股轉換價格為 318 元**。

所以只認「每股轉換價格為 X 元」這種結論句，並用「基準價 × 溢價率」交叉驗證，
避免抓到括號裡的均價（324.5）當成轉換價。

---

## 拆解日

`拆解日 = 撥券／掛牌日起算第 6 個交易日（掛牌日本身算第 1 個交易日）`

例：博智三掛牌日 2026/08/13（四）
→ 8/13(1)、8/14(2)、8/17(3)、8/18(4)、8/19(5)、**8/20(6) 就是拆解日**。

交易日以證交所「市場開休市日期」為準，快取在 `scripts/market_holidays.json`。
若掛牌日落在還沒公告休市日曆的年度（通常是跨年），會退回「只排除週末」推算，
並在事件說明裡標註「推估」。颱風假這種臨時休市也無法事先預知，同樣會偏早。

要改天數，調整 `scripts/generate_twse_auction_calendar.py` 的
`SPLIT_TRADING_DAY_NTH`（`notify_discord.py` 內有同名常數需一併調整）。

---

## 專案內的重要檔案（白話解釋）

- `scripts/generate_twse_auction_calendar.py`  
  抓取證交所資料，轉成 `.ics` 行事曆檔。

- `scripts/fetch_conversion_prices.py`  
  到承銷商公會抓公告 PDF，解析轉換價，存成 `scripts/conversion_prices.json`。
  已經抓到價格的案件不會重抓；加 `--recheck` 可以強制全部重驗。

- `scripts/conversion_price_index.py`  
  把轉換價對回證交所每一檔可轉債的比對邏輯。

- `scripts/trading_calendar.py`  
  台股交易日曆，用來算拆解日。

- `scripts/notify_discord.py`  
  比對前後快照，有變動（含轉換價後補上來）時發 Discord 通知。

- `.github/workflows/update-twse-auction-calendar.yml`  
  GitHub 的自動排程設定。每天會自動執行上面的 Python 程式。

- `calendar/twse-auction.ics`  
  最終可訂閱的日曆檔。

---

## 如何手動產生日曆（一次）

> 如果你只想先試一次，可以在專案根目錄執行：

```bash
python3 scripts/generate_twse_auction_calendar.py --output calendar/twse-auction.ics
```

---

## 如何取得「可訂閱」連結

當這個專案放在 GitHub，且 `calendar/twse-auction.ics` 已存在時，可用下列格式：

```text
https://raw.githubusercontent.com/<你的GitHub帳號>/<你的Repo名稱>/main/calendar/twse-auction.ics
```

把上面 `<...>` 換成你的實際資訊即可。

---

## 匯入到常見行事曆

### Google 日曆
1. 打開 Google Calendar
2. 左側「其他日曆」旁邊按 `+`
3. 選「透過網址新增」
4. 貼上 `.ics` 連結
5. 確認新增

### Apple Calendar（macOS / iOS）
1. 開啟 Calendar
2. 選「新增訂閱行事曆」
3. 貼上 `.ics` 連結
4. 設定更新頻率（建議每天）

---

## 注意事項

- 證交所公告內容若異動，行事曆會在排程後自動更新。
- 日曆平台本身（Google/Apple）可能有自己的快取時間，通常不會立刻反映。
- 資料以證交所公告為準。
