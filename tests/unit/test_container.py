#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试依赖注入容器。"""

import pytest
from src.core.container import Container, ContainerError


class MockService:
    """模拟服务。"""
    pass


class MockServiceWithDep:
    """带依赖的模拟服务。"""
    def __init__(self, dep: MockService):
        self.dep = dep


def test_container_register_and_resolve():
    """测试服务注册和解析。"""
    container = Container()
    container.register(MockService)
    
    instance = container.resolve(MockService)
    assert isinstance(instance, MockService)
    
    # 再次解析应该得到新实例（非单例）
    instance2 = container.resolve(MockService)
    assert instance2 is not instance


def test_container_singleton():
    """测试单例服务。"""
    container = Container()
    container.register(MockService, singleton=True)
    
    instance = container.resolve(MockService)
    instance2 = container.resolve(MockService)
    
    assert instance2 is instance


def test_container_factory():
    """测试自定义工厂函数。"""
    container = Container()
    container.register(MockService, factory=lambda: MockService(), singleton=True)
    
    instance = container.resolve(MockService)
    assert isinstance(instance, MockService)


def test_container_dependency_injection():
    """测试手动依赖注入。"""
    container = Container()
    container.register(MockService, singleton=True)
    
    # 手动注入依赖
    container.register(
        MockServiceWithDep, 
        factory=lambda: MockServiceWithDep(container.resolve(MockService))
    )
    
    service = container.resolve(MockServiceWithDep)
    assert isinstance(service.dep, MockService)
    assert service.dep is container.resolve(MockService)


def test_container_unregistered_error():
    """测试解析未注册服务报错。"""
    container = Container()
    with pytest.raises(ContainerError, match="服务未注册"):
        container.resolve(MockService)


def test_container_duplicate_register_error():
    """测试重复注册报错。"""
    container = Container()
    container.register(MockService)
    with pytest.raises(ContainerError, match="服务已注册"):
        container.register(MockService)


def test_container_try_resolve():
    """测试 try_resolve 方法。"""
    container = Container()
    assert container.try_resolve(MockService) is None
    
    container.register(MockService)
    assert isinstance(container.try_resolve(MockService), MockService)


def test_container_is_registered():
    """测试 is_registered 方法。"""
    container = Container()
    assert not container.is_registered(MockService)
    
    container.register(MockService)
    assert container.is_registered(MockService)


def test_container_unregister():
    """测试注销服务。"""
    container = Container()
    container.register(MockService, singleton=True)
    container.resolve(MockService) # 创建单例
    
    container.unregister(MockService)
    assert not container.is_registered(MockService)
    assert container.try_resolve(MockService) is None


def test_container_clear():
    """测试清除容器。"""
    container = Container()
    container.register(MockService)
    container.clear()
    assert not container.is_registered(MockService)


def test_container_get_registered_services():
    """测试获取已注册服务列表。"""
    container = Container()
    container.register(MockService)
    services = container.get_registered_services()
    assert MockService in services
    assert len(services) == 1
