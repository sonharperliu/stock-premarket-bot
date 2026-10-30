# 📈 盤前自動分析 + Telegram 推播機器人

在 GitHub Actions 上免費執行，不需要開著電腦。美股、台股都在**開盤前 10 分鐘**準時推播：
- 美股：紐約時間 09:20 推播（自動處理冬令/夏令，台灣時間夏天 21:20、冬天 22:20）
- 台股：台北時間 08:50 推播
- 休市日（感恩節、聖誕節、春節、國慶日……）自動跳過，**不會呼叫 Claude、不花 API 費用**

## 架構

```
第一層 廣度掃描   S&P 500 + 50 檔全球大型 ADR（約 550 檔）＋ 11 大產業 ETF ＋ 主題/跨資產 ETF
                  → 相對強弱 RS20/RS60、量比、CMF 資金流、52 週高點距離、均線位置 → 綜合分數
                  → 強勢榜 + 量價異動榜，取美股 20 / 台股 12 檔候選
第二層 六維度     總經｜基本面｜護城河｜技術面(ATR)｜籌碼面｜消息面
第三層 Claude     機構級提示詞交叉驗證 → 美股 10 檔 / 台股 5 檔
                  進場區、停損、停利1/2、風報比、部位大小
程式驗算          價格順序、停損 1~3 ATR、風報比 ≥ 1.5；不合格自動改用 ATR 基準計畫
推播              Telegram 摘要訊息 + 完整 Markdown 報告附檔
```

## 資料來源對照（免費、可自動化的部分都已接上）

| 你指定的來源 | 程式實際使用 | 說明 |
|---|---|---|
| FRED | ✅ FRED API / fredgraph CSV | GDP、非農、CPI/核心CPI、核心PCE、PPI、失業率、初領失業金、2Y/10Y、利差、零售銷售、耐久財、消費者信心、Fed 資產負債表、ON RRP、TGA、高收益債利差、淨流動性 |
| CME FedWatch | ⚠️ 以未來 1–3 個月 30 天 Fed Funds 期貨曲線粗估升降息次數 | CME 無免費 API |
| Investing.com / Trading Economics 財經日曆 | ✅ 改用 ForexFactory 公開週曆 JSON | Investing.com 有 Cloudflare 防爬 |
| MacroMicro | ⚠️ 以自建「淨流動性」、殖利率曲線、信用利差替代 | 無公開 API |
| Finviz | ✅ 個股快照（機構持股、空單、內部人、目標價、財報日） | 網頁抓取，失敗不影響主流程 |
| Koyfin（RS 分析） | ✅ 自行計算相對強弱 RS、板塊輪動 | |
| SEC / 財報狗 / Roic.ai | ✅ yfinance 財報：毛利率穩定度、ROIC、研發強度、FCF 利潤率、營收 CAGR、分析師共識 | |
| MOPS 月營收 | ✅ 證交所 OpenAPI t187ap05（月增/年增/累計年增） | |
| 台股籌碼 | ✅ 證交所 T86 三大法人（5 日）、MI_MARGN 融資融券、集保 TDCC 千張大戶 vs 散戶 | 上櫃股的法人資料目前缺 |
| Unusual Whales / Cheddar Flow | ⚠️ 以 yfinance 選擇權鏈找大單異動、Put/Call 比 + FINRA 每日空單量 | 付費 API，`chips_us.py` 預留了擴充點 |
| TradingView | ✅ 技術指標自行計算（ATR、RSI、MACD、布林、樞紐點、支撐壓力） | |
| TrendForce / 券商分點 / 借券 | ❌ 未接 | 需付費或無公開 API |
| 消息面 | ✅ Yahoo Finance 新聞 + Google News RSS（中英文） | |

## 安裝步驟（約 15 分鐘）

### 1. 建立 GitHub repo
把整個資料夾推上一個新的 GitHub repo。**建議設為 Public**：公開 repo 的 Actions 分鐘數無上限，Secrets 依然是加密的，別人看不到你的金鑰。
（Private repo 每月有 2,000 分鐘免費額度，本程式每月約用 1,100–1,300 分鐘，也夠用。）

### 2. 取得金鑰
| 名稱 | 取得方式 | 必要 |
|---|---|---|
| `ANTHROPIC_API_KEY` | https://console.anthropic.com → API Keys | ✅ |
| `TELEGRAM_BOT_TOKEN` | Telegram 搜尋 **@BotFather** → `/newbot` → 取得 token | ✅ |
| `TELEGRAM_CHAT_ID` | 先對你的 bot 傳一則訊息，再開 `https://api.telegram.org/bot<TOKEN>/getUpdates`，找 `"chat":{"id":…}` | ✅ |
| `FRED_API_KEY` | https://fred.stlouisfed.org/docs/api/api_key.html（免費） | **強烈建議**：FRED 免金鑰端點會擋 GitHub 伺服器，沒有金鑰就沒有 GDP/CPI/就業/流動性等總經數據 |

### 3. 設定 Secrets
repo → **Settings → Secrets and variables → Actions → New repository secret**，逐一加入上表金鑰。
（選用）在同頁的 **Variables** 分頁新增 `ANTHROPIC_MODEL` 可指定模型，預設 `claude-sonnet-5-5`；
`ANTHROPIC_FALLBACK_MODEL` 為主模型連續過載時的備援模型，預設 `claude-opus-5-5`。

> Claude 遇到伺服器過載會自動退避重試（20/45/90 秒）3 次，仍失敗才改用備援模型；再失敗則推送量化備援版並在報告中註明。

### 4. 測試
repo → **Actions → Premarket Stock Report → Run workflow**，選市場、勾「立即執行」，約 5–10 分鐘後 Telegram 就會收到報告。
完整報告與原始數據包（datapack.json）也可在該次執行頁面底部的 Artifacts 下載。

之後就全自動，週一到週五準時推播。

## 排程原理（為什麼不用每年改設定、也不怕 GitHub 延遲）
GitHub cron 只支援 UTC，而且**尖峰時段常延遲 30~60 分鐘、偶爾漏跑**（實測 10/29、10/30 都延遲約 50 分鐘）。因此：

1. 每個市場在推播前約 4 小時內排 6~7 個觸發（美股 UTC 09:47~13:47、台股 UTC 21:37~00:32）。
2. 程式用 `America/New_York` / `Asia/Taipei` 計算今天真正的推播時間（冬夏令自動處理）。
3. **最先抵達**的觸發：休眠到推播前 25 分鐘 → 抓最新數據 + Claude 分析 → 準點推播 → 在 `bot-state` 分支記錄「今日已推播」。
4. **之後抵達**的觸發：同市場排隊，輪到時看到今日已推播，約 30 秒內結束（不安裝套件、不呼叫 Claude）。
5. 萬一所有觸發都延遲到推播時間之後：3 小時內仍補送，並在訊息開頭標示「⚠️ 開盤後送達」。

模擬「每個觸發隨機延遲 0~75 分鐘、10% 漏跑」各 1000 天：美股夏令、冬令、台股皆 100% 準時、0 重複。

> 這個 repo 是公開的，Actions 分鐘數不限，休眠不額外收費。若改為 Private，每月約需 5,000+ 分鐘，會超過免費額度。

### 除錯用的分支（程式自動維護，不要手動修改）
| 分支 | 內容 |
|---|---|
| `bot-state` | 各市場最近一次成功推播的當地日期（防止同日重複推播） |
| `run-logs` | 每次觸發的時間窗判斷與執行摘要（哪個觸發幾點抵達、做了什麼） |
| `test-results` | 測試開關最近一次的完整 log 與報告 |

## 自訂
- 推播檔數、候選數、流動性門檻、ETF 清單、台股股票池：`src/config.py`
- 颱風假等臨時休市：編輯 `data/tw_extra_closures.txt`（一行一個日期）
- 分析框架與輸出格式：`src/analyzer.py` 的 `SYSTEM_PROMPT`
- 停損 ATR 倍數、風報比：`src/technicals.py` 的 `atr_plan()`

## 測試開關（不必等排程）
修改 repo 根目錄的 `.run-test` 並 commit，會立即執行一次並真的推播到 Telegram。
檔案內容第一個字為 `us` 或 `tw`；內容含 `noforce` 則走正式排程邏輯（時間窗、休市、今日已推播檢查，且成功後會寫入今日已推播）。執行 log 與報告會推到 `test-results` 分支，方便除錯。
也可以在 Actions 頁面手動 Run workflow。

## 本機測試
```bash
pip install -r requirements.txt
python main.py --market us --force --dry-run --no-llm   # 不花 API、不發 Telegram
python main.py --market tw --force --dry-run            # 呼叫 Claude，結果印在終端機
```

## 費用估算
每次送給 Claude 約 3–6 萬 input tokens、6–10k output tokens。用 Sonnet 等級模型，每次約 US$0.2–0.35，兩個市場每月約 US$10–15。休市日不呼叫。

---
⚠️ 本工具僅供研究與學習，不構成任何投資建議。AI 與量化模型都可能出錯，請自行判斷並嚴格控制部位與風險。
