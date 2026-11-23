"""统一业务入口：真实故事板索引取帧、完整字幕落盘、有界工具结果。"""

from __future__ import annotations

import asyncio
import hashlib
import struct
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Optional

from . import cache as cache_store
from .contact_sheet import TIMESTAMP_SUPPORT, build_sheet, decode_image
from .fetch import HttpFetcher
from .models import VideoOptions, VideoResult
from .resolve import resolve_url


def _snapshot_entries(data: Mapping[str, Any], covered: float) -> list[tuple[float, int]]:
    raws = []
    for index, item in enumerate(data.get("index") or []):
        timestamp = item.get("timestamp") if isinstance(item, Mapping) else item
        try:
            raws.append((float(timestamp), index))
        except (TypeError, ValueError):
            continue
    if not raws:
        return []
    biggest = max(value for value, _ in raws)
    scale = 1000.0 if covered > 0 and biggest > covered * 1.5 else 1.0
    return [(value / scale, index) for value, index in raws]


async def _pvdata_entries(fetcher: Any, data: Mapping[str, Any]) -> list[tuple[float, int]]:
    url = data.get("pvdata")
    if not url:
        return []
    try:
        raw = await fetcher.storyboard_image(str(url))
    except (ValueError, OSError):
        return []
    if not raw or len(raw) % 2 or len(raw) // 2 > 20000:
        return []
    count = len(raw) // 2
    return [(float(seconds), index) for index, seconds in enumerate(struct.unpack(f">{count}H", raw))]


async def _storyboard(fetcher: Any, bvid: str, cid: int, covered: float) -> tuple[dict, list]:
    snapshot: dict = {}
    entries: list[tuple[float, int]] = []
    for attempt in range(2):
        try:
            snapshot = dict(await fetcher.storyboard_snapshot(bvid, cid))
        except (ValueError, TypeError, OSError):
            snapshot = {}
        entries = _snapshot_entries(snapshot, covered)
        if not entries:
            entries = await _pvdata_entries(fetcher, snapshot)
        if entries and snapshot.get("image"):
            break
        if attempt == 0:
            await asyncio.sleep(0.5)
    return snapshot, entries


def _excerpt(cues: Optional[list]) -> dict:
    if not cues:
        return {"status": "unavailable", "language": None, "excerpt": "", "cue_count": 0}
    text = " ".join(str(cue.get("text") or "") for cue in cues[:20]).strip()
    return {"status": "available", "language": "auto", "excerpt": text[:400],
            "cue_count": len(cues), "source": "bilibili_subtitle"}


def _change_score(left: bytes, right: bytes, width: int, height: int) -> float:
    """Cheap normalized RGB difference on a 24x14 sample grid."""
    total = 0
    samples = 0
    for gy in range(14):
        y = min(height - 1, gy * height // 14)
        for gx in range(24):
            x = min(width - 1, gx * width // 24)
            offset = (y * width + x) * 3
            total += abs(left[offset] - right[offset])
            total += abs(left[offset + 1] - right[offset + 1])
            total += abs(left[offset + 2] - right[offset + 2])
            samples += 3
    return total / (samples * 255) if samples else 0.0


def _adaptive_frames(frames: list[dict], covered: float) -> list[dict]:
    """Keep scene changes plus duration anchors, with no fixed frame-count cap."""
    if len(frames) <= 2:
        return frames
    ordered = sorted(frames, key=lambda frame: float(frame.get("actual_seconds") or 0))
    stride = 30.0
    anchors = {0.0, max(0.0, float(covered) - 0.1)}
    cursor = stride
    while cursor < covered:
        anchors.add(cursor)
        cursor += stride
    anchor_tiles = {
        min(ordered, key=lambda frame: abs(float(frame.get("actual_seconds") or 0) - anchor)).get("tile_index")
        for anchor in anchors
    }
    chosen = [ordered[0]]
    previous = ordered[0]
    for frame in ordered[1:-1]:
        gap = float(frame.get("actual_seconds") or 0) - float(previous.get("actual_seconds") or 0)
        score = _change_score(previous["rgb"], frame["rgb"], int(frame["width"]), int(frame["height"]))
        if frame.get("tile_index") in anchor_tiles or gap >= stride or score >= 0.12:
            chosen.append(frame)
            previous = frame
    if ordered[-1] is not chosen[-1]:
        chosen.append(ordered[-1])
    return chosen


async def _uncached(fetcher: Any, options: VideoOptions, cache_dir: Path,
                    ref: Any, view: Mapping[str, Any], page_info: Mapping[str, Any],
                    key: str, video_meta: dict, warnings: list) -> VideoResult:
    cid = int(video_meta["cid"])
    duration = float(video_meta["duration_seconds"])
    covered = float((view.get("pages") or [{}])[0].get("duration") or duration) if ref.page > 1 else duration
    coverage: dict = {"page": ref.page, "seconds": duration, "method": "storyboard"}
    frames: list[dict] = []
    if ref.page > 1:
        warnings.append("故事板只覆盖第 1P，不把第一P画面用于其他分P")
        coverage["method"] = "unavailable_for_page"
        coverage["covered_seconds"] = 0.0
    else:
        snapshot, entries = await _storyboard(fetcher, ref.bvid, cid, covered)
        if not entries or not snapshot.get("image"):
            warnings.append("取不到故事板时间戳或拼图，画面不可用")
            coverage["method"] = "unavailable"
        else:
            entries = sorted(set(entries))
            story_max = max(value for value, _ in entries)
            if story_max > 0:
                covered = min(covered, story_max)
            coverage["covered_seconds"] = round(covered, 1)
            targets = ([covered * (index + 1) / (options.frame_count + 1)
                        for index in range(options.frame_count)] if options.frame_count > 0
                       else [seconds for seconds, _tile in entries])
            # 同一块素材只取一次：先在未用候选里找最近时间点，再按实际时间排序。
            # 时间戳不均匀时也能用满候选，不会把“目标碰撞”误报成素材不足。
            picked: list[tuple[float, float, int]] = []
            remaining = list(entries)
            for target in targets:
                if not remaining:
                    break
                actual, tile = min(remaining, key=lambda item: (abs(item[0] - target), item[1]))
                remaining.remove((actual, tile))
                picked.append((target, actual, tile))
            picked.sort(key=lambda item: item[1])
            if len(picked) < len(targets):
                warnings.append(
                    f"可用故事板素材不足：请求 {len(targets)} 帧，实际 {len(picked)} 帧"
                    f"（可用素材 {len(entries)} 块）")
            tile_w = int(snapshot.get("img_x_size") or 480)
            tile_h = int(snapshot.get("img_y_size") or 270)
            if not (16 <= tile_w <= 1024 and 16 <= tile_h <= 1024):
                raise ValueError("invalid_storyboard_tile")
            images = [str(item) for item in snapshot["image"]]
            sheets: dict[int, tuple[int, int, bytes]] = {}
            cap = 0

            async def sheet(index: int) -> tuple[int, int, bytes]:
                if index not in sheets:
                    if index >= len(images):
                        raise ValueError("storyboard_sheet_missing")
                    sheets[index] = decode_image(await fetcher.storyboard_image(images[index]))
                return sheets[index]

            try:
                first_width, first_height, _ = await sheet(0)
            except (ValueError, OSError, IndexError, RuntimeError, KeyError):
                # 故事板图片整体读不到：降级为无画面，不让整次调用失败。
                warnings.append("故事板图片读取失败，画面不可用")
                picked = []
                first_width = first_height = 0
            cap = max(1, (first_width // tile_w) * (first_height // tile_h))
            for want, actual, tile in picked:
                try:
                    sheet_no, position = divmod(tile, cap)
                    width, height, rgb = await sheet(sheet_no)
                    if position >= (width // tile_w) * (height // tile_h):
                        raise ValueError("tile_out_of_range")
                    piece = bytearray(tile_w * tile_h * 3)
                    for y in range(tile_h):
                        source = ((position // (width // tile_w) * tile_h + y) * width
                                  + (position % (width // tile_w)) * tile_w) * 3
                        piece[y * tile_w * 3:(y + 1) * tile_w * 3] = rgb[source:source + tile_w * 3]
                    frames.append({"requested_seconds": round(want, 1),
                                   "actual_seconds": round(actual, 1),
                                   "tile_index": tile, "sheet_index": sheet_no,
                                   "width": tile_w, "height": tile_h, "rgb": bytes(piece)})
                except (ValueError, OSError, IndexError, RuntimeError, KeyError):
                    warnings.append(f"时间点 {want:.1f}s 的画面不可用")
            if len(frames) < len(picked):
                warnings.append(f"其中 {len(picked) - len(frames)} 帧画面读取失败")
            if options.frame_count == 0:
                frames = _adaptive_frames(frames, covered)
        if not TIMESTAMP_SUPPORT:
            warnings.append("Pillow 不可用，拼图未烧录时间戳（标准库 PNG 兜底）")

    cues = await fetcher.subtitles(ref.bvid, cid, options.subtitle_language)
    subtitles = _excerpt(cues)
    # 只算引用与哈希，不落盘：字幕、拼图、索引一起提交，失败不覆盖旧有效产物。
    sub_meta = cache_store.subtitle_meta(Path(cache_dir), key, cues) if cues else {}
    subtitles.update(sub_meta)
    sheet_bytes = b""
    mime = ""
    if frames:
        try:
            sheet_bytes, mime = build_sheet(frames, title=str(video_meta["title"]),
                                            page=ref.page, max_bytes=options.max_bytes)
        except (ValueError, OSError):
            warnings.append("拼图生成失败，画面不可用")
            frames = []
            sheet_bytes, mime = b"", ""
    if cues:
        subtitles["language"] = str(cues[0].get("language") or options.subtitle_language)
    created = cache_store.now()
    artifacts = ()
    if sheet_bytes:
        extension = "jpg" if mime == "image/jpeg" else "png"
        artifacts = ({"kind": "contact_sheet", "path": f"cache/{key}.sheet.{extension}",
                      "mime": mime, "sha256": hashlib.sha256(sheet_bytes).hexdigest(),
                      "size_bytes": len(sheet_bytes)},)
    has_sheet, has_sub = bool(sheet_bytes), subtitles["status"] == "available"
    status = "complete" if has_sheet and has_sub else "partial" if (has_sheet or has_sub) else "unavailable"
    record = {"status": status, "video": video_meta,
              "frames": [{k: v for k, v in item.items() if k != "rgb"} for item in frames],
              "coverage": coverage, "subtitles": subtitles, "artifacts": list(artifacts),
              "warnings": warnings, "created_at": created,
              "expires_at": cache_store.expires_at(created, options.cache_ttl_hours),
              "cache_ttl_hours": options.cache_ttl_hours, **sub_meta}
    stored = (await cache_store.save(Path(cache_dir), key, record, sheet_bytes,
                                     subtitle_cues=cues if cues else None)) if sheet_bytes else {}
    if not stored:
        # 本轮没有提交任何派生产物：不要给出指向不存在文件的字幕引用。
        subtitles.pop("subtitle_path", None)
        subtitles.pop("subtitle_sha256", None)
    artifacts = ({"kind": "contact_sheet", "path": stored["path"], "mime": mime,
                  "sha256": stored["sha256"], "size_bytes": len(sheet_bytes)},) if stored else ()
    return VideoResult(True, status, video=video_meta,
                       cache={"hit": False, "key": key, "created_at": created},
                       frames=tuple({k: v for k, v in item.items() if k != "rgb"} for item in frames),
                       coverage=coverage, subtitles=subtitles, artifacts=artifacts,
                       sheet_bytes=sheet_bytes, warnings=tuple(warnings))


async def inspect_video(url: str, options: VideoOptions, cache_dir: Path,
                        *, fetcher: Any = None) -> VideoResult:
    """可独立调用；fixture fetcher 与真实 fetcher 共用同一接口。"""

    ref = resolve_url(url)
    fetcher = fetcher or HttpFetcher(timeout=options.timeout_seconds)
    if ref.short:
        ref = resolve_url(await fetcher.resolve_short(ref.raw))
    view = await fetcher.view(ref.bvid)
    pages = view.get("pages") or []
    page_info = next((item for item in pages if int(item.get("page") or 0) == ref.page), None)
    if page_info is None:
        return VideoResult(False, "failed", error={"code": "page_not_found", "message": "分P不存在"})
    cid = int(page_info.get("cid") or 0)
    duration = float(page_info.get("duration") or view.get("duration") or 0)
    video_meta = {"bvid": ref.bvid, "cid": cid, "page": ref.page,
                  "title": str(view.get("title") or "")[:200],
                  "duration_seconds": duration,
                  "canonical_url": f"https://www.bilibili.com/video/{ref.bvid}?p={ref.page}"}
    key = cache_store.cache_key(ref.bvid, cid, ref.page, options.frame_count,
                                options.subtitle_language, options.credential_domain)
    async with cache_store.key_lock(key, cache_dir=Path(cache_dir)):
        miss = ""
        if not options.refresh:
            cached, miss = await cache_store.load_detail(
                Path(cache_dir), key, max_age_seconds=options.cache_ttl_hours * 3600)
            if cached is not None:
                return VideoResult(
                    True, str(cached.get("status") or "partial"),
                    video=cached.get("video") or video_meta,
                    cache={"hit": True, "key": key, "created_at": cached.get("created_at", "")},
                    frames=tuple(cached.get("frames") or ()),
                    coverage=cached.get("coverage") or {},
                    subtitles=cached.get("subtitles") or {},
                    artifacts=tuple(cached.get("artifacts") or ()),
                    warnings=tuple(cached.get("warnings") or ()),
                )
        result = await _uncached(fetcher, options, cache_dir, ref, view, page_info,
                                 key, video_meta, [])
        if miss and not options.refresh:
            # 如实说明为何没命中：缺失/过期/损坏/缺图/哈希不符，不冒充命中。
            result = replace(result, cache={**dict(result.cache), "miss": miss})
        return result
