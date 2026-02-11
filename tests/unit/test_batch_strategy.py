#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试批量处理策略。"""

import pytest
import asyncio
from src.interfaces.batch_strategy import (
    BatchProgress,
    SerialStrategy,
    ChunkedStrategy,
    AsyncStrategy,
    BatchStrategyFactory
)


def mock_processor(item):
    """模拟处理函数。"""
    if item == "error":
        raise ValueError("Intentional error")
    return f"processed_{item}"


async def async_mock_processor(item):
    """异步模拟处理函数。"""
    await asyncio.sleep(0.01)
    if item == "error":
        raise ValueError("Intentional error")
    return f"processed_{item}"


def test_serial_strategy():
    """测试串行策略。"""
    strategy = SerialStrategy()
    items = [1, 2, "error", 4]
    
    results = strategy.process(items, mock_processor)
    
    assert len(results) == 3
    assert results == ["processed_1", "processed_2", "processed_4"]
    assert strategy.get_strategy_name() == "serial"


def test_serial_strategy_progress():
    """测试串行策略进度回调。"""
    strategy = SerialStrategy()
    progress_updates = []
    
    def on_progress(p):
        progress_updates.append(p)
        
    strategy.process([1, 2], mock_processor, on_progress=on_progress)
    
    assert len(progress_updates) == 2
    assert progress_updates[0].current == 1
    assert progress_updates[1].current == 2
    assert progress_updates[1].success_count == 2


def test_chunked_strategy():
    """测试分块策略。"""
    strategy = ChunkedStrategy(chunk_size=2)
    items = [1, 2, 3, 4, 5]
    
    results = strategy.process(items, mock_processor)
    
    assert len(results) == 5
    assert results[0] == "processed_1"
    assert results[4] == "processed_5"
    assert strategy.get_strategy_name() == "chunked"


def test_async_strategy():
    """测试异步策略。"""
    strategy = AsyncStrategy(max_concurrency=2)
    items = [1, 2, "error", 4]
    
    # 因为 AsyncStrategy.process 在没有事件循环时使用 asyncio.run
    # 所以可以在同步测试中运行
    results = strategy.process(items, mock_processor)
    
    assert len(results) == 3
    assert "processed_1" in results
    assert "processed_2" in results
    assert "processed_4" in results
    assert strategy.get_strategy_name() == "async"


def test_async_strategy_with_async_processor():
    """测试带异步处理器的异步策略。"""
    strategy = AsyncStrategy()
    items = [1, 2]
    
    results = strategy.process(items, async_mock_processor)
    assert len(results) == 2


def test_batch_strategy_factory():
    """测试策略工厂。"""
    strategy = BatchStrategyFactory.create("serial")
    assert isinstance(strategy, SerialStrategy)
    
    strategy = BatchStrategyFactory.create("chunked", chunk_size=5)
    assert isinstance(strategy, ChunkedStrategy)
    assert strategy.chunk_size == 5
    
    strategy = BatchStrategyFactory.create("async", max_concurrency=10)
    assert isinstance(strategy, AsyncStrategy)
    assert strategy.max_concurrency == 10
    
    with pytest.raises(ValueError, match="不支持的策略"):
        BatchStrategyFactory.create("invalid")


def test_batch_progress_dataclass():
    """测试 BatchProgress 数据类。"""
    progress = BatchProgress(current=5, total=10, success_count=4, error_count=1)
    assert progress.percentage == 50.0
    assert not progress.is_complete
    
    progress.current = 10
    assert progress.is_complete
    
    # 零总数情况
    progress = BatchProgress(0, 0, 0, 0)
    assert progress.percentage == 0.0
