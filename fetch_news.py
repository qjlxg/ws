import sqlite3
import os
import re
from datetime import datetime, timezone
from pathlib import Path
import requests
from bs4 import BeautifulSoup

# 路径配置
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
REPORTS_DIR = DATA_DIR / "reports"
DATA_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "news.db"
CHANNELS_FILE = BASE_DIR / "channels.txt"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT,
            post_id TEXT,
            text TEXT,
            link TEXT,
            published_at TEXT,
            fetched_at TEXT,
            category TEXT,
            language TEXT,
            UNIQUE(channel, post_id)
        )
    """)
    conn.commit()
    conn.close()

def load_channels():
    """从 channels.txt 读取频道和语种，格式：channel_name lang"""
    channels = []
    if not CHANNELS_FILE.exists():
        print(f"[-] 找不到频道配置文件: {CHANNELS_FILE}")
        return channels
    
    with CHANNELS_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            channel_name = parts[0].lstrip("@")
            lang = parts[1] if len(parts) > 1 else "en"
            channels.append({"channel": channel_name, "language": lang})
    return channels

def fetch_telegram_channel(channel_info):
    channel = channel_info["channel"]
    default_lang = channel_info["language"]
    url = f"https://t.me/s/{channel}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200:
            print(f"[-] 抓取失败 @{channel}: HTTP 状态码 {response.status_code}")
            return []
            
        soup = BeautifulSoup(response.text, "html.parser")
        posts = soup.find_all("div", class_="tgme_widget_message")
        
        messages = []
        fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        
        for post in posts:
            post_id = post.get("data-post", "")
            if "/" in post_id:
                post_id = post_id.split("/")[-1]
            if not post_id:
                continue
                
            # 提取文本
            text_div = post.find("div", class_="tgme_widget_message_text")
            text = text_div.get_text(separator="\n", strip=True) if text_div else ""
            
            if not text:
                continue
                
            # 严格按照你的建议修复的时间提取选择器
            time_tag = post.select_one(".tgme_widget_message_date time[datetime], time[datetime]")
            published_at = time_tag.get("datetime", "").strip() if time_tag else ""
            
            if not published_at:
                date_link = post.select_one("a.tgme_widget_message_date")
                if date_link:
                    t_tag = date_link.find("time")
                    if t_tag:
                        published_at = t_tag.get("datetime", "").strip()
            
            # 调试保护：若依然抓不到时间，打印提示以便直接对账 HTML 结构
            if not published_at:
                date_element = post.select_one(".tgme_widget_message_date")
                print(f"[DEBUG] @{channel}/{post_id}: 未能提取到发布时间，HTML 元素为 -> {date_element if date_element else '未找到对应元素'}")
                
            # 提取永久链接
            link_tag = post.find("a", class_="tgme_widget_message_date")
            link = link_tag["href"] if link_tag and link_tag.has_attr("href") else f"https://t.me/{channel}/{post_id}"
            
            messages.append({
                "channel": channel,
                "post_id": post_id,
                "text": text,
                "link": link,
                "published_at": published_at,
                "fetched_at": fetched_at,
                "category": "general",
                "language": default_lang
            })
            
        return messages
    except Exception as e:
        print(f"[-] 抓取异常 @{channel}: {e}")
        return []

def save_and_patch_db(messages):
    """
    入库函数：
    1. 若为新消息，直接 INSERT。
    2. 若消息已存在（UNIQUE 冲突），但旧记录的 published_at 为空而新抓取到了时间，则执行 UPDATE 安全补全。
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    new_count = 0
    patched_count = 0
    
    for msg in messages:
        try:
            # 尝试插入新记录
            cursor.execute("""
                INSERT INTO messages 
                (channel, post_id, text, link, published_at, fetched_at, category, language)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                msg["channel"],
                msg["post_id"],
                msg["text"],
                msg["link"],
                msg["published_at"],
                msg["fetched_at"],
                msg["category"],
                msg["language"]
            ))
            new_count += 1
        except sqlite3.IntegrityError:
            # 记录已存在，检查并安全补全旧记录中缺失的 published_at
            if msg["published_at"]:
                cursor.execute("""
                    UPDATE messages 
                    SET published_at = ? 
                    WHERE channel = ? AND post_id = ? AND (published_at IS NULL OR published_at = '')
                """, (msg["published_at"], msg["channel"], msg["post_id"]))
                if cursor.rowcount > 0:
                    patched_count += 1
                    
    conn.commit()
    conn.close()
    return new_count, patched_count

def generate_latest_report():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    # 严格按照你建议的健壮排序规则生成报告
    cursor.execute("""
        SELECT channel, post_id, text, link, published_at, fetched_at, language 
        FROM messages 
        ORDER BY 
            CASE WHEN published_at IS NULL OR published_at = '' THEN 1 ELSE 0 END,
            published_at DESC,
            fetched_at DESC
        LIMIT 50
    """)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    
    report_path = REPORTS_DIR / "latest_news.md"
    with report_path.open("w", encoding="utf-8") as f:
        f.write("# 📰 最新采集新闻速递（时间优化版）\n\n")
        f.write(f"> 生成时间: `{datetime.now(timezone.utc).isoformat(timespec='seconds')}` | 严格按发布时间排序\n\n---\n\n")
        
        for idx, row in enumerate(rows, 1):
            pub_time = row['published_at'] or f"原时间不详 (抓取于 {row['fetched_at']})"
            f.write(f"### [{idx}] @{row['channel']} ({row['language'].upper()}) | ⏱️ {pub_time}\n\n")
            f.write(f"> {row['text'].replace(chr(10), ' ')}\n\n")
            f.write(f"🔗 [查看原文]({row['link']})\n\n---\n\n")

def main():
    init_db()
    channels = load_channels()
    if not channels:
        print("[-] 未在 channels.txt 中找到任何有效频道配置。")
        return
        
    print(f"[*] 开始采集，共加载 {len(channels)} 个频道...")
    total_new = 0
    total_patched = 0
    
    for ch_info in channels:
        msgs = fetch_telegram_channel(ch_info)
        if msgs:
            new_added, patched = save_and_patch_db(msgs)
            total_new += new_added
            total_patched += patched
            print(f"[+] @{ch_info['channel']}: 获取 {len(msgs)} 条 | 新增入库 {new_added} 条 | 补全旧时间 {patched} 条")
        else:
            print(f"[-] @{ch_info['channel']}: 未获取到有效消息")
            
    print(f"\n[*] 采集与补全完成！本轮新增 {total_new} 条，成功补全历史时间 {total_patched} 条。")
    generate_latest_report()

if __name__ == "__main__":
    main()
