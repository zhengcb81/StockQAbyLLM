#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP 客户端工厂模块。

提供同步和异步 HTTP 客户端，支持连接池和重试机制。
"""

import asyncio
from typing import Optional
import httpx
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from src.utils.logger import get_logger

logger = get_logger(__name__)


class SyncHTTPClient:
    """同步 HTTP 客户端工厂。

    提供共享的 requests.Session 实例，支持连接池和重试。
    """

    _session: Optional[requests.Session] = None

    @classmethod
    def get_session(cls) -> requests.Session:
        """获取共享的 Session 实例。

        Returns:
           配置了重试和连接池的 requests.Session
        """
        if cls._session is None:
            session = requests.Session()

            # 配置重试策略
            retry_strategy = Retry(
                total=3,  # 最大重试次数
                backoff_factor=1,  # 退避因子：1, 2, 4 秒
                status_forcelist=[429, 500, 502, 503, 504],  # 需要重试的状态码
                allowed_methods=["GET", "POST", "PUT", "DELETE"],  # 允许重试的方法
            )

            # 为 HTTPS 和 HTTP 配置适配器
            adapter = HTTPAdapter(
                max_retries=retry_strategy,
                pool_connections=10,  # 连接池大小
                pool_maxsize=10,  # 最大连接数
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)

            cls._session = session
            logger.debug("创建新的同步 HTTP Session（连接池: 10）")

        return cls._session

    @classmethod
    def close_session(cls) -> None:
        """关闭共享的 Session 实例。"""
        if cls._session is not None:
            cls._session.close()
            cls._session = None
            logger.debug("关闭同步 HTTP Session")


class AsyncHTTPClient:
    """异步 HTTP 客户端工厂。

    提供共享的 httpx.AsyncClient 实例，支持连接池和超时。
    """

    _client: Optional[httpx.AsyncClient] = None

    @classmethod
    async def get_client(cls) -> httpx.AsyncClient:
        """获取共享的 AsyncClient 实例。

        Returns:
           配置了连接池的 httpx.AsyncClient
        """
        if cls._client is None:
            # 配置连接池限制
            limits = httpx.Limits(
                max_connections=10,  # 最大连接数
                max_keepalive_connections=5,  # 保持活动连接数
            )

            # 配置超时
            timeout = httpx.Timeout(
                connect=10.0,  # 连接超时
                read=60.0,  # 读取超时
                write=10.0,  # 写入超时
                pool=10.0,  # 连接池超时
            )

            cls._client = httpx.AsyncClient(
                limits=limits,
                timeout=timeout,
                follow_redirects=True,
            )
            logger.debug("创建新的异步 HTTP Client（连接池: 10，保持活动: 5）")

        return cls._client

    @classmethod
    async def close_client(cls) -> None:
        """关闭共享的 AsyncClient 实例。"""
        if cls._client is not None:
            await cls._client.aclose()
            cls._client = None
            logger.debug("关闭异步 HTTP Client")

    @classmethod
    def get_sync_client(cls) -> httpx.Client:
        """获取同步的 httpx.Client 实例。

        用于需要 httpx 同步客户端的场景。

        Returns:
           配置了连接池的 httpx.Client
        """
        limits = httpx.Limits(
            max_connections=10,
            max_keepalive_connections=5,
        )
        timeout = httpx.Timeout(
            connect=10.0,
            read=60.0,
            write=10.0,
            pool=10.0,
        )

        return httpx.Client(
            limits=limits,
            timeout=timeout,
            follow_redirects=True,
        )


# 全局 HTTP 客户端管理器
class HTTPClientManager:
    """HTTP 客户端管理器。

    提供统一的 HTTP 客户端访问接口。
    """

    @staticmethod
    def get_sync_session() -> requests.Session:
        """获取同步 Session。"""
        return SyncHTTPClient.get_session()

    @staticmethod
    async def get_async_client() -> httpx.AsyncClient:
        """获取异步 Client。"""
        return await AsyncHTTPClient.get_client()

    @staticmethod
    def get_httpx_sync_client() -> httpx.Client:
        """获取同步 httpx Client。"""
        return AsyncHTTPClient.get_sync_client()

    @staticmethod
    def close_all() -> None:
        """关闭所有 HTTP 客户端。"""
        SyncHTTPClient.close_session()
        # 注意：异步客户端需要在异步环境中关闭
        logger.info("已关闭所有同步 HTTP 客户端")

    @staticmethod
    async def aclose_all() -> None:
        """异步关闭所有 HTTP 客户端。"""
        SyncHTTPClient.close_session()
        await AsyncHTTPClient.close_client()
        logger.info("已关闭所有 HTTP 客户端（同步和异步）")


# 全局实例
http_client_manager = HTTPClientManager()
