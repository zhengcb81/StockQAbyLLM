#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试 LLM 重试策略。"""

import pytest
from src.providers.llm_retry_strategy import LLMRetryStrategy


def test_linear_backoff():
    """测试线性退避。"""
    strategy = LLMRetryStrategy(base_delay=1.0, exponential=False, jitter=False)
    assert strategy.get_wait_time(0) == 1.0
    assert strategy.get_wait_time(1) == 2.0
    assert strategy.get_wait_time(2) == 3.0


def test_exponential_backoff():
    """测试指数退避。"""
    strategy = LLMRetryStrategy(base_delay=1.0, exponential=True, jitter=False)
    assert strategy.get_wait_time(0) == 1.0 # 1 * 2^0
    assert strategy.get_wait_time(1) == 2.0 # 1 * 2^1
    assert strategy.get_wait_time(2) == 4.0 # 1 * 2^2


def test_jitter():
    """测试抖动。"""
    strategy = LLMRetryStrategy(base_delay=1.0, jitter=True)
    wait_time = strategy.get_wait_time(0)
    # 应该在 1.0 和 1.1 之间 (0-10% jitter)
    assert 1.0 <= wait_time <= 1.1


def test_should_retry():
    """测试重试判断。"""
    strategy = LLMRetryStrategy(max_retries=3)
    assert strategy.should_retry(0, Exception()) is True
    assert strategy.should_retry(1, Exception()) is True
    assert strategy.should_retry(2, Exception()) is False
