import html
import logging
import secrets

logger = logging.getLogger(__name__)

# H3: _sanitize_html 用真实 HTML 解析器（lxml Cleaner）替换正则净化。
# 正则无法处理嵌套/变体/编码绕过（如 <scr<script>ipt>），lxml 解析树后剥危险节点。

try:
    from lxml import etree
    from lxml import html as lxml_html
    from lxml.html.clean import Cleaner

    _LXML_AVAILABLE = True
    # lxml_html_clean 无 events 参数：on* 事件属性由 javascript=True + safe_attrs_only=True 剥除
    _cleaner = Cleaner(
        scripts=True,
        javascript=True,
        inline_style=True,
        style=True,
        comments=True,
        processing_instructions=True,
        forms=False,
        annoying_tags=True,
        remove_unknown_tags=False,
        safe_attrs_only=True,
        meta=False,
    )
except ImportError:
    _LXML_AVAILABLE = False
    _cleaner = None
    logger.warning(
        "lxml/html_clean not available, _sanitize_html falls back to regex "
        "(less secure). Install lxml[html-clean]."
    )


# LOW-5/CWE-79: 去掉 style-src 'unsafe-inline'，改每渲染一次性 nonce。
# {nonce} 由 _gen_nonce 注入；同一文档的 CSP 声明与 <style nonce="..."> 必须一致。
_SHARE_DOC_CSP_TMPL = (
    "default-src 'none'; "
    "script-src 'none'; "
    "style-src 'nonce-{nonce}'; "
    "img-src data:; "
    "font-src data:; "
    "connect-src 'none';"
)


def _gen_nonce() -> str:
    # LOW-5: 单次渲染随机 nonce。token_urlsafe(16) → ~22 字符 URL 安全串。
    return secrets.token_urlsafe(16)


def _html_escape(text: str) -> str:
    return html.escape(text or "", quote=True)


def _sanitize_html_lxml(html_str: str) -> str:
    # H3: 解析 HTML 树 -> Cleaner 剥 script/event/style/危险协议 -> 序列化。
    # 解析失败（畸形 HTML）fail-closed：返回转义原文，绝不裸放危险内容。
    try:
        doc = lxml_html.fromstring(html_str)
    except (etree.ParserError, etree.XMLSyntaxError, ValueError) as e:
        logger.warning("lxml parse failed, sanitize escaped raw: %s", e)
        return _html_escape(html_str)
    _cleaner(doc)
    result = lxml_html.tostring(doc, encoding="unicode")
    # tostring 可能包 <p>..</p> 或片段；strip 外层多余换行
    return result.strip()


# 回退正则——仅 lxml 不可用时用。保留以兼容旧测试与降级场景。
import re

_SCRIPT_TAG_RE = re.compile(r"<\s*script\b[^>]*>.*?<\s*/\s*script\s*>", re.IGNORECASE | re.DOTALL)
_SCRIPT_OPEN_RE = re.compile(r"<\s*script\b", re.IGNORECASE)
_EVENT_ATTR_RE = re.compile(r"\son\w+\s*=\s*([^\s>]+|'[^']*'|\"[^\"]*\")", re.IGNORECASE)
_JS_PROTO_RE = re.compile(r"javascript:", re.IGNORECASE)


def _sanitize_html_regex(html_str: str) -> str:
    html_str = _SCRIPT_TAG_RE.sub("", html_str)
    html_str = _SCRIPT_OPEN_RE.sub("&lt;script", html_str)
    html_str = _EVENT_ATTR_RE.sub("", html_str)
    html_str = _JS_PROTO_RE.sub("", html_str)
    return html_str


def _sanitize_html(html_str: str) -> str:
    if _LXML_AVAILABLE:
        return _sanitize_html_lxml(html_str)
    logger.warning("sanitize via regex fallback (lxml missing)")
    return _sanitize_html_regex(html_str)


def render_share_html(artifact, content: str) -> str:
    # H7: 从 engine.py 抽出的分享渲染层。iframe sandbox + CSP + 净化。
    # LOW-5: 单次渲染 nonce，去掉 style-src 'unsafe-inline'，CSP 与 <style> 共用同一 nonce。
    atype = artifact.type
    title = _html_escape(artifact.name or "Shared Artifact")
    nonce = _gen_nonce()
    csp = _SHARE_DOC_CSP_TMPL.format(nonce=nonce)
    if atype in ("html", "react"):
        iframe_doc = _html_escape(_sanitize_html(content))
        return (
            f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<title>{title}</title>"
            f"<meta http-equiv='Content-Security-Policy' content=\"{csp}\">"
            f"<style nonce=\"{nonce}\">html,body,iframe{{margin:0;padding:0;height:100%;border:0}}</style>"
            f"</head><body>"
            f"<iframe sandbox srcdoc=\"{iframe_doc}\"></iframe>"
            f"</body></html>"
        )
    if atype == "markdown":
        import markdown

        rendered = markdown.markdown(content, extensions=["fenced_code", "tables"])
        rendered = _sanitize_html(rendered)
        return (
            f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<title>{title}</title>"
            f"<meta http-equiv='Content-Security-Policy' content=\"{csp}\">"
            f"<style nonce=\"{nonce}\">body{{font-family:system-ui,sans-serif;max-width:900px;"
            f"margin:2rem auto;padding:0 1rem;line-height:1.6}}"
            f"pre{{background:#f4f4f4;padding:1rem;overflow:auto;border-radius:4px}}"
            f"code{{background:#f4f4f4;padding:2px 6px;border-radius:3px}}</style>"
            f"</head><body>{rendered}</body></html>"
        )
    if atype == "data":
        import json as _json

        if content.strip():
            try:
                pretty = _html_escape(
                    _json.dumps(_json.loads(content), indent=2, ensure_ascii=False)
                )
            except (ValueError, TypeError):
                logger.warning("data share content not valid JSON, render escaped raw")
                pretty = _html_escape(content)
        else:
            pretty = _html_escape(content)
        return (
            f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<title>{title}</title>"
            f"<style nonce=\"{nonce}\">body{{font-family:monospace;white-space:pre;padding:1rem}}</style>"
            f"</head><body>{pretty}</body></html>"
        )
    if atype == "svg":
        iframe_doc = _html_escape(_sanitize_html(content))
        return (
            f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<title>{title}</title>"
            f"<meta http-equiv='Content-Security-Policy' content=\"{csp}\">"
            f"<style nonce=\"{nonce}\">html,body,iframe{{margin:0;padding:0;height:100%;border:0}}</style>"
            f"</head><body>"
            f"<iframe sandbox srcdoc=\"{iframe_doc}\"></iframe>"
            f"</body></html>"
        )
    escaped = _html_escape(content)
    return (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title>"
        f"<style nonce=\"{nonce}\">body{{font-family:monospace;white-space:pre-wrap;"
        f"padding:1rem;overflow:auto}}</style>"
        f"</head><body>{escaped}</body></html>"
    )
