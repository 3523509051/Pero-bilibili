"""把宿主通用 `sdk.network.read` 适配成业务包所需的 fetcher 协议。

本模块不导入 Pero：reader 是任何符合 `async (url, *, max_bytes, timeout) -> {"bytes": bytes}`
的可调用对象。业务模块仍可独立使用自己的 `HttpFetcher`。
"""

from __future__ import annotations

import re
import asyncio
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping
from urllib.parse import urlsplit

Reader = Callable[..., Awaitable[Mapping[str, Any]]]


class NetworkHostDenied(ValueError):
    """目标服务不在插件已声明/管理员批准的域名集合内。"""


class ShortLinkUnsupported(ValueError):
    """短链接无法在批准范围及跳转次数预算内解析。"""


def normalize_url(url: str) -> str:
    text = str(url or "").strip()
    if text.startswith("//"):
        return "https:" + text
    return text


class SDKNetworkFetcher:
    _locks: dict[str, asyncio.Lock] = {}

    def __init__(self, reader: Reader, hosts: Iterable[str], *,
                 cache_dir: Path | None = None, cache_ttl_seconds: float = 30 * 86400,
                 refresh: bool = False) -> None:
        self._reader = reader
        self._hosts = frozenset(str(host).lower() for host in hosts)
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._cache_ttl_seconds = max(0.0, float(cache_ttl_seconds))
        self._refresh = bool(refresh)

    def _cache_path(self, target: str) -> Path | None:
        if self._cache_dir is None:
            return None
        key = hashlib.sha256(target.encode("utf-8")).hexdigest()
        return self._cache_dir / "source" / (key + ".bin")

    async def _cached_read(self, target: str, *, max_bytes: int, timeout: float,
                           redirect_metadata: bool = False) -> Mapping[str, Any]:
        path = self._cache_path(target)
        if path is None or self._cache_ttl_seconds <= 0:
            return await self._reader(target, max_bytes=max_bytes, timeout=timeout,
                                      **({"redirect_metadata": True} if redirect_metadata else {}))
        key = str(path)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            meta_path = path.with_suffix(".json")
            if not self._refresh:
                try:
                    meta = await asyncio.to_thread(lambda: json.loads(meta_path.read_text(encoding="utf-8")))
                    data = await asyncio.to_thread(path.read_bytes)
                    valid = (time.time() - float(meta.get("stored_at", 0)) <= self._cache_ttl_seconds
                             and hashlib.sha256(data).hexdigest() == meta.get("sha256"))
                    if valid:
                        result = {"bytes": data}
                        if redirect_metadata:
                            result.update({"status_code": meta.get("status_code"),
                                           "location": meta.get("location")})
                        return result
                except (OSError, ValueError, TypeError):
                    pass
            response = await self._reader(target, max_bytes=max_bytes, timeout=timeout,
                                          **({"redirect_metadata": True} if redirect_metadata else {}))
            body = response.get("bytes") if isinstance(response, Mapping) else None
            if isinstance(body, bytes):
                def write() -> None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    descriptor, temporary = tempfile.mkstemp(prefix=path.name + "-", dir=path.parent)
                    try:
                        with os.fdopen(descriptor, "wb") as stream:
                            stream.write(body)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temporary, path)
                        record = {"stored_at": time.time(), "sha256": hashlib.sha256(body).hexdigest()}
                        if redirect_metadata:
                            record.update({"status_code": response.get("status_code"),
                                           "location": response.get("location")})
                        meta_tmp = meta_path.with_suffix(".json.tmp")
                        meta_tmp.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
                        os.replace(meta_tmp, meta_path)
                    finally:
                        if os.path.exists(temporary):
                            os.unlink(temporary)
                await asyncio.to_thread(write)
            return response

    async def _get(self, url: str, *, max_bytes: int = 4 * 1024 * 1024,
                   timeout: float = 15.0) -> Mapping[str, Any]:
        target = normalize_url(url)
        parts = urlsplit(target)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not host or host not in self._hosts:
            raise NetworkHostDenied(host or "unknown")
        response = await self._cached_read(target, max_bytes=max_bytes, timeout=timeout)
        if not isinstance(response, Mapping) or not isinstance(response.get("bytes"), bytes):
            raise ValueError("invalid_network_response")
        return response

    async def _json(self, url: str, *, max_bytes: int = 2 * 1024 * 1024) -> Mapping[str, Any]:
        response = await self._get(url, max_bytes=max_bytes)
        try:
            data = response["bytes"].decode("utf-8")
            parsed = __import__("json").loads(data)
        except (UnicodeDecodeError, ValueError, KeyError):
            raise ValueError("invalid_json_response") from None
        if not isinstance(parsed, Mapping):
            raise ValueError("invalid_json_response")
        return parsed

    async def view(self, bvid: str) -> Mapping[str, Any]:
        payload = await self._json("https://api.bilibili.com/x/web-interface/view?bvid=" + bvid)
        if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            raise ValueError("view_unavailable")
        return payload["data"]

    async def storyboard_snapshot(self, bvid: str, cid: int) -> Mapping[str, Any]:
        payload = await self._json(
            f"https://api.bilibili.com/x/player/videoshot?bvid={bvid}&cid={cid}&index=1")
        data = payload.get("data")
        if payload.get("code") != 0 or not isinstance(data, dict):
            raise ValueError("storyboard_unavailable")
        return data

    async def storyboard_image(self, url: str) -> bytes:
        return bytes((await self._get(url, max_bytes=6 * 1024 * 1024))["bytes"])

    async def subtitles(self, bvid: str, cid: int, language: str = "auto") -> list | None:
        try:
            payload = await self._json(
                f"https://api.bilibili.com/x/player/v2?bvid={bvid}&cid={cid}")
            items = ((payload.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
            if not items:
                return None
            chosen = items[0]
            if language != "auto":
                chosen = next((item for item in items if item.get("lan") == language), chosen)
            url = chosen.get("subtitle_url")
            if not isinstance(url, str):
                return None
            body = await self._json(normalize_url(url), max_bytes=2 * 1024 * 1024)
            cues = body.get("body") or []
            return [{"start_seconds": float(cue.get("from", 0)),
                     "end_seconds": float(cue.get("to", 0)),
                     "text": str(cue.get("content") or "")[:2000]}
                    for cue in cues if isinstance(cue, Mapping)]
        except (ValueError, TypeError, KeyError, NetworkHostDenied):
            return None

    async def resolve_short(self, url: str) -> str:
        """最多 3 次显式跳转；只接受已声明且已批准的 HTTPS 目标。"""

        current = normalize_url(url)
        for _ in range(3):
            parts = urlsplit(current)
            if parts.scheme != "https" or (parts.hostname or "").lower() not in self._hosts:
                raise NetworkHostDenied(parts.hostname or "unknown")
            response = await self._cached_read(current, max_bytes=4096, timeout=15,
                                               redirect_metadata=True)
            if not isinstance(response, Mapping):
                raise ValueError("invalid_network_response")
            status = int(response.get("status_code") or 0)
            location = response.get("location")
            if not (300 <= status < 400) or not isinstance(location, str) or not location:
                raise ShortLinkUnsupported("短链没有返回可用的跳转地址")
            current = normalize_url(location)
            target = urlsplit(current)
            if target.scheme != "https" or (target.hostname or "").lower() not in self._hosts:
                raise NetworkHostDenied(target.hostname or "unknown")
            if re.search(r"BV[0-9A-Za-z]{10}", current):
                return current
            if (target.hostname or "").lower() != "b23.tv":
                raise ShortLinkUnsupported("跳转目标不是受支持的视频链接")
        raise ShortLinkUnsupported("短链跳转超过 3 次，请提供完整 BV 链接")
