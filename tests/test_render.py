import importlib
import sys
import types

from fusion_artifacts_engine import render as render_mod
from fusion_artifacts_engine.render import (
    _html_escape,
    _sanitize_html,
    _sanitize_html_lxml,
    _sanitize_html_regex,
    render_share_html,
)


def _make_artifact(atype, name="shared"):
    # render_share_html 仅读 .type / .name，用 SimpleNamespace 免造完整 Artifact
    return types.SimpleNamespace(type=atype, name=name)


# ── _html_escape ───────────────────────────────────────────


def test_html_escape_none_and_basic():
    assert _html_escape(None) == ""
    assert _html_escape("<b>") == "&lt;b&gt;"
    assert _html_escape('"x"') == "&quot;x&quot;"


# ── _sanitize_html_lxml: parse-fail fail-closed (lines 57-59) ──


def test_sanitize_lxml_strips_script():
    out = _sanitize_html_lxml("<p>ok</p><script>alert(1)</script>")
    assert "<script" not in out.lower()
    assert "alert" not in out
    assert "ok" in out


def test_sanitize_lxml_empty_returns_escaped_raw():
    # 空串触发 lxml ParserError -> fail-closed 转义原文
    assert _sanitize_html_lxml("") == ""


def test_sanitize_lxml_malformed_does_not_raise():
    # 畸形 HTML 不抛异常，返回转义字符串
    out = _sanitize_html_lxml("<div><p>unclosed")
    assert isinstance(out, str)


# ── _sanitize_html_regex: 直接调用 (lines 76-80) ──────────


def test_sanitize_regex_strips_script_tag():
    out = _sanitize_html_regex("<p>ok</p><script>alert(1)</script>")
    assert "</script>" not in out.lower()


def test_sanitize_regex_escapes_open_script():
    out = _sanitize_html_regex("<script evil>")
    assert "<script" not in out.lower()


def test_sanitize_regex_strips_event_attr():
    out = _sanitize_html_regex('<div onclick="evil()">x</div>')
    assert "onclick" not in out.lower()


def test_sanitize_regex_strips_js_proto():
    out = _sanitize_html_regex('<a href="javascript:evil">x</a>')
    assert "javascript:" not in out.lower()


# ── _sanitize_html dispatcher: regex fallback 警告 (lines 86-87) ──


def test_sanitize_html_regex_fallback_warning(monkeypatch, caplog):
    monkeypatch.setattr(render_mod, "_LXML_AVAILABLE", False)
    with caplog.at_level("WARNING", logger="fusion_artifacts_engine.render"):
        out = _sanitize_html("<script>x</script>")
    assert "</script>" not in out.lower()
    assert any("regex fallback" in r.message for r in caplog.records)


# ── lxml ImportError fallback (lines 29-32) ───────────────


def test_lxml_import_error_fallback_emits_warning(caplog):
    # 模拟 lxml 不可用：sys.modules['lxml']=None 触发 ImportError，重载 render
    saved = {}
    for key in list(sys.modules):
        if key == "lxml" or key.startswith("lxml."):
            saved[key] = sys.modules[key]
    try:
        sys.modules["lxml"] = None
        sys.modules["lxml.html"] = None
        sys.modules["lxml.html.clean"] = None
        sys.modules["lxml.etree"] = None
        with caplog.at_level("WARNING", logger="fusion_artifacts_engine.render"):
            importlib.reload(render_mod)
        assert render_mod._LXML_AVAILABLE is False
        assert render_mod._cleaner is None
        assert any("falls back to regex" in r.message for r in caplog.records)
    finally:
        # 恢复 sys.modules 并重载回正常状态，避免污染后续测试
        for key in list(sys.modules):
            if key == "lxml" or key.startswith("lxml."):
                if key in saved:
                    sys.modules[key] = saved[key]
                else:
                    del sys.modules[key]
        importlib.reload(render_mod)
        assert render_mod._LXML_AVAILABLE is True


# ── render_share_html: data 类型分支 (lines 121-133) ──────


def test_render_share_data_valid_json():
    art = _make_artifact("data", name="data.json")
    out = render_share_html(art, '{"a": 1, "b": [2, 3]}')
    assert "<!DOCTYPE html>" in out
    # 合法 JSON 走 pretty 分支：键名应出现在输出
    assert '"a"' in out or "a" in out


def test_render_share_data_invalid_json(caplog):
    art = _make_artifact("data", name="bad.json")
    with caplog.at_level("WARNING", logger="fusion_artifacts_engine.render"):
        out = render_share_html(art, "not valid json{{{")
    # 非法 JSON 走 except 分支：转义原文
    assert "not valid json" in out
    assert any("not valid JSON" in r.message for r in caplog.records)


def test_render_share_data_empty_content():
    art = _make_artifact("data", name="empty.json")
    out = render_share_html(art, "   ")
    # 空内容走 else 分支：转义空串
    assert "<!DOCTYPE html>" in out
    assert "<body>" in out


# ── render_share_html: svg 类型分支 (lines 140-141) ───────


def test_render_share_svg():
    art = _make_artifact("svg", name="drawing.svg")
    svg_content = '<svg xmlns="http://www.w3.org/2000/svg"><circle r="10"/></svg>'
    out = render_share_html(art, svg_content)
    assert "<!DOCTYPE html>" in out
    assert "iframe" in out
    assert "sandbox" in out
    assert "srcdoc" in out


def test_render_share_svg_sanitizes_script():
    art = _make_artifact("svg", name="evil.svg")
    evil = '<svg><script>alert(1)</script></svg>'
    out = render_share_html(art, evil)
    assert "alert" not in out
