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
    """提取文本核心特征指纹用于快速去重与简单匹配"""
    text = re.sub(r"https?://\S+", "", text)  # 移除 URL
    text = re.sub(r"[^\w\s]", "", text)       # 移除标点
    words = text.lower().split()
    return " ".join(words[:20])               # 截取前20个核心词组


def run_processing_pipeline():
    if not DB_PATH.exists():
        print(f"[-] 找不到数据库文件: {DB_PATH}")
        return

    conn = connect_db()
    cursor = conn.cursor()

    # 读取所有未处理或历史存量消息
    cursor.execute("""
        SELECT channel, post_id, text, link, published_at, fetched_at, category, language 
        FROM messages 
        ORDER BY fetched_at DESC
    """)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()

    total_raw = len(rows)
    if total_raw == 0:
        print("[-] 数据库中无任何新闻消息，跳过二次处理。")
        return

    # 1. 过滤与去重漏斗统计
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

    # 2. 统计语言分布与多源事件归类（按分类与频道聚合）
    language_counts = Counter(m["language"] for m in deduped_messages)
    category_groups = defaultdict(list)
    channel_groups = defaultdict(list)

    for msg in deduped_messages:
        category_groups[msg["category"]].append(msg)
        channel_groups[msg["channel"]].append(msg)

    # 3. 输出 Markdown 综合事件与素材报告
    digest_md_path = REPORTS_DIR / "events_digest.md"
    with digest_md_path.open("w", encoding="utf-8") as f:
        f.write(f"# 🌐 全球多语言新闻情报与素材简报\n\n")
        f.write(f"> **处理时间**: `{utc_now_iso()}` | **原始积累**: {total_raw} 条 | **独立去重后**: {len(deduped_messages)} 条\n\n")

        f.write("## 📊 数据漏斗与语言分布统计\n\n")
        f.write(f"- **总接收原始消息**: `{total_raw}` 条\n")
        f.write(f"- **重复/高度相似剥离**: `{duplicate_count}` 条\n")
        f.write(f"- **独立有效新闻素材**: `{len(deduped_messages)}` 条\n\n")
        
        f.write("### 语种分布:\n")
        for lang, count in language_counts.items():
            f.write(f"- **{lang.upper()}**: {count} 条\n")
        f.write("\n---\n\n")

        f.write("## 📁 分类新闻素材汇总\n\n")
        for cat, msgs in category_groups.items():
            f.write(f"### 🏷️ 分类: {cat.upper()} (共 {len(msgs)} 条)\n\n")
            for idx, msg in enumerate(msgs[:30], 1):  # 最多精选前30条示范
                snippet = msg["text"].replace("\n", " ")
                if len(snippet) > 150:
                    snippet = snippet[:150] + "..."
                f.write(f"**[{idx}] @{msg['channel']}** | `{msg['published_at'] or '未知时间'}`\n")
                f.write(f"> {snippet}\n\n")
                f.write(f"🔗 [阅读原文]({msg['link']})\n\n---\n\n")

    # 4. 导出 JSON 漏斗数据指标
    stats_json_path = REPORTS_DIR / "pipeline_stats.json"
    stats_data = {
        "processed_at": utc_now_iso(),
        "total_raw": total_raw,
        "duplicate_count": duplicate_count,
        "unique_count": len(deduped_messages),
        "languages": dict(language_counts),
        "categories": {k: len(v) for k, v in category_groups.items()}
    }
    with stats_json_path.open("w", encoding="utf-8") as f:
        json.dump(stats_data, f, ensure_ascii=False, indent=2)

    print(f"\n[+] 新闻二次处理完成！")
    print(f"    - 原始消息: {total_raw} 条")
    print(f"    - 过滤重复: {duplicate_count} 条")
    print(f"    - 剩余精选: {len(deduped_messages)} 条")
    print(f"    - 报告已生成: {digest_md_path}\n")


if __name__ == "__main__":
    run_processing_pipeline()
