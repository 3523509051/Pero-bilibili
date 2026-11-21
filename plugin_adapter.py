"""现行通用 SDK v1 接线层：只调用公开门面，不导入宿主私有实现。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bilibili_video import (NetworkHostDenied, ShortLinkUnsupported, SDKNetworkFetcher,
                            VideoOptions, inspect_video)
from bilibili_video.resolve import resolve_url


def _declared_hosts() -> list[str]:
    manifest = json.loads((Path(__file__).resolve().parent / "plugin.json").read_text(encoding="utf-8"))
    return [str(host) for host in manifest.get("network_hosts") or []]


def _result(ok, status, data, *, artifacts=None, warnings=None, error=None, cache=None):
    payload = {"ok": bool(ok), "status": status, "data": data,
               "artifacts": list(artifacts or []), "warnings": list(warnings or [])}
    if error is not None:
        payload["error"] = error
    if cache is not None:
        payload["cache"] = dict(cache)
    return payload


def register():
    async def handler(arguments, sdk):
        url = str(arguments.get("url") or "").strip()
        refresh = bool(arguments.get("refresh", False))
        try:
            reference = resolve_url(url)
        except ValueError:
            return _result(False, "unavailable", {"hint": "请提供完整的 HTTPS B 站视频链接"},
                           error={"code": "invalid_url", "message": "链接格式不受支持"})
        options = VideoOptions(
            frame_count=int(sdk.config.get("frame_count", 6)),
            refresh=refresh,
            subtitle_language=str(sdk.config.get("subtitle_language", "auto")),
            credential_domain="public",
        )
        fetcher = SDKNetworkFetcher(
            lambda target, **kwargs: sdk.network.read(target, **kwargs),
            _declared_hosts(),
        )
        try:
            result = await inspect_video(url, options, sdk.storage.cache_dir, fetcher=fetcher)
        except ShortLinkUnsupported:
            return _result(False, "unavailable",
                           {"hint": "请提供完整 BV 链接，例如 https://www.bilibili.com/video/BV...?p=1"},
                           error={"code": "short_link_unsupported",
                                  "message": "短链无法解析为受支持的 B 站视频链接"})
        except NetworkHostDenied:
            return _result(False, "unavailable",
                           {"hint": "短链或资源域名未获批准，请联系管理员批准域名"},
                           error={"code": "network_host_denied",
                                  "message": "目标域名不在已批准的读取范围"})
        except Exception as exc:
            code = getattr(exc, "code", None) or "video_unavailable"
            return _result(False, "unavailable",
                           {"hint": "公开资料读取失败；未使用缓存或未授权域名时不会重试"},
                           warnings=[f"读取失败：{str(code)[:60]}"],
                           error={"code": str(code)[:60], "message": "公开资料读取失败"})

        warnings = list(result.warnings)
        artifacts = []
        attached = False
        mime = ""
        for item in result.artifacts:
            relative = str(item.get("path") or "")
            mime = str(item.get("mime") or "")
            extension = Path(relative).suffix or (".jpg" if "jpeg" in mime else ".png")
            name = f"{result.video.get('bvid', 'video')}-p{result.video.get('page', 1)}-sheet{extension}"
            try:
                data_bytes = await sdk.storage.read_blob(relative)
                handle = await sdk.artifacts.publish(data_bytes, name, kind="image")
                artifacts.append(handle)
                try:
                    await sdk.artifacts.attach(handle["handle"])
                    attached = True
                except Exception as attach_exc:
                    code = getattr(attach_exc, "code", None) or type(attach_exc).__name__
                    warnings.append("图片未挂载到本轮：" + str(code)[:40])
            except Exception as publish_exc:
                code = getattr(publish_exc, "code", None) or type(publish_exc).__name__
                warnings.append("图片产物未发布：" + str(code)[:40])
        status = result.status
        if result.artifacts and not artifacts:
            status = "partial"
            warnings.append("拼图未发布到本轮产物")
        if artifacts and not attached:
            status = "partial"

        video = dict(result.video)
        data = {
            "video": {key: video.get(key) for key in
                      ("bvid", "cid", "page", "title", "duration_seconds", "canonical_url")},
            "frames": [{"requested_seconds": item.get("requested_seconds"),
                        "actual_seconds": item.get("actual_seconds")} for item in result.frames],
            "subtitle": {key: result.subtitles.get(key) for key in
                         ("status", "language", "excerpt", "cue_count")},
            "coverage": {key: result.coverage.get(key) for key in
                         ("page", "seconds", "covered_seconds", "method")},
            "cache": {"hit": bool(result.cache.get("hit", False))},
        }
        if artifacts:
            data["image"] = {"attached": attached, "mime": mime, "count": len(artifacts)}
        if len(json.dumps(data, ensure_ascii=False)) > 2800:
            data["frames"] = data["frames"][:1]
            data["subtitle"]["excerpt"] = str(data["subtitle"].get("excerpt") or "")[:80]
        try:
            sdk.events.emit("plugin.record", {
                "tool": "watch_bilibili", "status": status,
                "cache_hit": bool(result.cache.get("hit", False)),
            })
        except Exception:
            warnings.append("事件未授权，已忽略")
        error = dict(result.error) if result.error else None
        if not result.ok and error is None:
            error = {"code": "video_unavailable", "message": "视频资料不完整"}
        return _result(result.ok, status, data, artifacts=artifacts, warnings=warnings,
                       error=error, cache={"hit": bool(result.cache.get("hit", False))})

    return [{
        "name": "watch_bilibili",
        "description": "读取用户给定的完整 B 站视频链接：分P元数据、真实时间戳拼图与有界字幕预览；需管理员批准对应公开域名。",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "minLength": 8, "maxLength": 500},
                "refresh": {"type": "boolean"},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        "handler": handler,
    }]
