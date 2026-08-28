# -*- coding: utf-8 -*-
"""WxPusher SPT sender for private, low-volume alerts.

The SPT is read only from the WXPUSHER_SPT environment variable.  Never put
an SPT in source code or logs: it is effectively a personal push address.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import requests

from src.formatters import strip_hidden_markdown_metadata


logger = logging.getLogger(__name__)


class WxPusherSptSender:
    """Send Markdown notifications through WxPusher Simple Push Token."""

    API_URL = "https://wxpusher.zjiecode.com/api/send/message/simple-push"

    def __init__(self, spt: Optional[str] = None):
        self._spt = (spt or os.getenv("WXPUSHER_SPT") or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self._spt)

    def send(
        self,
        content: str,
        title: Optional[str] = None,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        if not self._spt:
            logger.warning("WXPUSHER_SPT 未配置，跳过 WxPusher 推送")
            return False

        sanitized = strip_hidden_markdown_metadata(content).strip()
        summary = (title or "股票关键节点提醒").strip()[:100]
        payload = {
            "content": sanitized,
            "summary": summary,
            "contentType": 3,
            "spt": self._spt,
        }

        try:
            response = requests.post(
                self.API_URL,
                json=payload,
                timeout=timeout_seconds or 10,
            )
        except Exception as exc:
            logger.error("WxPusher 请求失败: %s", exc)
            return False

        if response.status_code != 200:
            logger.error("WxPusher 请求失败: HTTP %s", response.status_code)
            return False

        try:
            result = response.json()
        except Exception:
            logger.error("WxPusher 返回了无法解析的响应")
            return False

        # WxPusher currently uses code=1000 for success.  Keep 200/0 accepted
        # for compatibility with gateway variants without ever logging the SPT.
        code = result.get("code")
        if code in (1000, 200, 0):
            logger.info("WxPusher 消息发送成功")
            return True

        logger.error("WxPusher 返回错误: %s", result.get("msg", "未知错误"))
        return False
