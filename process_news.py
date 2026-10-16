import sqlite3
import json
import re
import os
import time
import hashlib
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import Counter

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "news.db"
REPORTS_DIR = BASE_DIR / "data" / "reports"
CACHE_PATH = BASE_DIR / "data" / "translation_cache.json"

REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# 严格限定的目标频道白名单
TARGET_CHANNELS = {"zvezdanews", "rtnews", "CNN_English_News"}

# 从环境变量安全获取 Gemini API Key
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = "gemini-3.1-flash-lite"


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def load_translation_cache():
    """加载本地持久化翻译缓存，避免重复请求消耗额度"""
    if CACHE_PATH.exists():
        try:
            with CACHE_PATH.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_translation_cache(cache_data):
    """保存翻译缓存到 data/translation_cache.json"""
    try:
        with CACHE_PATH.open("w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[-] 保存翻译缓存失败: {e}")


def get_text_hash(text):
    """基于原文内容和模型生成唯一哈希"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def translate_text_with_retry(text, source_lang, cache):
    """
    带持久化缓存、503重试与退避机制的智能翻译
    返回: (translated_text, status)
    status 取值: cached, success, failed, skipped
    """
    if not text or not text.strip():
        return "", "skipped"
    
    if source_lang == "zh" or not source_lang:
        return text, "skipped"
        
    if not GEMINI_API_KEY:
        return text, "skipped"

    # 计算缓存 Key
    text_hash = get_text_hash(text)
    cache_key = f"{text_hash}_{source_lang}_{GEMINI_MODEL}"
    
    # 1. 检查持久化缓存
    if cache_key in cache:
        cached_item = cache[cache_key]
        if cached_item.get("status") == "success":
            return cached_item["translated_text"], "cached"

    # 2. 调用 Gemini API（带 503 重试与退避）
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    
    lang_name = "俄语" if source_lang.lower() == "ru" else "英语" if source_lang.lower() == "en" else source_lang
    prompt = f"请将以下{lang_name}新闻精准翻译成流畅的中文，直接输出翻译结果，不要加任何多余的解释：\n\n{text}"
    
    payload = {
        "contents": [{
            "parts": [{"text": prompt}]
        }]
    }

    max_retries = 3
    backoff_factor = 2  # 2秒, 4秒, 8秒
    
    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=25)
            if response.status_code == 200:
                res_json = response.json()
                translated = res_json["candidates"][0]["content"]["parts"][0]["text"].strip()
                
                # 写入缓存
                cache[cache_key] = {
                    "translated_text": translated,
                    "status": "success",
                    "updated_at": utc_now_iso()
                }
                return translated, "success"
                
            elif response.status_code in [503, 429]:
                # 遇到高负载 503 或限流 429，进行退避重试
                sleep_time = backoff_factor ** (attempt + 1)
                print(f"[!] API 遇到负载高峰 ({response.status_code})，将在 {sleep_time} 秒后进行第 {attempt+1} 次重试...")
                time.sleep(sleep_time)
            else:
                print(f"[-] 翻译 API 状态码异常 ({response.status_code}): {response.text}")
                break
        except Exception as e:
            sleep_time = backoff_factor ** (attempt + 1)
            print(f"[!] 翻译请求异常 ({e})，将在 {sleep_time} 秒后重试...")
            time.sleep(sleep_time)

    # 失败降级：记录失败状态，返回原文
    cache[cache_key] = {
        "translated_text": None,
        "status": "failed",
        "updated_at": utc_now_iso()
    }
    return text, "failed"


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
        # 仅使用成功翻译的中文或原文进行粗颗粒度主题候选聚类
        target_text = msg.get("translated_text") if msg.get("translation_status") in ["success", "cached"] else msg["text"]
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
    pub_parse_failed_count = 0
    fetch_parse_failed_count = 0
    both_parse_failed_count = 0

    for msg in target_raw_rows:
        text = msg.get("text")
        if not text or not text.strip():
            empty_text_count += 1
            continue
            
        _, p_ok = parse_effective_time(msg.get("published_at"))
        _, f_ok = parse_effective_time(msg.get("fetched_at"))
        
        if not p_ok:
            pub_parse_failed_count += 1
        if not f_ok:
            fetch_parse_failed_count += 1
        if not p_ok and not f_ok:
            both_parse_failed_count += 1
            
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

    # 加载翻译缓存
    translation_cache = load_translation_cache()
    
    translation_stats = {"cached": 0, "success": 0, "failed": 0, "skipped": 0}

    print(f"[*] 开始处理最近 7 天的 {len(recent_rows)} 条有效素材（启用持久化缓存与重试机制）...")

    # 执行 AI 翻译与状态记录
    for msg in recent_rows:
        orig_text = msg["text"]
        lang = msg.get("language", "en")
        translated, status = translate_text_with_retry(orig_text, lang, translation_cache)
        
        msg["translated_text"] = translated if status in ["success", "cached"] else None
        msg["translation_status"] = status
        translation_stats[status] += 1

    # 保存更新后的缓存
    save_translation_cache(translation_cache)

    # 去重处理（基于成功翻译的中文或原文）
    unique_materials_map = {}
    duplicate_count = 0

    for msg in recent_rows:
        text_for_dedup = msg.get("translated_text") if msg.get("translation_status") in ["success", "cached"] else msg["text"]
        norm_text = normalize_full_text(text_for_dedup)
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
                "translated_text": msg.get("translated_text"),
                "translation_status": msg.get("translation_status")
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
        "published_at_parse_failed_count": pub_parse_failed_count,
        "fetched_at_parse_failed_count": fetch_parse_failed_count,
        "both_timestamps_parse_failed_count": both_parse_failed_count,
        "recent_total_count": len(recent_rows),
        "older_total_count": len(older_rows),
        "duplicate_content_count": duplicate_count,
        "recent_valid_unique_count": len(deduped_recent_valid),
        "multi_source_candidates_count": len(multi_source_candidates),
        "translation_stats": translation_stats,
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
        f.write("## 📊 统计漏斗与翻译漏斗\n\n")
        f.write(f"- 数据库总量: `{total_database_count}` | 目标频道抓取: `{target_raw_count}`\n")
        f.write(f"- 近7天有效: `{len(recent_rows)}` | 去重后独立素材: `{len(deduped_recent_valid)}`\n")
        f.write(f"- 翻译缓存命中: `{translation_stats['cached']}` | 新翻译成功: `{translation_stats['success']}` | 翻译失败: `{translation_stats['failed']}`\n\n---\n\n")

        f.write("## 🧩 同主题候选提示 (待核查)\n\n")
        for idx, cluster in enumerate(multi_source_candidates, 1):
            sources = cluster["sources"]
            primary_msg = sources[0]
            display_text = primary_msg.get('translated_text') or primary_msg['text']
            f.write(f"### 候选组 #{idx} (共 {len(sources)} 个来源)\n\n")
            f.write(f"> **核心摘要**: {display_text}\n\n")
            f.write(f"- 主来源: @{primary_msg['channel']} ({primary_msg['language'].upper()}) | 🔗 [原文]({primary_msg['link']})\n\n---\n\n")

    latest_md_path = REPORTS_DIR / "latest_news.md"
    with latest_md_path.open("w", encoding="utf-8") as f:
        f.write("# 📰 最新规范新闻速递\n\n")
        f.write(f"> 生成时间: `{utc_now_iso()}` | 展示近 7 天去重有效素材\n\n---\n\n")
        for idx, row in enumerate(deduped_recent_valid, 1):
            pub_time = row['published_at'] or f"抓取于 {row['fetched_at']}"
            display_content = row.get('translated_text') or row['text']
            status_tag = "【AI译文】" if row.get('translation_status') in ["success", "cached"] else "【原文降级】"
            
            f.write(f"### [{idx}] @{row['channel']} ({row['language'].upper()}) | ⏱️ {pub_time}\n\n")
            f.write(f"**{status_tag}**: {display_content}\n\n")
            if row.get('translation_status') in ["success", "cached"]:
                f.write(f"<details><summary>点击查看外文原文</summary>\n\n> {row['text']}\n\n</details>\n\n")
            f.write(f"🔗 [查看原文]({row['link']})\n\n---\n\n")

    print(f"\n[+] 新闻处理流水线执行完毕！")
    print(f"    - 独立素材数: {len(deduped_recent_valid)}")
    print(f"    - 翻译统计: 缓存命中 {translation_stats['cached']}, 成功 {translation_stats['success']}, 失败 {translation_stats['failed']}")
    print(f"    - 报告已生成至: {REPORTS_DIR}\n")


if __name__ == "__main__":
    run_processing_pipeline()
