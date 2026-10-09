import pytest

from fusion_artifacts_engine.visual_primitives import (
    COMPILER_REGISTRY,
    VALID_VISUAL_TYPES,
    ArrayGridCompiler,
    BucketDividerCompiler,
    DataChartCompiler,
    FlowCardCompiler,
    Geometry2DCompiler,
    Isometric3DCompiler,
    TapeDiagramCompiler,
    TrackTimelineCompiler,
    get_compiler,
)

_DSL = {
    "meta": {
        "id": "vis_01",
        "grade": 4,
        "topic": "测试可视化",
        "visual_type": "array_grid",
        "fallback_type": "tape_diagram",
    },
    "parameters": {
        "count": {"label": "总数", "value": 12, "unit": "个"},
        "rows": {"label": "行", "value": 3, "unit": "行"},
        "cols": {"label": "列", "value": 4, "unit": "列"},
        "a": {"label": "A", "value": 30, "unit": "个"},
        "b": {"label": "B", "value": 20, "unit": "个"},
    },
    "entities": [
        {"id": "dot_0", "type": "dot", "label": "点1", "properties": {}},
        {"id": "runner_a", "type": "runner", "label": "甲", "properties": {"speed": 60}},
        {
            "id": "box_0",
            "type": "box",
            "label": "方块",
            "properties": {"width": 2, "height": 2, "depth": 2},
        },
    ],
    "pipeline": [
        {
            "step": 1,
            "title": "步骤一",
            "formula": "3×4",
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
            "title": "步骤二",
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


def _valid_html(html_str: str) -> bool:
    return isinstance(html_str, str) and "<!DOCTYPE html>" in html_str and "</html>" in html_str


@pytest.mark.parametrize(
    "compiler_cls",
    [
        ArrayGridCompiler,
        TapeDiagramCompiler,
        Geometry2DCompiler,
        Isometric3DCompiler,
        TrackTimelineCompiler,
        BucketDividerCompiler,
        DataChartCompiler,
        FlowCardCompiler,
    ],
)
def test_each_compiler_produces_valid_html(compiler_cls):
    compiler = compiler_cls()
    html_out = compiler.compile(_DSL, progress=0.5)
    assert _valid_html(html_out), f"{compiler_cls.__name__} did not produce valid HTML"


def test_registry_has_8_types():
    assert len(COMPILER_REGISTRY) == 8
    assert len(VALID_VISUAL_TYPES) == 8
    expected = {
        "array_grid",
        "tape_diagram",
        "geometry_2d",
        "isometric_3d",
        "track_timeline",
        "bucket_divider",
        "data_chart",
        "flow_card",
    }
    assert set(COMPILER_REGISTRY.keys()) == expected


def test_get_compiler_known_type():
    c = get_compiler("array_grid")
    assert isinstance(c, ArrayGridCompiler)


def test_get_compiler_unknown_type_falls_back():
    c = get_compiler("nonexistent_type")
    assert isinstance(c, FlowCardCompiler)


def test_flow_card_never_throws_empty_dict():
    compiler = FlowCardCompiler()
    html_out = compiler.compile({}, progress=0.5)
    assert _valid_html(html_out)


def test_flow_card_never_throws_none():
    compiler = FlowCardCompiler()
    html_out = compiler.compile(None, progress=0.5)
    assert _valid_html(html_out)


def test_flow_card_never_throws_empty_pipeline():
    compiler = FlowCardCompiler()
    dsl = {"meta": {"id": "x", "topic": "空"}, "parameters": {}, "entities": [], "pipeline": []}
    html_out = compiler.compile(dsl, progress=0.5)
    assert _valid_html(html_out)
    assert "暂无" in html_out or "步骤" in html_out


def test_tape_diagram_handles_generic_quantities():
    compiler = TapeDiagramCompiler()
    dsl = {
        "meta": {"id": "x", "visual_type": "tape_diagram"},
        "parameters": {
            "x": {"label": "X", "value": 50, "unit": "个"},
            "y": {"label": "Y", "value": 30, "unit": "个"},
        },
        "entities": [],
        "pipeline": [],
    }
    html_out = compiler.compile(dsl, progress=1.0)
    assert _valid_html(html_out)
    assert "50" in html_out
    assert "30" in html_out


def test_isometric_3d_uses_threejs_cdn():
    compiler = Isometric3DCompiler()
    html_out = compiler.compile(_DSL, progress=1.0)
    assert "three" in html_out.lower()
    assert "cdn" in html_out.lower() or "jsdelivr" in html_out.lower()


def test_data_chart_uses_chartjs_cdn():
    compiler = DataChartCompiler()
    html_out = compiler.compile(_DSL, progress=1.0)
    assert "chart.js" in html_out.lower() or "chart.umd" in html_out.lower()


def test_progress_zero_shows_initial():
    compiler = FlowCardCompiler()
    html0 = compiler.compile(_DSL, progress=0.0)
    html1 = compiler.compile(_DSL, progress=1.0)
    assert _valid_html(html0)
    assert _valid_html(html1)


def test_active_entities_highlighted():
    compiler = ArrayGridCompiler()
    html_out = compiler.compile(_DSL, progress=0.25)
    assert "dot_0" in html_out
