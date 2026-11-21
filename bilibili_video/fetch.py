"""只读网络访问：固定主机、无登录 Cookie、无重定向跟随、有界响应。"""

from __future__ import annotations

from typing import Any, Mapping, Optional
from urllib.parse import urlparse

import httpx

_ALLOWED_HOSTS = {"api.bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv"}
_MAX_BYTES = 8 * 1024 * 1024


def _normalize(url: str) -> str:
    text = str(url or "").strip()
    return "https:" + text if text.startswith("//") else text


def _check(url: str) -> None:
    parsed = urlparse(_normalize(url))
    host = (parsed.hostname or "").lower()
    allowed = (host in _ALLOWED_HOSTS or host.endswith(".bilibili.com")
               or host.endswith(".bilivideo.com") or host.endswith(".hdslb.com"))
    if parsed.scheme != "https" or not allowed:
        raise ValueError("unsupported_host")


class HttpFetcher:
    """默认只读 fetcher；调用方可用同接口 fixture 完全离线替换。"""

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout

    async def _get(self, url: str) -> httpx.Response:
        url = _normalize(url)
        _check(url)
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False,
                                     trust_env=False) as client:
            response = await client.get(url, headers={"User-Agent": "PeroVideo/1.0"})
        if 300 <= response.status_code < 400:
            raise ValueError("redirect_not_allowed")
        if response.status_code != 200 or len(response.content) > _MAX_BYTES:
            raise ValueError("fetch_failed")
        return response

    async def resolve_short(self, url: str) -> str:
        _check(url)
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False,
                                     trust_env=False) as client:
            response = await client.get(url, headers={"User-Agent": "PeroVideo/1.0"})
        location = response.headers.get("location", "")
        if 300 <= response.status_code < 400 and location:
            _check(location)
            return location
        raise ValueError("short_link_unresolved")

    async def view(self, bvid: str) -> Mapping[str, Any]:
        response = await self._get("https://api.bilibili.com/x/web-interface/view?bvid=" + bvid)
        payload = response.json()
        if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            raise ValueError("view_unavailable")
        return payload["data"]

    async def storyboard_snapshot(self, bvid: str, cid: int) -> Mapping[str, Any]:
        """x/player/videoshot：image 列表、index 时间戳、pvdata、块尺寸。"""

        response = await self._get(
            f"https://api.bilibili.com/x/player/videoshot?bvid={bvid}&cid={cid}&index=1")
        payload = response.json()
        data = payload.get("data")
        if payload.get("code") != 0 or not isinstance(data, dict):
            raise ValueError("storyboard_unavailable")
        return data

    async def storyboard_image(self, url: str) -> bytes:
        _check(url)
        return (await self._get(url)).content

    async def subtitles(self, bvid: str, cid: int, language: str = "auto") -> Optional[list]:
        try:
            response = await self._get(
                f"https://api.bilibili.com/x/player/v2?bvid={bvid}&cid={cid}")
            payload = response.json()
            items = ((payload.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
            if not items:
                return None
            chosen = items[0]
            if language != "auto":
                chosen = next((item for item in items if item.get("lan") == language), chosen)
            url = chosen.get("subtitle_url")
            if not isinstance(url, str) or url.startswith("//"):
                url = "https:" + str(url)
            _check(url)
            body = (await self._get(url)).json().get("body") or []
            return [{"start_seconds": float(cue.get("from", 0)),
                     "end_seconds": float(cue.get("to", 0)),
                     "text": str(cue.get("content") or "")[:2000]}
                    for cue in body if isinstance(cue, Mapping)]
        except (ValueError, TypeError, KeyError):
            return None
