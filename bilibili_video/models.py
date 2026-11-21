"""输入/输出契约：结构化元数据与独立产物路径，不塞二进制进正文。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class VideoOptions:
    frame_count: int = 6
    refresh: bool = False
    subtitle_language: str = "auto"
    credential_domain: str = "public"
    timeout_seconds: float = 15.0
    max_bytes: int = 3 * 1024 * 1024
    cache_ttl_hours: float = 24.0

    def __post_init__(self) -> None:
        if not 1 <= int(self.frame_count) <= 6:
            raise ValueError("frame_count 必须在 1-6")
        if not self.credential_domain or len(self.credential_domain) > 80:
            raise ValueError("credential_domain 不合法")


@dataclass(frozen=True)
class VideoResult:
    ok: bool
    status: str  # complete / partial / unavailable / failed
    video: Mapping[str, Any] = field(default_factory=dict)
    cache: Mapping[str, Any] = field(default_factory=dict)
    frames: tuple = ()
    coverage: Mapping[str, Any] = field(default_factory=dict)
    subtitles: Mapping[str, Any] = field(default_factory=dict)
    artifacts: tuple = ()
    sheet_bytes: bytes = b""
    warnings: tuple = ()
    error: Mapping[str, Any] | None = None

    def public(self) -> dict:
        """给宿主的受控正文：字幕只有有界摘录，不含图片字节与完整原文。"""

        return {
            "ok": self.ok,
            "status": self.status,
            "video": dict(self.video),
            "cache": dict(self.cache),
            "frames": [dict(item) for item in self.frames],
            "coverage": dict(self.coverage),
            "subtitles": dict(self.subtitles),
            "artifacts": [dict(item) for item in self.artifacts],
            "warnings": list(self.warnings),
            "error": dict(self.error) if self.error else None,
        }
