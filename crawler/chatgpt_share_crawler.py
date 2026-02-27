from __future__ import annotations

import argparse
import difflib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from markdownify import markdownify as html_to_md


@dataclass(frozen=True)
class Turn:
    role: str
    content_md: str


INVALID_WIN_FILENAME_CHARS = r'<>:"/\\|?*'


def sanitize_filename(name: str, max_len: int = 120) -> str:
    name = re.sub(rf"[{re.escape(INVALID_WIN_FILENAME_CHARS)}]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        name = "chatgpt_share"
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name


def role_to_heading(role: str) -> str:
    r = (role or "").lower()
    if r in {"user", "human"}:
        return "使用者"
    if r in {"assistant", "gpt"}:
        return "ChatGPT"
    if r in {"system"}:
        return "系統"
    return role or "未知"


def normalize_text(s: str) -> str:
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s


def format_turn_content_md(s: str) -> str:
    s = normalize_text(s)
    if not s:
        return s

    out: list[str] = []
    for raw_line in s.split("\n"):
        line = raw_line.rstrip()

        # 把常見的提示行改成 Markdown 小標題，讀起來比較像「段落結構」。
        m = re.match(r"^\s*[🎬👉]\s*(.+?)\s*$", line)
        if m:
            title = m.group(1)
            title = re.sub(r"[：:]\s*$", "", title).strip()
            if title:
                out.append(f"#### {title}")
                continue

        out.append(line)

    formatted = "\n".join(out).strip()
    formatted = re.sub(r"\n{3,}", "\n\n", formatted)
    return formatted


def clean_article_md(md: str) -> str:
    """
    通用文章頁的 Markdown 常會以一串 logo / 導覽連結開頭。
    這裡做保守清理：只移除「開頭連續」的純圖片/純連結/很短的選單行，
    一旦遇到像正文的行就停止。
    """
    md = normalize_text(md)
    if not md:
        return md

    lines = md.split("\n")
    cut = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            cut = i + 1
            continue

        # 純圖片/Logo 連結
        if re.fullmatch(r"\[!\[.*?\]\(.*?\)\]\(.*?\)", s) or s.startswith("[![]("):
            cut = i + 1
            continue

        # 常見回首頁/導覽小碎片
        if any(tok in s for tok in ("回首頁", "登入", "註冊", "購物車")) and len(s) <= 60:
            cut = i + 1
            continue

        # 只有很短的選單項目或單行連結
        if (s.startswith("* ") or s.startswith("- ")) and len(s) <= 30:
            cut = i + 1
            continue

        # 像正文的行就停（含中文/數字且長度夠）
        if len(s) >= 12 and re.search(r"[\u4e00-\u9fff0-9]", s):
            break

    cleaned = "\n".join(lines[cut:]).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def trim_text_to_keywords(text: str, keywords: list[str]) -> str:
    text = normalize_text(text)
    if not text:
        return text
    lines = text.split("\n")
    for i, line in enumerate(lines):
        for kw in keywords:
            if kw and kw in line:
                return "\n".join(lines[i:]).strip()
    return text


def trim_text_footer(text: str, stop_markers: list[str]) -> str:
    text = normalize_text(text)
    if not text:
        return text
    lines = text.split("\n")
    for i, line in enumerate(lines):
        for m in stop_markers:
            if m and m in line:
                return "\n".join(lines[:i]).strip()
    return text


def format_pxmart_bestbuy(md: str) -> str:
    """
    將全聯「本檔必買」頁面的商品區塊轉成 Markdown table：
    | 商品 | 規格與促銷 |
    一個商品一行，不同商品換行。
    """
    md = normalize_text(md)
    if not md:
        return md

    lines = md.split("\n")
    n = len(lines)

    def is_category(name: str) -> bool:
        name = name.strip()
        if not name:
            return False
        if "商品篩選" in name or "活動日期" in name:
            return True
        if "類" in name and "(" in name and ")" in name:
            return True
        if "類" in name and " (" in name:
            return True
        return False

    products: list[tuple[int, int, str, list[str]]] = []  # (start_idx, end_idx, name, promo_lines)

    i = 0
    while i < n:
        name = lines[i].strip()
        if not name or is_category(name):
            i += 1
            continue

        # 嘗試把接下來的幾行視為同一個商品的敘述
        j = i + 1
        block: list[str] = []
        while j < n:
            t = lines[j].strip()
            if not t:
                break
            # 如果看起來像下一個商品名稱（沒有價格關鍵字、也不是類別），就結束目前商品
            if block and not any(x in t for x in ("特價", "元", "包", "盒", "平均")) and not is_category(t):
                break
            block.append(t)
            j += 1

        # 判斷這一段是不是「真的有價格/特價資訊的商品」
        if block and any(("元" in b and any(ch.isdigit() for ch in b)) or ("特價" in b) for b in block):
            promo_lines = [b for b in block if b]
            products.append((i, j - 1, name, promo_lines))
            i = j
        else:
            i += 1

    if not products:
        return md

    first_name_idx = products[0][0]
    last_end_idx = products[-1][1]

    new_lines: list[str] = []

    # 1) 保留商品列表前面的說明（活動日期 / 類別等）
    for idx in range(0, first_name_idx):
        new_lines.append(lines[idx])

    if new_lines and new_lines[-1].strip():
        new_lines.append("")

    # 2) 插入商品 table
    new_lines.append("| 商品 | 規格與促銷 |")
    new_lines.append("| --- | --- |")
    for _, _, name, promo in products:
        desc = " / ".join(promo).replace("|", "\\|")
        row_name = name.replace("|", "\\|")
        new_lines.append(f"| {row_name} | {desc} |")

    # 3) 保留尾段（例如「來全聯 方便又省錢」等）
    if last_end_idx + 1 < n:
        if new_lines[-1].strip():
            new_lines.append("")
        for idx in range(last_end_idx + 1, n):
            new_lines.append(lines[idx])

    return "\n".join(new_lines).strip()


def dedupe_consecutive_turns(turns: list[Turn]) -> list[Turn]:
    out: list[Turn] = []
    # 有些分享頁的資料會出現同一則訊息重複（甚至不是完全相同換行/空白）。
    # 這裡用「壓扁空白」做 key，並做兩層保護：
    # 1) 最近幾則的去重（快速處理連續重複）
    # 2) 全域 seen 去重（處理 mapping/節點重複導致的重複訊息）
    recent_keys: list[tuple[str, str]] = []
    seen_keys: set[tuple[str, str]] = set()
    prev_role_lower: str | None = None
    prev_content_key: str | None = None
    for t in turns:
        role = (t.role or "unknown").strip()
        content = normalize_text(t.content_md)
        if not content:
            continue
        content_key = re.sub(r"\s+", " ", content).strip()
        key = (role.lower(), content_key)
        role_lower = role.lower()

        # 先處理「幾乎同一段、但長度不同」的相鄰訊息：保留比較完整的那個
        if out and out[-1].role.lower() == role_lower:
            prev = out[-1].content_md
            prev_key = re.sub(r"\s+", " ", normalize_text(prev)).strip()
            if prev_key and (
                content_key in prev_key
                or prev_key in content_key
                or difflib.SequenceMatcher(a=prev_key, b=content_key).ratio() >= 0.985
            ):
                if len(content_key) > len(prev_key):
                    out[-1] = Turn(role=role, content_md=content)
                    prev_role_lower = role_lower
                    prev_content_key = content_key
                    seen_keys.add(key)
                continue

        if key in seen_keys or key in recent_keys:
            continue

        # 內容看起來幾乎一樣（只差極少字元）也當作重複，避免同段落被抓兩次。
        if (
            prev_role_lower == role_lower
            and prev_content_key
            and difflib.SequenceMatcher(a=prev_content_key, b=content_key).ratio() >= 0.995
        ):
            continue

        seen_keys.add(key)
        recent_keys.append(key)
        if len(recent_keys) > 5:
            recent_keys.pop(0)
        prev_role_lower = role_lower
        prev_content_key = content_key
        out.append(Turn(role=role, content_md=content))
    return out


def _iter_dicts(obj: Any) -> Iterable[dict]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_dicts(v)
    elif isinstance(obj, list):
        for it in obj:
            yield from _iter_dicts(it)


def _extract_text_from_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        parts = content.get("parts")
        if isinstance(parts, list):
            return "\n".join(p for p in parts if isinstance(p, str)).strip()
        for k in ("text", "value", "markdown"):
            v = content.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _extract_role(msg: dict) -> str:
    author = msg.get("author")
    if isinstance(author, dict):
        for k in ("role", "name"):
            v = author.get(k)
            if isinstance(v, str) and v:
                return v
    for k in ("role", "author_role", "authorRole"):
        v = msg.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def _extract_title_from_next_data(data: dict) -> str:
    # Best-effort: find a meaningful title-like field.
    candidates: list[str] = []
    for d in _iter_dicts(data):
        for k in ("title", "shareTitle", "conversation_title"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                candidates.append(v.strip())
    if candidates:
        # Prefer shorter, human-looking titles.
        candidates.sort(key=lambda s: (len(s), s))
        return candidates[0]
    return ""


def _extract_turns_from_openai_mapping(conv_like: dict) -> Optional[list[Turn]]:
    mapping = conv_like.get("mapping")
    if not isinstance(mapping, dict) or not mapping:
        return None

    msgs: list[dict] = []
    for node in mapping.values():
        if not isinstance(node, dict):
            continue
        msg = node.get("message")
        if isinstance(msg, dict):
            msgs.append(msg)

    if not msgs:
        return None

    def sort_key(m: dict):
        ct = m.get("create_time")
        if isinstance(ct, (int, float)):
            return (0, float(ct))
        return (1, 0.0)

    msgs.sort(key=sort_key)

    turns: list[Turn] = []
    for msg in msgs:
        role = _extract_role(msg)
        text = _extract_text_from_content(msg.get("content"))
        if not text:
            continue
        turns.append(Turn(role=role or "unknown", content_md=text))

    return turns or None


def extract_from_next_data_json(next_data_json: str) -> tuple[str, list[Turn]]:
    data = json.loads(next_data_json)

    # Find the first object that looks like the OpenAI conversation export format.
    for d in _iter_dicts(data):
        turns = _extract_turns_from_openai_mapping(d)
        if turns:
            title = ""
            v = d.get("title")
            if isinstance(v, str) and v.strip():
                title = v.strip()
            if not title:
                title = _extract_title_from_next_data(data)
            return title, turns

    # Fallback: look for message-like dicts anywhere (less ordered, but better than nothing).
    message_like: list[dict] = []
    for d in _iter_dicts(data):
        if "author" in d and "content" in d:
            message_like.append(d)

    turns: list[Turn] = []
    for msg in message_like:
        role = _extract_role(msg)
        text = _extract_text_from_content(msg.get("content"))
        if not text:
            continue
        turns.append(Turn(role=role or "unknown", content_md=text))

    title = _extract_title_from_next_data(data)
    return title, turns


def extract_from_dom(driver: webdriver.Chrome) -> tuple[str, list[Turn]]:
    title = driver.title or ""

    # Strategy 1: ChatGPT UI turns
    turns_elems = driver.find_elements(By.CSS_SELECTOR, '[data-testid="conversation-turn"]')
    if not turns_elems:
        # Strategy 2: generic message role markers
        turns_elems = driver.find_elements(By.CSS_SELECTOR, "[data-message-author-role]")

    turns: list[Turn] = []
    for el in turns_elems:
        role = el.get_attribute("data-message-author-role") or ""
        if not role:
            # Some layouts nest role inside.
            role_nodes = el.find_elements(By.CSS_SELECTOR, "[data-message-author-role]")
            if role_nodes:
                role = role_nodes[0].get_attribute("data-message-author-role") or ""

        # Prefer markdown-ish container if present.
        content_el = None
        for sel in ("div.markdown", "div[data-testid='markdown']", "article", "main"):
            found = el.find_elements(By.CSS_SELECTOR, sel)
            if found:
                content_el = found[0]
                break

        text = (content_el.text if content_el is not None else el.text) or ""
        text = text.strip()
        if not text:
            continue
        turns.append(Turn(role=role or "unknown", content_md=text))

    return title, turns


def extract_generic_article(driver: webdriver.Chrome, url: str) -> tuple[str, list[Turn]]:
    title = driver.title or ""

    body_text = normalize_text(driver.find_element(By.TAG_NAME, "body").text or "")

    # 在瀏覽器端做一次「正文容器」挑選：
    # - 先排除 nav/header/footer/aside 等 chrome
    # - 再在候選節點中挑出 innerText 最長者（通常是正文或商品清單區）
    html = driver.execute_script(
        """
        const KEYWORDS = ['活動日期','商品篩選','特價','平均一盒','平均一組','猜你喜歡'];
        const killSelectors = [
          'script','style','noscript','svg','canvas','iframe',
          'header','footer','nav','aside',
          '[role="navigation"]','[aria-label*="導覽"]','[aria-label*="navigation"]',
          'form','button','input','select','textarea'
        ];
        function strip(el) {
          for (const sel of killSelectors) {
            for (const n of Array.from(el.querySelectorAll(sel))) n.remove();
          }
          // 移除常見的浮動/遮罩/彈窗
          for (const n of Array.from(el.querySelectorAll('[class*="modal" i],[class*="popup" i],[class*="overlay" i]'))) n.remove();
          return el;
        }

        const body = document.body;
        if (!body) return '';

        // 優先從 main/article 開始找；沒有再退到 body
        const roots = [];
        for (const sel of ['article','main','[role="main"]']) {
          const el = document.querySelector(sel);
          if (el) roots.push(el);
        }
        if (!roots.length) roots.push(body);

        function pickBest(candidates) {
          let best = null;
          let bestLen = 0;
          for (const el of candidates) {
            const text = (el.innerText || '').trim();
            const len = text.length;
            if (len < 400) continue;
            if (len > bestLen) {
              best = el;
              bestLen = len;
            }
          }
          return best;
        }

        let best = null;
        for (const root of roots) {
          const cloneRoot = strip(root.cloneNode(true));
          const candidates = [cloneRoot, ...Array.from(cloneRoot.querySelectorAll('article,section,div,main'))];

          // 先用關鍵字命中：更容易抓到正文/商品清單區
          const kwCandidates = [];
          for (const el of candidates) {
            const t = (el.innerText || '');
            if (!t) continue;
            for (const kw of KEYWORDS) {
              if (t.includes(kw)) {
                kwCandidates.push(el);
                break;
              }
            }
          }
          best = pickBest(kwCandidates) || pickBest(candidates) || best || cloneRoot;
        }

        if (!best) return '';
        return best.outerHTML || '';
        """
    )

    if not isinstance(html, str) or not html.strip():
        text = normalize_text(driver.find_element(By.TAG_NAME, "body").text)
        if text:
            return title, [Turn(role="article", content_md=text)]
        return title, []

    md = html_to_md(html, heading_style="ATX", strip=["script", "style", "noscript"])
    md_raw = normalize_text(md)

    # Reduce nav/footer leftovers if any slipped through.
    md_raw = re.sub(r"\n{3,}", "\n\n", md_raw).strip()
    md_clean = clean_article_md(md_raw)
    md = md_clean or md_raw

    # 動態頁面常常 HTML 結構很難挑到「正確容器」，
    # 但 body.innerText 已經是乾淨的可見文字；此時用文字當正文更穩。
    kw = ["活動日期", "商品篩選", "特價"]
    if body_text:
        body_main = trim_text_to_keywords(body_text, kw)
        if any(k in body_main for k in kw):
            body_main = trim_text_footer(
                body_main,
                stop_markers=[
                    "版權所有",
                    "隱私權",
                    "個資告知",
                    "Cookies",
                    "客服：",
                    "我同意",
                ],
            )
            # 若 markdownify 的結果太短/沒關鍵字，就用純文字版取代
            if (len(md) < len(body_main) * 0.6) or (not any(k in md for k in kw)):
                md = body_main

    # 針對全聯「本檔必買」這類頁面，把商品區轉成 Markdown table（每個商品一行）。
    try:
        u = urlparse(url)
        host = (u.hostname or "").lower()
        path = u.path or ""
    except Exception:
        host = ""
        path = ""

    if md and "pxmart.com.tw" in host and "best-buy" in path:
        md = format_pxmart_bestbuy(md)

    if md:
        return title, [Turn(role="article", content_md=md)]
    return title, []


def build_driver(headless: bool) -> webdriver.Chrome:
    options = Options()
    if headless:
        # "new" headless is the modern Chrome headless mode.
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,900")
    options.add_argument("--lang=zh-TW")
    # Keep it conservative: avoid aggressive "stealth" flags; just reduce obvious noise.
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")

    return webdriver.Chrome(options=options)


def write_markdown(out_dir: Path, title: str, url: str, turns: list[Turn]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)

    safe_title = sanitize_filename(title or "chatgpt_share")
    out_path = out_dir / f"{safe_title}.md"

    now = datetime.now(timezone.utc).astimezone()

    turns = dedupe_consecutive_turns(turns)

    lines: list[str] = []
    lines.append(f"# {title or safe_title}")
    lines.append("")
    lines.append(f"- **來源**: {url}")
    lines.append(f"- **擷取時間**: {now.strftime('%Y-%m-%d %H:%M:%S %z')}")
    lines.append("")
    is_article = len(turns) == 1 and (turns[0].role or "").lower() == "article"
    lines.append("## 文章" if is_article else "## 對話")
    lines.append("")

    for i, t in enumerate(turns, start=1):
        if not is_article:
            heading = role_to_heading(t.role)
            lines.append(f"### {i}. {heading}")
            lines.append("")
        lines.append(format_turn_content_md(t.content_md))
        lines.append("")

    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return out_path


def parse_pxmart_bestbuy_rows(md: str, source_title: str) -> list[tuple[str, str, str]]:
    """
    從已格式化的本檔必買 markdown 裡，把 table 解析成 (來源, 商品, 規格與促銷)。
    """
    md = normalize_text(md)
    lines = md.split("\n")
    rows: list[tuple[str, str, str]] = []

    in_table = False
    for line in lines:
        s = line.strip()
        if s.startswith("| 商品 ") and "規格與促銷" in s:
            in_table = True
            continue
        if in_table and s.startswith("| ---"):
            continue
        if in_table:
            if not s or not s.startswith("|"):
                break
            parts = [c.strip() for c in s.strip("|").split("|")]
            if len(parts) < 2:
                continue
            product = parts[0]
            spec = parts[1]
            rows.append((source_title, product, spec))
    return rows


def is_chatgpt_share_url(url: str) -> bool:
    try:
        u = urlparse(url)
    except Exception:
        return False
    host = (u.hostname or "").lower()
    path = u.path or ""
    return host.endswith("chatgpt.com") and path.startswith("/s/")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="用 Selenium 擷取 ChatGPT 分享連結 / 通用網頁內容，輸出 Markdown（檔名用網頁標題）。"
    )
    parser.add_argument("url", nargs="?", help="單一網址，例如：https://chatgpt.com/s/xxxxx")
    parser.add_argument(
        "--urls-file",
        help="批次網址清單檔，每行一個 URL（例如 crawler_url.txt）",
    )
    parser.add_argument("--wait", type=int, default=5, help="頁面開啟後額外等待秒數（預設 5）")
    parser.add_argument("--headless", action="store_true", help="使用 headless 模式")
    parser.add_argument(
        "--output-dir",
        default=str(Path(__file__).resolve().parents[1] / "output"),
        help="輸出資料夾（預設：專案根目錄/output）",
    )
    args = parser.parse_args()

    urls: list[str] = []
    if args.urls_file:
        path = Path(args.urls_file)
        if not path.is_file():
            print(f"[ERROR] 找不到 urls 檔案：{path}")
            return 1
        for line in path.read_text(encoding="utf-8").splitlines():
            u = line.strip()
            if not u or u.startswith("#"):
                continue
            urls.append(u)
    if args.url:
        urls.append(args.url)

    if not urls:
        parser.error("必須提供 url 或 --urls-file")

    extra_wait_s: int = max(0, int(args.wait))
    out_dir = Path(args.output_dir)

    batch_px_rows: list[tuple[str, str, str]] = []

    try:
        driver = build_driver(headless=bool(args.headless))
    except WebDriverException as exc:
        print(
            "[ERROR] Chrome WebDriver 啟動失敗。你可能缺 Chrome 或驅動（或版本不匹配）。\n"
            "        先確認已安裝 Chrome；Selenium 4.6+ 會嘗試自動管理 driver。\n"
            f"        詳細錯誤：{exc}"
        )
        return 2

    success = False
    try:
        for url in urls:
            print(f"[INFO] 處理網址：{url}")
            driver.get(url)

            # 依需求：先等固定秒數讓頁面渲染（有反爬/動態載入時很常需要）
            if extra_wait_s:
                time.sleep(extra_wait_s)

            # 再多等一下直到主要內容出現（不取代上面 sleep，只是更穩）
            try:
                WebDriverWait(driver, 15).until(lambda d: (d.find_elements(By.CSS_SELECTOR, "main") != []))
            except TimeoutException:
                pass

            title = ""
            turns: list[Turn] = []

            if is_chatgpt_share_url(url):
                # ChatGPT 分享頁：優先走 __NEXT_DATA__（通常能拿到最乾淨的原始 markdown）
                next_data_json = driver.execute_script(
                    "return document.getElementById('__NEXT_DATA__')?.textContent || '';"
                )
                if isinstance(next_data_json, str) and next_data_json.strip().startswith("{"):
                    try:
                        title, turns = extract_from_next_data_json(next_data_json)
                    except Exception:
                        title, turns = "", []

                if not turns:
                    title, turns = extract_from_dom(driver)
            else:
                # 通用文章頁：抽主體 -> Markdown
                # 很多頁面會 lazy-load，簡單滾動一下提升命中率
                try:
                    # 緩慢滾動幾次，讓 lazy-load 有機會掛上 DOM
                    for _ in range(3):
                        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                        time.sleep(1.0)
                except Exception:
                    pass

                # 針對常見活動頁：等到關鍵字出現再抽（仍保留你要求的 --wait 固定 sleep）
                try:
                    WebDriverWait(driver, 15).until(
                        lambda d: ("活動日期" in (d.find_element(By.TAG_NAME, "body").text or ""))
                        or ("商品篩選" in (d.find_element(By.TAG_NAME, "body").text or ""))
                    )
                except Exception:
                    pass
                title, turns = extract_generic_article(driver, url)

            if not title:
                title = driver.title or "chatgpt_share"

            if not turns:
                print(f"[WARN] 本頁沒抓到內容，略過：{url}")
                continue

            out_path = write_markdown(out_dir=out_dir, title=title, url=url, turns=turns)
            print(f"[OK] 輸出完成：{out_path}")
            success = True

            # 若是全聯本檔必買頁，抽出商品 table 供彙總用
            try:
                u = urlparse(url)
                host = (u.hostname or "").lower()
                path = u.path or ""
            except Exception:
                host = ""
                path = ""
            if host.endswith("pxmart.com.tw") and "best-buy" in path:
                article_turns = [t for t in turns if (t.role or "").lower() == "article"]
                if article_turns:
                    batch_px_rows.extend(parse_pxmart_bestbuy_rows(article_turns[0].content_md, title))

        # 若是批次模式且有全聯商品，輸出一份彙總 table（包含來源欄位）
        if args.urls_file and batch_px_rows:
            agg_path = out_dir / "pxmart_bestbuy_batch.md"
            lines: list[str] = []
            lines.append("# 全聯本檔必買彙總")
            lines.append("")
            lines.append("| 來源 | 商品 | 規格與促銷 |")
            lines.append("| --- | --- | --- |")
            for src, product, spec in batch_px_rows:
                safe_src = src.replace("|", "\\|")
                safe_product = product.replace("|", "\\|")
                safe_spec = spec.replace("|", "\\|")
                lines.append(f"| {safe_src} | {safe_product} | {safe_spec} |")
            agg_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
            print(f"[OK] 全聯彙總輸出：{agg_path}")

            # 額外產出「本檔最殺整合版.md」
            # 僅針對「來源」以「本檔最殺 - 」開頭的商品，並從「本檔最殺 - 本檔必買」頁取得活動日期與擷取時間。
            best_rows = [row for row in batch_px_rows if row[0].startswith("本檔最殺 - ")]
            if best_rows:
                base_title = None
                for src, _, _ in best_rows:
                    if src.startswith("本檔最殺 - 本檔必買"):
                        base_title = src
                        break

                event_line = ""
                capture_line = ""
                if base_title:
                    base_filename = sanitize_filename(base_title) + ".md"
                    base_path = out_dir / base_filename
                    if base_path.is_file():
                        text = base_path.read_text(encoding="utf-8")
                        for line in text.splitlines():
                            s = line.strip()
                            if not capture_line and s.startswith("- **擷取時間**"):
                                capture_line = s
                            if not event_line and "活動日期" in s:
                                event_line = s
                            if capture_line and event_line:
                                break

                total_path = out_dir / "本檔最殺整合版.md"
                t_lines: list[str] = []
                t_lines.append("# 本檔最殺整合版")
                t_lines.append("")
                if event_line:
                    t_lines.append(event_line)
                if capture_line:
                    t_lines.append(capture_line)
                if event_line or capture_line:
                    t_lines.append("")
                t_lines.append("| 來源 | 商品 | 規格與促銷 |")
                t_lines.append("| --- | --- | --- |")
                for src, product, spec in best_rows:
                    safe_src = src.replace("|", "\\|")
                    safe_product = product.replace("|", "\\|")
                    safe_spec = spec.replace("|", "\\|")
                    t_lines.append(f"| {safe_src} | {safe_product} | {safe_spec} |")
                total_path.write_text("\n".join(t_lines).rstrip() + "\n", encoding="utf-8")
                print(f"[OK] 本檔最殺整合版輸出：{total_path}")

        return 0 if success else 1
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())

