# -*- coding: utf-8 -*-
"""
B站全量评论素材自动发现脚本（修复 ps out of bounds 限制版）
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

BEIJING = timezone(timedelta(hours=8))
NOW = datetime.now(BEIJING)
OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)
MASTER_CSV = OUTPUT_DIR / "master_comment_pool.csv"
METRICS_JSON = OUTPUT_DIR / "run_metrics.json"

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

SESSION = requests.Session()
SESSION.headers.update(BASE_HEADERS)

MIN_TEXT_LEN = 2      
TARGET_CANDIDATES = 100 

def log(msg: str):
    print(f"[{datetime.now(BEIJING).strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def text_hash(text: str) -> str:
    return hashlib.md5(re.sub(r"\s+", "", text).encode("utf-8")).hexdigest()

def score_comment(text: str, likes: int) -> float:
    return float(likes) + len(text) * 0.1

def filter_comment(text: str, likes: int) -> bool:
    if not text or len(text.strip()) < MIN_TEXT_LEN:
        return False
    return True

def fetch_bilibili_popular_videos(limit: int = 20) -> List[Dict]:
    url = "https://api.bilibili.com/x/web-interface/popular"
    headers = {**BASE_HEADERS, "Referer": "https://www.bilibili.com/"}
    try:
        r = SESSION.get(url, params={"ps": limit, "pn": 1}, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            videos = []
            if data.get("code") == 0:
                for item in data.get("data", {}).get("list", []):
                    aid = item.get("aid")
                    bvid = item.get("bvid")
                    title = item.get("title", "")
                    if aid and bvid:
                        videos.append({
                            "aid": str(aid),
                            "bvid": bvid,
                            "title": title,
                            "url": f"https://www.bilibili.com/video/{bvid}"
                        })
            log(f"成功获取 B站热门视频 {len(videos)} 个")
            return videos
    except Exception as e:
        log(f"获取视频列表异常: {e}")
    return []

def fetch_bilibili_comments(aid: str, bvid: str, max_count: int = 20) -> List[Dict]:
    """将 max_count / ps 严格限制在 20 以内，防止触发 out of bounds 错误"""
    url = "https://api.bilibili.com/x/v2/reply"
    headers = {
        **BASE_HEADERS,
        "Referer": f"https://www.bilibili.com/video/{bvid}"
    }
    params = {"type": 1, "oid": aid, "sort": 2, "ps": max_count}
    
    try:
        r = SESSION.get(url, params=params, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            code = data.get("code")
            if code != 0:
                log(f"视频 [aid: {aid}] 评论接口返回非零状态码: code={code}, message={data.get('message')}")
                return []
            
            replies = data.get("data", {}).get("replies") or []
            comments = []
            for r_item in replies:
                try:
                    content = r_item.get("content", {}).get("message", "").strip()
                    likes = int(r_item.get("like", 0))
                    uname = r_item.get("member", {}).get("uname", "匿名用户")
                    ctime = r_item.get("ctime", int(time.time()))
                    c_dt = datetime.fromtimestamp(ctime, tz=BEIJING)
                    
                    if content:
                        comments.append({
                            "text": content,
                            "likes": likes,
                            "create_time": c_dt,
                            "user_name": uname,
                        })
                except Exception:
                    continue
            return comments
        else:
            log(f"评论请求 HTTP 状态码异常: {r.status_code}")
    except Exception as e:
        log(f"获取评论异常: {e}")
    return []

def main():
    log("=== B站全量评论素材自动化发现任务启动（ps 修正版） ===")
    
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
        bvid = vid["bvid"]
        video_title = vid["title"]
        video_url = vid["url"]
        
        # 传入 max_count=20
        comments = fetch_bilibili_comments(aid, bvid, max_count=20)
        time.sleep(0.8)

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
