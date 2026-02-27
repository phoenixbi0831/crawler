# 分享頁/通用網頁爬蟲（Selenium）

同一支腳本支援：

- ChatGPT 分享連結（`https://chatgpt.com/s/...`）→ 擷取「對話」
- 一般網頁（任何 URL）→ 擷取「文章主體」（以可見文字為主，並轉成 Markdown 風格輸出）

輸出成 Markdown（檔名用網頁標題）。

## 安裝

在專案根目錄執行：

```bash
python -m venv crawler/.venv
crawler/.venv/Scripts/activate
pip install -r crawler/requirements.txt
```

## 使用

```bash
python crawler/chatgpt_share_crawler.py "https://chatgpt.com/s/xxxxx"
```

輸出會在專案根目錄的 `output/`。

### 常用參數

```bash
python crawler/chatgpt_share_crawler.py "https://chatgpt.com/s/xxxxx" --wait 5 --headless
```

