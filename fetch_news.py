import sqlite3
import time
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from load_channels import load_and_clean_channels

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "news.db"
REPORTS_DIR = BASE_DIR / "data" / "reports"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
REQUEST_TIMEOUT = 15
REQUEST_DELAY = 1.0


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db():
    with connect_db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                post_id TEXT NOT NULL,
                text TEXT NOT NULL,
                link TEXT NOT NULL,
                published_at TEXT,
                fetched_at REAL NOT NULL,
                category TEXT NOT NULL DEFAULT 'general',
                language TEXT NOT NULL DEFAULT 'en',
                UNIQUE(channel, post_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_status (
                channel TEXT PRIMARY KEY,
                last_check REAL NOT NULL,
                status TEXT NOT NULL,
                error_msg TEXT NOT NULL DEFAULT '',
                new_fetched INTEGER NOT NULL DEFAULT 0,
                parsed_posts INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_published_at ON messages(published_at)")


def fetch_telegram_channel_preview(channel_name, session=None):
    """Return (messages, status, detail, parsed_count, media_only_count)."""
    session = session or requests.Session()
    url = f"https://t.me/s/{channel_name}"
    try:
        response = session.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5"
            },
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        if response.status_code != 200:
            return [], "HTTP_ERROR", f"HTTP_{response.status_code}", 0, 0

        # A redirect to a different host should not be treated as a valid Telegram preview.
        final_host = (urlparse(response.url).hostname or "").lower()
        if final_host not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
            return [], "UNEXPECTED_REDIRECT", f"host={final_host}", 0, 0

        soup = BeautifulSoup(response.text, "html.parser")
        posts = soup.select(".tgme_widget_message")
        if not posts:
            return [], "PARSE_EMPTY_POSTS", "No .tgme_widget_message elements", 0, 0

        messages = []
        media_only = 0
        for post in posts:
            post_link_elem = post.select_one(".tgme_widget_message_date")
            link_href = post_link_elem.get("href", "").strip() if post_link_elem else ""
            if not link_href:
                continue
            parsed_link = urlparse(link_href)
            link_host = (parsed_link.hostname or "").lower()
            if link_host not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
                continue
            parts = [p for p in parsed_link.path.split("/") if p]
            if len(parts) < 2 or parts[0].lower() != channel_name.lower():
                continue
            post_id = parts[1]
            if not post_id.isdigit():
                continue

            text_elem = post.select_one(".tgme_widget_message_text")
            text_content = text_elem.get_text(separator="\n", strip=True) if text_elem else ""
            if not text_content:
                media_only += 1
                continue

            time_elem = post.select_one("time.tgme_widget_message_date")
            published_at = time_elem.get("datetime", "").strip() if time_elem else ""
            messages.append({
                "channel": channel_name,
                "post_id": post_id,
                "text": text_content,
                "link": link_href,
                "published_at": published_at or None,
                "fetched_at": time.time(),
            })

        if not messages:
            return [], "PARSE_NO_TEXT", f"Parsed posts={len(posts)}; media_only={media_only}", len(posts), media_only
        return messages, "SUCCESS_WITH_MESSAGES", "", len(posts), media_only
    except requests.exceptions.Timeout:
        return [], "NETWORK_TIMEOUT", "Request timed out", 0, 0
    except requests.exceptions.RequestException as e:
        return [], "NETWORK_ERROR", f"{type(e).__name__}: {e}", 0, 0
    except Exception as e:
        return [], "PARSE_EXCEPTION", f"{type(e).__name__}: {e}", 0, 0


def save_channel_result(conn, channel, messages, status, detail, parsed_count, media_only_count):
    """Atomically store new messages and the channel status."""
    new_count = 0
    saved_messages = []
    with conn:  # SQLite transaction: messages and status commit together.
        for msg in messages:
            cur = conn.execute("""
                INSERT OR IGNORE INTO messages
                (channel, post_id, text, link, published_at, fetched_at, category, language)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                msg["channel"], msg["post_id"], msg["text"], msg["link"],
                msg["published_at"], msg["fetched_at"], channel["category"], channel["language"],
            ))
            if cur.rowcount > 0:
                new_count += 1
                saved_messages.append({
                    "channel": msg["channel"],
                    "post_id": msg["post_id"],
                    "text": msg["text"],
                    "link": msg["link"],
                    "published_at": msg["published_at"],
                    "fetched_at": msg["fetched_at"],
                    "category": channel["category"],
                    "language": channel["language"]
                })
        final_status = status
        if status == "SUCCESS_WITH_MESSAGES" and new_count == 0:
            final_status = "SUCCESS_NO_NEW_MESSAGES"
        conn.execute("""
            INSERT INTO channel_status (channel, last_check, status, error_msg, new_fetched, parsed_posts)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel) DO UPDATE SET
                last_check=excluded.last_check,
                status=excluded.status,
                error_msg=excluded.error_msg,
                new_fetched=excluded.new_fetched,
                parsed_posts=excluded.parsed_posts
        """, (channel["channel"], time.time(), final_status, detail or "OK", new_count, parsed_count))
    return new_count, final_status, saved_messages


def run_pipeline(csv_path="channels.csv"):
    init_db()
    channels = load_and_clean_channels(BASE_DIR / csv_path if not Path(csv_path).is_absolute() else csv_path)
    if not channels:
        print("[-] 没有可用频道名单，退出采集。")
        return

    stats = {"total_channels": len(channels), "new_messages": 0, "media_only_posts": 0, "statuses": {}}
    all_new_messages = []
    session = requests.Session()
    print(f"\n[*] 开始采集：目标频道 {len(channels)} 个；数据库：{DB_PATH}")
    try:
        with connect_db() as conn:
            for index, ch in enumerate(channels, 1):
                name = ch["channel"]
                print(f"[{index}/{len(channels)}] @{name} ...", flush=True)
                messages, status, detail, parsed_count, media_only = fetch_telegram_channel_preview(name, session)
                stats["media_only_posts"] += media_only
                try:
                    if status in {"SUCCESS_WITH_MESSAGES", "PARSE_NO_TEXT"}:
                        new_count, final_status, saved_msgs = save_channel_result(
                            conn, ch, messages, status, detail, parsed_count, media_only
                        )
                        all_new_messages.extend(saved_msgs)
                    else:
                        # Persist failure state; a later run will try the channel again.
                        with conn:
                            conn.execute("""
                                INSERT INTO channel_status (channel, last_check, status, error_msg, new_fetched, parsed_posts)
                                VALUES (?, ?, ?, ?, 0, ?)
                                ON CONFLICT(channel) DO UPDATE SET
                                    last_check=excluded.last_check,
                                    status=excluded.status,
                                    error_msg=excluded.error_msg,
                                    new_fetched=0,
                                    parsed_posts=excluded.parsed_posts
                            """, (name, time.time(), status, detail, parsed_count))
                        new_count, final_status = 0, status
                    stats["new_messages"] += new_count
                    stats["statuses"][final_status] = stats["statuses"].get(final_status, 0) + 1
                    print(f"    {final_status} | 新增 {new_count} | 解析消息 {parsed_count} | 纯媒体 {media_only}" + (f" | {detail}" if detail else ""))
                    
                    # 控制台明细预览本轮新增项
                    if new_count > 0:
                        for m_idx, s_msg in enumerate(saved_msgs, 1):
                            preview_text = s_msg["text"].replace('\n', ' ')
                            if len(preview_text) > 80:
                                preview_text = preview_text[:80] + "..."
                            print(f"      [{m_idx}] 时间: {s_msg['published_at'] or '未知'} | 内容: {preview_text}")
                            print(f"          链接: {s_msg['link']}")
                except sqlite3.Error as e:
                    stats["statuses"]["DATABASE_ERROR"] = stats["statuses"].get("DATABASE_ERROR", 0) + 1
                    print(f"[!] @{name} 数据库写入失败：{type(e).__name__}: {e}")
                time.sleep(REQUEST_DELAY)
    finally:
        session.close()

    # 导出本轮新增报告文件 (Markdown & JSONL)
    if all_new_messages:
        md_path = REPORTS_DIR / "latest_news.md"
        jsonl_path = REPORTS_DIR / "latest_news.jsonl"
        
        with md_path.open("w", encoding="utf-8") as f_md:
            f_md.write(f"# 国际新闻采集报告 (更新时间: {utc_now_iso()})\n\n")
            f_md.write(f"本轮共新增 **{len(all_new_messages)}** 条消息。\n\n---\n\n")
            for msg in all_new_messages:
                f_md.write(f"### 频道: @{msg['channel']} ({msg['category']} / {msg['language']})\n")
                f_md.write(f"- **发布时间**: `{msg['published_at'] or '未知'}`\n")
                f_md.write(f"- **原文链接**: [点击访问]({msg['link']})\n\n")
                f_md.write(f"> {msg['text'].replace(chr(10), chr(10) + '> ')}\n\n---\n\n")
                
        with jsonl_path.open("w", encoding="utf-8") as f_jl:
            for msg in all_new_messages:
                f_jl.write(json.dumps(msg, ensure_ascii=False) + "\n")
        print(f"[+] 已成功生成独立报告文件：{md_path} 与 {jsonl_path}")

    with connect_db() as conn:
        total_stored = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    print("\n" + "=" * 48)
    print("GlobalNewsCollector V1 运行摘要")
    print(f"频道总数       : {stats['total_channels']}")
    print(f"本轮新增消息   : {stats['new_messages']}")
    print(f"数据库消息总数 : {total_stored}")
    print(f"纯媒体跳过数   : {stats['media_only_posts']}")
    print(f"状态分布       : {stats['statuses']}")
    print("=" * 48)


if __name__ == "__main__":
    run_pipeline("channels.csv")
