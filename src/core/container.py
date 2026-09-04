#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""依赖注入容器。

该模块提供简单的依赖注入容器，支持服务注册和解析。
遵循显式依赖注入原则，不推荐使用全局容器。
"""

from typing import Any, Callable, Dict, List, Optional, Type, TypeVar

from src.utils.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


class ContainerError(Exception):
    """容器错误。"""

    pass


class Container:
    """简单的依赖注入容器。

    支持服务注册、解析和单例管理。

    Example:
        >>> container = Container()
        >>> container.register(SearchService, lambda: SearchService())
        >>> service = container.resolve(SearchService)
    """

    def __init__(self) -> None:
        """初始化容器。"""
        self._services: Dict[Type[Any], Callable[[], Any]] = {}
        self._singletons: Dict[Type[Any], Any] = {}
        self._singleton_types: set[Type[Any]] = set()
        logger.debug("依赖注入容器初始化完成")

    def register(
        self,
        service_type: Type[T],
        factory: Optional[Callable[[], T]] = None,
        singleton: bool = False,
    ) -> None:
        """注册服务。

        Args:
            service_type: 服务类型
            factory: 服务工厂函数（可选，默认使用类型构造函数）
            singleton: 是否为单例

        Raises:
            ContainerError: 如果服务已注册
        """
        if service_type in self._services:
            raise ContainerError(f"服务已注册: {service_type.__name__}")

        if factory is None:
            # 使用类型本身作为工厂
            if isinstance(service_type, type):
                self._services[service_type] = service_type
            else:
                raise ContainerError(f"服务类型必须是类或提供工厂函数: {service_type}")
        else:
            self._services[service_type] = factory

        if singleton:
            self._singleton_types.add(service_type)

        logger.debug("服务已注册: %s (单例: %s)", service_type.__name__, singleton)

    def resolve(self, service_type: Type[T]) -> T:
        """解析服务。

        Args:
            service_type: 服务类型

        Returns:
            服务实例

        Raises:
            ContainerError: 如果服务未注册
        """
        if service_type not in self._services:
            raise ContainerError(f"服务未注册: {service_type.__name__}")

        # 如果是单例且已创建，返回缓存的实例
        if service_type in self._singleton_types:
            if service_type in self._singletons:
                logger.debug("返回单例实例: %s", service_type.__name__)
                return self._singletons[service_type]  # type: ignore[no-any-return]

        # 创建新实例
        factory = self._services[service_type]
        try:
            instance = factory()
        except Exception as e:
            logger.error("解析服务 %s 时出错: %s", service_type.__name__, e)
            raise ContainerError(f"解析服务 {service_type.__name__} 失败: {str(e)}") from e

        # 如果是单例，缓存实例
        if service_type in self._singleton_types:
            self._singletons[service_type] = instance
            logger.debug("创建并缓存单例实例: %s", service_type.__name__)

        return instance  # type: ignore[no-any-return]

    def try_resolve(self, service_type: Type[T]) -> Optional[T]:
        """尝试解析服务。"""
        try:
            return self.resolve(service_type)
        except ContainerError:
            return None

    def is_registered(self, service_type: Type[Any]) -> bool:
        """检查服务是否已注册。"""
        return service_type in self._services

    def unregister(self, service_type: Type[Any]) -> None:
        """注销服务。"""
        if service_type not in self._services:
            raise ContainerError(f"服务未注册: {service_type.__name__}")

        del self._services[service_type]
        self._singleton_types.discard(service_type)
        self._singletons.pop(service_type, None)

        logger.debug("服务已注销: %s", service_type.__name__)

    def clear(self) -> None:
        """清除所有注册的服务。"""
        self._services.clear()
        self._singletons.clear()
        self._singleton_types.clear()
        logger.debug("容器已清除")

    def get_registered_services(self) -> List[Type[Any]]:
        """获取所有已注册的服务类型。"""
        return list(self._services.keys())
