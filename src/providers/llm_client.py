#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 客户端。

负责与 LLM API 进行底层通信。
"""

from typing import Any, Dict, cast

import httpx
import requests

from src.config.settings import DEFAULT_TIMEOUT, MAX_TOKENS, TEMPERATURE
from src.utils.http_client import http_client_manager
from src.utils.logger import get_logger

logger = get_logger(__name__)


class LLMClient:
    """同步 LLM 客户端。"""

    def __init__(self, api_key: str, model: str, base_url: str, timeout: float = DEFAULT_TIMEOUT):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.timeout = timeout

    def send_request(
        self, prompt: str, system_prompt: str = "你是一位专业的投资分析师，擅长分析公司的投资价值。"
    ) -> str:
        """发送同步请求。"""
        session = http_client_manager.get_sync_session()

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

        data = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": TEMPERATURE,
            "max_tokens": MAX_TOKENS,
        }

        logger.debug("发送请求到 %s (模型: %s)", self.base_url, self.model)

        response = session.post(self.base_url, headers=headers, json=data, timeout=self.timeout)
        response.raise_for_status()
        result = response.json()

        content = cast(str, result["choices"][0]["message"]["content"])
        return content


class AsyncLLMClient:
    """异步 LLM 客户端。"""

    def __init__(self, api_key: str, model: str, base_url: str, timeout: float = DEFAULT_TIMEOUT):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.timeout = timeout

    async def send_request_async(
        self, prompt: str, system_prompt: str = "你是一位专业的投资分析师，擅长分析公司的投资价值。"
    ) -> str:
        """发送异步请求。"""
        client = await http_client_manager.get_async_client()

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

        data = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": TEMPERATURE,
            "max_tokens": MAX_TOKENS,
        }

        logger.debug("发送异步请求到 %s (模型: %s)", self.base_url, self.model)

        response = await client.post(
            self.base_url,
            headers=headers,
            json=data,
            timeout=self.timeout,
        )
        response.raise_for_status()
        result = response.json()

        content = cast(str, result["choices"][0]["message"]["content"])
        return content
