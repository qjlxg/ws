#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微头条评论素材自动发现脚本
- 平台：今日头条微头条（公开评论）
- 运行环境：GitHub Actions 免费定时
- 时间：优先最近24h，不足扩展至7天
- 输出：原文、来源链接、时间、点赞数、评分 + 去重筛选后的候选
- 明确限制：无官方免费API，依赖公开接口，可能失效；可选Cookie；不伪造数据
"""

import os
import json
import time
import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional
from urllib.parse import quote

import requests
from dateutil import parser as date_parser

# ==================== 配置 ====================
BEIJING = timezone(timedelta(hours=8))
NOW = datetime.now(BEIJING)
OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)

# 可选：从环境变量读取 Cookie（GitHub Secrets: TOUTIAO_COOKIE）
COOKIE = os.getenv("TOUTIAO_COOKIE", "").strip()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.toutiao.com/",
    "Origin": "https://www.toutiao.com",
}
if COOKIE:
    HEADERS["Cookie"] = COOKIE

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# 筛选阈值
MIN_LIKES = 5
MIN_TEXT_LEN = 15
MAX_COMMENTS_PER_POST = 30
MAX_POSTS = 15
TARGET_CANDIDATES = 20  # 目标候选数，不足时扩展时间

# 反差/共鸣/讨论空间关键词（启发式）
RESONANCE_KEYWORDS = [
    "真的", "同感", "一样", "我也", "共鸣", "说到心坎", "太真实", "扎心",
    "反差", "居然", "没想到", "竟然", "但是", "然而", "其实", "真相",
    "讨论", "怎么看", "你怎么看", "争议", "对立", "两边", "立场"
]
CONTRAST_PATTERNS = [
    r"表面.{0,10}实际", r"看起来.{0,10}其实", r"嘴上.{0,10}心里",
    r"以前.{0,10}现在", r"说好.{0,10}结果"
]

# ==================== 工具函数 ====================
def log(msg: str):
    print(f"[{datetime.now(BEIJING).strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def safe_get(url: str, params: dict = None, timeout: int = 12) -> Optional[dict]:
    try:
        r = SESSION.get(url, params=params, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        log(f"HTTP {r.status_code} for {url[:80]}")
    except Exception as e:
        log(f"请求失败: {e}")
    return None

def parse_time(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            # 秒或毫秒
            if ts > 1e12:
                ts = ts / 1000
            return datetime.fromtimestamp(ts, tz=BEIJING)
        return date_parser.parse(str(ts)).astimezone(BEIJING)
    except Exception:
        return None

def is_within_hours(dt: Optional[datetime], hours: int) -> bool:
    if not dt:
        return False
    return (NOW - dt) <= timedelta(hours=hours)

def text_hash(text: str) -> str:
    return hashlib.md5(re.sub(r"\s+", "", text).encode("utf-8")).hexdigest()

def score_comment(text: str, likes: int) -> float:
    """简单评分：点赞 + 长度 + 关键词 + 反差模式"""
    score = min(likes * 0.5, 50)
    score += min(len(text) / 10, 20)
    text_lower = text.lower()
    for kw in RESONANCE_KEYWORDS:
        if kw in text:
            score += 8
    for pat in CONTRAST_PATTERNS:
        if re.search(pat, text):
            score += 15
    if "?" in text or "？" in text:
        score += 5  # 有讨论空间
    return round(score, 1)

def filter_candidate(c: Dict) -> bool:
    text = c.get("text", "").strip()
    likes = c.get("likes", 0)
    if len(text) < MIN_TEXT_LEN or likes < MIN_LIKES:
        return False
    # 过滤纯表情/广告
    if re.fullmatch(r"[\U0001F300-\U0001F9FF\s]+", text) or "加微信" in text or "私聊" in text:
        return False
    return True

# ==================== 数据源 ====================
def fetch_hot_topics() -> List[Dict]:
    """免费热榜聚合（今日头条相关）"""
    topics = []
    # 60s API（免费、无需key）
    urls = [
        "https://60s.viki.moe/v2/toutiao",
        "https://v2.xxapi.cn/api/toutiaohot",  # 备选
    ]
    for url in urls:
        data = safe_get(url)
        if not data:
            continue
        items = data.get("data") or data.get("list") or []
        if isinstance(items, dict):
            items = items.get("data") or items.get("list") or []
        for item in items[:30]:
            title = item.get("title") or item.get("name") or item.get("word") or ""
            hot = item.get("hot") or item.get("hot_value") or item.get("score") or 0
            link = item.get("url") or item.get("link") or ""
            if title:
                topics.append({
                    "title": title,
                    "hot": hot,
                    "link": link,
                    "source": "hot_board"
                })
        if topics:
            log(f"热榜获取成功，共 {len(topics)} 条")
            break
    return topics

def search_posts_by_keyword(keyword: str, count: int = 5) -> List[Dict]:
    """通过搜索接口尝试获取相关微头条/文章（公开部分）"""
    # 今日头条搜索接口经常变化，这里使用较稳定的公开路径示意
    # 实际可能需要更新；失败时返回空，不伪造
    url = "https://www.toutiao.com/api/pc/feed/"
    params = {
        "category": "search",
        "keyword": keyword,
        "max_behot_time": 0,
        "count": count,
    }
    data = safe_get(url, params)
    posts = []
    if data and "data" in data:
        for item in data.get("data", [])[:count]:
            group_id = item.get("group_id") or item.get("item_id")
            title = item.get("title") or item.get("abstract") or keyword
            if group_id:
                posts.append({
                    "group_id": str(group_id),
                    "title": title,
                    "url": f"https://www.toutiao.com/article/{group_id}/",
                    "publish_time": parse_time(item.get("behot_time") or item.get("publish_time")),
                })
    return posts

def fetch_comments(group_id: str, offset: int = 0, count: int = 20) -> List[Dict]:
    """获取评论（公开接口，可能随时失效）"""
    url = "https://www.toutiao.com/article/v2/tab_comments/"
    params = {
        "aid": 24,
        "app_name": "toutiao-web",
        "group_id": group_id,
        "item_id": group_id,
        "offset": offset,
        "count": count,
    }
    data = safe_get(url, params)
    comments = []
    if not data:
        return comments

    # 兼容多种返回结构
    raw_list = []
    if "data" in data:
        d = data["data"]
        if isinstance(d, list):
            raw_list = d
        elif isinstance(d, dict):
            raw_list = d.get("comments") or d.get("data") or []
    elif "comments" in data:
        raw_list = data["comments"]

    for item in raw_list:
        try:
            text = item.get("text") or item.get("content") or item.get("comment_content") or ""
            if not text:
                continue
            likes = int(item.get("digg_count") or item.get("like_count") or item.get("likes") or 0)
            create_time = parse_time(item.get("create_time") or item.get("create_timestamp") or item.get("time"))
            user = item.get("user") or {}
            user_name = user.get("name") or user.get("screen_name") or item.get("user_name") or "匿名"
            comment_id = str(item.get("id") or item.get("comment_id") or text_hash(text))
            comments.append({
                "comment_id": comment_id,
                "text": text.strip(),
                "likes": likes,
                "create_time": create_time.isoformat() if create_time else None,
                "user_name": user_name,
                "group_id": group_id,
                "source_url": f"https://www.toutiao.com/article/{group_id}/",
            })
        except Exception:
            continue
    return comments

# ==================== 主流程 ====================
def main():
    log("=== 微头条评论素材自动发现开始 ===")
    log(f"当前北京时间: {NOW.isoformat()}")
    log(f"Cookie 状态: {'已提供（可选）' if COOKIE else '未提供（匿名模式）'}")

    all_candidates = []
    seen_hashes = set()
    hours_list = [24, 24*7]  # 优先24h，不足扩展到7天

    # 1. 获取热榜
    hot_topics = fetch_hot_topics()
    if not hot_topics:
        log("警告：热榜获取失败，将尝试直接关键词搜索（结果可能为空）")
        hot_topics = [{"title": "社会", "hot": 0, "link": "", "source": "fallback"}]

    for max_hours in hours_list:
        log(f"--- 尝试时间范围：最近 {max_hours} 小时 ---")
        for topic in hot_topics[:12]:
            keyword = topic["title"]
            log(f"处理热门话题: {keyword[:40]}...")

            # 搜索相关内容
            posts = search_posts_by_keyword(keyword, count=4)
            if not posts:
                # 无搜索结果时，尝试用热榜链接中的ID（如果有）
                continue

            for post in posts:
                group_id = post["group_id"]
                pub_time = post.get("publish_time")
                if pub_time and not is_within_hours(pub_time, max_hours):
                    continue

                comments = fetch_comments(group_id, count=MAX_COMMENTS_PER_POST)
                time.sleep(1.2)  # 礼貌延时

                for c in comments:
                    c_time = parse_time(c.get("create_time"))
                    if c_time and not is_within_hours(c_time, max_hours):
                        continue
                    if not filter_candidate(c):
                        continue
                    h = text_hash(c["text"])
                    if h in seen_hashes:
                        continue
                    seen_hashes.add(h)

                    score = score_comment(c["text"], c["likes"])
                    candidate = {
                        "text": c["text"],
                        "source_url": c["source_url"],
                        "create_time": c.get("create_time"),
                        "likes": c["likes"],
                        "score": score,
                        "user_name": c.get("user_name"),
                        "group_id": group_id,
                        "topic": keyword,
                        "collected_at": NOW.isoformat(),
                    }
                    all_candidates.append(candidate)

        if len(all_candidates) >= TARGET_CANDIDATES:
            log(f"已达到目标候选数 {TARGET_CANDIDATES}，停止扩展时间范围")
            break

    # 排序并截断
    all_candidates.sort(key=lambda x: x["score"], reverse=True)
    final = all_candidates[:50]

    # 输出
    out_file = OUTPUT_DIR / f"comments_{NOW.strftime('%Y%m%d_%H%M%S')}.json"
    result = {
        "meta": {
            "script": "weituo_tiao_comment_discovery",
            "beijing_time": NOW.isoformat(),
            "cookie_used": bool(COOKIE),
            "total_candidates": len(final),
            "note": "仅真实公开评论；接口可能随时失效；无官方免费API；不伪造数据",
            "limitations": [
                "评论接口为逆向公开接口，随时可能加签/限流/失效",
                "匿名模式数据量有限，深度评论建议提供 Cookie",
                "热榜与搜索结果受平台推荐与反爬影响",
                "筛选为启发式，非完美语义理解"
            ]
        },
        "candidates": final
    }
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    log(f"完成！共筛选出 {len(final)} 条候选评论")
    log(f"结果已保存: {out_file}")
    if final:
        log("Top 3 示例:")
        for i, c in enumerate(final[:3], 1):
            log(f"  {i}. [赞{c['likes']}|分{c['score']}] {c['text'][:60]}...")
    else:
        log("警告：本次未采集到符合条件的真实评论（接口可能受限或无热门内容）")

if __name__ == "__main__":
    main()
