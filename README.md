# 地球科學日報小幫手

每天早上 8 點（台北時間）自動抓取地球物理／地震學為主、其他地球科學領域為輔的最新內容，
交給 Gemini 篩選出真正值得看的項目並寫成雙語摘要（英文在前、中文在後，順便練專業英語），
推播到你的 Discord。完全跑在 GitHub Actions 的免費額度上，不需要自己架伺服器，月成本 0 元。

## 涵蓋來源

**地球物理與地震學（主要焦點）**
- USGS 地震速報（規模 4.5 以上，過去 24 小時，全球）
- EMSC 歐洲地中海地震中心（規模 5.0 以上）
- arXiv physics.geo-ph 地球物理預印本
- AGU：Journal of Geophysical Research – Solid Earth、Geophysical Research Letters
- EGU/Copernicus：Solid Earth 期刊
- Smithsonian Global Volcanism Program 全球火山活動週報

**其他地球科學領域（次要補充）**
- Nature Geoscience
- NHESS（自然災害與地球系統科學）
- Eos（AGU 新聞）

**台灣地震專區**
- 預設：USGS 台灣周邊範圍查詢（北緯 21–26.5、東經 118.5–123.5，規模 4 以上），免金鑰。
- 進階：到[氣象署開放資料平台](https://opendata.cwa.gov.tw/)免費註冊取得授權碼，
  存成 repo secret `CWA_API_KEY`，就會改用「顯著有感地震報告」（含各地震度，資訊更完整）。

想增減來源，直接編輯 `scripts/digest.py` 裡的 `RSS_SOURCES` 清單即可（每個項目要標記
`category` 是 `"seismo"` 還是 `"other"`）。

### 來源品質政策

- **白名單制**：系統只從上面這份人工挑選的權威來源清單抓取，不會自行搜尋網路，
  掠奪性期刊（predatory journals）沒有進入日報的管道。
- **新增來源前請自行把關**：確認出版方信譽，可查 [DOAJ](https://doaj.org/) 收錄狀態、
  出版社是否為 [OASPA](https://oaspa.org/) / [COPE](https://publicationethics.org/) 成員，
  或對照 [Beall's List](https://beallslist.net/) 等掠奪性期刊清單。
- **預印本透明標示**：arXiv 為預印本平台（未經同儕審查），這類來源在程式中標記
  `peer_reviewed: False`，日報中入選標準從嚴，且來源會標示「未經同儕審查」提醒讀者。
- **每則附來源**：日報中每一則都會標明出處期刊／機構名稱，方便你自行判斷可信度。

## 設定步驟

### 1. 建立 GitHub repo
把這個資料夾推到一個新的 GitHub repository（public 或 private 皆可，private 也有每月
2000 分鐘的免費 Actions 額度，這個工作一天只需要跑不到 1 分鐘，完全夠用）。

### 2. 申請 Google Gemini API Key（免費）
1. 前往 [Google AI Studio](https://aistudio.google.com/apikey) 並用你的 Google 帳號登入。
2. 點「Create API key」，複製產生的金鑰。
3. 免費額度：`gemini-2.5-flash` 每天 250 次請求，本專案一天只呼叫 1 次，非常寬裕。

### 3. 建立 Discord Webhook（免費）
1. 在你的 Discord 伺服器（沒有的話開一個個人專用的伺服器也可以）建立或選一個文字頻道。
2. 頻道設定 →「整合 Integrations」→「Webhooks」→「新增 Webhook」。
3. 複製 Webhook URL。
4. 手機裝 Discord App、加入該伺服器，就會在該頻道收到通知。

### 4. 在 GitHub repo 設定 Secrets
到 repo 的 **Settings → Secrets and variables → Actions → New repository secret**，新增兩組：

| Name | 值 |
|---|---|
| `GOOGLE_API_KEY` | 步驟 2 拿到的 Gemini API Key |
| `DISCORD_WEBHOOK_URL` | 步驟 3 拿到的 Discord Webhook URL |
| `CWA_API_KEY`（選填） | 氣象署開放資料授權碼，設定後台灣地震專區改用氣象署顯著有感地震報告 |

### 5. 確認 Actions 已啟用
Repo 的 **Actions** 分頁，如果顯示需要啟用，點一下啟用即可。排程檔案在
`.github/workflows/daily-digest.yml`，預設每天 UTC 00:00（= 台北時間 08:00）執行。

### 6. 手動測試一次
到 **Actions → Daily Earth Science Digest → Run workflow**，手動觸發一次，
確認 Discord 頻道有收到訊息。第一次執行因為 `data/seen.json` 是空的，
可能會抓到比較多候選項目；之後每天只會抓「上次執行之後新出現」的內容。

## 運作方式

1. `scripts/digest.py` 抓取上面所有來源，只保留過去 30 小時內、且還沒推播過的項目
   （靠 `data/seen.json` 去重，重複項目不會再出現）。
2. 把候選清單交給 Gemini，請它以「地球物理與地震學優先、其他領域次要」為原則篩選、
   合併重複事件，輸出結構化 JSON——每則都有英文標題＋英文一句摘要（自然道地的學術
   新聞英語，當英文教材用）與中文標題＋中文重點（不是直譯），導言也是英中雙語。
3. 組成三張 Discord Embed 卡片推播：湖水綠的「今日導言」卡、紅色的「地球物理與地震學」
   專區卡、藍色的「其他地球科學」卡（含當日統計頁尾），每則標題下方都有可點的原文連結。
4. 推播成功後才把這批項目記進 `data/seen.json`，並由 workflow 自動 commit 回 repo，
   確保下次不會重複，也不會因為單次失敗而漏掉項目。

## 日報內容結構

每天一則訊息、五張卡片：

1. **🌍 導言卡**（湖水綠）：英中雙語的今日看點。
2. **🇹🇼 台灣地震動態**（琥珀）：台灣及周邊 M4+ 地震，平靜時會直接告訴你「台灣平安」。
3. **🌋 地球物理與地震學**（紅）：主要焦點，每則英文標題＋英文摘要＋中文重點＋來源＋連結。
4. **🔭 其他地球科學**（藍）：次要補充。
5. **📖 每日一詞**（紫）：從當天內容挑一個專業術語，附英文定義、學術例句、台灣譯名
   與記憶點；已教過的詞會記錄在 `data/words.json`，90 天內不重複。

**每週日**額外加發「📚 週日回顧特刊」：本週 3~5 則精選回顧（把一週的點連成線的回顧視角）
＋本週詞彙總複習。

## 日報歸檔（可搜尋的網頁版）

每天的日報會同步存成 Markdown 檔進 `archive/`（`archive/index.md` 是總目錄），
由 workflow 自動 commit。想要網頁版：到 repo **Settings → Pages →
Build and deployment → Deploy from a branch**，選 `main` 分支根目錄，儲存後
`https://<你的帳號>.github.io/<repo名>/archive/` 就是可瀏覽的歸檔網站
（GitHub Pages 免費，Jekyll 會自動把 Markdown 轉成網頁）。
想找回某篇論文，也可以直接用 GitHub 的 repo 搜尋功能搜 `archive/` 資料夾。

## 個人品質偏好（preferences.json）

repo 根目錄的 `preferences.json` 是你的個人品質守則，改完 commit push 後隔天生效：

- `blocked_sources`：整個關掉某個來源（填 `RSS_SOURCES` 裡的 name）。
- `blocked_keywords`：標題或摘要含這些關鍵字的項目，送給 AI 前就直接丟棄。
- `editorial_guidelines`：給 AI 編輯的額外守則，每行一條。

**設計原則——品質過濾，不是同溫層**：這個檔案的定位是「把品質不好的內容濾掉」，
不是「只推我喜歡的主題」。守則會連同一句強制指令一起交給 AI：
「這些僅是品質標準，你仍必須維持領域與主題的多樣性」。
所以建議寫「更正啟事不值得報導」這類品質判斷，
而不是「我只想看海嘯相關」這類主題偏好——後者會讓你的資訊視野越來越窄。

## 之後可以調整的地方

- **時間**：改 `.github/workflows/daily-digest.yml` 裡的 `cron` 值。
- **篩選口味**：改 `scripts/digest.py` 裡 `build_prompt()` 的文字，例如要更嚴格篩選、
  或想要更多其他領域的內容。
- **地震規模門檻**：改 `USGS_URL`（如 `2.5_day.geojson` 會更寬鬆）或
  `fetch_seismicportal()` 裡的 `minmag` 參數。
