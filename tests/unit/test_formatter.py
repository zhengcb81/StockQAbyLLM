#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试输出格式化器。"""

import pytest
import json
from src.interfaces.formatter import (
    JSONFormatter,
    YAMLFormatter,
    CSVFormatter,
    FormatterFactory
)
from src.core.models import QABatchResult, QAResult, Question, Answer


@pytest.fixture
def sample_batch_result():
    """提供样本批量结果。"""
    result = QABatchResult(total_questions=2)
    
    q1 = Question(text="问题1")
    a1 = Answer(text="答案1", score=8, source="mock")
    r1 = QAResult(question=q1, answer=a1)
    
    q2 = Question(text="问题2")
    a2 = Answer(text="答案2", score=9, source="mock")
    r2 = QAResult(question=q2, answer=a2)
    
    result.add_result(r1)
    result.add_result(r2)
    return result


def test_json_formatter(sample_batch_result):
    """测试 JSON 格式化器。"""
    formatter = JSONFormatter(indent=2)
    output = formatter.format(sample_batch_result)
    
    data = json.loads(output)
    assert "问题1" in data
    assert data["问题1"]["score"] == 8
    assert formatter.get_file_extension() == ".json"
    assert formatter.get_format_name() == "JSON"


def test_yaml_formatter(sample_batch_result):
    """测试 YAML 格式化器。"""
    try:
        import yaml
    except ImportError:
        pytest.skip("PyYAML 未安装")
        
    formatter = YAMLFormatter()
    output = formatter.format(sample_batch_result)
    
    assert "问题1" in output
    assert "score: 8" in output
    assert formatter.get_file_extension() == ".yaml"


def test_csv_formatter(sample_batch_result):
    """测试 CSV 格式化器。"""
    formatter = CSVFormatter()
    output = formatter.format(sample_batch_result)
    
    assert "Question,Score,Description,Source" in output
    assert "问题1,8,答案1,mock" in output
    assert formatter.get_file_extension() == ".csv"


def test_formatter_factory():
    """测试格式化器工厂。"""
    formatter = FormatterFactory.create("json")
    assert isinstance(formatter, JSONFormatter)
    
    formatter = FormatterFactory.create("yaml")
    assert isinstance(formatter, YAMLFormatter)
    
    formatter = FormatterFactory.create("csv")
    assert isinstance(formatter, CSVFormatter)
    
    with pytest.raises(ValueError, match="不支持的格式"):
        FormatterFactory.create("invalid")


def test_formatter_factory_registration():
    """测试注册自定义格式化器。"""
    class MockFormatter(JSONFormatter):
        def get_format_name(self): return "MOCK"
        
    FormatterFactory.register_formatter("mock", MockFormatter)
    formatter = FormatterFactory.create("mock")
    assert isinstance(formatter, MockFormatter)
    assert "mock" in FormatterFactory.get_supported_formats()
