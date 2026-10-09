import json

import pytest

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.methods import RPCHandler

_DSL = {
    "meta": {
        "id": "rpc_01",
        "grade": 3,
        "topic": "RPC渲染测试",
        "visual_type": "flow_card",
        "fallback_type": "flow_card",
    },
    "parameters": {
        "a": {"label": "A", "value": 30, "unit": "个"},
        "b": {"label": "B", "value": 20, "unit": "个"},
    },
    "entities": [{"id": "ent_0", "type": "item", "label": "物品", "properties": {}}],
    "pipeline": [
        {
            "step": 1,
            "title": "求和",
            "formula": "30+20",
            "eval": "50",
            "result_unit": "个",
            "visual_state": {
                "progress_range": [0.0, 1.0],
                "active_entities": ["ent_0"],
                "action": "show",
            },
        }
    ],
}

_DSL_STR = json.dumps(_DSL, ensure_ascii=False)
_RAW_OUTPUT = f"<thinking>\n分析题意\n</thinking>\n<dsl>\n{_DSL_STR}\n</dsl>"


@pytest.fixture
def engine(tmp_path):
    config = ArtifactEngineConfig(storage_root=tmp_path / "artifacts")
    return ArtifactEngine(config)


@pytest.fixture
def rpc_handler(engine):
    return RPCHandler(engine)


def _valid_html(s):
    return isinstance(s, str) and "<!DOCTYPE html>" in s and "</html>" in s


def test_engine_render_visual_with_raw_text(engine):
    result = engine.render_visual(raw_text=_RAW_OUTPUT, progress=0.5)
    assert _valid_html(result["html"])
    assert result["visual_type"] == "flow_card"
    assert result["valid"] is True
    assert result["thinking"] == "分析题意"


def test_engine_render_visual_with_dsl(engine):
    result = engine.render_visual(dsl=_DSL, progress=1.0)
    assert _valid_html(result["html"])
    assert result["visual_type"] == "flow_card"
    assert result["valid"] is True


def test_engine_render_visual_empty_input(engine):
    result = engine.render_visual(raw_text="", progress=0.5)
    assert _valid_html(result["html"])
    assert result["valid"] is False
    assert "empty input" in result["errors"]


def test_engine_render_visual_none_input(engine):
    result = engine.render_visual(raw_text=None, dsl=None, progress=0.5)
    assert _valid_html(result["html"])


def test_engine_render_visual_invalid_dsl(engine):
    result = engine.render_visual(raw_text="<dsl>\n{bad json}\n</dsl>", progress=0.5)
    assert _valid_html(result["html"])
    assert result["valid"] is False


async def test_rpc_render_visual_method(rpc_handler):
    result = await rpc_handler.dispatch("render.visual", {"raw_text": _RAW_OUTPUT, "progress": 0.5})
    assert _valid_html(result["html"])
    assert result["visual_type"] == "flow_card"


async def test_rpc_render_visual_with_dsl_param(rpc_handler):
    result = await rpc_handler.dispatch("render.visual", {"dsl": _DSL, "progress": 1.0})
    assert _valid_html(result["html"])


async def test_rpc_render_visual_invalid_progress(rpc_handler):
    result = await rpc_handler.dispatch("render.visual", {"dsl": _DSL, "progress": "not_a_number"})
    assert _valid_html(result["html"])


def test_rpc_render_visual_method_registered(rpc_handler):
    method_map = rpc_handler._build_methods()
    assert "render.visual" in method_map
