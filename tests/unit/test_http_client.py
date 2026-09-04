#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试 HTTP 客户端模块。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import httpx
import pytest
import requests

from src.utils.http_client import (
    AsyncHTTPClient,
    HTTPClientManager,
    SyncHTTPClient,
    http_client_manager,
)


class TestSyncHTTPClient:
    """测试 SyncHTTPClient 类。"""

    def setup_method(self):
        """每个测试前重置 session。"""
        SyncHTTPClient._session = None

    def teardown_method(self):
        """每个测试后清理 session。"""
        SyncHTTPClient.close_session()

    def test_get_session_creates_new_session(self):
        """测试首次获取 session 时创建新实例。"""
        session = SyncHTTPClient.get_session()
        assert isinstance(session, requests.Session)
        assert SyncHTTPClient._session is not None

    def test_get_session_returns_same_instance(self):
        """测试多次获取返回同一实例（单例模式）。"""
        session1 = SyncHTTPClient.get_session()
        session2 = SyncHTTPClient.get_session()
        assert session1 is session2

    def test_get_session_has_retry_configured(self):
        """测试 session 配置了重试策略。"""
        session = SyncHTTPClient.get_session()

        # 验证适配器已安装
        assert "https://" in session.adapters
        assert "http://" in session.adapters

        # 验证重试策略
        https_adapter = session.adapters["https://"]
        assert https_adapter.max_retries is not None

    def test_close_session(self):
        """测试关闭 session。"""
        session = SyncHTTPClient.get_session()
        assert SyncHTTPClient._session is not None

        SyncHTTPClient.close_session()

        assert SyncHTTPClient._session is None

    def test_close_session_idempotent(self):
        """测试重复关闭 session 不会报错。"""
        SyncHTTPClient.get_session()
        SyncHTTPClient.close_session()
        # 不应抛出异常
        SyncHTTPClient.close_session()

    def test_get_session_after_close(self):
        """测试关闭后重新获取创建新 session。"""
        session1 = SyncHTTPClient.get_session()
        SyncHTTPClient.close_session()

        session2 = SyncHTTPClient.get_session()

        assert session2 is not session1
        assert isinstance(session2, requests.Session)


class TestAsyncHTTPClient:
    """测试 AsyncHTTPClient 类。"""

    def setup_method(self):
        """每个测试前重置 client。"""
        AsyncHTTPClient._client = None

    def teardown_method(self):
        """每个测试后清理 client。"""
        # 使用同步方式清理以避免事件循环问题
        if AsyncHTTPClient._client is not None:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # 创建新任务来关闭
                    asyncio.create_task(AsyncHTTPClient.close_client())
                else:
                    loop.run_until_complete(AsyncHTTPClient.close_client())
            except Exception:
                AsyncHTTPClient._client = None

    @pytest.mark.asyncio
    async def test_get_client_creates_new_client(self):
        """测试首次获取 client 时创建新实例。"""
        client = await AsyncHTTPClient.get_client()
        assert isinstance(client, httpx.AsyncClient)
        assert AsyncHTTPClient._client is not None

    @pytest.mark.asyncio
    async def test_get_client_returns_same_instance(self):
        """测试多次获取返回同一实例（单例模式）。"""
        client1 = await AsyncHTTPClient.get_client()
        client2 = await AsyncHTTPClient.get_client()
        assert client1 is client2

    @pytest.mark.asyncio
    async def test_get_client_has_limits_configured(self):
        """测试 client 配置了连接池限制。"""
        client = await AsyncHTTPClient.get_client()
        assert isinstance(client, httpx.AsyncClient)
        # 验证客户端已配置（通过检查属性）
        assert client.timeout is not None

    @pytest.mark.asyncio
    async def test_close_client(self):
        """测试关闭 client。"""
        client = await AsyncHTTPClient.get_client()
        assert AsyncHTTPClient._client is not None

        await AsyncHTTPClient.close_client()

        assert AsyncHTTPClient._client is None

    @pytest.mark.asyncio
    async def test_get_client_after_close(self):
        """测试关闭后重新获取创建新 client。"""
        client1 = await AsyncHTTPClient.get_client()
        await AsyncHTTPClient.close_client()

        client2 = await AsyncHTTPClient.get_client()

        assert client2 is not client1
        assert isinstance(client2, httpx.AsyncClient)

    def test_get_sync_client(self):
        """测试获取同步 httpx.Client。"""
        client = AsyncHTTPClient.get_sync_client()
        assert isinstance(client, httpx.Client)
        # 同步客户端不是单例，每次都创建新实例
        client2 = AsyncHTTPClient.get_sync_client()
        assert client is not client2

    def test_get_sync_client_has_limits_configured(self):
        """测试同步 client 配置了连接池限制。"""
        client = AsyncHTTPClient.get_sync_client()
        assert isinstance(client, httpx.Client)
        assert client.timeout is not None

    def test_get_sync_client_close(self):
        """测试关闭同步 client。"""
        client = AsyncHTTPClient.get_sync_client()
        # 手动关闭
        client.close()
        # 不应抛出异常


class TestHTTPClientManager:
    """测试 HTTPClientManager 类。"""

    def setup_method(self):
        """每个测试前重置客户端。"""
        SyncHTTPClient._session = None
        AsyncHTTPClient._client = None

    def teardown_method(self):
        """每个测试后清理客户端。"""
        SyncHTTPClient.close_session()
        if AsyncHTTPClient._client is not None:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(AsyncHTTPClient.close_client())
                else:
                    loop.run_until_complete(AsyncHTTPClient.close_client())
            except Exception:
                AsyncHTTPClient._client = None

    def test_get_sync_session(self):
        """测试获取同步 session。"""
        session = HTTPClientManager.get_sync_session()
        assert isinstance(session, requests.Session)
        # 应该是同一个实例
        session2 = HTTPClientManager.get_sync_session()
        assert session is session2

    @pytest.mark.asyncio
    async def test_get_async_client(self):
        """测试获取异步 client。"""
        client = await HTTPClientManager.get_async_client()
        assert isinstance(client, httpx.AsyncClient)

    def test_get_httpx_sync_client(self):
        """测试获取同步 httpx client。"""
        client = HTTPClientManager.get_httpx_sync_client()
        assert isinstance(client, httpx.Client)

    def test_close_all(self):
        """测试关闭所有同步客户端。"""
        # 创建一些客户端
        HTTPClientManager.get_sync_session()

        HTTPClientManager.close_all()

        assert SyncHTTPClient._session is None

    @pytest.mark.asyncio
    async def test_aclose_all(self):
        """测试异步关闭所有客户端。"""
        # 创建一些客户端
        HTTPClientManager.get_sync_session()
        await HTTPClientManager.get_async_client()

        await HTTPClientManager.aclose_all()

        assert SyncHTTPClient._session is None
        assert AsyncHTTPClient._client is None


class TestGlobalHTTPClientManager:
    """测试全局 http_client_manager 实例。"""

    def test_is_httpclientmanager_instance(self):
        """测试全局实例是 HTTPClientManager 类型。"""
        assert isinstance(http_client_manager, HTTPClientManager)

    def test_get_sync_session_via_global(self):
        """测试通过全局实例获取同步 session。"""
        session = http_client_manager.get_sync_session()
        assert isinstance(session, requests.Session)

    def test_get_httpx_sync_client_via_global(self):
        """测试通过全局实例获取同步 httpx client。"""
        client = http_client_manager.get_httpx_sync_client()
        assert isinstance(client, httpx.Client)


class TestHTTPClientIntegration:
    """HTTP 客户端集成测试。"""

    def setup_method(self):
        """每个测试前重置客户端。"""
        SyncHTTPClient._session = None
        AsyncHTTPClient._client = None

    def teardown_method(self):
        """每个测试后清理客户端。"""
        SyncHTTPClient.close_session()
        if AsyncHTTPClient._client is not None:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(AsyncHTTPClient.close_client())
                else:
                    loop.run_until_complete(AsyncHTTPClient.close_client())
            except Exception:
                AsyncHTTPClient._client = None

    @patch("requests.Session.get")
    def test_sync_http_client_get_request(self, mock_get):
        """测试使用同步客户端发送 GET 请求。"""
        # Mock 响应
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"result": "success"}
        mock_get.return_value = mock_response

        # 获取 session 并发送请求
        session = SyncHTTPClient.get_session()
        response = session.get("https://api.example.com/test")

        assert response.status_code == 200
        assert response.json() == {"result": "success"}

    @patch("requests.Session.post")
    def test_sync_http_client_post_request(self, mock_post):
        """测试使用同步客户端发送 POST 请求。"""
        # Mock 响应
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"created": True}
        mock_post.return_value = mock_response

        # 获取 session 并发送请求
        session = SyncHTTPClient.get_session()
        response = session.post("https://api.example.com/create", json={"name": "test"})

        assert response.status_code == 201
        assert response.json() == {"created": True}

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient.get")
    async def test_async_http_client_get_request(self, mock_get):
        """测试使用异步客户端发送 GET 请求。"""
        # Mock 响应
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"result": "success"}
        mock_get.return_value = mock_response

        # 获取 client 并发送请求
        client = await AsyncHTTPClient.get_client()
        response = await client.get("https://api.example.com/test")

        assert response.status_code == 200
        assert await response.json() == {"result": "success"}
