# -*- coding: utf-8 -*-
"""
B站全量评论素材自动发现脚本（多维度扩展精简版）
"""

import os
import json
import time
import random
import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional
import requests
import pandas as pd

# ==================== 核心配置参数（统一集中在头部） ====================
MIN_TEXT_LEN = 3          # 最小文本长度（字符数少于此值的评论将被过滤）
TARGET_CANDIDATES = 1500  # 目标候选数/单次上限（累计收集到此数量的新评论后主动结束）
MAX_PAGES = 10            # 单个视频最大翻页页数（每页20条，10页即最多抓取200条评论）
# ======================================================================

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

def fetch_bilibili_videos_multi_source() -> List[Dict]:
    """多维度获取视频源：全站热门 + 各大分区排行榜"""
    all_videos = {}
    
    # 1. 获取全站热门
    popular_url = "https://api.bilibili.com/x/web-interface/popular"
    headers = {**BASE_HEADERS, "Referer": "https://www.bilibili.com/"}
    try:
        r = SESSION.get(popular_url, params={"ps": 50, "pn": 1}, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get("code") == 0:
                for item in data.get("data", {}).get("list", []):
                    aid = item.get("aid")
                    bvid = item.get("bvid")
                    title = item.get("title", "")
                    if aid and bvid:
                        all_videos[str(aid)] = {
                            "aid": str(aid),
                            "bvid": bvid,
                            "title": title,
                            "url": f"https://www.bilibili.com/video/{bvid}",
                            "source_type": "全站热门"
                        }
        log(f"成功获取 B站全站热门视频 {len(all_videos)} 个")
    except Exception as e:
        log(f"获取全站热门视频异常: {e}")

    # 2. 获取各大分区排行榜 (rid: 1-动画, 36-科技, 4-游戏, 160-生活, 5-娱乐)
    partition_rids = [1, 36, 4, 160, 5]
    ranking_url = "https://api.bilibili.com/x/web-interface/ranking/v2"
    
    for rid in partition_rids:
        try:
            r = SESSION.get(ranking_url, params={"rid": rid}, headers=headers, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if data.get("code") == 0:
                    list_items = data.get("data", {}).get("list", [])
                    count = 0
                    for item in list_items:
                        aid = item.get("aid")
                        bvid = item.get("bvid")
                        title = item.get("title", "")
                        if aid and bvid and str(aid) not in all_videos:
                            all_videos[str(aid)] = {
                                "aid": str(aid),
                                "bvid": bvid,
                                "title": title,
                                "url": f"https://www.bilibili.com/video/{bvid}",
                                "source_type": f"分区排行_{rid}"
                            }
                            count += 1
                    log(f"成功加载分区 {rid} 视频 {count} 个")
            time.sleep(random.uniform(1.0, 2.5))
        except Exception as e:
            log(f"获取分区 {rid} 异常: {e}")

    video_list = list(all_videos.values())
    log(f"多维度视频源合并去重后，总计获得有效视频: {len(video_list)} 个")
    return video_list

def fetch_bilibili_comments_paginated(aid: str, bvid: str, max_pages: int = MAX_PAGES) -> List[Dict]:
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
            # 引入随机休眠，防止请求过快被风控
            time.sleep(random.uniform(2.0, 4.5))
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

    videos = fetch_bilibili_videos_multi_source()
    new_candidates = []

    for vid in videos:
        aid = vid["aid"]
        bvid = vid["bvid"]
        video_title = vid["title"]
        video_url = vid["url"]
        source_type = vid["source_type"]
        
        comments = fetch_bilibili_comments_paginated(aid, bvid, max_pages=MAX_PAGES)
        time.sleep(random.uniform(0.5, 1.2))

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
                "分类/话题": f"B站多维精选_{source_type}",
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
