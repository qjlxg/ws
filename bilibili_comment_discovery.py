# -*- coding: utf-8 -*-
"""
B站全量评论素材自动发现脚本（精简干净版）
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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
}

SESSION = requests.Session()
SESSION.headers.update(BASE_HEADERS)

MIN_TEXT_LEN = 3   # 最小文本长度  意味着只要评论的文字长度达到 2个字及以上（例如“哈哈”、“路过”、“赞同”等），就会被允许收录进素材库。如果您觉得太短的无意义水评太多，可以把它调大（比如改成 5 或 10）   
TARGET_CANDIDATES = 3000  #目标候选数/单次上限 意味着脚本在本次运行中，只要累计收集到 300 条符合条件的新评论，就会主动停止继续向下翻页和扫描，从而节省时间和接口请求。如果您希望每次跑的时候抓得更多，可以把这个数字调得更大（比如 500 或 1000）

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

def fetch_bilibili_popular_videos(limit: int = 50) -> List[Dict]:
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

def fetch_bilibili_comments_paginated(aid: str, bvid: str, max_pages: int = 5) -> List[Dict]:
    url = "https://api.bilibili.com/x/v2/reply"
    headers = {
        **BASE_HEADERS,
        "Referer": f"https://www.bilibili.com/video/{bvid}"
    }
    
    all_comments = []
    for page in range(1, max_pages + 1):
        params = {
            "type": 1, 
            "oid": aid, 
            "sort": 2, 
            "ps": 20,  
            "pn": page 
        }
        
        try:
            r = SESSION.get(url, params=params, headers=headers, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if data.get("code") != 0:
                    break
                
                replies = data.get("data", {}).get("replies") or []
                if not replies:
                    break
                
                for r_item in replies:
                    try:
                        content = r_item.get("content", {}).get("message", "").strip()
                        likes = int(r_item.get("like", 0))
                        uname = r_item.get("member", {}).get("uname", "匿名用户")
                        ctime = r_item.get("ctime", int(time.time()))
                        c_dt = datetime.fromtimestamp(ctime, tz=BEIJING)
                        
                        if content:
                            all_comments.append({
                                "text": content,
                                "likes": likes,
                                "create_time": c_dt,
                                "user_name": uname,
                            })
                    except Exception:
                        continue
            time.sleep(0.4)
        except Exception:
            break
            
    return all_comments

def main():
    log("=== B站全量评论素材自动化发现任务启动 ===")
    
    existing_hashes = set()
    if MASTER_CSV.exists():
        try:
            df_old = pd.read_csv(MASTER_CSV)
            if "评论指纹" in df_old.columns:
                existing_hashes.update(df_old["评论指纹"].dropna().tolist())
            log(f"已加载历史素材库，去重指纹数: {len(existing_hashes)} 条")
        except Exception as e:
            log(f"加载历史库提示: {e}")

    videos = fetch_bilibili_popular_videos(limit=50)
    new_candidates = []

    for vid in videos:
        aid = vid["aid"]
        bvid = vid["bvid"]
        video_title = vid["title"]
        video_url = vid["url"]
        
        comments = fetch_bilibili_comments_paginated(aid, bvid, max_pages=5)
        time.sleep(0.6)

        for c in comments:
            c_text = c["text"]
            c_likes = c["likes"]
            c_time = c["create_time"]
            
            if not filter_comment(c_text, c_likes):
                continue
                
            h = text_hash(c_text)
            if h in existing_hashes or h in {item["评论指纹"] for item in new_candidates}:
                continue
                
            is_24h = "是" if (NOW - c_time) <= timedelta(hours=24) else "否"
            score = score_comment(c_text, c_likes)

            new_candidates.append({
                "评论内容": c_text,
                "点赞数": c_likes,
                "用户昵称": c["user_name"],
                "视频标题": video_title,
                "来源链接": video_url,
                "发表时间": c_time.strftime('%Y-%m-%d %H:%M:%S'),
                "综合评分": score,
                "24h内": is_24h,
                "视频ID": aid,
                "分类/话题": "B站热门精选",
                "审核状态": "未审核",
                "采集时间": NOW.strftime('%Y-%m-%d %H:%M:%S'),
                "评论指纹": h
            })

        if len(new_candidates) >= TARGET_CANDIDATES:
            log(f"已收集到充足候选评论 ({len(new_candidates)} 条)，结束本轮抓取")
            break

    total_24h = sum(1 for x in new_candidates if x["24h内"] == "是")
    
    if new_candidates:
        df_new = pd.DataFrame(new_candidates)
        if MASTER_CSV.exists():
            df_master = pd.read_csv(MASTER_CSV)
            df_master = pd.concat([df_master, df_new], ignore_index=True)
            if "评论指纹" in df_master.columns:
                df_master = df_master.drop_duplicates(subset=["评论指纹"], keep="first")
        else:
            df_master = df_new

        column_order = [
            "评论内容", "点赞数", "视频标题", "来源链接", 
            "发表时间", "用户昵称", "综合评分", "24h内", 
            "分类/话题", "审核状态", "采集时间", "视频ID", "评论指纹"
        ]
        column_order = [col for col in column_order if col in df_master.columns]
        df_master = df_master[column_order]

        df_master = df_master.sort_values(by="综合评分", ascending=False)
        df_master.to_csv(MASTER_CSV, index=False, encoding="utf-8-sig")
        log(f"持久化成功！本次无条件收录新评论 {len(new_candidates)} 条 (24h内: {total_24h})。总库累计: {len(df_master)} 条。")
    else:
        log("本次运行未获取到有效评论。")

    metrics = {
        "last_run_time": NOW.isoformat(),
        "platform": "bilibili_cn_header",
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
