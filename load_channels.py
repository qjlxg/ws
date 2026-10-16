import csv
import re
from pathlib import Path
from urllib.parse import urlparse, unquote

ALLOWED_TG_DOMAINS = {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}
DISABLED_VALUES = {"0", "false", "no", "disabled", "off"}
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{2,32}$")


def _channel_name(raw_input):
    """Return a normalized public Telegram channel username, or None for invalid input."""
    raw = (raw_input or "").strip()
    if not raw:
        return None

    # Plain username, optionally prefixed with @.
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw) and not raw.lower().startswith(("t.me/", "www.t.me/", "telegram.me/", "www.telegram.me/")):
        name = raw.lstrip("@").strip().lower()
        return name if USERNAME_RE.fullmatch(name) else None

    target = raw if re.match(r"^https?://", raw, re.I) else "https://" + raw
    parsed = urlparse(target)
    if parsed.scheme.lower() not in {"http", "https"} or parsed.hostname is None:
        return None
    if parsed.hostname.lower() not in ALLOWED_TG_DOMAINS or parsed.username or parsed.password or parsed.port:
        return None

    parts = [unquote(p) for p in parsed.path.split("/") if p]
    if not parts:
        return None
    # Public preview URL: /s/channel. Direct channel URL: /channel.
    if parts[0].lower() == "s":
        if len(parts) < 2:
            return None
        name = parts[1]
    else:
        # A post link /channel/123 is acceptable; use the first path segment.
        name = parts[0]

    name = name.lstrip("@").lower()
    return name if USERNAME_RE.fullmatch(name) else None


def load_and_clean_channels(csv_path):
    channels = []
    seen = set()
    path = Path(csv_path)
    if not path.is_file():
        print(f"[!] 错误：找不到频道名单文件：{csv_path}")
        return channels

    try:
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            fields = [x.strip().lower() for x in (reader.fieldnames or [])]
            if "channel" not in fields:
                print("[!] 错误：CSV 文件缺少必要的 channel 表头")
                return channels
            # Normalize header names, including whitespace/case.
            reader.fieldnames = fields
            invalid = 0
            disabled = 0
            duplicates = 0
            for row in reader:
                if not row:
                    continue
                row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                enabled = row.get("enabled", "1").lower()
                if enabled in DISABLED_VALUES:
                    disabled += 1
                    continue
                name = _channel_name(row.get("channel", ""))
                if not name:
                    invalid += 1
                    print(f"[!] 跳过无效频道输入：{row.get('channel', '')!r}")
                    continue
                if name in seen:
                    duplicates += 1
                    continue
                seen.add(name)
                channels.append({
                    "channel": name,
                    "category": row.get("category", "").strip() or "general",
                    "language": row.get("language", "").strip().lower() or "en",
                })
        print(f"[+] 名单清洗完成：有效 {len(channels)}，重复 {duplicates}，禁用 {disabled}，无效 {invalid}")
    except (OSError, csv.Error) as e:
        print(f"[!] 读取 CSV 文件异常：{type(e).__name__}: {e}")
    return channels
