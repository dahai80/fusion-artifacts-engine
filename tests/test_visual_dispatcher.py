import json

import pytest

from fusion_artifacts_engine.dsl_parser import ParsedDSL, parse_model_output
from fusion_artifacts_engine.visual_dispatcher import VisualEngineDispatcher
from fusion_artifacts_engine.visual_error_boundary import VisualErrorBoundary
from fusion_artifacts_engine.visual_primitives import (
    FlowCardCompiler,
)

_DSL = {
    "meta": {
        "id": "disp_01",
        "grade": 4,
        "topic": "调度测试",
        "visual_type": "array_grid",
        "fallback_type": "tape_diagram",
    },
    "parameters": {
        "count": {"label": "总数", "value": 12, "unit": "个"},
        "rows": {"label": "行", "value": 3, "unit": "行"},
        "cols": {"label": "列", "value": 4, "unit": "列"},
    },
    "entities": [{"id": "dot_0", "type": "dot", "label": "点", "properties": {}}],
    "pipeline": [
        {
            "step": 1,
            "title": "步骤",
            "formula": "3×4=12",
            "eval": "12",
            "result_unit": "个",
            "visual_state": {
                "progress_range": [0.0, 1.0],
                "active_entities": ["dot_0"],
                "action": "show",
            },
        }
    ],
}


def _valid_html(s):
    return isinstance(s, str) and "<!DOCTYPE html>" in s and "</html>" in s


@pytest.mark.parametrize(
    "vt",
    [
        "array_grid",
        "tape_diagram",
        "geometry_2d",
        "isometric_3d",
        "track_timeline",
        "bucket_divider",
        "data_chart",
        "flow_card",
    ],
)
def test_dispatcher_routes_all_8_types(vt):
    dsl = json.loads(json.dumps(_DSL))
    dsl["meta"]["visual_type"] = vt
    dispatcher = VisualEngineDispatcher()
    html_out = dispatcher.dispatch(dsl, progress=0.5)
    assert _valid_html(html_out)


def test_dispatcher_invalid_visual_type_falls_back():
    dsl = json.loads(json.dumps(_DSL))
    dsl["meta"]["visual_type"] = "nonexistent"
    dispatcher = VisualEngineDispatcher()
    html_out = dispatcher.dispatch(dsl, progress=0.5)
    assert _valid_html(html_out)


def test_dispatcher_none_dsl_returns_flowcard():
    dispatcher = VisualEngineDispatcher()
    html_out = dispatcher.dispatch(None, progress=0.5)
    assert _valid_html(html_out)


def test_dispatcher_empty_dsl_returns_flowcard():
    dispatcher = VisualEngineDispatcher()
    html_out = dispatcher.dispatch({}, progress=0.5)
    assert _valid_html(html_out)


def test_error_boundary_catches_exception():
    class _ExplodingCompiler:
        def compile(self, dsl, progress=1.0):
            raise RuntimeError("boom")

        __class__name__ = "ExplodingCompiler"

    boundary = VisualErrorBoundary(FlowCardCompiler())
    html_out = boundary.render(_ExplodingCompiler(), _DSL, progress=0.5)
    assert _valid_html(html_out)


def test_error_boundary_returns_html_on_empty_output():
    class _EmptyCompiler:
        def compile(self, dsl, progress=1.0):
            return ""

    boundary = VisualErrorBoundary(FlowCardCompiler())
    html_out = boundary.render(_EmptyCompiler(), _DSL, progress=0.5)
    assert _valid_html(html_out)


def test_error_boundary_fallback_also_fails():
    class _ExplodingCompiler:
        def compile(self, dsl, progress=1.0):
            raise RuntimeError("primary boom")

    class _ExplodingFallback:
        def compile(self, dsl, progress=1.0):
            raise RuntimeError("fallback boom")

    boundary = VisualErrorBoundary(_ExplodingFallback())
    html_out = boundary.render(_ExplodingCompiler(), _DSL, progress=0.5)
    assert _valid_html(html_out)
    assert "failed" in html_out.lower() or "error" in html_out.lower()


def test_dispatcher_3_level_fallback_primary_throws():
    class _SpyDispatcher(VisualEngineDispatcher):
        pass

    dispatcher = _SpyDispatcher()

    def exploding_get(vt):
        class _Boom:
            def compile(self, dsl, progress=1.0):
                raise RuntimeError("primary fail")

        return _Boom()

    import fusion_artifacts_engine.visual_dispatcher as vd_mod

    original = vd_mod.get_compiler
    vd_mod.get_compiler = exploding_get
    try:
        html_out = dispatcher.dispatch(_DSL, progress=0.5)
        assert _valid_html(html_out)
    finally:
        vd_mod.get_compiler = original


def test_dispatcher_fallback_type_override_flow_card():
    dsl = json.loads(json.dumps(_DSL))
    dsl["meta"]["visual_type"] = "array_grid"
    dsl["meta"]["fallback_type"] = "flow_card"
    dispatcher = VisualEngineDispatcher()
    import fusion_artifacts_engine.visual_dispatcher as vd_mod

    original = vd_mod.get_compiler

    def exploding_get(vt):
        class _Boom:
            def compile(self, dsl, progress=1.0):
                raise RuntimeError("primary fail")

        return _Boom()

    vd_mod.get_compiler = exploding_get
    try:
        html_out = dispatcher.dispatch(dsl, progress=0.5)
        assert _valid_html(html_out)
    finally:
        vd_mod.get_compiler = original


def test_dispatch_parsed_invalid_dsl():
    dispatcher = VisualEngineDispatcher()
    parsed = ParsedDSL(valid=False, errors=["test error"])
    html_out = dispatcher.dispatch_parsed(parsed, progress=0.5)
    assert _valid_html(html_out)


def test_dispatch_parsed_valid_dsl():
    dispatcher = VisualEngineDispatcher()
    parsed = parse_model_output(
        f"<thinking>ok</thinking>\n<dsl>\n{json.dumps(_DSL, ensure_ascii=False)}\n</dsl>"
    )
    assert parsed.valid is True
    html_out = dispatcher.dispatch_parsed(parsed, progress=0.5)
    assert _valid_html(html_out)
