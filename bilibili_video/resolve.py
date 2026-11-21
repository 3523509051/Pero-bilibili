"""只解析明确支持的 B 站视频链接；不搜索、不推荐、不执行外部命令。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

_BV = re.compile(r"^(BV[0-9A-Za-z]{10})$")
_SUPPORTED = {"bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv"}


@dataclass(frozen=True)
class VideoRef:
    bvid: str
    page: int
    short: bool
    raw: str


def resolve_url(url: str) -> VideoRef:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("empty_url")
    raw = url.strip()
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host in _SUPPORTED or host.endswith(".bilibili.com")):
        raise ValueError("unsupported_host")
    path_bv = next((part for part in parsed.path.split("/") if _BV.fullmatch(part)), "")
    bvid = path_bv or (parse_qs(parsed.query).get("bvid") or [""])[0]
    if not _BV.fullmatch(bvid):
        # b23.tv 短链接本身没有 BV；由 fetcher 解析真实链接后再校验。
        if host == "b23.tv" and parsed.path.strip("/"):
            return VideoRef(bvid="", page=1, short=True, raw=raw)
        raise ValueError("bvid_not_found")
    page_raw = (parse_qs(parsed.query).get("p") or ["1"])[0]
    try:
        page = int(page_raw)
    except ValueError:
        raise ValueError("invalid_page") from None
    if not 1 <= page <= 999:
        raise ValueError("invalid_page")
    return VideoRef(bvid=bvid, page=page, short=False, raw=raw)
