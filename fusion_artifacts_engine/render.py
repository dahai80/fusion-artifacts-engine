import html
import logging
import secrets

logger = logging.getLogger(__name__)

# H3: _sanitize_html uses a real HTML parser (lxml Cleaner) instead of regex sanitization.
# Regex cannot handle nested/variant/encoded bypasses (e.g. <scr<script>ipt>); lxml parses the tree then strips dangerous nodes.

try:
    from lxml import etree
    from lxml import html as lxml_html
    from lxml.html.clean import Cleaner

    _LXML_AVAILABLE = True
    # lxml_html_clean has no events parameter: on* event attributes are stripped via javascript=True + safe_attrs_only=True
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


# LOW-5/CWE-79: drop style-src 'unsafe-inline' in favor of a one-time nonce per render.
# {nonce} is injected by _gen_nonce; the CSP declaration and the <style nonce="..."> in the same document must match.
_SHARE_DOC_CSP_TMPL = (
    "default-src 'none'; "
    "script-src 'none'; "
    "style-src 'nonce-{nonce}'; "
    "img-src data:; "
    "font-src data:; "
    "connect-src 'none';"
)


def _gen_nonce() -> str:
    # LOW-5: random nonce per render. token_urlsafe(16) → ~22-char URL-safe string.
    return secrets.token_urlsafe(16)


def _html_escape(text: str) -> str:
    return html.escape(text or "", quote=True)


def _sanitize_html_lxml(html_str: str) -> str:
    # H3: parse HTML tree -> Cleaner strips script/event/style/dangerous protocols -> serialize.
    # Parse failure (malformed HTML) fails closed: return the escaped original, never emit dangerous content raw.
    try:
        doc = lxml_html.fromstring(html_str)
    except (etree.ParserError, etree.XMLSyntaxError, ValueError) as e:
        logger.warning("lxml parse failed, sanitize escaped raw: %s", e)
        return _html_escape(html_str)
    _cleaner(doc)
    result = lxml_html.tostring(doc, encoding="unicode")
    # tostring may wrap in <p>..</p> or emit a fragment; strip extra outer newlines
    return result.strip()


# Fallback regex — used only when lxml is unavailable. Kept for legacy test compatibility and degraded mode.
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


def _share_footer(nonce: str, permission: str, remix_url: str | None) -> str:
    # #83: public share footer — permission badge + remix entry. CSP only allows nonce'd
    # <style>, so styles share the same nonce as the document (inline style attributes are blocked).
    if remix_url:
        action = (
            f"<a class='btn' href=\"{_html_escape(remix_url)}\">"
            f"{'Remix to my library' if permission == 'remix' else 'Open in library'}</a>"
        )
    else:
        action = ""
    return (
        f"<style nonce=\"{nonce}\">.fae-share-bar{{position:fixed;bottom:0;left:0;right:0;"
        f"display:flex;gap:12px;align-items:center;justify-content:space-between;"
        f"padding:10px 16px;background:rgba(24,24,27,.92);color:#e4e4e7;"
        f"font:13px/1.4 system-ui,sans-serif}}.fae-share-bar .perm{{opacity:.75}}"
        f".fae-share-bar .btn{{background:#6366f1;color:#fff;text-decoration:none;"
        f"padding:6px 14px;border-radius:10px;font-weight:600}}"
        f"body{{padding-bottom:56px}}</style>"
        f"<div class='fae-share-bar'><span>Shared artifact · "
        f"<span class='perm'>{_html_escape(permission)}</span></span>{action}</div>"
    )


def _append_share_footer(
    doc: str, nonce: str, permission: str, remix_url: str | None
) -> str:
    if not remix_url and permission == "view":
        return doc
    footer = _share_footer(nonce, permission, remix_url)
    return doc.replace("</body>", footer + "</body>", 1)


def render_share_html(
    artifact, content: str, remix_url: str | None = None, permission: str = "view"
) -> str:
    # H7: share rendering layer extracted from engine.py. iframe sandbox + CSP + sanitization.
    # LOW-5: nonce per render, drop style-src 'unsafe-inline', CSP and <style> share the same nonce.
    # #83: append the permission footer when remix_url is non-empty (view/remix badge and remix entry).
    doc, nonce = _render_share_body(artifact, content)
    return _append_share_footer(doc, nonce, permission, remix_url)


def _render_share_body(artifact, content: str) -> tuple[str, str]:
    # R2 (#86): CSP meta emitted once here — every render branch inherits it.
    atype = artifact.type
    title = _html_escape(artifact.name or "Shared Artifact")
    nonce = _gen_nonce()
    csp = _SHARE_DOC_CSP_TMPL.format(nonce=nonce)
    if atype in ("html", "react", "svg"):
        iframe_doc = _html_escape(_sanitize_html(content))
        style = "html,body,iframe{margin:0;padding:0;height:100%;border:0}"
        body = f"<iframe sandbox srcdoc=\"{iframe_doc}\"></iframe>"
    elif atype == "markdown":
        import markdown

        rendered = markdown.markdown(content, extensions=["fenced_code", "tables"])
        body = _sanitize_html(rendered)
        style = (
            "body{font-family:system-ui,sans-serif;max-width:900px;"
            "margin:2rem auto;padding:0 1rem;line-height:1.6}"
            "pre{background:#f4f4f4;padding:1rem;overflow:auto;border-radius:4px}"
            "code{background:#f4f4f4;padding:2px 6px;border-radius:3px}"
        )
    elif atype == "data":
        import json as _json

        if content.strip():
            try:
                pretty = _json.dumps(_json.loads(content), indent=2, ensure_ascii=False)
            except (ValueError, TypeError):
                logger.warning("data share content not valid JSON, render escaped raw")
                pretty = content
        else:
            pretty = content
        style = "body{font-family:monospace;white-space:pre;padding:1rem}"
        body = _html_escape(pretty)
    else:
        style = "body{font-family:monospace;white-space:pre-wrap;padding:1rem;overflow:auto}"
        body = _html_escape(content)
    doc = (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title>"
        f"<meta http-equiv='Content-Security-Policy' content=\"{csp}\">"
        f"<style nonce=\"{nonce}\">{style}</style>"
        f"</head><body>{body}</body></html>"
    )
    return doc, nonce
