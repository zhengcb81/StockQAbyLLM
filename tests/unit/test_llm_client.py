#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试 LLM 客户端。"""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from src.providers.llm_client import AsyncLLMClient, LLMClient


class TestLLMClient:
    """测试同步 LLM 客户端。"""

    @pytest.fixture
    def client(self):
        return LLMClient(
            api_key="test_key",
            model="test_model",
            base_url="https://api.example.com/v1/chat/completions",
        )

    @patch("src.providers.llm_client.http_client_manager")
    def test_send_request_success(self, mock_http_manager, client):
        """测试成功发送请求。"""
        # 模拟 HTTP 响应
        mock_session = Mock()
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"choices": [{"message": {"content": "测试回答"}}]}
        mock_session.post.return_value = mock_response
        mock_http_manager.get_sync_session.return_value = mock_session

        result = client.send_request("测试问题")

        assert result == "测试回答"
        mock_session.post.assert_called_once()

    @patch("src.providers.llm_client.http_client_manager")
    def test_send_request_with_custom_system_prompt(self, mock_http_manager, client):
        """测试使用自定义系统提示词。"""
        mock_session = Mock()
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"choices": [{"message": {"content": "回答"}}]}
        mock_session.post.return_value = mock_response
        mock_http_manager.get_sync_session.return_value = mock_session

        client.send_request("问题", system_prompt="自定义系统提示")

        # 验证请求包含自定义系统提示
        call_args = mock_session.post.call_args
        assert call_args.kwargs["json"]["messages"][0]["content"] == "自定义系统提示"

    @patch("src.providers.llm_client.http_client_manager")
    def test_send_request_http_error(self, mock_http_manager, client):
        """测试HTTP错误处理。"""
        mock_session = Mock()
        mock_response = Mock()
        mock_response.raise_for_status.side_effect = RuntimeError("HTTP 500")
        mock_session.post.return_value = mock_response
        mock_http_manager.get_sync_session.return_value = mock_session

        with pytest.raises(RuntimeError):
            client.send_request("问题")

    @patch("src.providers.llm_client.http_client_manager")
    def test_send_request_timeout(self, mock_http_manager, client):
        """测试请求超时。"""
        mock_session = Mock()
        mock_session.post.side_effect = Exception("Timeout")
        mock_http_manager.get_sync_session.return_value = mock_session

        with pytest.raises(Exception, match="Timeout"):
            client.send_request("问题")

    def test_init_with_custom_timeout(self):
        """测试使用自定义超时时间初始化。"""
        client = LLMClient(
            api_key="test_key",
            model="test_model",
            base_url="https://api.example.com/v1",
            timeout=60.0,
        )
        assert client.timeout == 60.0


class TestAsyncLLMClient:
    """测试异步 LLM 客户端。"""

    @pytest.fixture
    def client(self):
        return AsyncLLMClient(
            api_key="test_key",
            model="test_model",
            base_url="https://api.example.com/v1/chat/completions",
        )

    @pytest.mark.asyncio
    @patch("src.providers.llm_client.http_client_manager")
    async def test_send_request_async_success(self, mock_http_manager, client):
        """测试异步请求成功。"""
        # 模拟异步 HTTP 客户端
        mock_async_client = AsyncMock()
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"choices": [{"message": {"content": "异步回答"}}]}

        # 创建协程返回值的 mock
        async def mock_post(*args, **kwargs):
            return mock_response

        mock_async_client.post = mock_post
        mock_http_manager.get_async_client = AsyncMock(return_value=mock_async_client)

        result = await client.send_request_async("测试问题")

        assert result == "异步回答"

    @pytest.mark.asyncio
    @patch("src.providers.llm_client.http_client_manager")
    async def test_send_request_async_with_custom_system_prompt(self, mock_http_manager, client):
        """测试异步请求使用自定义系统提示。"""
        mock_async_client = AsyncMock()
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"choices": [{"message": {"content": "回答"}}]}

        # 记录调用参数
        call_params = {}

        async def mock_post(*args, **kwargs):
            call_params.update(kwargs)
            return mock_response

        mock_async_client.post = mock_post
        mock_http_manager.get_async_client = AsyncMock(return_value=mock_async_client)

        await client.send_request_async("问题", system_prompt="自定义系统提示")

        # 验证请求包含自定义系统提示
        assert call_params["json"]["messages"][0]["content"] == "自定义系统提示"

    @pytest.mark.asyncio
    @patch("src.providers.llm_client.http_client_manager")
    async def test_send_request_async_http_error(self, mock_http_manager, client):
        """测试异步请求HTTP错误。"""
        mock_async_client = AsyncMock()
        mock_response = Mock()
        mock_response.raise_for_status.side_effect = RuntimeError("HTTP 500")

        async def mock_post_with_error(*args, **kwargs):
            return mock_response

        mock_async_client.post = mock_post_with_error
        mock_http_manager.get_async_client = AsyncMock(return_value=mock_async_client)

        with pytest.raises(RuntimeError):
            await client.send_request_async("问题")

    @pytest.mark.asyncio
    @patch("src.providers.llm_client.http_client_manager")
    async def test_send_request_async_timeout(self, mock_http_manager, client):
        """测试异步请求超时。"""
        mock_async_client = AsyncMock()
        mock_async_client.post = Mock(side_effect=Exception("Async Timeout"))
        mock_http_manager.get_async_client = AsyncMock(return_value=mock_async_client)

        with pytest.raises(Exception, match="Async Timeout"):
            await client.send_request_async("问题")

    def test_async_init_with_custom_timeout(self):
        """测试异步客户端使用自定义超时初始化。"""
        client = AsyncLLMClient(
            api_key="test_key",
            model="test_model",
            base_url="https://api.example.com/v1",
            timeout=120.0,
        )
        assert client.timeout == 120.0
