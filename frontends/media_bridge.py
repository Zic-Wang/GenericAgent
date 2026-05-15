"""Media bridge for GA Feishu Channel migration.

Side-effect-free helper around ``GAFeishuChannelAdapter`` media operations.
It centralizes upload/download conventions without wiring legacy ``fsapp.py`` yet.
"""
from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from frontends.feishu_channel_adapter import GAChannelSendResult, GAFeishuChannelAdapter
from frontends.ga_inbound_message import GAInboundMessage, GAResource


@dataclass(frozen=True)
class DownloadedResource:
    resource: GAResource
    path: Path


@dataclass(frozen=True)
class UploadedMedia:
    kind: str
    media_key: str
    source: Any
    file_name: Optional[str] = None


class MediaBridge:
    """GA-facing media upload/download façade."""

    def __init__(self, channel: GAFeishuChannelAdapter, *, download_dir: str | Path) -> None:
        self.channel = channel
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def download_resource(self, resource: GAResource, *, message_id: Optional[str] = None) -> Optional[bytes]:
        if not resource.key:
            return None
        return self.channel.download_resource(resource.key, resource_type=resource.kind, message_id=message_id)

    def download_resource_to_file(
        self,
        resource: GAResource,
        *,
        message_id: Optional[str] = None,
        file_name: Optional[str] = None,
    ) -> Optional[DownloadedResource]:
        if not resource.key:
            return None
        name = file_name or resource.name or self._default_file_name(resource)
        path = self.channel.download_resource_to_file(
            resource.key,
            resource_type=resource.kind,
            message_id=message_id,
            dest_dir=self.download_dir,
            file_name=name,
        )
        return DownloadedResource(resource=resource, path=Path(path))

    def download_all_to_files(self, message: GAInboundMessage) -> list[DownloadedResource]:
        out: list[DownloadedResource] = []
        for resource in message.resources:
            item = self.download_resource_to_file(resource, message_id=message.message_id)
            if item is not None:
                out.append(item)
        return out

    def upload_file(self, source: Any, *, file_name: Optional[str] = None, file_type: Optional[str] = None) -> UploadedMedia:
        inferred_name = file_name or self._source_name(source)
        inferred_type = file_type or self._guess_file_type(inferred_name)
        key = self.channel.upload_media(source, kind="file", file_name=inferred_name, file_type=inferred_type)
        return UploadedMedia(kind="file", media_key=key, source=source, file_name=inferred_name)

    def upload_image(self, source: Any, *, file_name: Optional[str] = None) -> UploadedMedia:
        key = self.channel.upload_media(source, kind="image", file_name=file_name or self._source_name(source))
        return UploadedMedia(kind="image", media_key=key, source=source, file_name=file_name or self._source_name(source))

    def send_file(self, to: str, source: Any, *, file_name: Optional[str] = None, reply_to: Optional[str] = None) -> GAChannelSendResult:
        return self.channel.send_file(to, source, file_name=file_name or self._source_name(source), reply_to=reply_to)

    def send_image(self, to: str, source: Any, *, reply_to: Optional[str] = None) -> GAChannelSendResult:
        return self.channel.send_image(to, source, reply_to=reply_to)

    @staticmethod
    def _source_name(source: Any) -> Optional[str]:
        if isinstance(source, (str, Path)):
            return Path(source).name
        return None

    @staticmethod
    def _guess_file_type(file_name: Optional[str]) -> Optional[str]:
        if not file_name:
            return None
        mime, _ = mimetypes.guess_type(file_name)
        if not mime:
            return None
        return mime.split("/", 1)[-1]

    @staticmethod
    def _default_file_name(resource: GAResource) -> str:
        suffix = {
            "image": ".png",
            "file": "",
            "audio": ".mp3",
            "video": ".mp4",
        }.get(resource.kind, "")
        key = resource.key or "resource"
        return f"{resource.kind}_{key}{suffix}"


__all__ = ["DownloadedResource", "MediaBridge", "UploadedMedia"]
