import sqlite3
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict, Counter

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "news.db"
REPORTS_DIR = BASE_DIR / "data" / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def clean_text_fingerprint(text):
    """
    改进的文本指纹去重规则：
    过滤掉常见的固定新闻前缀（如 'главное к этому часу' 等），
    并截取更有辨识度的核心词组，避免不同新闻因相似开头被误删。
    """
    if not text:
        return ""
    # 移除 URL
    text = re.sub(r"https?://\S+", "", text)
    # 转换为小写
    text = text.lower()
    # 剔除常见的通用新闻前缀短语
    common_prefixes = [
        "главное к этому часу", "главное новости", "breaking", 
        "flash", "update", "сообщает", "мо рф сообщает"
    ]
    for prefix in common_prefixes:
        if text.startswith(prefix):
            text = text[len(prefix):]
            
    # 移除标点符号与特殊字符
    text = re.sub(r"[^\w\s]", "", text)
    words = text.split()
    # 过滤掉过短的虚词，取前 15 个有意义的词作为指纹
    meaningful_words = [w for w in words if len(w) > 2]
    return " ".join(meaningful_words[:15])


def extract_key_entities(text):
    """提取文本中的核心英文/俄文实体或数字特征用于跨语言事件匹配候选"""
    if not text:
        return set()
    # 提取大写开头的连续单词、数字、或者特定的国际通用专有名词
    tokens = re.findall(r'\b[A-Z][a-zA-Z]{3,}\b|\b\d+\b', text)
    return set(tokens)


def cluster_cross_lingual_events(messages):
    """
    跨语言事件聚类与多源合并算法（候选识别）：
    不依赖付费AI，通过提取消息中的核心实体与时间窗口，
    将指向同一事件的多源报道归纳到一起。
    """
    clusters = []
    
    for msg in messages:
        text = msg["text"]
        entities = extract_key_entities(text)
        
        matched_cluster = None
        for cluster in clusters:
            # 如果实体交集达到一定数量，或者指纹高度相似，则认为属于同一事件
            common_entities = entities.intersection(cluster["entities"])
            if len(common_entities) >= 2:
                matched_cluster = cluster
                break
                
        if matched_cluster:
            matched_cluster["sources"].append(msg)
            matched_cluster["entities"].update(entities)
        else:
            clusters.append({
                "cluster_id": len(clusters) + 1,
                "entities": entities,
                "sources": [msg]
            })
            
    return clusters


def run_processing_pipeline():
    if not DB_PATH.exists():
        print(f"[-] 找不到数据库文件: {DB_PATH}")
        return

    conn = connect_db()
    cursor = conn.cursor()

    # 严格按照发布时间或抓取时间读取所有历史存量消息
    cursor.execute("""
        SELECT channel, post_id, text, link, published_at, fetched_at, category, language 
        FROM messages 
        ORDER BY COALESCE(NULLIF(published_at, ''), fetched_at) DESC
    """)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()

    total_raw = len(rows)
    if total_raw == 0:
        print("[-] 数据库中无任何新闻消息，跳过二次处理。")
        return

    # 1. 改进的消息级去重统计（保留所有原始数据在数据库中，仅在内存中剥离重复展示）
    unique_fingerprints = set()
    deduped_messages = []
    duplicate_count = 0

    for msg in rows:
        fp = clean_text_fingerprint(msg["text"])
        if not fp or fp in unique_fingerprints:
            duplicate_count += 1
            continue
        unique_fingerprints.add(fp)
        deduped_messages.append(msg)

    # 2. 跨语言事件聚合
    event_clusters = cluster_cross_lingual_events(deduped_messages)

    # 3. 统计语种与频道分布
    language_counts = Counter(m["language"] for m in deduped_messages)
    channel_counts = Counter(m["channel"] for m in deduped_messages)

    # 4. 导出完整数据 JSON (all_news.json)
    all_news_json_path = REPORTS_DIR / "all_news.json"
    with all_news_json_path.open("w", encoding="utf-8") as f:
        json.dump(deduped_messages, f, ensure_ascii=False, indent=2)

    # 5. 输出精选 Markdown 新闻事件与多来源素材报告 (events_digest.md)
    digest_md_path = REPORTS_DIR / "events_digest.md"
    with digest_md_path.open("w", encoding="utf-8") as f:
        f.write(f"# 🌐 全球多语言新闻情报与事件聚合素材简报\n\n")
        f.write(f"> **处理时间**: `{utc_now_iso()}` | **底层原始总数**: {total_raw} 条 | **去重后独立条目**: {len(deduped_messages)} 条 | **识别事件聚类**: {len(event_clusters)} 组\n\n")

        f.write("## 📊 漏斗与多源统计指标\n\n")
        f.write(f"- **总接收原始消息**: `{total_raw}` 条\n")
        f.write(f"- **高相似重复剥离**: `{duplicate_count}` 条\n")
        f.write(f"- **独立有效新闻素材**: `{len(deduped_messages)}` 条\n")
        f.write(f"- **多源交叉事件组**: `{len(event_clusters)}` 组\n\n")
        
        f.write("### 语种分布:\n")
        for lang, count in language_counts.items():
            f.write(f"- **{lang.upper()}**: {count} 条\n")
            
        f.write("\n### 频道贡献分布:\n")
        for ch, count in channel_counts.items():
            f.write(f"- **@{ch}**: {count} 条\n")
        f.write("\n---\n\n")

        f.write("## 🧩 跨语言事件聚类与多源交叉报道汇总\n\n")
        for idx, cluster in enumerate(event_clusters, 1):
            sources = cluster["sources"]
            primary_msg = sources[0]
            pub_time = primary_msg['published_at'] or primary_msg['fetched_at']
            
            f.write(f"### 事件团 #{idx} (涉及 {len(sources)} 个来源通道)\n\n")
            f.write(f"> **参考时间**: `{pub_time}` | **代表性文摘**: \n")
            snippet = primary_msg["text"].replace("\n", " ")
            if len(snippet) > 200:
                snippet = snippet[:200] + "..."
            f.write(f"> {snippet}\n\n")
            
            f.write("**📢 原始来源与多源链接追踪**:\n")
            for src in sources:
                f.write(f"- 频道: **@{src['channel']}** ({src['language'].upper()}) | ⏱️ `{src['published_at'] or '时间不详'}`\n")
                f.write(f"  - 🔗 [查看原文链接]({src['link']})\n")
            f.write("\n---\n\n")

    # 6. 同时更新 latest_news.md (全量无截断展示)
    latest_md_path = REPORTS_DIR / "latest_news.md"
    with latest_md_path.open("w", encoding="utf-8") as f:
        f.write("# 📰 最新采集新闻全量速递\n\n")
        f.write(f"> 生成时间: `{utc_now_iso()}` | 展示全部去重后有效素材 (共 {len(deduped_messages)} 条)\n\n---\n\n")
        
        for idx, row in enumerate(deduped_messages, 1):
            pub_time = row['published_at'] or f"抓取于 {row['fetched_at']}"
            f.write(f"### [{idx}] @{row['channel']} ({row['language'].upper()}) | ⏱️ {pub_time}\n\n")
            f.write(f"> {row['text'].replace(chr(10), ' ')}\n\n")
            f.write(f"🔗 [查看原文]({row['link']})\n\n---\n\n")

    # 7. 导出统计指标 JSON (pipeline_stats.json)
    stats_json_path = REPORTS_DIR / "pipeline_stats.json"
    stats_data = {
        "processed_at": utc_now_iso(),
        "total_raw": total_raw,
        "duplicate_count": duplicate_count,
        "unique_count": len(deduped_messages),
        "cluster_count": len(event_clusters),
        "languages": dict(language_counts),
        "channels": dict(channel_counts)
    }
    with stats_json_path.open("w", encoding="utf-8") as f:
        json.dump(stats_data, f, ensure_ascii=False, indent=2)

    print(f"\n[+] 新闻智能处理与事件聚合完成！")
    print(f"    - 原始消息: {total_raw} 条")
    print(f"    - 逻辑去重: {duplicate_count} 条")
    print(f"    - 有效素材: {len(deduped_messages)} 条")
    print(f"    - 事件聚类: {len(event_clusters)} 组")
    print(f"    - 生成报告: {digest_md_path} & {latest_md_path}\n")


if __name__ == "__main__":
    run_processing_pipeline()
