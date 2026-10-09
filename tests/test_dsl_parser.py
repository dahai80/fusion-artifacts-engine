import json

from fusion_artifacts_engine.dsl_parser import (
    ParsedDSL,
    extract_visual_type,
    get_pipeline_progress,
    parse_dsl_json,
    parse_model_output,
)

_VALID_DSL = {
    "meta": {
        "id": "test_01",
        "grade": 3,
        "topic": "乘法阵列",
        "visual_type": "array_grid",
        "fallback_type": "tape_diagram",
    },
    "parameters": {
        "count": {"label": "总数", "value": 12, "unit": "个"},
        "rows": {"label": "行", "value": 3, "unit": "行"},
        "cols": {"label": "列", "value": 4, "unit": "列"},
    },
    "entities": [{"id": "dot_0", "type": "dot", "label": "点1", "properties": {}}],
    "pipeline": [
        {
            "step": 1,
            "title": "展示阵列",
            "formula": "3×4=12",
            "eval": "12",
            "result_unit": "个",
            "visual_state": {
                "progress_range": [0.0, 0.5],
                "active_entities": ["dot_0"],
                "action": "show",
            },
        },
        {
            "step": 2,
            "title": "计算结果",
            "formula": "=12",
            "eval": "12",
            "result_unit": "个",
            "visual_state": {
                "progress_range": [0.5, 1.0],
                "active_entities": [],
                "action": "highlight",
            },
        },
    ],
}

_VALID_DSL_STR = json.dumps(_VALID_DSL, ensure_ascii=False)
_VALID_OUTPUT = f"<thinking>\n分析题意\n</thinking>\n<dsl>\n{_VALID_DSL_STR}\n</dsl>"


def test_parse_model_output_valid():
    parsed = parse_model_output(_VALID_OUTPUT)
    assert isinstance(parsed, ParsedDSL)
    assert parsed.valid is True
    assert parsed.errors == []
    assert parsed.thinking == "分析题意"
    assert parsed.dsl is not None
    assert parsed.visual_type == "array_grid"
    assert parsed.fallback_type == "tape_diagram"


def test_parse_model_output_missing_dsl_tag():
    parsed = parse_model_output("<thinking>just thinking</thinking> no dsl here")
    assert parsed.valid is False
    assert "missing dsl tag" in parsed.errors
    assert parsed.thinking == "just thinking"


def test_parse_model_output_missing_thinking_tag():
    output = f"<dsl>\n{_VALID_DSL_STR}\n</dsl>"
    parsed = parse_model_output(output)
    assert parsed.valid is True
    assert parsed.thinking is None


def test_parse_model_output_malformed_json():
    parsed = parse_model_output("<dsl>\n{not valid json}\n</dsl>")
    assert parsed.valid is False
    assert any("json parse error" in e for e in parsed.errors)


def test_parse_model_output_schema_violation():
    bad_dsl = json.dumps({"meta": {"id": "x"}, "parameters": {}, "entities": [], "pipeline": []})
    parsed = parse_model_output(f"<dsl>\n{bad_dsl}\n</dsl>")
    assert parsed.valid is False
    assert any("schema violation" in e for e in parsed.errors)


def test_parse_model_output_empty_input():
    parsed = parse_model_output("")
    assert parsed.valid is False
    assert "empty input" in parsed.errors


def test_parse_model_output_none_input():
    parsed = parse_model_output(None)
    assert parsed.valid is False
    assert "empty input" in parsed.errors


def test_parse_dsl_json_valid():
    dsl_dict, errors = parse_dsl_json(_VALID_DSL_STR)
    assert dsl_dict is not None
    assert errors == []
    assert dsl_dict["meta"]["visual_type"] == "array_grid"


def test_parse_dsl_json_empty():
    dsl_dict, errors = parse_dsl_json("")
    assert dsl_dict is None
    assert "empty dsl string" in errors


def test_parse_dsl_json_malformed():
    dsl_dict, errors = parse_dsl_json("{broken")
    assert dsl_dict is None
    assert any("json parse error" in e for e in errors)


def test_extract_visual_type_from_meta():
    assert extract_visual_type(_VALID_DSL) == "array_grid"


def test_extract_visual_type_fallback_to_fallback_type():
    dsl = {"meta": {"id": "x", "visual_type": "", "fallback_type": "flow_card"}}
    assert extract_visual_type(dsl) == "flow_card"


def test_extract_visual_type_none_dict():
    assert extract_visual_type(None) == "flow_card"


def test_extract_visual_type_empty_meta():
    assert extract_visual_type({}) == "flow_card"


def test_get_pipeline_progress_valid():
    ranges = get_pipeline_progress(_VALID_DSL)
    assert len(ranges) == 2
    assert ranges[0] == (0.0, 0.5)
    assert ranges[1] == (0.5, 1.0)


def test_get_pipeline_progress_empty():
    assert get_pipeline_progress(None) == []
    assert get_pipeline_progress({}) == []


def test_get_pipeline_progress_missing_range():
    dsl = {"pipeline": [{"step": 1, "visual_state": {}}]}
    ranges = get_pipeline_progress(dsl)
    assert ranges == [(0.0, 1.0)]


def test_no_eval_or_exec_in_output():
    # security constraint: parser must not eval/exec model output
    malicious = '<dsl>\n{"__import__": "os"}\n</dsl>'
    parsed = parse_model_output(malicious)
    assert parsed.valid is False
