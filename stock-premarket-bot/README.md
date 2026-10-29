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
| CME FedWatch | ⚠️ 以 30 天 Fed Funds 期貨（ZQ=F）粗估 | CME 無免費 API |
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
| `FRED_API_KEY` | https://fred.stlouisfed.org/docs/api/api_key.html（免費） | 選用 |

### 3. 設定 Secrets
repo → **Settings → Secrets and variables → Actions → New repository secret**，逐一加入上表金鑰。
（選用）在同頁的 **Variables** 分頁新增 `ANTHROPIC_MODEL` 可指定模型，預設 `claude-sonnet-5-5`。

### 4. 測試
repo → **Actions → Premarket Stock Report → Run workflow**，選市場、勾「立即執行」，約 5–10 分鐘後 Telegram 就會收到報告。
完整報告與原始數據包（datapack.json）也可在該次執行頁面底部的 Artifacts 下載。

之後就全自動，週一到週五準時推播。

## 排程原理（為什麼不用每年改設定）
GitHub cron 只支援 UTC。workflow 為美股同時排了兩個時間：UTC 12:50（對應夏令）和 13:50（對應冬令）。
`gate.py` 用 `America/New_York` 時區計算「今天真正的 09:20」，只有落在時間窗內的那一次會繼續，另一次幾秒內結束。
程式提早約 30 分鐘啟動，做完分析後 **sleep 到 09:20 整點才送出**；若 GitHub 排程延遲，仍保證在開盤前送出，超過開盤就不送。

> GitHub 排程在尖峰時段偶爾會延遲 10–30 分鐘以上。如果你要求分秒不差，可用免費的 cron-job.org 在指定時間呼叫 GitHub API 的 `workflow_dispatch`（force=false），時間窗與休市檢查一樣有效。

## 自訂
- 推播檔數、候選數、流動性門檻、ETF 清單、台股股票池：`src/config.py`
- 颱風假等臨時休市：編輯 `data/tw_extra_closures.txt`（一行一個日期）
- 分析框架與輸出格式：`src/analyzer.py` 的 `SYSTEM_PROMPT`
- 停損 ATR 倍數、風報比：`src/technicals.py` 的 `atr_plan()`

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
