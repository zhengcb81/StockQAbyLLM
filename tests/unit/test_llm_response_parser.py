#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试 LLM 响应解析器。"""

import json

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


@pytest.mark.parametrize("score", [True, False, 0, 11, 1.5, "8", "NaN"])
def test_invalid_score_values_are_rejected(score):
    """SC-03：非法类型和越界分数必须拒绝，不能钳制或猜分。"""
    parser = LLMResponseParser()
    result = parser.parse_response(json.dumps({"score": score, "description": "test"}))
    assert result is None


@pytest.mark.parametrize("score", [1, 5, 10])
def test_valid_score_boundaries_are_preserved(score):
    parser = LLMResponseParser()
    assert parser.parse_response(json.dumps({"score": score, "description": "test"})) == (
        score,
        "test",
    )


def test_native_insufficient_evidence_is_nullable():
    parser = LLMResponseParser()
    result = parser.parse_structured_response(
        '{"status":"insufficient_evidence","score":null,"description":"缺少可核验依据"}'
    )
    assert result is not None
    assert result.status == "insufficient_evidence"
    assert result.score is None


def test_non_scored_status_cannot_carry_transport_score():
    parser = LLMResponseParser()
    assert (
        parser.parse_structured_response('{"status":"unknown","score":5,"description":"没有依据"}')
        is None
    )


@pytest.mark.parametrize("status", [[], {}], ids=["array-status", "object-status"])
def test_unhashable_status_values_are_rejected_without_raising(status):
    parser = LLMResponseParser()
    content = json.dumps({"status": status, "score": None, "description": "状态无效"})

    assert parser.parse_structured_response(content) is None


def test_expected_question_id_is_required_and_matched_exactly():
    parser = LLMResponseParser()
    assert (
        parser.parse_structured_response(
            '{"question_id":"IQS_05","score":8,"description":"同题回答"}',
            expected_question_id="IQS_05",
        ).score
        == 8
    )
    assert (
        parser.parse_structured_response(
            '{"question_id":"IQS_06","score":8,"description":"错题回答"}',
            expected_question_id="IQS_05",
        )
        is None
    )
    assert (
        parser.parse_structured_response(
            '{"score":8,"description":"缺少ID"}', expected_question_id="IQS_05"
        )
        is None
    )


def test_quick_scan_strict_mode_accepts_single_bound_json_after_vendor_preamble():
    """Q02/Q03 联合批次：厂商前导文本后的唯一完整绑定 JSON 在严格模式下可解析。"""
    parser = LLMResponseParser()
    content = 'prefix {"question_id":"IQS_05","score":8,"description":"valid object"}'

    parsed = parser.parse_structured_response(
        content, expected_question_id="IQS_05", strict_json_only=True
    )
    assert parsed is not None
    assert parsed.score == 8
    # Legacy non-quick-scan compatibility remains intact.
    assert parser.parse_structured_response(content, expected_question_id="IQS_05") is not None


def test_quick_scan_strict_mode_rejects_multiple_json_candidates_in_surrounding_text():
    """严格模式对多候选 fail-closed：内容必须恰好包含一个完整外层 JSON 对象。"""
    parser = LLMResponseParser()
    content = (
        'prefix {"question_id":"IQS_05","score":8,"description":"first"} '
        'and {"question_id":"IQS_05","score":2,"description":"second"}'
    )

    assert (
        parser.parse_structured_response(
            content, expected_question_id="IQS_05", strict_json_only=True
        )
        is None
    )


def test_quick_scan_strict_mode_rejects_content_without_any_json_object():
    """严格模式对零候选 fail-closed：无完整外层 JSON 对象即拒绝。"""
    parser = LLMResponseParser()

    assert (
        parser.parse_structured_response(
            "没有任何 JSON 对象的纯散文回答",
            expected_question_id="IQS_05",
            strict_json_only=True,
        )
        is None
    )


def test_quick_scan_strict_mode_binds_entity_inside_preamble_extraction():
    """前导文本提取路径必须执行实体/公司精确绑定。"""
    parser = LLMResponseParser()
    content = (
        'analysis preamble {"entity_id":"issuer:two","company_name":"Other Corp",'
        '"question_id":"IQS_05","score":8,"description":"bound?"}'
    )

    assert (
        parser.parse_structured_response(
            content,
            expected_question_id="IQS_05",
            expected_entity_id="issuer:one",
            expected_company_name="Example Corp",
            strict_json_only=True,
        )
        is None
    )


def test_expected_entity_identity_is_required_and_matched_exactly():
    parser = LLMResponseParser()
    good = (
        '{"entity_id":"issuer:one","company_name":"Example Corp",'
        '"question_id":"IQS_05","score":8,"description":"evidence"}'
    )
    wrong_entity = good.replace("issuer:one", "issuer:two")
    wrong_name = good.replace("Example Corp", "Other Corp")

    parsed = parser.parse_structured_response(
        good,
        expected_question_id="IQS_05",
        expected_entity_id="issuer:one",
        expected_company_name="Example Corp",
        strict_json_only=True,
    )
    assert parsed is not None
    assert parsed.entity_id == "issuer:one"
    assert parsed.company_name == "Example Corp"
    assert (
        parser.parse_structured_response(
            wrong_entity, expected_entity_id="issuer:one", strict_json_only=True
        )
        is None
    )
    assert (
        parser.parse_structured_response(
            wrong_name,
            expected_entity_id="issuer:one",
            expected_company_name="Example Corp",
            strict_json_only=True,
        )
        is None
    )


@pytest.mark.parametrize(
    "content",
    [
        json.dumps(
            {
                "entity_id": "issuer:review-fixture",
                "company_name": "Fictional Fixture Corp",
                "question_id": "IQS_05",
                "score": 5,
                "description": json.dumps(
                    {
                        "id": "IQS_05",
                        "status": "insufficient_evidence",
                        "score": None,
                        "confidence": "medium",
                        "rationale": "No disclosed evidence",
                    }
                ),
            }
        ),
        json.dumps(
            {
                "entity_id": "issuer:review-fixture",
                "company_name": "Fictional Fixture Corp",
                "question_id": "IQS_05",
                "score": 5,
                "description": json.dumps(
                    {
                        "id": "IQS_05",
                        "status": "scored",
                        "score": 8,
                        "rationale": "inner 8",
                    }
                ),
            }
        ),
        json.dumps(
            {
                "entity_id": "issuer:review-fixture",
                "company_name": "Fictional Fixture Corp",
                "question_id": "IQS_05",
                "score": 8,
                "description": json.dumps(
                    {
                        "id": "IQS_06",
                        "status": "scored",
                        "score": 8,
                        "rationale": "wrong item",
                    }
                ),
            }
        ),
        (
            '{"entity_id":"issuer:review-fixture","company_name":"Fictional Fixture Corp",'
            '"question_id":"IQS_06","question_id":"IQS_05","score":8,'
            '"description":"ambiguous ID"}'
        ),
    ],
)
def test_ambiguous_or_nested_legacy_identity_never_becomes_a_scored_native_answer(
    content,
):
    parser = LLMResponseParser()

    assert (
        parser.parse_structured_response(
            content,
            expected_question_id="IQS_05",
            expected_entity_id="issuer:review-fixture",
            expected_company_name="Fictional Fixture Corp",
            strict_json_only=True,
        )
        is None
    )


def test_duplicate_json_object_keys_are_rejected_in_legacy_extraction_mode():
    parser = LLMResponseParser()
    content = (
        '{"question_id":"IQS_06","question_id":"IQS_05",' '"score":8,"description":"ambiguous ID"}'
    )

    assert parser.parse_structured_response(content, expected_question_id="IQS_05") is None


def test_consistent_nested_scoring_protocol_remains_compatible_with_screening_consumer():
    parser = LLMResponseParser()
    nested = {
        "id": "IQS_05",
        "status": "scored",
        "score": 8,
        "confidence": "high",
        "rationale": "offline compatibility fixture",
    }
    description = json.dumps(nested)
    content = json.dumps(
        {
            "question_id": "IQS_05",
            "status": "scored",
            "score": 8,
            "description": description,
        }
    )

    parsed = parser.parse_structured_response(content, expected_question_id="IQS_05")

    assert parsed is not None
    assert parsed.score == 8
    assert parsed.description == description


@pytest.mark.parametrize(
    "content",
    [
        (
            '{"question_id":"IQS_06","question_id":"IQS_05","score":8,'
            '"description":"ambiguous outer answer",'
            '"metadata":{"question_id":"IQS_05","score":9,"description":"not the answer"}}'
        ),
        (
            "Model response:\n```json\n"
            '{"question_id":"IQS_06","question_id":"IQS_05","score":8,'
            '"description":"ambiguous outer answer",'
            '"metadata":{"question_id":"IQS_05","score":9,"description":"not the answer"}}'
            "\n```"
        ),
    ],
    ids=["direct-object", "markdown-wrapped-object"],
)
def test_duplicate_outer_keys_cannot_fall_back_to_nested_metadata_answer(content):
    parser = LLMResponseParser()

    assert parser.parse_structured_response(content, expected_question_id="IQS_05") is None
