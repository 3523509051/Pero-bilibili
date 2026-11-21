"""按视频内容身份缓存：TTL、哈希校验、并发同 key 单次构建、原子写与字幕原件。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Mapping

SAMPLING_VERSION = "3"
SHEET_VERSION = "2"

_LOCKS: dict[str, asyncio.Lock] = {}


@asynccontextmanager
async def key_lock(key: str) -> AsyncIterator[None]:
    """同一缓存 key 在进程内只构建一个版本；失败不覆盖旧索引。"""

    lock = _LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        yield


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def cache_key(bvid: str, cid: int, page: int, frame_count: int,
              subtitle_language: str, credential_domain: str = "public") -> str:
    parts = (bvid, str(cid), str(page), str(frame_count), SAMPLING_VERSION,
             subtitle_language, SHEET_VERSION, credential_domain)
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:32]


def _index_path(cache_dir: Path, key: str) -> Path:
    return Path(cache_dir) / (key + ".json")


def _sheet_suffix(data: bytes) -> str:
    return ".png" if data.startswith(b"\x89PNG\r\n\x1a\n") else ".jpg"


def _sheet_path(cache_dir: Path, key: str, suffix: str = ".jpg") -> Path:
    return Path(cache_dir) / (key + ".sheet" + suffix)


def _subtitle_path(cache_dir: Path, key: str) -> Path:
    return Path(cache_dir) / (key + ".subtitles.json")


async def load(cache_dir: Path, key: str, *, max_age_seconds: float = 86400) -> Mapping[str, Any] | None:
    index = _index_path(cache_dir, key)

    def work():
        if not index.is_file():
            return None
        try:
            record = json.loads(index.read_text(encoding="utf-8"))
            created = _parse(record.get("created_at"))
            if created is None or (datetime.now(timezone.utc) - created).total_seconds() > max_age_seconds:
                return None
            valid = False
            for suffix in (".jpg", ".png"):
                sheet = _sheet_path(cache_dir, key, suffix)
                if sheet.is_file() and hashlib.sha256(sheet.read_bytes()).hexdigest() == record.get("sha256"):
                    valid = True
                    break
            if not valid:
                return None
            subtitle = _subtitle_path(cache_dir, key)
            subtitle_rel = str(record.get("subtitle_path") or "")
            if subtitle.is_file() and subtitle_rel:
                if hashlib.sha256(subtitle.read_bytes()).hexdigest() != record.get("subtitle_sha256"):
                    record["subtitle_integrity"] = "failed"
        except (OSError, ValueError, TypeError):
            return None
        return record

    return await asyncio.to_thread(work)


async def load_subtitles(cache_dir: Path, key: str) -> list | None:
    path = _subtitle_path(cache_dir, key)

    def work():
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return list(data.get("cues") or []) if isinstance(data, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    return await asyncio.to_thread(work)


def _atomic_write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=target.name + "-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


async def save_subtitles(cache_dir: Path, key: str, cues: Iterable[Mapping[str, Any]]) -> dict:
    """完整带时间戳字幕独立落盘；工具结果只引用路径与哈希。"""

    items = [dict(item) for item in cues]
    payload = json.dumps({"cues": items, "sha256": hashlib.sha256(
        json.dumps(items, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()},
        ensure_ascii=False).encode("utf-8")
    await asyncio.to_thread(_atomic_write, _subtitle_path(cache_dir, key), payload)
    return {"subtitle_path": f"cache/{_subtitle_path(cache_dir, key).name}",
            "subtitle_sha256": hashlib.sha256(payload).hexdigest()}


async def save(cache_dir: Path, key: str, record: Mapping[str, Any], sheet_bytes: bytes,
               subtitle_cues: Iterable[Mapping[str, Any]] | None = None) -> dict:
    root = Path(cache_dir)
    sheet_sha = hashlib.sha256(sheet_bytes).hexdigest()
    final = dict(record)
    final["sha256"] = sheet_sha
    final["size_bytes"] = len(sheet_bytes)
    suffix = _sheet_suffix(sheet_bytes)
    final["sheet_path"] = f"cache/{_sheet_path(root, key, suffix).name}"

    subtitle_payload = None
    if subtitle_cues is not None:
        cues = [dict(cue) for cue in subtitle_cues]
        subtitle_payload = json.dumps({"cues": cues, "sha256": hashlib.sha256(
            json.dumps(cues, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()},
            ensure_ascii=False).encode("utf-8")
        final["subtitle_path"] = f"cache/{_subtitle_path(root, key).name}"
        final["subtitle_sha256"] = hashlib.sha256(subtitle_payload).hexdigest()

    def work() -> None:
        _atomic_write(_sheet_path(root, key, suffix), sheet_bytes)
        if subtitle_payload is not None:
            _atomic_write(_subtitle_path(root, key), subtitle_payload)
        _atomic_write(_index_path(root, key),
                      json.dumps(final, ensure_ascii=False, sort_keys=True).encode("utf-8"))

    await asyncio.to_thread(work)
    result = {"key": key, "sha256": sheet_sha, "path": final["sheet_path"],
              "created_at": str(final.get("created_at") or "")}
    for field in ("subtitle_path", "subtitle_sha256"):
        if field in final:
            result[field] = final[field]
    return result
