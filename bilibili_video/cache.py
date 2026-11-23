"""按视频内容身份缓存：TTL、哈希校验、并发同 key 单次构建、原子写与字幕原件。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Mapping

SAMPLING_VERSION = "4"
SHEET_VERSION = "2"

#: 业务时间统一按北京时间（+08:00）对外呈现；TTL 仍用真实时刻比较。
BEIJING = timezone(timedelta(hours=8))

MISS_ABSENT = "absent"
MISS_EXPIRED = "expired"
MISS_CORRUPT = "corrupt"
MISS_SHEET_MISSING = "sheet_missing"
MISS_HASH_MISMATCH = "hash_mismatch"
MISS_SUBTITLE_MISSING = "subtitle_missing"
MISS_SUBTITLE_MISMATCH = "subtitle_mismatch"

_LOCKS: dict[str, asyncio.Lock] = {}
_LOCK_POLL_SECONDS = 0.05
_LOCK_WAIT_SECONDS = 120.0
_LOCK_STALE_SECONDS = 300.0


def _lock_path(cache_dir: Path, key: str) -> Path:
    return Path(cache_dir) / "locks" / (key + ".lock")


async def _acquire_file_lock(cache_dir: Path | None, key: str) -> Path | None:
    """跨进程构建锁：worker 隔离下同 key 只允许一个进程构建。

    先占位（O_EXCL）再写 pid；等待超时或锁文件过期都不阻断业务——
    写盘本身仍是原子的，宁可重复构建也不能卡死。
    """

    if cache_dir is None:
        return None
    path = _lock_path(Path(cache_dir), key)
    try:
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
    except OSError:
        return None
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    while True:
        try:
            descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                age = 0.0
            if age > _LOCK_STALE_SECONDS:
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() > deadline:
                return None
            await asyncio.sleep(_LOCK_POLL_SECONDS)
            continue
        except OSError:
            return None
        try:
            os.write(descriptor, str(os.getpid()).encode("ascii"))
        finally:
            os.close(descriptor)
        return path


def _release_file_lock(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


@asynccontextmanager
async def key_lock(key: str, *, cache_dir: Path | None = None) -> AsyncIterator[None]:
    """同一缓存 key 只构建一个版本：进程内串行 + 跨进程占位锁。"""

    lock = _LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        handle = await _acquire_file_lock(cache_dir, key)
        try:
            yield
        finally:
            _release_file_lock(handle)


def now() -> str:
    """插件业务时间：北京时间（+08:00），不依赖宿主 timebase。"""

    return datetime.now(BEIJING).isoformat(timespec="seconds")


def expires_at(created: Any, ttl_hours: float) -> str:
    moment = _parse(created)
    if moment is None:
        return ""
    return (moment + timedelta(hours=float(ttl_hours))).isoformat(timespec="seconds")


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


async def load_detail(cache_dir: Path, key: str, *,
                      max_age_seconds: float = 86400) -> tuple[Mapping[str, Any] | None, str]:
    """读取派生缓存；未命中返回真实原因（缺失/过期/损坏/缺图/哈希不符）。"""

    index = _index_path(cache_dir, key)

    def work() -> tuple[Mapping[str, Any] | None, str]:
        if not index.is_file():
            return None, MISS_ABSENT
        try:
            record = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None, MISS_CORRUPT
        if not isinstance(record, dict):
            return None, MISS_CORRUPT
        created = _parse(record.get("created_at"))
        if created is None:
            return None, MISS_CORRUPT
        if (datetime.now(timezone.utc) - created).total_seconds() > max_age_seconds:
            return None, MISS_EXPIRED
        expected = record.get("sha256")
        found_sheet = False
        matched = False
        for suffix in (".jpg", ".png"):
            sheet = _sheet_path(cache_dir, key, suffix)
            if not sheet.is_file():
                continue
            found_sheet = True
            try:
                if hashlib.sha256(sheet.read_bytes()).hexdigest() == expected:
                    matched = True
                    break
            except OSError:
                return None, MISS_CORRUPT
        if not found_sheet:
            return None, MISS_SHEET_MISSING
        if not matched:
            return None, MISS_HASH_MISMATCH
        # 索引与字幕必须自洽：不一致就判未命中重建，不把损坏字幕当正常命中返回。
        subtitle_rel = str(record.get("subtitle_path") or "")
        if subtitle_rel:
            subtitle = _subtitle_path(cache_dir, key)
            if not subtitle.is_file():
                return None, MISS_SUBTITLE_MISSING
            try:
                if hashlib.sha256(subtitle.read_bytes()).hexdigest() != record.get("subtitle_sha256"):
                    return None, MISS_SUBTITLE_MISMATCH
            except OSError:
                return None, MISS_SUBTITLE_MISSING
        return record, "ok"

    return await asyncio.to_thread(work)


async def load(cache_dir: Path, key: str, *,
               max_age_seconds: float = 86400) -> Mapping[str, Any] | None:
    record, _reason = await load_detail(cache_dir, key, max_age_seconds=max_age_seconds)
    return record


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


def subtitle_meta(cache_dir: Path, key: str, cues: Iterable[Mapping[str, Any]]) -> dict:
    """只计算字幕引用与哈希，不落盘——由 save() 与索引一起提交。"""

    items = [dict(item) for item in cues]
    payload = json.dumps({"cues": items, "sha256": hashlib.sha256(
        json.dumps(items, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()},
        ensure_ascii=False).encode("utf-8")
    return {"subtitle_path": f"cache/{_subtitle_path(Path(cache_dir), key).name}",
            "subtitle_sha256": hashlib.sha256(payload).hexdigest()}


async def save_subtitles(cache_dir: Path, key: str, cues: Iterable[Mapping[str, Any]]) -> dict:
    """兼容旧调用：单独落盘字幕（业务主路径已改为与索引一起提交）。"""

    meta = subtitle_meta(cache_dir, key, cues)
    items = [dict(item) for item in cues]
    payload = json.dumps({"cues": items, "sha256": hashlib.sha256(
        json.dumps(items, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()},
        ensure_ascii=False).encode("utf-8")
    await asyncio.to_thread(_atomic_write, _subtitle_path(cache_dir, key), payload)
    return meta


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
        # 提交策略：先暂存旧产物到 .staging，再写新 sheet/字幕，最后写索引（提交点）。
        # 任一步普通异常都把旧产物原样放回，绝不留下“旧索引 + 新字幕”的混合版本。
        # 边界：这是普通异常的补偿回滚，不是断电/kill 下的多文件事务。
        index_target = _index_path(root, key)
        targets: list[tuple[Path, bytes]] = [(_sheet_path(root, key, suffix), sheet_bytes)]
        if subtitle_payload is not None:
            targets.append((_subtitle_path(root, key), subtitle_payload))
        staging = root / ".staging"
        backups: list[tuple[Path, Path]] = []
        created: list[Path] = []
        try:
            for target, _ in targets:
                if target.exists():
                    staging.mkdir(parents=True, exist_ok=True)
                    backup = staging / f"{target.name}.{uuid.uuid4().hex}.bak"
                    os.replace(target, backup)
                    backups.append((backup, target))
            for target, payload in targets:
                if not target.exists():
                    created.append(target)
                _atomic_write(target, payload)
            _atomic_write(index_target,
                          json.dumps(final, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        except Exception:
            for target in created:
                try:
                    target.unlink()
                except OSError:
                    pass
            for backup, target in backups:
                try:
                    os.replace(backup, target)
                except OSError:
                    pass
            raise
        finally:
            for backup, _ in backups:
                try:
                    backup.unlink()
                except OSError:
                    pass
            try:
                staging.rmdir()
            except OSError:
                pass

    await asyncio.to_thread(work)
    result = {"key": key, "sha256": sheet_sha, "path": final["sheet_path"],
              "created_at": str(final.get("created_at") or "")}
    for field in ("subtitle_path", "subtitle_sha256"):
        if field in final:
            result[field] = final[field]
    return result
