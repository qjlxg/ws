# -*- coding: utf-8 -*-
"""
B站全量评论素材自动发现脚本（全收录宽松版）
- 功能：获取B站热门视频，无条件抓取所有有效公开评论，去重后全量保存至 master_comment_pool.csv
"""

import os
import json
import time
import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional
import requests
import pandas as pd

# ==================== 配置 ====================
BEIJING = timezone(timedelta(hours=8))
NOW = datetime.now(BEIJING)
OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)
MASTER_CSV = OUTPUT_DIR / "master_comment_pool.csv"
METRICS_JSON = OUTPUT_DIR / "run_metrics.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# 放宽后的阈值：只要有实际内容就收录
MIN_TEXT_LEN = 2      
TARGET_CANDIDATES = 100 # 扩大单次抓取上限，确保尽可能多收录

def log(msg: str):
    print(f"[{datetime.now(BEIJING).strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def safe_get(url: str, params: dict = None) -> Optional[dict]:
    try:
        r = SESSION.get(url, params=params, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log(f"请求异常 {url}: {e}")
    return None

def text_hash(text: str) -> str:
    return hashlib.md5(re.sub(r"\s+", "", text).encode("utf-8")).hexdigest()

def score_comment(text: str, likes: int) -> float:
    """宽松评分：以点赞数和字数做基础排序，不设高门槛"""
    return float(likes) + len(text) * 0.1

def filter_comment(text: str, likes: int) -> bool:
    """极度宽松的过滤：只要不是空文本或极短乱码，全部放行"""
    if not text or len(text.strip()) < MIN_TEXT_LEN:
        return False
    return True

# ==================== 核心数据获取 ====================
def fetch_bilibili_popular_videos(limit: int = 20) -> List[Dict]:
    """获取B站全站热门视频列表"""
    url = "https://api.bilibili.com/x/web-interface/popular"
    params = {"ps": limit, "pn": 1}
    data = safe_get(url, params)
    
    videos = []
    if data and data.get("code") == 0:
        items = data.get("data", {}).get("list", [])
        for item in items:
            aid = item.get("aid")
            bvid = item.get("bvid")
            title = item.get("title", "")
            owner = item.get("owner", {}).get("name", "")
            if aid:
                videos.append({
                    "aid": str(aid),
                    "bvid": bvid,
                    "title": title,
                    "url": f"https://www.bilibili.com/video/{bvid}",
                    "author": owner
                })
    log(f"成功获取 B站热门视频 {len(videos)} 个")
    return videos

def fetch_bilibili_comments(aid: str, max_count: int = 30) -> List[Dict]:
    """获取评论（sort=2 按热度，同时也可以抓取更多）"""
    url = "https://api.bilibili.com/x/v2/reply"
    params = {"type": 1, "oid": aid, "sort": 2, "ps": max_count}
    data = safe_get(url, params)
    
    comments = []
    if data and data.get("code") == 0:
        replies = data.get("data", {}).get("replies") or []
        for r in replies:
            try:
                content = r.get("content", {}).get("message", "").strip()
                likes = int(r.get("like", 0))
                member = r.get("member", {})
                uname = member.get("uname", "匿名用户")
                ctime = r.get("ctime", int(time.time()))
                
                c_dt = datetime.fromtimestamp(ctime, tz=BEIJING)
                
                if not content:
                    continue
                
                comments.append({
                    "text": content,
                    "likes": likes,
                    "create_time": c_dt,
                    "user_name": uname,
                })
            except Exception:
                continue
    return comments

# ==================== 主流程与持久化 ====================
def main():
    log("=== B站全量评论素材自动化发现任务启动（全收录模式） ===" )
    
    # 1. 加载历史去重库
    existing_hashes = set()
    if MASTER_CSV.exists():
        try:
            df_old = pd.read_csv(MASTER_CSV)
            if "comment_hash" in df_old.columns:
                existing_hashes.update(df_old["comment_hash"].dropna().tolist())
            log(f"已加载历史素材库，去重指纹数: {len(existing_hashes)} 条")
        except Exception as e:
            log(f"加载历史库提示: {e}")

    videos = fetch_bilibili_popular_videos(limit=20)
    new_candidates = []

    for vid in videos:
        aid = vid["aid"]
        video_title = vid["title"]
        video_url = vid["url"]
        
        comments = fetch_bilibili_comments(aid, max_count=30)
        time.sleep(0.5)

        for c in comments:
            c_text = c["text"]
            c_likes = c["likes"]
            c_time = c["create_time"]
            
            if not filter_comment(c_text, c_likes):
                continue
                
            h = text_hash(c_text)
            if h in existing_hashes or h in {item["comment_hash"] for item in new_candidates}:
                continue
                
            is_24h = 1 if (NOW - c_time) <= timedelta(hours=24) else 0
            score = score_comment(c_text, c_likes)

            new_candidates.append({
                "comment_hash": h,
                "text": c_text,
                "source_url": video_url,
                "source_title": video_title,
                "create_time": c_time.isoformat(),
                "is_recent_24h": is_24h,
                "likes": c_likes,
                "score": score,
                "user_name": c["user_name"],
                "group_id": aid,
                "topic": "B站全量精选",
                "status": "unreviewed",
                "collected_at": NOW.isoformat(),
            })

        if len(new_candidates) >= TARGET_CANDIDATES:
            log(f"已收集到充足候选评论 ({len(new_candidates)} 条)，结束本轮抓取")
            break

    # 2. 持久化合并写入主库
    total_24h = sum(1 for x in new_candidates if x["is_recent_24h"] == 1)
    
    if new_candidates:
        df_new = pd.DataFrame(new_candidates)
        if MASTER_CSV.exists():
            df_master = pd.read_csv(MASTER_CSV)
            df_master = pd.concat([df_master, df_new], ignore_index=True)
        else:
            df_master = df_new

        df_master = df_master.sort_values(by="score", ascending=False)
        df_master.to_csv(MASTER_CSV, index=False, encoding="utf-8-sig")
        log(f"持久化成功！本次无条件收录新评论 {len(new_candidates)} 条 (24h内: {total_24h})。总库累计: {len(df_master)} 条。")
    else:
        log("本次运行未获取到有效评论。")

    # 3. 记录运行统计
    metrics = {
        "last_run_time": NOW.isoformat(),
        "platform": "bilibili_all",
        "videos_scanned": len(videos),
        "new_candidates_added": len(new_candidates),
        "total_master_pool": len(pd.read_csv(MASTER_CSV)) if MASTER_CSV.exists() else 0,
        "status": "success"
    }
    with open(METRICS_JSON, "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)

    log("=== B站自动化发现任务圆满结束 ===")

if __name__ == "__main__":
    main()
