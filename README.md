# 推薦系統工作區（Phoenix Tsai）

這個 repo 是我整理個人品牌、爬蟲工具與文件轉 Markdown 的工作區，主要包含：

- **crawler**：Selenium 爬蟲（ChatGPT 分享頁＋全聯本檔最殺頁＋一般文章頁）
- **extract_docs.py**：把 `resource/` 裡的 DOCX/PPTX/PDF 轉成 `doc/` 下的 Markdown，圖片輸出到 `img/`
- **personal-website/**：個人網站（Next/React）原始碼

## 專案結構簡介

- **extract_docs.py**：從 `resource/` 讀入檔案 → 產生 `doc/*.md` 與 `img/*`
- **crawler/**  
  - `chatgpt_share_crawler.py`：Selenium + Chrome 爬蟲  
    - ChatGPT 分享頁：擷取完整對話，整理成好讀的 Markdown  
    - 一般文章頁：擷取主體內容（`main/article/body` 可見文字）轉 Markdown  
    - 全聯「本檔最殺」系列：自動把商品區轉成 Table（每個商品一行），並支援多頁彙總  
  - `requirements.txt`：crawler 用的 Python 相依（selenium、markdownify…）
- **output/**：各種自動產生的 Markdown（ChatGPT 對話、全聯活動商品表、彙總檔等）

## crawler 安裝

在專案根目錄執行（Windows / PowerShell）：

```bash
python -m venv crawler\.venv
crawler\.venv\Scripts\python -m pip install --upgrade pip
crawler\.venv\Scripts\python -m pip install -r crawler\requirements.txt
```

## crawler 用法

### 1. 抓單一 ChatGPT 分享頁

```bash
crawler\.venv\Scripts\python crawler\chatgpt_share_crawler.py "https://chatgpt.com/s/xxxxx" --wait 5 --headless
```

- 開頁後先固定等 `--wait` 秒（預設 5），再抓對話  
- 優先讀 `__NEXT_DATA__`，拿到乾淨 markdown；不行再 fallback DOM  
- 輸出：`output/<網頁標題>.md`，含來源 URL、擷取時間與分段好的對話結構

### 2. 抓一般網頁（文章主體 → Markdown）

```bash
crawler\.venv\Scripts\python crawler\chatgpt_share_crawler.py "https://example.com/..." --wait 5
```

- 會嘗試從 `article/main/body` 中找最大文字區塊  
- 去掉明顯的 header/nav/footer/script 等雜訊，再用 `markdownify` 轉 Markdown  
- 針對高度動態頁，會 fallback 用 `body.innerText` + 關鍵字裁切

### 3. 全聯「本檔最殺」活動頁（單頁）

例如本檔必買頁：  
`https://www.pxmart.com.tw/campaign/life-will/best-buy/%E6%9C%AC%E6%AA%94%E5%BF%85%E8%B2%B7`

```bash
crawler\.venv\Scripts\python crawler\chatgpt_share_crawler.py "https://www.pxmart.com.tw/..." --wait 5
```

- 會額外把商品區解析成 Table：

```markdown
| 商品 | 規格與促銷 |
| --- | --- |
| 白蘭氏雞精(68ml*19入) | 單盒特價959元 買二特價 / 2盒1858元 / 平均一盒 / 929元 |
...
```

### 4. 批次抓多個全聯「本檔最殺」頁並整合

1. 在 `crawler/crawler_url.txt` 填入多個 URL（每行一個，支援 `#` 註解），例如：

   ```text
   https://www.pxmart.com.tw/campaign/life-will/best-buy/recommend
   https://www.pxmart.com.tw/campaign/life-will/best-buy/%E6%9C%AC%E6%AA%94%E5%BF%85%E8%B2%B7
   https://www.pxmart.com.tw/campaign/life-will/best-buy/%E6%8A%97%E6%BC%B2%E5%B0%88%E5%8D%80
   ...
   ```

2. 執行批次模式：

   ```bash
   crawler\.venv\Scripts\python crawler\chatgpt_share_crawler.py --urls-file crawler\crawler_url.txt --wait 5
   ```

3. 產出結果：

- 每個網址：各自一份 `output/本檔最殺 - xxxx 來全聯 方便又省錢.md`
- `output/pxmart_bestbuy_batch.md`  
  - 彙總所有商品，欄位：`來源 | 商品 | 規格與促銷`
- `output/本檔最殺整合版.md`  
  - 只整合「來源」以 `本檔最殺 - ...` 開頭的頁面  
  - 保留「本檔最殺 - 本檔必買」那一頁的：
    - `活動日期 ...`
    - `- **擷取時間**: ...`
  - 下方是一張大表：`來源 | 商品 | 規格與促銷`

## extract_docs.py 用法（文件 → Markdown）

1. 把 DOCX / PPTX / PDF 丟到 `resource/` 目錄
2. 在專案根目錄裝相依套件（自行在 venv 或全域安裝）：

   ```bash
   pip install python-docx python-pptx pdfplumber
   ```

3. 執行轉檔：

   ```bash
   python extract_docs.py
   ```

- 文字與標題 → 對應的 Markdown 段落與標題
- 表格 → Markdown table
- 圖片 → 存到 `img/`，並在對應 md 內嵌連結

## Git 注意事項

- 根目錄 `.gitignore` 已排除：
  - `personal-website/`
  - `doc/`
  - `img/`
  - `resource/`
  - `integration/`
  - `crawler/.venv/`（crawler 用 venv 不會被傳上 GitHub）
- **不要再用** `git add . --force`，避免把 venv 或輸出檔強制加進版本庫。

