#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试 LLM 响应解析器。"""

import pytest

from src.providers.llm_response_parser import LLMResponseParser


def test_parse_valid_json_direct():
    """测试直接解析有效的 JSON。"""
    parser = LLMResponseParser()
    content = '{"score": 8, "description": "非常好的公司"}'
    result = parser.parse_response(content)
    assert result == (8, "非常好的公司")


def test_parse_json_with_markdown_blocks():
    """测试从 Markdown 代码块中提取 JSON。"""
    parser = LLMResponseParser()
    content = (
        "分析结果如下：\n```json\n" + '{"score": 9, "description": "卓越"}' + "\n```\n请参考。"
    )
    result = parser.parse_response(content)
    assert result == (9, "卓越")


def test_parse_json_with_text_around():
    """测试从普通文本中提取 JSON。"""
    parser = LLMResponseParser()
    content = '评分结果是 {"score": 7, "description": "一般"} 以后再看。'
    result = parser.parse_response(content)
    assert result == (7, "一般")


def test_parse_json_with_nested_braces():
    """测试带有嵌套大括号的 JSON 提取。"""
    parser = LLMResponseParser()
    content = '{"score": 6, "description": "数据: {\'key\': \'val\'}"}'
    result = parser.parse_response(content)
    assert result == (6, "数据: {'key': 'val'}")


def test_parse_invalid_json():
    """测试解析无效 JSON。"""
    parser = LLMResponseParser()
    content = "这不是 JSON"
    result = parser.parse_response(content)
    assert result is None


def test_parse_missing_fields():
    """测试缺失字段。"""
    parser = LLMResponseParser()
    content = '{"score": 8}'
    result = parser.parse_response(content)
    assert result is None


def test_score_clamping():
    """测试评分范围限制。"""
    parser = LLMResponseParser()
    # 超过 10
    assert parser.parse_response('{"score": 15, "description": "test"}')[0] == 10
    # 低于 1
    assert parser.parse_response('{"score": -5, "description": "test"}')[0] == 1
