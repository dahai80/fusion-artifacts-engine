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
    # render_share_html only reads .type / .name; SimpleNamespace avoids building a full Artifact
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
    # Empty string triggers lxml ParserError -> fail-closed escapes the raw text
    assert _sanitize_html_lxml("") == ""


def test_sanitize_lxml_malformed_does_not_raise():
    # Malformed HTML does not raise; an escaped string is returned
    out = _sanitize_html_lxml("<div><p>unclosed")
    assert isinstance(out, str)


# ── _sanitize_html_regex: direct call (lines 76-80) ──────────


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


# ── _sanitize_html dispatcher: regex fallback warning (lines 86-87) ──


def test_sanitize_html_regex_fallback_warning(monkeypatch, caplog):
    monkeypatch.setattr(render_mod, "_LXML_AVAILABLE", False)
    with caplog.at_level("WARNING", logger="fusion_artifacts_engine.render"):
        out = _sanitize_html("<script>x</script>")
    assert "</script>" not in out.lower()
    assert any("regex fallback" in r.message for r in caplog.records)


# ── lxml ImportError fallback (lines 29-32) ───────────────


def test_lxml_import_error_fallback_emits_warning(caplog):
    # Simulate lxml unavailable: sys.modules['lxml']=None triggers ImportError, reload render
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
        # Restore sys.modules and reload back to the normal state, avoiding pollution of later tests
        for key in list(sys.modules):
            if key == "lxml" or key.startswith("lxml."):
                if key in saved:
                    sys.modules[key] = saved[key]
                else:
                    del sys.modules[key]
        importlib.reload(render_mod)
        assert render_mod._LXML_AVAILABLE is True


# ── render_share_html: data type branch (lines 121-133) ──────


def test_render_share_data_valid_json():
    art = _make_artifact("data", name="data.json")
    out = render_share_html(art, '{"a": 1, "b": [2, 3]}')
    assert "<!DOCTYPE html>" in out
    # Valid JSON takes the pretty branch: key names should appear in the output
    assert '"a"' in out or "a" in out


def test_render_share_data_invalid_json(caplog):
    art = _make_artifact("data", name="bad.json")
    with caplog.at_level("WARNING", logger="fusion_artifacts_engine.render"):
        out = render_share_html(art, "not valid json{{{")
    # Invalid JSON takes the except branch: raw text escaped
    assert "not valid json" in out
    assert any("not valid JSON" in r.message for r in caplog.records)


def test_render_share_data_empty_content():
    art = _make_artifact("data", name="empty.json")
    out = render_share_html(art, "   ")
    # Empty content takes the else branch: empty string escaped
    assert "<!DOCTYPE html>" in out
    assert "<body>" in out


# ── render_share_html: svg type branch (lines 140-141) ───────


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
