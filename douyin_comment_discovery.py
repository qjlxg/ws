# -*- coding: utf-8 -*-
"""
抖音评论采集链路严谨版 MVP 验证探针脚本 (douyin_mvp_probe.py)
- 修复：导入 Optional、修复异常赋值语法错误。
- 强化：保留链接顺序、保存调试截图与 HTML 证据、严格区分原始节点与核验样本。
"""

import os
import re
import time
from pathlib import Path
from typing import Optional, List, Dict, Any
from playwright.sync_api import sync_playwright

KEYWORD = "县城生活"
SEARCH_URL = f"https://www.douyin.com/search/{KEYWORD}?type=general"
DEBUG_DIR = Path("debug_evidence")
DEBUG_DIR.mkdir(exist_ok=True)

def log(msg: str):
    print(f"[MVP-PROBE] {msg}")

def parse_likes(text: str) -> Optional[int]:
    """严格解析点赞数，失败返回 None"""
    if not text:
        return None
    text = text.strip()
    try:
        if "w" in text.lower() or "万" in text:
            num = float(re.sub(r"[^\d.]", "", text))
            return int(num * 10000)
        cleaned = re.sub(r"\D", "", text)
        return int(cleaned) if cleaned else None
    except Exception:
        return None

def run_probe():
    log(f"=== 开始抖音采集链路严谨版 MVP 验证 ===")
    
    diagnostics: Dict[str, Any] = {
        "search_page_status": "PENDING",
        "video_links_found": 0,
        "final_video_url": None,
        "navigated_to_video": False,
        "comment_nodes_raw": 0,
        "verified_comments_extracted": 0,
        "failure_reason": None
    }

    with sync_playwright() as p:
        # 本地调试建议设为 headless=False 观察真实浏览器行为
        browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        page = context.new_page()

        try:
            # 1. 访问搜索页并保存证据
            log("1. 正在加载抖音搜索结果页...")
            page.goto(SEARCH_URL, timeout=30000)
            page.wait_for_timeout(5000)
            
            # 保存搜索页证据用于人工核验
            page.screenshot(path=str(DEBUG_DIR / "search_page.png"), full_page=False)
            with open(DEBUG_DIR / "search_page.html", "w", encoding="utf-8") as f:
                f.write(page.content())
            diagnostics["search_page_status"] = "LOADED"

            # 2. 提取真实视频链接（保持原始顺序并去重）
            log("2. 正在提取搜索结果中的真实视频链接...")
            raw_links = page.eval_on_selector_all("a[href*='/video/']", "elements => elements.map(e => e.href)")
            
            seen = set()
            video_urls = []
            for l in raw_links:
                if "/video/" in l and l not in seen:
                    seen.add(l)
                    video_urls.append(l)

            diagnostics["video_links_found"] = len(video_urls)
            log(f"按原始顺序捕获到唯一候选视频链接数: {len(video_urls)}")

            if not video_urls:
                diagnostics["failure_reason"] = "未能从搜索页捕获到任何 /video/ 链接，可能触发反爬或页面DOM变更。请检查 debug_evidence/search_page.html。"
                log(f"【验证中断】 {diagnostics['failure_reason']}")
                browser.close()
                print_diagnostics(diagnostics)
                return

            # 取第一个视频进行穿透测试
            target_video_url = video_urls[0]
            diagnostics["final_video_url"] = target_video_url
            log(f"3. 尝试进入第一个真实视频详情页: {target_video_url}")
            
            page.goto(target_video_url, timeout=30000)
            page.wait_for_timeout(6000)
            
            # 保存视频页证据
            page.screenshot(path=str(DEBUG_DIR / "video_detail.png"), full_page=False)
            with open(DEBUG_DIR / "video_detail.html", "w", encoding="utf-8") as f:
                f.write(page.content())
            diagnostics["navigated_to_video"] = True

            # 4. 在详情页抓取评论节点（区分原始疑似节点与核验样本）
            log("4. 正在扫描视频详情页的评论区...")
            comment_elements = page.query_selector_all("[class*='comment'], [data-e2e*='comment']")
            diagnostics["comment_nodes_raw"] = len(comment_elements)
            log(f"页面捕获到疑似评论相关DOM节点数: {len(comment_elements)}")

            verified_samples = []
            for el in comment_elements:
                try:
                    text = el.inner_text().strip()
                    # 基础过滤：过短或不包含换行（通常非结构化评论卡片）的排除
                    if len(text) < 12 or "\n" not in text:
                        continue
                    
                    # 严格要求：不使用默认值。若拿不到点赞或时间，显式设为 None
                    verified_samples.append({
                        "source_url": target_video_url,
                        "text": text[:200],  # 截取前200字
                        "likes": None,       // 待后续选择器精准匹配，绝不赋假值 5 或 10
                        "create_time": None, // 待后续选择器精准匹配
                        "extracted_at": time.strftime('%Y-%m-%d %H:%M:%S')
                    })
                    if len(verified_samples) >= 5: # 仅取前5条做验证
                        break
                except Exception:
                    continue

            diagnostics["verified_comments_extracted"] = len(verified_samples)

            if verified_samples:
                log(f"【阶段性验证成功】成功提取到通过基础结构核验的原始评论块 {len(verified_samples)} 条！")
                log(f"提示：页面截图与 HTML 已保存在 {DEBUG_DIR}/ 目录，请人工打开检查其是否真正属于评论区。")
            else:
                diagnostics["failure_reason"] = "成功进入视频详情页，但未能在当前选择器下提取到符合长度和结构的评论块。"
                log(f"【验证受阻】 {diagnostics['failure_reason']}")

        except Exception as e:
            diagnostics["failure_reason"] = str(e)
            log(f"【验证异常】 发生未捕获异常: {e}")

        browser.close()
        print_diagnostics(diagnostics)

def print_diagnostics(d: dict):
    print("\n" + "="*50)
    print(" 抖音采集链路严谨版 MVP 验证诊断报告 ")
    print("="*50)
    for k, v in d.items():
        print(f" - {k}: {v}")
    print("="*50 + "\n")

if __name__ == "__main__":
    run_probe()
