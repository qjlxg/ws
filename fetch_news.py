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
        # Telegram 网页预览版的消息块
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
            
            # 如果没有文本（纯媒体消息），跳过或根据需要保留，这里过滤纯空消息
            if not text:
                continue
                
            # 提取真实发布时间：解析 <time datetime="..."> 标签
            time_tag = post.find("time", class_="tgme_widget_message_date")
            published_at = ""
            if time_tag and time_tag.has_attr("datetime"):
                published_at = time_tag["datetime"]
                
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
                "category": "general", # 后续由清洗模块分类
                "language": default_lang
            })
            
        return messages
    except Exception as e:
        print(f"[-] 抓取异常 @{channel}: {e}")
        return []

def save_to_db(messages):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    new_count = 0
    
    for msg in messages:
        try:
            cursor.execute("""
                INSERT OR IGNORE INTO messages 
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
            if cursor.rowcount > 0:
                new_count += 1
        except Exception as e:
            print(f"[-] 数据库写入错误: {e}")
            
    conn.commit()
    conn.close()
    return new_count

def generate_latest_report():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT channel, post_id, text, link, published_at, language 
        FROM messages 
        ORDER BY fetched_at DESC 
        LIMIT 50
    """)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    
    report_path = REPORTS_DIR / "latest_news.md"
    with report_path.open("w", encoding="utf-8") as f:
        f.write("# 📰 最新采集新闻速递\n\n")
        f.write(f"> 生成时间: `{datetime.now(timezone.utc).isoformat(timespec='seconds')}` | 展示最新 50 条\n\n---\n\n")
        
        for idx, row in enumerate(rows, 1):
            pub_time = row['published_at'] or '时间不详'
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
    
    for ch_info in channels:
        msgs = fetch_telegram_channel(ch_info)
        if msgs:
            new_added = save_to_db(msgs)
            total_new += new_added
            print(f"[+] @{ch_info['channel']}: 成功抓取 {len(msgs)} 条，新增入库 {new_added} 条")
        else:
            print(f"[-] @{ch_info['channel']}: 未获取到有效消息")
            
    print(f"\n[*] 采集完成！本轮共新增入库 {total_new} 条有效消息。")
    generate_latest_report()

if __name__ == "__main__":
    main()
