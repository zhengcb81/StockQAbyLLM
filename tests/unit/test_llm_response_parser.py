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


# Question-pack parsing is a bounded parser primitive, not a dispatch/ACK claim.
def _question_pack_item(qid, **changes):
    item = {
        "question_id": qid,
        "entity_id": "issuer:pack-fixture",
        "company_name": "Pack Fixture Corp",
        "status": "scored",
        "score": 8,
        "description": "independent synthetic evidence",
    }
    item.update(changes)
    return item


def _parse_question_pack(content, expected_ids=("IQS_01", "IQS_02", "IQS_03")):
    return LLMResponseParser().parse_question_pack_response(
        content,
        expected_ids,
        expected_entity_id="issuer:pack-fixture",
        expected_company_name="Pack Fixture Corp",
    )


@pytest.mark.parametrize("count", [1, 3, 30])
def test_question_pack_preserves_all_answers_in_frozen_expected_order(count):
    from dataclasses import FrozenInstanceError

    ids = tuple(f"IQS_{i:02d}" for i in range(1, count + 1))
    content = json.dumps({"answers": [_question_pack_item(qid) for qid in reversed(ids)]})
    result = _parse_question_pack(content, ids)
    assert result is not None
    assert tuple(answer.question_id for answer in result.answers) == ids
    assert tuple(answer.score for answer in result.answers) == (8,) * count
    assert result.missing_question_ids == ()
    assert result.invalid_question_ids == ()
    assert isinstance(result.answers, tuple)
    with pytest.raises(FrozenInstanceError):
        result.answers = ()
    with pytest.raises(FrozenInstanceError):
        result.answers[0].score = 5


def test_question_pack_retains_valid_answers_without_filling_missing_or_invalid_members():
    content = json.dumps(
        {
            "answers": [
                _question_pack_item("IQS_01"),
                _question_pack_item("IQS_02", status="unknown", score=5),
            ]
        }
    )
    result = _parse_question_pack(content)
    assert result is not None
    assert tuple(answer.question_id for answer in result.answers) == ("IQS_01",)
    assert result.invalid_question_ids == ("IQS_02",)
    assert result.missing_question_ids == ("IQS_03",)


@pytest.mark.parametrize("status", ["unknown", "insufficient_evidence", "not_applicable"])
def test_question_pack_preserves_nullable_non_scored_members(status):
    content = json.dumps({"answers": [_question_pack_item("IQS_01", status=status, score=None)]})
    result = _parse_question_pack(content)
    assert result is not None
    assert result.answers[0].status == status
    assert result.answers[0].score is None
    assert result.missing_question_ids == ("IQS_02", "IQS_03")


def test_question_pack_distinguishes_empty_partial_response_from_structural_rejection():
    result = _parse_question_pack('{"answers":[]}')
    assert result is not None
    assert result.answers == ()
    assert result.missing_question_ids == ("IQS_01", "IQS_02", "IQS_03")
    assert result.invalid_question_ids == ()


@pytest.mark.parametrize(
    "changes",
    [
        {"score": True},
        {"score": 1.5},
        {"description": ""},
        {"description": json.dumps({"id": "IQS_02", "status": "scored", "score": 3})},
        {"description": json.dumps({"id": "IQS_03", "status": "scored", "score": 8})},
        {"description": '{"id":"IQS_02","id":"IQS_02","status":"scored","score":8}'},
    ],
)
def test_question_pack_reuses_single_answer_rejections_per_identified_member(changes):
    content = json.dumps(
        {
            "answers": [
                _question_pack_item("IQS_01"),
                _question_pack_item("IQS_02", **changes),
            ]
        }
    )
    result = _parse_question_pack(content)
    assert result is not None
    assert tuple(answer.question_id for answer in result.answers) == ("IQS_01",)
    assert result.invalid_question_ids == ("IQS_02",)
    assert result.missing_question_ids == ("IQS_03",)


@pytest.mark.parametrize(
    "content",
    [
        "null",
        "[]",
        "{}",
        '{"answers":{}}',
        '{"answers":[],"reasoning":"hidden"}',
        '{"answers":[],"answers":[]}',
        '{"answers":[{"question_id":"IQS_01","question_id":"IQS_01","score":8,"description":"x"}]}',
        '{"answers":[{"question_id":"IQS_01","score":8,"description":"x","metadata":{"x":1,"x":2}}]}',
        '{"answers":[{"question_id":"IQS_01","score":8,"description":"x","metadata":{"value":NaN}}]}',
        '{"answers":[null]}',
        '{"answers":[{"score":8,"description":"missing ID"}]}',
        '<think>private analysis</think>{"answers":[]}',
        '```json\n{"answers":[]}\n```',
        '{"answers":[]} trailing text',
        '{"answers":[]} {"answers":[]}',
        '{"answers":[' + "[" * 1200 + "0" + "]" * 1200 + "]}",
    ],
)
def test_question_pack_rejects_whole_root_ambiguity_or_thinking_pollution(content):
    assert _parse_question_pack(content) is None


@pytest.mark.parametrize(
    "bad_member",
    [
        _question_pack_item("EXTRA_01"),
        _question_pack_item("IQS_01"),
        _question_pack_item("IQS_02", entity_id="issuer:other"),
        _question_pack_item("IQS_02", company_name="Other Corp"),
        _question_pack_item("IQS_02", company_name=["Other Corp"]),
    ],
)
def test_question_pack_rejects_entire_response_for_extra_duplicate_or_wrong_company(bad_member):
    content = json.dumps({"answers": [_question_pack_item("IQS_01"), bad_member]})
    assert _parse_question_pack(content) is None


@pytest.mark.parametrize(
    "expected_ids",
    [
        None,
        "IQS_01",
        [],
        ["IQS_01", "IQS_01"],
        [""],
        [" IQS_01"],
        [None],
        [True],
        [[]],
        {"IQS_01"},
        ["IQS_" + "1" * 160],
        [f"IQS_{i:02d}" for i in range(31)],
    ],
)
def test_question_pack_rejects_invalid_or_unbounded_expected_ids(expected_ids):
    with pytest.raises(ValueError, match="expected_question_ids"):
        _parse_question_pack('{"answers":[]}', expected_ids)


@pytest.mark.parametrize(
    "content",
    [
        None,
        b'{"answers":[]}',
        "x" * (1024 * 1024 + 1),
        json.dumps(
            {"answers": [_question_pack_item("IQS_01", description="中" * 400000)]},
            ensure_ascii=False,
        ),
    ],
    ids=["not-text", "bytes", "ascii-over-limit", "utf8-over-limit"],
)
def test_question_pack_rejects_invalid_or_oversized_content(content):
    assert _parse_question_pack(content) is None


def test_question_pack_optional_identity_binding_reuses_single_answer_interface():
    content = '{"answers":[{"question_id":"IQS_01","status":"scored","score":8,"description":"x"}]}'
    result = LLMResponseParser().parse_question_pack_response(content, ["IQS_01"])
    assert result is not None
    assert result.answers[0].question_id == "IQS_01"
    assert result.answers[0].entity_id is None


@pytest.mark.parametrize(
    "field,value", [("entity_id", "issuer:other"), ("company_name", "Other Corp")]
)
def test_question_pack_cannot_mix_declared_identities_without_a_caller_binding(field, value):
    content = json.dumps(
        {
            "answers": [
                _question_pack_item("IQS_01"),
                _question_pack_item("IQS_02", **{field: value}),
            ]
        }
    )
    assert LLMResponseParser().parse_question_pack_response(content, ["IQS_01", "IQS_02"]) is None


def test_question_pack_rejects_overflowed_json_number_without_raising():
    content = '{"answers":[{"question_id":"IQS_01","score":8,"description":"x","metadata":{"value":1e999}}]}'
    assert _parse_question_pack(content) is None


@pytest.mark.parametrize(
    "content,code,pointer",
    [
        ('```json\n{"answers":[]}\n```', "invalid_json", ""),
        ('{"answers":[],"private_field":"not exported"}', "pack_root_fields", ""),
        ('{"answers":[],"answers":[]}', "duplicate_json_key", ""),
    ],
)
def test_pack_diagnostics_identify_root_failure_without_relaxing_parser(content, code, pointer):
    parser = LLMResponseParser()
    result, diagnostic = parser.parse_question_pack_response_with_diagnostics(content, ["IQS_01"])
    assert result is None and parser.parse_question_pack_response(content, ["IQS_01"]) is None
    assert {"code": code, "pointer": pointer} in diagnostic["rejections"]
    assert content not in json.dumps(diagnostic)
    assert "not exported" not in json.dumps(diagnostic)


def test_pack_diagnostics_precise_identity_and_member_fields_do_not_leak_values():
    parser = LLMResponseParser()
    foreign = _question_pack_item("IQS_01", entity_id="ENT_PRIVATE_FOREIGN")
    result, diagnostic = parser.parse_question_pack_response_with_diagnostics(
        json.dumps({"answers": [foreign]}), ["IQS_01"], expected_entity_id="ENT_EXPECTED"
    )
    assert result is None
    assert diagnostic["rejections"] == [
        {"code": "foreign_entity_id", "pointer": "/answers/0/entity_id"}
    ]
    assert "PRIVATE_FOREIGN" not in json.dumps(diagnostic)
    content = json.dumps(
        {"answers": [_question_pack_item("IQS_01", score=True), _question_pack_item("IQS_02")]}
    )
    result, diagnostic = parser.parse_question_pack_response_with_diagnostics(
        content, ["IQS_01", "IQS_02"]
    )
    assert result.invalid_question_ids == ("IQS_01",)
    assert [answer.question_id for answer in result.answers] == ["IQS_02"]
    assert {
        "code": "invalid_score",
        "pointer": "/answers/0/score",
        "question_id": "IQS_01",
    } in diagnostic["rejections"]
    clean, clean_diagnostic = parser.parse_question_pack_response_with_diagnostics(
        json.dumps({"answers": [_question_pack_item("IQS_02")]}), ["IQS_02"]
    )
    assert clean is not None and clean_diagnostic["rejections"] == []
