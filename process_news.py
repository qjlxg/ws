import sqlite3
import json
import re
import os
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import Counter

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "news.db"
REPORTS_DIR = BASE_DIR / "data" / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# 严格限定的目标频道白名单
TARGET_CHANNELS = {"zvezdanews", "rtnews", "CNN_English_News"}

# 从环境变量安全获取 Gemini API Key（本地或 GitHub Secrets 传入）
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = "gemini-3.1-flash-lite"


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def translate_text_with_gemini(text, source_lang):
    """
    使用 Gemini 3.1 Flash Lite 进行智能翻译：
    若未配置 API Key 或请求异常，自动安全降级返回原文，绝不中断流水线。
    """
    if not text or not text.strip():
        return ""
    
    # 如果本身就是中文或没有配置 Key，直接返回原文
    if source_lang == "zh" or not source_lang or not GEMINI_API_KEY:
        return text

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    
    lang_name = "俄语" if source_lang.lower() == "ru" else "英语" if source_lang.lower() == "en" else source_lang
    prompt = f"请将以下{lang_name}新闻精准翻译成流畅的中文，直接输出翻译结果，不要加任何多余的解释：\n\n{text}"
    
    payload = {
        "contents": [{
            "parts": [{"text": prompt}]
        }]
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        if response.status_code == 200:
            res_json = response.json()
            translated = res_json["candidates"][0]["content"]["parts"][0]["text"].strip()
            return translated
        else:
            print(f"[-] 翻译 API 状态码异常 ({response.status_code}): {response.text}")
    except Exception as e:
        print(f"[-] 翻译请求异常 ({source_lang}): {e}")
        
    # 降级策略：翻译失败时返回原文
    return text


def parse_effective_time(time_str):
    if time_str is None:
        return None, False
    if isinstance(time_str, (int, float)):
        try:
            return datetime.fromtimestamp(time_str, tz=timezone.utc), True
        except Exception:
            return None, False
            
    if isinstance(time_str, str):
        time_str = time_str.strip()
        if not time_str:
            return None, False
        if time_str.replace('.', '', 1).isdigit():
            try:
                ts = float(time_str)
                if ts > 10**11:
                    ts /= 1000.0
                return datetime.fromtimestamp(ts, tz=timezone.utc), True
            except Exception:
                pass
        try:
            cleaned = time_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(cleaned)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt, True
        except Exception:
            pass
    return None, False


def get_message_timestamp(msg):
    dt, success = parse_effective_time(msg.get("published_at"))
    if not success:
        dt, success = parse_effective_time(msg.get("fetched_at"))
    if not success:
        dt = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return dt


def normalize_full_text(text):
    if not text:
        return ""
    text = re.sub(r"https?://\S+", "", text)
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    return " ".join(text.split())


def build_safe_topic_candidates(messages):
    clusters = []
    for msg in messages:
        msg_time = get_message_timestamp(msg)
        target_text = msg.get("translated_text") or msg["text"]
        words = set(w for w in re.findall(r'[\u4e00-\u9fa5a-z]{2,}', target_text.lower()))
        
        matched_cluster = None
        for cluster in clusters:
            time_diff = abs((msg_time - cluster["base_time"]).total_seconds())
            if time_diff > 24 * 3600:
                continue
            common_words = words.intersection(cluster["keywords"])
            if len(common_words) >= 3:
                matched_cluster = cluster
                break
                
        if matched_cluster:
            matched_cluster["sources"].append(msg)
            matched_cluster["keywords"].update(words)
        else:
            clusters.append({
                "cluster_id": len(clusters) + 1,
                "base_time": msg_time,
                "keywords": words,
                "sources": [msg]
            })
    return clusters


def run_processing_pipeline():
    if not DB_PATH.exists():
        print(f"[-] 找不到数据库文件: {DB_PATH}")
        return

    conn = connect_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT channel, post_id, text, link, published_at, fetched_at, category, language 
        FROM messages
    """)
    all_database_rows = [dict(r) for r in cursor.fetchall()]
    conn.close()

    total_database_count = len(all_database_rows)
    if total_database_count == 0:
        print("[-] 数据库中无任何记录。")
        return

    target_raw_rows = [m for m in all_database_rows if m["channel"] in TARGET_CHANNELS]
    target_raw_count = len(target_raw_rows)
    if target_raw_count == 0:
        print(f"[-] 数据库中未发现目标频道 {TARGET_CHANNELS} 的消息。")
        return

    valid_text_rows = []
    empty_text_count = 0
    time_parse_failed_count = 0

    for msg in target_raw_rows:
        text = msg.get("text")
        if not text or not text.strip():
            empty_text_count += 1
            continue
        _, p_ok = parse_effective_time(msg.get("published_at"))
        _, f_ok = parse_effective_time(msg.get("fetched_at"))
        if not p_ok and not f_ok:
            time_parse_failed_count += 1
        valid_text_rows.append(msg)

    sorted_rows = sorted(valid_text_rows, key=lambda x: get_message_timestamp(x), reverse=True)

    now_utc = datetime.now(timezone.utc)
    seven_days_ago = now_utc - timedelta(days=7)

    recent_rows = []
    older_rows = []
    for msg in sorted_rows:
        if get_message_timestamp(msg) >= seven_days_ago:
            recent_rows.append(msg)
        else:
            older_rows.append(msg)

    if GEMINI_API_KEY:
        print(f"[*] 检测到有效 API Key，开始对最近 7 天的 {len(recent_rows)} 条有效素材进行 AI 智能翻译...")
    else:
        print(f"[!] 未检测到 GEMINI_API_KEY 环境变量，将跳过 AI 翻译，直接使用原文进行处理。")

    # 执行 AI 翻译并挂载字段
    for msg in recent_rows:
        orig_text = msg["text"]
        lang = msg.get("language", "en")
        translated = translate_text_with_gemini(orig_text, lang)
        msg["translated_text"] = translated

    # 去重处理
    unique_materials_map = {}
    duplicate_count = 0

    for msg in recent_rows:
        norm_text = normalize_full_text(msg.get("translated_text") or msg["text"])
        if not norm_text:
            continue
            
        if norm_text not in unique_materials_map:
            msg_copy = dict(msg)
            msg_copy["duplicate_sources"] = []
            unique_materials_map[norm_text] = msg_copy
        else:
            duplicate_count += 1
            primary_item = unique_materials_map[norm_text]
            primary_item["duplicate_sources"].append({
                "channel": msg["channel"],
                "language": msg["language"],
                "published_at": msg["published_at"],
                "fetched_at": msg["fetched_at"],
                "link": msg["link"],
                "post_id": msg["post_id"],
                "text": msg["text"],
                "translated_text": msg.get("translated_text")
            })

    deduped_recent_valid = list(unique_materials_map.values())
    topic_clusters = build_safe_topic_candidates(deduped_recent_valid)
    multi_source_candidates = [c for c in topic_clusters if len(c["sources"]) > 1]
    single_source_groups = [c for c in topic_clusters if len(c["sources"]) == 1]

    language_counts = Counter(m["language"] for m in deduped_recent_valid)
    channel_counts = Counter(m["channel"] for m in deduped_recent_valid)

    # 导出文件
    with (REPORTS_DIR / "all_news.json").open("w", encoding="utf-8") as f:
        json.dump(all_database_rows, f, ensure_ascii=False, indent=2)

    with (REPORTS_DIR / "recent_valid_news.json").open("w", encoding="utf-8") as f:
        json.dump(deduped_recent_valid, f, ensure_ascii=False, indent=2)

    stats_data = {
        "processed_at": utc_now_iso(),
        "target_channels": list(TARGET_CHANNELS),
        "total_database_count": total_database_count,
        "target_raw_count": target_raw_count,
        "empty_text_count": empty_text_count,
        "time_parse_failed_count": time_parse_failed_count,
        "recent_total_count": len(recent_rows),
        "older_total_count": len(older_rows),
        "duplicate_content_count": duplicate_count,
        "recent_valid_unique_count": len(deduped_recent_valid),
        "multi_source_candidates_count": len(multi_source_candidates),
        "languages": dict(language_counts),
        "channels": dict(channel_counts)
    }
    with (REPORTS_DIR / "pipeline_stats.json").open("w", encoding="utf-8") as f:
        json.dump(stats_data, f, ensure_ascii=False, indent=2)

    # 生成 Markdown 简报
    digest_md_path = REPORTS_DIR / "events_digest.md"
    with digest_md_path.open("w", encoding="utf-8") as f:
        f.write(f"# 🌐 全球多语言情报与 AI 智能聚合简报\n\n")
        f.write(f"> **处理时间**: `{utc_now_iso()}` | **AI 模型**: `{GEMINI_MODEL if GEMINI_API_KEY else '未启用'}`\n\n")
        f.write("## 📊 统计漏斗\n\n")
        f.write(f"- 数据库总量: `{total_database_count}` | 目标频道抓取: `{target_raw_count}`\n")
        f.write(f"- 近7天有效: `{len(recent_rows)}` | 去重后独立素材: `{len(deduped_recent_valid)}`\n\n---\n\n")

        f.write("## 🧩 同主题候选提示\n\n")
        for idx, cluster in enumerate(multi_source_candidates, 1):
            sources = cluster["sources"]
            primary_msg = sources[0]
            f.write(f"### 候选组 #{idx} (共 {len(sources)} 个来源)\n\n")
            f.write(f"> **核心摘要**: {primary_msg.get('translated_text') or primary_msg['text']}\n\n")
            f.write(f"- 主来源: @{primary_msg['channel']} ({primary_msg['language'].upper()}) | 🔗 [原文]({primary_msg['link']})\n\n---\n\n")

    latest_md_path = REPORTS_DIR / "latest_news.md"
    with latest_md_path.open("w", encoding="utf-8") as f:
        f.write("# 📰 最新规范新闻速递\n\n")
        f.write(f"> 生成时间: `{utc_now_iso()}` | 展示近 7 天去重有效素材\n\n---\n\n")
        for idx, row in enumerate(deduped_recent_valid, 1):
            pub_time = row['published_at'] or f"抓取于 {row['fetched_at']}"
            f.write(f"### [{idx}] @{row['channel']} ({row['language'].upper()}) | ⏱️ {pub_time}\n\n")
            f.write(f"**[内容]**: {row.get('translated_text') or row['text']}\n\n")
            if row.get('translated_text') and row.get('translated_text') != row['text']:
                f.write(f"<details><summary>点击查看外文原文</summary>\n\n> {row['text']}\n\n</details>\n\n")
            f.write(f"🔗 [查看原文]({row['link']})\n\n---\n\n")

    print(f"\n[+] 新闻处理流水线执行完毕！")
    print(f"    - 独立素材数: {len(deduped_recent_valid)}")
    print(f"    - 报告已生成至: {REPORTS_DIR}\n")


if __name__ == "__main__":
    run_processing_pipeline()
