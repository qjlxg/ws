import asyncio
import csv
import os
from datetime import datetime
import pytz
from telethon import TelegramClient

# ================= 基础配置 =================
# 从环境变量中安全读取 Telegram 凭证
API_ID = os.environ.get("TG_API_ID")
API_HASH = os.environ.get("TG_API_HASH")
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")

# 输入的候选清单文件路径
CANDIDATE_CSV = "telegram_candidates.csv"

# 基础下载保存路径
BASE_DIR = "tg_materials"


def get_beijing_time():
    """获取当前的北京时间"""
    tz = pytz.timezone("Asia/Shanghai")
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")


async def main():
    if not API_ID or not API_HASH:
        print("❌ 错误：未找到 TG_API_ID 或 TG_API_HASH 环境变量！")
        return

    if not os.path.exists(CANDIDATE_CSV):
        print(f"❌ 错误：未找到候选清单文件 {CANDIDATE_CSV}，请先准备好 CSV 文件。")
        return

    # 启动 Telegram 客户端 (优先使用 Bot Token)
    if BOT_TOKEN:
        client = TelegramClient("bot_session", int(API_ID), API_HASH)
        await client.start(bot_token=BOT_TOKEN)
    else:
        client = TelegramClient("user_session", int(API_ID), API_HASH)
        await client.start()

    os.makedirs(BASE_DIR, exist_ok=True)
    success_count = 0
    fail_count = 0

    print(f"[{get_beijing_time()}] 开始读取候选清单并执行下载任务...")

    async with client:
        with open(CANDIDATE_CSV, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                channel = row.get("channel")
                post_id = row.get("post_id") or row.get("message_id")
                category = row.get("category", "other")

                if not channel or not post_id:
                    continue

                int_post_id = int(post_id)

                # 按类别和频道建立存储目录：tg_materials/geography/photos/
                category_dir = os.path.join(BASE_DIR, category)
                photos_dir = os.path.join(category_dir, "photos")
                videos_dir = os.path.join(category_dir, "videos")
                os.makedirs(photos_dir, exist_ok=True)
                os.makedirs(videos_dir, exist_ok=True)

                try:
                    # 通过 Telethon 精准获取指定帖子的消息对象
                    msg = await client.get_messages(channel, ids=int_post_id)

                    if msg and msg.media:
                        # 判断媒体类型
                        save_dir = photos_dir
                        if (
                            hasattr(msg.media, "document")
                            and msg.media.document.mime_type
                            and msg.media.document.mime_type.startswith(
                                "video"
                            )
                        ):
                            save_dir = videos_dir

                        views = row.get("views", "0")
                        filename_prefix = f"{channel}_{int_post_id}_{views}"
                        
                        # 检查是否已经下载过该文件前缀，避免重复下载
                        already_downloaded = any(
                            f.startswith(filename_prefix)
                            for f in os.listdir(save_dir)
                        )

                        if already_downloaded:
                            print(
                                f"  ⏩ [跳过已存在] 频道: @{channel} | ID: {int_post_id}"
                            )
                            continue

                        # 执行下载
                        file_path = await client.download_media(
                            msg, file=os.path.join(save_dir, filename_prefix)
                        )

                        if file_path:
                            print(
                                f"  ✅ [成功下载] 类别: {category} | 路径: {file_path}"
                            )
                            success_count += 1
                        else:
                            print(
                                f"  ⚠️ [下载为空] 频道: @{channel} | ID: {int_post_id}"
                            )
                            fail_count += 1
                    else:
                        print(
                            f"  ⚠️ [无媒体内容] 频道: @{channel} | ID: {int_post_id}"
                        )

                except Exception as e:
                    print(
                        f"  ❌ [下载出错] 频道: @{channel} | ID: {int_post_id} | 错误: {e}"
                    )
                    fail_count += 1

    print(
        f"\n[{get_beijing_time()}] 任务全部结束！成功下载: {success_count} 个，失败/跳过: {fail_count} 个。"
    )


if __name__ == "__main__":
    asyncio.run(main())
