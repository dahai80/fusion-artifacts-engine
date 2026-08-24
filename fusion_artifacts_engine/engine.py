import difflib
import hashlib
import json
import logging
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

from fusion_artifacts_engine.auto_identifier import should_create_artifact
from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.models import (
    Artifact,
    ArtifactEvent,
    ArtifactFolder,
    ArtifactShare,
    ArtifactTag,
    ArtifactVersion,
    infer_kind,
)
from fusion_artifacts_engine.ref_parser import generate_ref_text
from fusion_artifacts_engine.section_index import extract_sections, normalize_anchor
from fusion_artifacts_engine.storage.sqlite_storage import SQLiteStorage
from fusion_artifacts_engine.token_counter import count_tokens
from fusion_artifacts_engine.utils import generate_artifact_id
from fusion_artifacts_engine.rpc.event_bus import EventBus

logger = logging.getLogger(__name__)

_SUMMARY_MAX_LEN = 200


def _is_expired(expires_at: str | None) -> bool:
    # L-1: fail-closed——无过期时间才放行；解析失败一律视为已过期，拒绝访问
    if not expires_at:
        return False
    try:
        exp = datetime.fromisoformat(expires_at)
    except ValueError:
        logger.warning("Unparseable expires_at %r, treat as expired (fail-closed)", expires_at)
        return True
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    return datetime.now(UTC) > exp


def _parse_expires_at(expires_at: str | None) -> datetime | None:
    # L-6: 解析 expires_at 为 aware datetime；None 放行，不可解析抛 ValueError
    if not expires_at:
        return None
    try:
        exp = datetime.fromisoformat(expires_at)
    except ValueError as e:
        raise ValueError(f"Invalid expires_at format: {expires_at!r}") from e
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    return exp


def _truncate_summary(content: str) -> str:
    return content[:_SUMMARY_MAX_LEN].replace("\n", " ").strip()


def _auto_changelog(old_content: str, new_content: str) -> str:
    # 基于真实行级 diff 统计增删，非仅行数算术 (P2-11)。
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()
    diff_lines = list(difflib.unified_diff(old_lines, new_lines, lineterm=""))
    added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    parts = []
    if added:
        parts.append(f"+{added} lines")
    if removed:
        parts.append(f"-{removed} lines")
    if not parts:
        if old_content != new_content:
            parts.append("content modified")
        else:
            parts.append("no change")
    elif added == removed and old_content != new_content:
        # 同行数内容替换：diff 报 +n/-n，补充 content modified 标记替换语义 (P2-11)。
        parts.append("content modified")
    return ", ".join(parts)


def _size_bytes(content: str) -> int:
    return len(content.encode("utf-8"))


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


_SCRIPT_TAG_RE = re.compile(r"<\s*script\b[^>]*>.*?<\s*/\s*script\s*>", re.IGNORECASE | re.DOTALL)
_SCRIPT_OPEN_RE = re.compile(r"<\s*script\b", re.IGNORECASE)
_EVENT_ATTR_RE = re.compile(r"\son\w+\s*=\s*([^\s>]+|'[^']*'|\"[^\"]*\")", re.IGNORECASE)
_JS_PROTO_RE = re.compile(r"javascript:", re.IGNORECASE)


def _sanitize_html(html: str) -> str:
    # C-2/C-3: stdlib 净化——剥 <script>、on* 事件属性、javascript: 协议
    html = _SCRIPT_TAG_RE.sub("", html)
    html = _SCRIPT_OPEN_RE.sub("&lt;script", html)
    html = _EVENT_ATTR_RE.sub("", html)
    html = _JS_PROTO_RE.sub("", html)
    return html


_SHARE_DOC_CSP = (
    "default-src 'none'; "
    "script-src 'none'; "
    "style-src 'unsafe-inline'; "
    "img-src data:; "
    "font-src data:; "
    "connect-src 'none';"
)


def _render_share_html(artifact: Artifact, content: str) -> str:
    atype = artifact.type
    title = _html_escape(artifact.name or "Shared Artifact")
    if atype in ("html", "react"):
        # C-2: sandbox 去 allow-scripts，CSP script-src 'none'，杜绝 iframe 内脚本执行
        iframe_doc = _html_escape(_sanitize_html(content))
        return (
            f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<title>{title}</title>"
            f"<meta http-equiv='Content-Security-Policy' content=\"{_SHARE_DOC_CSP}\">"
            f"<style>html,body,iframe{{margin:0;padding:0;height:100%;border:0}}</style>"
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
            f"<meta http-equiv='Content-Security-Policy' content=\"{_SHARE_DOC_CSP}\">"
            f"<style>body{{font-family:system-ui,sans-serif;max-width:900px;"
            f"margin:2rem auto;padding:0 1rem;line-height:1.6}}"
            f"pre{{background:#f4f4f4;padding:1rem;overflow:auto;border-radius:4px}}"
            f"code{{background:#f4f4f4;padding:2px 6px;border-radius:3px}}</style>"
            f"</head><body>{rendered}</body></html>"
        )
    if atype == "data":
        import json as _json

        # L-5: data 内容非法 JSON 不崩溃，回退转义原文
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
            f"<style>body{{font-family:monospace;white-space:pre;padding:1rem}}</style>"
            f"</head><body>{pretty}</body></html>"
        )
    if atype == "svg":
        # C-3: svg 同样 iframe sandbox + 净化 + CSP，剥 <script>/事件属性
        iframe_doc = _html_escape(_sanitize_html(content))
        return (
            f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<title>{title}</title>"
            f"<meta http-equiv='Content-Security-Policy' content=\"{_SHARE_DOC_CSP}\">"
            f"<style>html,body,iframe{{margin:0;padding:0;height:100%;border:0}}</style>"
            f"</head><body>"
            f"<iframe sandbox srcdoc=\"{iframe_doc}\"></iframe>"
            f"</body></html>"
        )
    escaped = _html_escape(content)
    return (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title>"
        f"<style>body{{font-family:monospace;white-space:pre-wrap;"
        f"padding:1rem;overflow:auto}}</style>"
        f"</head><body>{escaped}</body></html>"
    )


class ArtifactEngine:
    def __init__(self, config: ArtifactEngineConfig | None = None):
        self.config = config or ArtifactEngineConfig()
        self.storage = SQLiteStorage(
            db_path=self.config.db_path,
            content_dir=self.config.content_dir,
            small_content_limit=self.config.small_content_limit,
        )
        # A-1: _watchers 仅作注册簿记录；实际事件投递走 EventBus/SSE。
        # ThreadingMixIn 多线程改写，加锁防 dict changed size during iteration 竞态。
        self._watchers: dict[str, list[str]] = {}
        self._watchers_lock = threading.Lock()
        # A-2: EventBus 注入 engine 实例，避免模块级单例跨 engine 串流
        self.event_bus = EventBus()
        logger.info(
            "ArtifactEngine initialized: storage_root=%s", self.config.storage_root
        )

    async def create_artifact(
        self,
        session_id: str,
        name: str,
        artifact_type: str,
        content: str,
        summary: str = "",
        change_log: str = "Initial version",
        kind: str | None = None,
        project_id: str | None = None,
        metadata: dict | None = None,
    ) -> tuple[Artifact, ArtifactVersion, str]:
        artifact_id = generate_artifact_id(self.config.artifact_id_prefix)
        now = time.time()
        if not summary:
            summary = _truncate_summary(content)
        if kind is None:
            kind = infer_kind(artifact_type)
        artifact = Artifact(
            id=artifact_id,
            session_id=session_id,
            name=name,
            type=artifact_type,
            kind=kind,
            project_id=project_id,
            metadata=metadata,
            current_version=1,
            summary=summary,
            created_at=now,
            updated_at=now,
        )
        size = _size_bytes(content)
        sections = extract_sections(content, artifact_type)
        version = ArtifactVersion(
            artifact_id=artifact_id,
            version_num=1,
            content=content,
            size_bytes=size,
            token_count=count_tokens(content),
            section_index=json.dumps(sections) if sections else None,
            change_log=change_log,
            created_at=now,
        )
        self.storage.save_artifact_and_version(artifact, version)
        ref_text = generate_ref_text(artifact_id, name, artifact_type, 1, size, summary)
        logger.info(
            "Created artifact: %s name=%s size=%d tokens=%d sections=%d",
            artifact_id,
            name,
            size,
            version.token_count,
            len(sections),
        )
        return artifact, version, ref_text

    async def create_external_artifact(
        self,
        source_module: str,
        workspace_id: str,
        name: str,
        artifact_type: str,
        content: str,
        workflow_run_id: str | None = None,
        summary: str = "",
        kind: str | None = None,
        project_id: str | None = None,
        metadata: dict | None = None,
    ) -> tuple[Artifact, ArtifactVersion, str]:
        session_id = f"ext_{source_module}_{workspace_id}"
        artifact_id = generate_artifact_id(self.config.artifact_id_prefix)
        now = time.time()
        if not summary:
            summary = _truncate_summary(content)
        if kind is None:
            kind = infer_kind(artifact_type)
        artifact = Artifact(
            id=artifact_id,
            session_id=session_id,
            name=name,
            type=artifact_type,
            kind=kind,
            project_id=project_id,
            metadata=metadata,
            current_version=1,
            summary=summary,
            created_at=now,
            updated_at=now,
            source_module=source_module,
            workspace_id=workspace_id,
            workflow_run_id=workflow_run_id,
        )
        size = _size_bytes(content)
        sections = extract_sections(content, artifact_type)
        version = ArtifactVersion(
            artifact_id=artifact_id,
            version_num=1,
            content=content,
            size_bytes=size,
            token_count=count_tokens(content),
            section_index=json.dumps(sections) if sections else None,
            change_log="Created by external module",
            created_at=now,
        )
        self.storage.save_artifact_and_version(artifact, version)
        if project_id:
            self.storage.move_to_project_kb(artifact_id, project_id)
            artifact = self.storage.get_artifact(artifact_id)
            logger.info(
                "Auto-archived external artifact %s to project %s",
                artifact_id,
                project_id,
            )
        ref_text = generate_ref_text(artifact_id, name, artifact_type, 1, size, summary)
        logger.info(
            "Created external artifact: %s source=%s ws=%s size=%d",
            artifact_id,
            source_module,
            workspace_id,
            size,
        )
        return artifact, version, ref_text

    def list_by_source(
        self,
        source_module: str,
        workspace_id: str | None = None,
        workflow_run_id: str | None = None,
        page: int = 1,
        page_size: int = 200,
    ) -> list[Artifact]:
        return self.storage.list_by_source(
            source_module, workspace_id, workflow_run_id, page=page, page_size=page_size
        )

    def get_artifact(
        self, artifact_id: str, project_id: str | None = None
    ) -> Artifact | None:
        return self.storage.get_artifact(artifact_id, project_id)

    def list_artifacts(
        self,
        session_id: str,
        include_deleted: bool = False,
        project_id: str | None = None,
        metadata_filter: dict | None = None,
    ) -> list[Artifact]:
        return self.storage.list_artifacts(
            session_id, include_deleted, project_id, metadata_filter
        )

    def delete_artifact(
        self,
        artifact_id: str,
        soft_delete: bool = True,
        project_id: str | None = None,
    ) -> bool:
        return self.storage.delete_artifact(artifact_id, soft_delete, project_id)

    def get_version_content(
        self, artifact_id: str, version: int | None = None
    ) -> ArtifactVersion | None:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            return None
        ver = version if version is not None else artifact.current_version
        try:
            result = self.storage.get_version(artifact_id, ver)
        except FileNotFoundError as e:
            logger.error("Content file missing for %s v%s: %s", artifact_id, ver, e)
            return None
        # section_index 保持原始 JSON 字符串（与 list_versions 一致），不在原地改类型，
        # 避免 str/dict 漂移；调用方需 dict 时自行 json.loads (P2-8)。
        return result

    def list_versions(
        self,
        artifact_id: str,
        page: int = 1,
        page_size: int = 200,
        include_content: bool = True,
    ) -> list[ArtifactVersion]:
        return self.storage.list_versions(
            artifact_id, page=page, page_size=page_size, include_content=include_content
        )

    async def rollback_version(
        self,
        artifact_id: str,
        target_version: int,
    ) -> tuple[ArtifactVersion, str]:
        try:
            target = self.storage.get_version(artifact_id, target_version)
        except FileNotFoundError as e:
            logger.error(
                "Content file missing for %s v%s: %s", artifact_id, target_version, e
            )
            raise ValueError(
                f"Version content not found: {artifact_id} v{target_version}"
            ) from e
        if target is None:
            raise ValueError(f"Version not found: {artifact_id} v{target_version}")
        version, ref_text = await self.create_version(
            artifact_id,
            target.content,
            f"Rollback to v{target_version}",
            snapshot_type="rollback",
            parent_version=target_version,
        )
        logger.info(
            "Rolled back %s to v%s, new v%s (rollback-tagged, parent=%s)",
            artifact_id,
            target_version,
            version.version_num,
            target_version,
        )
        return version, ref_text

    def should_create_artifact(self, content: str, content_type: str = "text") -> bool:
        return should_create_artifact(
            content,
            content_type,
            self.config.auto_create_threshold_lines,
            self.config.auto_create_threshold_chars,
        )

    _LANG_EXT: ClassVar[dict[str, str]] = {
        "python": "py",
        "javascript": "js",
        "typescript": "ts",
        "html": "html",
        "css": "css",
        "markdown": "md",
        "json": "json",
        "yaml": "yaml",
        "rust": "rs",
        "go": "go",
        "java": "java",
        "c": "c",
        "cpp": "cpp",
    }

    _EXT_LANG: ClassVar[dict[str, str]] = {v: k for k, v in _LANG_EXT.items()}

    def export_code(self, artifact_id: str, language: str = "") -> dict:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        version = self.get_version_content(artifact_id)
        if version is None:
            raise ValueError(f"No content for artifact: {artifact_id}")
        ext = self._LANG_EXT.get(language, "")
        if not ext:
            name = artifact.name.lower()
            if "." in name:
                ext = name.rsplit(".", 1)[-1]
                language = self._EXT_LANG.get(ext, language)
            else:
                ext = self._LANG_EXT.get(artifact.type, "txt")
        code = version.content
        return {
            "code": code,
            "language": language,
            "ext": ext,
            "artifact_id": artifact_id,
            "version": version.version_num,
            "name": artifact.name,
        }

    async def import_code(
        self,
        session_id: str,
        code: str,
        language: str = "",
        name: str = "",
        metadata: dict | None = None,
    ) -> tuple[Artifact, ArtifactVersion, str]:
        meta = metadata or {}
        artifact_type = "code"
        ext = self._LANG_EXT.get(language, "")
        if language in ("html",) or ext == "html":
            artifact_type = "html"
        elif language in ("javascript", "typescript") and ext in ("jsx", "tsx"):
            artifact_type = "react"
        if not name:
            name = meta.get("filename", f"imported.{ext}" if ext else "imported.txt")
        summary = meta.get("summary", _truncate_summary(code))
        return await self.create_artifact(
            session_id=session_id,
            name=name,
            artifact_type=artifact_type,
            content=code,
            summary=summary,
        )

    def register_watcher(self, artifact_id: str, watcher_id: str) -> None:
        with self._watchers_lock:
            if artifact_id not in self._watchers:
                self._watchers[artifact_id] = []
            if watcher_id not in self._watchers[artifact_id]:
                self._watchers[artifact_id].append(watcher_id)
                logger.info(
                    "Watcher registered: %s for artifact %s", watcher_id, artifact_id
                )

    def unregister_watcher(self, artifact_id: str, watcher_id: str) -> None:
        with self._watchers_lock:
            if artifact_id in self._watchers:
                self._watchers[artifact_id] = [
                    w for w in self._watchers[artifact_id] if w != watcher_id
                ]
                if not self._watchers[artifact_id]:
                    del self._watchers[artifact_id]
                logger.info(
                    "Watcher unregistered: %s for artifact %s", watcher_id, artifact_id
                )

    def get_watch_events(self, artifact_id: str, since_version: int = 0) -> list[dict]:
        versions = self.storage.list_versions(artifact_id)
        events = []
        for v in versions:
            if v.version_num > since_version:
                events.append(
                    {
                        "artifact_id": artifact_id,
                        "version": v.version_num,
                        "change_log": v.change_log,
                        "created_at": v.created_at,
                    }
                )
        logger.debug(
            "Watch events for %s since v%d: %d events",
            artifact_id,
            since_version,
            len(events),
        )
        return events

    def get_artifact_raw_content(self, artifact_id: str) -> dict | None:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            return None
        version = self.get_version_content(artifact_id)
        if version is None:
            return None
        content_type_map = {
            "html": "text/html",
            "react": "text/html",
            "markdown": "text/markdown",
            "code": "text/plain",
            "data": "application/json",
        }
        ct = content_type_map.get(artifact.type, "text/plain")
        return {
            "content": version.content,
            "content_type": ct,
            "artifact_id": artifact_id,
            "version": version.version_num,
        }

    # ── P1: lifecycle + global repo ────────────────────────────

    def rename_artifact(self, artifact_id: str, new_name: str) -> bool:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        ok = self.storage.rename_artifact(artifact_id, new_name)
        logger.info("Renamed artifact %s -> %s ok=%s", artifact_id, new_name, ok)
        return ok

    def star_artifact(self, artifact_id: str, starred: bool = True) -> bool:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        ok = self.storage.star_artifact(artifact_id, starred)
        logger.info("Star artifact %s starred=%s ok=%s", artifact_id, starred, ok)
        return ok

    def pin_artifact(
        self, artifact_id: str, chat_id: str | None = None, pinned: bool = True
    ) -> bool:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        ok = self.storage.pin_artifact(artifact_id, chat_id, pinned)
        logger.info(
            "Pin artifact %s pinned=%s chat=%s ok=%s", artifact_id, pinned, chat_id, ok
        )
        return ok

    def duplicate_artifact(
        self, artifact_id: str, new_name: str | None = None
    ) -> Artifact | None:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        new_id = generate_artifact_id(self.config.artifact_id_prefix)
        dup = self.storage.duplicate_artifact(artifact_id, new_id, new_name)
        logger.info("Duplicated artifact %s -> %s", artifact_id, new_id)
        return dup

    def list_all_artifacts(
        self,
        filters: dict | None = None,
        sort: str = "updated_at",
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Artifact], int]:
        artifacts, total = self.storage.list_all_artifacts(
            filters, sort, page, page_size
        )
        logger.info("list_all_artifacts: %d/%d page=%d", len(artifacts), total, page)
        return artifacts, total

    # ── P1: recycle bin ────────────────────────────────────────

    def list_recycle(
        self, page: int = 1, page_size: int = 20
    ) -> tuple[list[Artifact], int]:
        artifacts, total = self.storage.list_recycle(page, page_size)
        logger.info("list_recycle: %d/%d page=%d", len(artifacts), total, page)
        return artifacts, total

    def restore_artifact(self, artifact_id: str) -> bool:
        ok = self.storage.restore_artifact(artifact_id)
        logger.info("Restored artifact %s ok=%s", artifact_id, ok)
        return ok

    def purge_expired(self) -> int:
        retention_days = getattr(self.config, "recycle_retention_days", 7)
        count = self.storage.purge_expired(retention_days)
        logger.info(
            "Purged %d expired artifacts (retention=%d days)", count, retention_days
        )
        return count

    # ── P1: optimistic lock ────────────────────────────────────

    async def create_version(
        self,
        artifact_id: str,
        content: str,
        change_log: str = "",
        source: str = "manual",
        expected_content_hash: str | None = None,
        snapshot_type: str = "auto",
        parent_version: int | None = None,
    ) -> tuple[ArtifactVersion, str]:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        if not change_log:
            old = self.storage.get_version(artifact_id, artifact.current_version)
            change_log = _auto_changelog(old.content if old else "", content)
        now = time.time()
        size = _size_bytes(content)
        sections = extract_sections(content, artifact.type)
        # C-8: version_num 由原子存储方法在事务内分配，这里用 0 占位
        version = ArtifactVersion(
            artifact_id=artifact_id,
            version_num=0,
            content=content,
            size_bytes=size,
            token_count=count_tokens(content),
            section_index=json.dumps(sections) if sections else None,
            change_log=change_log,
            source=source,
            created_at=now,
            snapshot_type=snapshot_type,
            parent_version=parent_version,
        )
        # L-8: rollback 的 content_hash 与历史版本内容相同会碰撞——乐观锁调用方
        # 无法区分"已回滚到 vN"与"仍在 vN"。回滚版本 hash 计算前加标记前缀，
        # 使其与原始版本 hash 不同，消除锁歧义。
        hash_input = content
        if snapshot_type == "rollback":
            hash_input = f"rollback:{parent_version}:{content}"
        content_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()[:16]
        summary = _truncate_summary(content) if not artifact.summary else None
        # C-8: 校验+分配+写入原子化，BEGIN IMMEDIATE 序列化并发写
        new_version = self.storage.create_version_atomic(
            artifact,
            version,
            content_hash,
            summary,
            expected_content_hash=expected_content_hash,
        )
        ref_text = generate_ref_text(
            artifact_id,
            artifact.name,
            artifact.type,
            new_version,
            size,
            artifact.summary,
        )
        logger.info(
            "Created version: %s v%s size=%d tokens=%d sections=%d hash=%s",
            artifact_id,
            new_version,
            size,
            version.token_count,
            len(sections),
            content_hash,
        )
        return version, ref_text

    # ── P1: share ──────────────────────────────────────────────

    def create_share(
        self,
        artifact_id: str,
        created_by: str | None = None,
        expires_at: str | None = None,
    ) -> ArtifactShare:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        # C-9: 仅拥有者可分享——artifact 有 owner_user_id 时校验 created_by 一致
        if artifact.owner_user_id and created_by != artifact.owner_user_id:
            logger.warning(
                "Share denied: caller %s != owner %s for artifact %s",
                created_by,
                artifact.owner_user_id,
                artifact_id,
            )
            raise PermissionError(
                f"Only the owner can share artifact {artifact_id}"
            )
        # L-6: 校验 expires_at 格式/范围——拒绝过去日期与超 max-TTL
        if expires_at is not None:
            try:
                exp = _parse_expires_at(expires_at)
            except ValueError:
                logger.warning("create_share bad expires_at %r", expires_at)
                raise
            if exp is not None:
                now_utc = datetime.now(UTC)
                if exp <= now_utc:
                    logger.warning("create_share past expires_at %r", expires_at)
                    raise ValueError(f"expires_at must be in the future: {expires_at}")
                max_ttl_days = self.config.share_max_ttl_days
                if max_ttl_days > 0:
                    max_exp = now_utc + timedelta(days=max_ttl_days)
                    if exp > max_exp:
                        logger.warning(
                            "create_share expires_at %r exceeds max TTL %d days",
                            expires_at, max_ttl_days,
                        )
                        raise ValueError(
                            f"expires_at exceeds max TTL {max_ttl_days} days: {expires_at}"
                        )
        existing = self.storage.get_share_by_artifact(artifact_id)
        if existing is not None:
            logger.info(
                "Returning existing share %s for artifact %s",
                existing.share_id,
                artifact_id,
            )
            return existing
        import uuid

        share_id = f"shr_{uuid.uuid4().hex[:12]}"
        now_iso = datetime.now(UTC).isoformat()
        share = ArtifactShare(
            share_id=share_id,
            artifact_id=artifact_id,
            created_by=created_by,
            created_at=now_iso,
            expires_at=expires_at,
        )
        self.storage.save_share(share)
        self.storage.set_artifact_share_id(artifact_id, share_id)
        logger.info("Created share %s for artifact %s", share_id, artifact_id)
        return share

    def get_shared_artifact(self, share_id: str) -> dict | None:
        share = self.storage.get_share(share_id)
        if share is None:
            return None
        if share.revoked:
            logger.info("Share %s is revoked", share_id)
            return None
        if _is_expired(share.expires_at):
            logger.info("Share %s expired", share_id)
            return None
        artifact = self.storage.get_artifact(share.artifact_id)
        if artifact is None:
            return None
        # L-2: increment 必须在 artifact 存在性校验之后，否则已删 artifact 会污染 access 统计
        self.storage.increment_share_access(share_id)
        version = self.get_version_content(share.artifact_id)
        content_type_map = {
            "html": "text/html",
            "react": "text/html",
            "markdown": "text/markdown",
            "code": "text/plain",
            "data": "application/json",
        }
        ct = content_type_map.get(artifact.type, "text/plain")
        return {
            "artifact": artifact.model_dump(),
            "content": version.content if version else "",
            "content_type": ct,
        }

    def revoke_share(self, share_id: str) -> bool:
        ok = self.storage.revoke_share(share_id)
        logger.info("Revoked share %s ok=%s", share_id, ok)
        return ok

    def get_public_share(self, share_id: str) -> dict:
        share = self.storage.get_share(share_id)
        if share is None:
            logger.info("Public share %s not found", share_id)
            return {"status": "not_found"}
        if share.revoked:
            logger.info("Public share %s revoked", share_id)
            return {"status": "gone", "reason": "revoked"}
        if _is_expired(share.expires_at):
            logger.info("Public share %s expired", share_id)
            return {"status": "gone", "reason": "expired"}
        artifact = self.storage.get_artifact(share.artifact_id)
        if artifact is None:
            logger.warning("Public share %s: artifact %s missing", share_id, share.artifact_id)
            return {"status": "not_found"}
        # L-2: increment 必须在 artifact 存在性校验之后，否则已删 artifact 会污染 access 统计
        self.storage.increment_share_access(share_id)
        version = self.get_version_content(share.artifact_id)
        raw_content = version.content if version else ""
        rendered_html = _render_share_html(artifact, raw_content)
        logger.info(
            "Public share %s served artifact %s (rendered, source isolated)",
            share_id,
            share.artifact_id,
        )
        # C-5: 公共未认证端点只回展示字段，不泄露 session_id/project_id/
        # owner_user_id/metadata/content_hash/folder_id/created_by 等内部 PII
        public_share = {
            "share_id": share.share_id,
            "expires_at": share.expires_at,
            "revoked": share.revoked,
        }
        public_artifact = {
            "id": artifact.id,
            "name": artifact.name,
            "type": artifact.type,
            "kind": artifact.kind,
            "summary": artifact.summary,
            "current_version": artifact.current_version,
            "created_at": artifact.created_at,
            "updated_at": artifact.updated_at,
        }
        return {
            "status": "ok",
            "share": public_share,
            "artifact": public_artifact,
            "rendered_html": rendered_html,
            "content_type": "text/html",
        }

    # ── P2: snapshots ──────────────────────────────────────────

    async def create_snapshot(
        self,
        artifact_id: str,
        label: str | None = None,
        author: str | None = None,
    ) -> ArtifactVersion:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        current = self.storage.get_version(artifact_id, artifact.current_version)
        if current is None:
            raise ValueError(f"No current version for artifact: {artifact_id}")
        new_ver_num = self.storage.next_version_num(artifact_id)
        now = time.time()
        snapshot_content = current.content
        snapshot_size = _size_bytes(snapshot_content)
        snapshot_sections = extract_sections(snapshot_content, artifact.type)
        snapshot = ArtifactVersion(
            artifact_id=artifact_id,
            version_num=new_ver_num,
            content=snapshot_content,
            size_bytes=snapshot_size,
            token_count=count_tokens(snapshot_content),
            section_index=json.dumps(snapshot_sections) if snapshot_sections else None,
            change_log=f"Snapshot: {label}" if label else "Snapshot",
            source="manual",
            created_at=now,
            snapshot_type="named",
            snapshot_label=label,
            author=author,
            parent_version=artifact.current_version,
        )
        self.storage.save_version(snapshot)
        artifact.current_version = new_ver_num
        artifact.updated_at = now
        self.storage.save_artifact(artifact)
        logger.info(
            "Created snapshot: %s v%d label=%s", artifact_id, new_ver_num, label
        )
        return snapshot

    def list_snapshots(
        self,
        artifact_id: str,
        page: int = 1,
        page_size: int = 200,
        include_content: bool = True,
    ) -> list[ArtifactVersion]:
        return self.storage.list_snapshots(
            artifact_id, page=page, page_size=page_size, include_content=include_content
        )

    # ── P2: folders ────────────────────────────────────────────

    def create_folder(
        self,
        name: str,
        parent_id: str | None = None,
        project_id: str | None = None,
    ) -> ArtifactFolder:
        import uuid

        folder_id = f"fld_{uuid.uuid4().hex[:12]}"
        now_iso = datetime.now(UTC).isoformat()
        folder = ArtifactFolder(
            folder_id=folder_id,
            name=name,
            parent_id=parent_id,
            project_id=project_id,
            created_at=now_iso,
        )
        self.storage.save_folder(folder)
        logger.info("Created folder: %s name=%s", folder_id, name)
        return folder

    def list_folders(self, project_id: str | None = None) -> list[ArtifactFolder]:
        return self.storage.list_folders(project_id)

    def rename_folder(self, folder_id: str, new_name: str) -> bool:
        ok = self.storage.rename_folder(folder_id, new_name)
        logger.info("Renamed folder %s -> %s ok=%s", folder_id, new_name, ok)
        return ok

    def delete_folder(self, folder_id: str) -> bool:
        ok = self.storage.delete_folder(folder_id)
        logger.info("Deleted folder %s ok=%s", folder_id, ok)
        return ok

    def move_to_folder(self, artifact_id: str, folder_id: str | None = None) -> bool:
        ok = self.storage.move_to_folder(artifact_id, folder_id)
        logger.info("Moved artifact %s to folder %s ok=%s", artifact_id, folder_id, ok)
        return ok

    # ── P4: tags ───────────────────────────────────────────────

    @staticmethod
    def _tag_scope(artifact: "Artifact") -> str | None:
        # L-7: tag 作用域优先 session_id，其次 project_id；都无则 None（全局）
        if artifact.session_id:
            return artifact.session_id
        if artifact.project_id:
            return artifact.project_id
        return None

    def add_tag(
        self, artifact_id: str, tag_name: str, color: str | None = None
    ) -> ArtifactTag:
        import uuid

        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        scope = self._tag_scope(artifact)
        existing = self.storage.get_tag_by_name(tag_name, scope)
        if existing:
            tag = existing
        else:
            tag_id = f"tag_{uuid.uuid4().hex[:12]}"
            tag = ArtifactTag(tag_id=tag_id, name=tag_name, color=color, scope=scope)
            self.storage.save_tag(tag)
            logger.info("Created tag: %s name=%s scope=%s", tag_id, tag_name, scope)
        self.storage.add_artifact_tag(artifact_id, tag.tag_id)
        logger.info("Added tag %s to artifact %s", tag.tag_id, artifact_id)
        return tag

    def remove_tag(self, artifact_id: str, tag_name: str) -> bool:
        artifact = self.storage.get_artifact(artifact_id)
        scope = self._tag_scope(artifact) if artifact else None
        tag = self.storage.get_tag_by_name(tag_name, scope)
        if tag is None:
            logger.warning("Tag not found: %s scope=%s", tag_name, scope)
            return False
        ok = self.storage.remove_artifact_tag(artifact_id, tag.tag_id)
        logger.info("Removed tag %s from artifact %s ok=%s", tag_name, artifact_id, ok)
        return ok

    def list_tags(self, scope: str | None = None) -> list[ArtifactTag]:
        return self.storage.list_tags(scope)

    def list_artifact_tags(self, artifact_id: str) -> list[ArtifactTag]:
        return self.storage.list_artifact_tags(artifact_id)

    # ── P4: events ─────────────────────────────────────────────

    def emit_event(
        self,
        event_type: str,
        artifact_id: str | None = None,
        session_id: str | None = None,
        payload: dict | None = None,
    ) -> ArtifactEvent:
        import uuid
        import time as _time

        event_id = f"evt_{uuid.uuid4().hex[:12]}"
        now_ts = _time.time()
        event = ArtifactEvent(
            event_id=event_id,
            artifact_id=artifact_id,
            session_id=session_id,
            event_type=event_type,
            payload=payload,
            created_at=now_ts,
        )
        self.storage.save_event(event)
        logger.info(
            "Emitted event: %s type=%s artifact=%s", event_id, event_type, artifact_id
        )
        return event

    def list_events(
        self,
        artifact_id: str | None = None,
        session_id: str | None = None,
        since_ts: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[ArtifactEvent], int]:
        events, total = self.storage.list_events(
            artifact_id, session_id, since_ts, page, page_size
        )
        logger.info("list_events: %d/%d page=%d", len(events), total, page)
        return events, total

    # ── P3: project KB ─────────────────────────────────────────

    def move_to_project_kb(self, artifact_id: str, project_id: str) -> bool:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        ok = self.storage.move_to_project_kb(artifact_id, project_id)
        logger.info(
            "Moved artifact %s to project_kb project=%s ok=%s",
            artifact_id,
            project_id,
            ok,
        )
        return ok

    # ── AE-1: patch_artifact ───────────────────────────────────

    async def patch_artifact(
        self,
        artifact_id: str,
        operation: str,
        anchor: str = "",
        content: str = "",
        expected_version: int | None = None,
    ) -> tuple[ArtifactVersion, dict]:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        if (
            expected_version is not None
            and artifact.current_version != expected_version
        ):
            raise ValueError(
                f"Optimistic lock failed: expected version {expected_version}, "
                f"got {artifact.current_version}"
            )
        current = self.storage.get_version(artifact_id, artifact.current_version)
        if current is None:
            raise ValueError(f"No current version for artifact: {artifact_id}")
        old_content = current.content
        old_tokens = count_tokens(old_content)

        if operation == "replace_section":
            if not anchor:
                raise ValueError("anchor is required for replace_section")
            new_content, replaced_content = self._replace_section(
                old_content, anchor, content, artifact.type
            )
        elif operation == "append":
            new_content = old_content + content
            replaced_content = ""
        elif operation == "prepend":
            new_content = content + old_content
            replaced_content = ""
        elif operation == "delete_section":
            if not anchor:
                raise ValueError("anchor is required for delete_section")
            new_content, replaced_content = self._delete_section(
                old_content, anchor, artifact.type
            )
        else:
            raise ValueError(f"Unknown operation: {operation}")

        version, _ref_text = await self.create_version(
            artifact_id, new_content, f"patch:{operation} anchor={anchor}"
        )
        replaced_tokens = count_tokens(replaced_content)
        new_tokens = count_tokens(new_content)
        tokens_added = new_tokens - old_tokens + replaced_tokens
        tokens_removed = replaced_tokens
        tokens_net = tokens_added - tokens_removed
        patch_info = {
            "artifact_id": artifact_id,
            "new_version": version.version_num,
            "tokens_added": tokens_added,
            "tokens_removed": tokens_removed,
            "tokens_net": tokens_net,
        }
        logger.info(
            "Patched artifact %s op=%s anchor=%s new_v=%d net_tokens=%d",
            artifact_id,
            operation,
            anchor,
            version.version_num,
            tokens_net,
        )
        return version, patch_info

    @staticmethod
    def _all_section_bounds(
        content: str, artifact_type: str
    ) -> list[tuple[int, int, int, str]]:
        # P-1: 一次线性扫预计算所有标题位置 + section 边界，避免 O(n^2)。
        # 返回 (start, end, level, anchor) 列表，end 为下一同级/更高级标题行号。
        lines = content.split("\n")
        headers: list[tuple[int, int, str]] = []
        for i, line in enumerate(lines):
            if artifact_type == "markdown":
                m = re.match(r"^(#{1,6})\s+(.+)$", line)
                if m:
                    headers.append((i, len(m.group(1)), m.group(2).strip()))
            elif artifact_type == "code":
                m = re.match(r"^(?:async\s+)?(?:def|class|func)\s+(\w+)", line)
                if m:
                    headers.append((i, 1, m.group(1)))
        bounds: list[tuple[int, int, int, str]] = []
        for idx, (start, level, anchor) in enumerate(headers):
            end = len(lines)
            if artifact_type == "markdown":
                for j in range(idx + 1, len(headers)):
                    s2, l2, _ = headers[j]
                    if l2 <= level:
                        end = s2
                        break
            else:
                if idx + 1 < len(headers):
                    end = headers[idx + 1][0]
            bounds.append((start, end, level, anchor))
        return bounds

    @staticmethod
    def _find_section_bounds(
        content: str, anchor: str, artifact_type: str
    ) -> list[tuple[int, int, int]]:
        # P-1: 复用 _all_section_bounds 线性结果，按 anchor 过滤；保持原多重匹配语义。
        anchor = normalize_anchor(anchor)
        return [
            (s, e, lvl)
            for s, e, lvl, anc in ArtifactEngine._all_section_bounds(
                content, artifact_type
            )
            if anc == anchor
        ]

    def _replace_section(
        self, content: str, anchor: str, new_content: str, artifact_type: str
    ) -> tuple[str, str]:
        matches = self._find_section_bounds(content, anchor, artifact_type)
        if len(matches) > 1:
            raise ValueError(
                f"Multiple matches for anchor '{anchor}': "
                f"found at lines {[m[0] + 1 for m in matches]}"
            )
        if not matches:
            raise ValueError(f"Anchor '{anchor}' not found in content")
        lines = content.split("\n")
        start, end, _ = matches[0]
        replaced_lines = lines[start:end]
        replaced_content = "\n".join(replaced_lines)
        new_lines = lines[:start] + new_content.split("\n") + lines[end:]
        return "\n".join(new_lines), replaced_content

    def _delete_section(
        self, content: str, anchor: str, artifact_type: str
    ) -> tuple[str, str]:
        matches = self._find_section_bounds(content, anchor, artifact_type)
        if len(matches) > 1:
            raise ValueError(
                f"Multiple matches for anchor '{anchor}': "
                f"found at lines {[m[0] + 1 for m in matches]}"
            )
        if not matches:
            raise ValueError(f"Anchor '{anchor}' not found in content")
        lines = content.split("\n")
        start, end, _ = matches[0]
        replaced_lines = lines[start:end]
        replaced_content = "\n".join(replaced_lines)
        new_lines = lines[:start] + lines[end:]
        return "\n".join(new_lines), replaced_content

    # ── AE-2: load_artifact ────────────────────────────────────

    def _build_sections_with_tokens(
        self, content: str, artifact_type: str
    ) -> list[dict]:
        # P-1: 单次线性扫导出所有 section 边界，避免每标题一次 O(n) 扫。
        lines = content.split("\n")
        sections = []
        for start, end, _lvl, anchor in ArtifactEngine._all_section_bounds(
            content, artifact_type
        ):
            section_text = "\n".join(lines[start:end])
            sections.append(
                {
                    "anchor": anchor,
                    "tokens": count_tokens(section_text),
                }
            )
        return sections

    def load_artifact(
        self,
        artifact_id: str,
        preview_only: bool = True,
        section: str | None = None,
    ) -> dict:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        version = self.get_version_content(artifact_id)
        if version is None:
            raise ValueError(f"No version found for artifact: {artifact_id}")

        sections = self._build_sections_with_tokens(version.content, artifact.type)

        result: dict = {
            "artifact_id": artifact_id,
            "title": artifact.name,
            "type": artifact.type,
            "version": version.version_num,
            "total_tokens": version.token_count,
            "sections": sections,
            "summary": artifact.summary or "",
        }

        if section:
            section = normalize_anchor(section)
            matches = self._find_section_bounds(version.content, section, artifact.type)
            if not matches:
                raise ValueError(f"Section '{section}' not found in content")
            if len(matches) > 1:
                raise ValueError(
                    f"Multiple matches for section '{section}': "
                    f"found at lines {[m[0] + 1 for m in matches]}"
                )
            lines = version.content.split("\n")
            start, end, _ = matches[0]
            sec_content = "\n".join(lines[start:end])
            sec_tokens = count_tokens(sec_content)
            matched_anchor = section
            for s in sections:
                if s["anchor"].strip() == section:
                    matched_anchor = s["anchor"]
                    break
            result["section"] = {"anchor": matched_anchor, "tokens": sec_tokens}
            result["content"] = sec_content
        elif preview_only:
            result["content"] = None
        else:
            result["content"] = version.content

        logger.info(
            "Loaded artifact %s preview=%s section=%s sections=%d",
            artifact_id,
            preview_only,
            section,
            len(sections),
        )
        return result

    # ── AE-6: context_budget ───────────────────────────────────

    def context_budget(
        self,
        session_id: str | None = None,
        context_window: int | None = None,
    ) -> dict:
        # P-2: 用存储层一条 SQL 聚合 token，替代 N+1 逐版本读 + 无界 page_size=100000。
        if context_window is not None and context_window < 1:
            raise ValueError("context_window must be >= 1")
        total_tokens, artifact_list = self.storage.sum_token_counts(
            session_id=session_id
        )
        effective_window = (
            context_window
            if context_window is not None
            else self.config.context_budget_default
        )
        utilization_pct = (
            round(total_tokens / effective_window * 100, 1)
            if effective_window > 0
            else 0.0
        )
        warning = utilization_pct > 70
        recommendation = None
        if warning:
            recommendation = "Consider using preview_only mode for artifact injection."
        logger.info(
            "Context budget session=%s total_tokens=%d window=%d utilization=%.1f%% warning=%s",
            session_id,
            total_tokens,
            effective_window,
            utilization_pct,
            warning,
        )
        return {
            "total_artifact_tokens": total_tokens,
            "artifact_count": len(artifact_list),
            "artifacts": artifact_list,
            "context_window": effective_window,
            "utilization_percent": utilization_pct,
            "warning": warning,
            "recommendation": recommendation,
        }

    # ── AE-7: auto_compact ─────────────────────────────────────

    async def auto_compact(self, artifact_id: str, token_budget: int) -> dict:
        from fusion_artifacts_engine.compactor import compact_content

        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        version = self.get_version_content(artifact_id)
        if version is None:
            raise ValueError(f"No version found for artifact: {artifact_id}")

        original_tokens = version.token_count
        if original_tokens <= token_budget:
            logger.info(
                "Artifact %s already within budget: %d <= %d",
                artifact_id,
                original_tokens,
                token_budget,
            )
            return {
                "version": version.model_dump(),
                "original_tokens": original_tokens,
                "compacted_tokens": original_tokens,
                "savings_pct": 0.0,
                "compacted": False,
                "reason": "already_within_budget",
            }

        compacted_content = compact_content(
            version.content, artifact.type, token_budget
        )
        compacted_tokens = count_tokens(compacted_content)

        if compacted_tokens < original_tokens:
            version, _ = await self.create_version(
                artifact_id,
                compacted_content,
                change_log=f"auto_compact: {original_tokens} -> {compacted_tokens} tokens (budget={token_budget})",
                source="ai_generation",
            )
            savings = round(
                (original_tokens - compacted_tokens) / original_tokens * 100, 1
            )
            logger.info(
                "Auto-compacted %s: %d -> %d tokens (%.1f%% savings)",
                artifact_id,
                original_tokens,
                compacted_tokens,
                savings,
            )
            return {
                "version": version.model_dump(),
                "original_tokens": original_tokens,
                "compacted_tokens": compacted_tokens,
                "savings_pct": savings,
                "compacted": True,
            }

        logger.warning(
            "Auto-compact could not reduce %s below budget %d",
            artifact_id,
            token_budget,
        )
        return {
            "version": version.model_dump(),
            "original_tokens": original_tokens,
            "compacted_tokens": compacted_tokens,
            "savings_pct": 0.0,
            "compacted": False,
            "reason": "could_not_reduce",
        }

    # ── #36: version_diff ───────────────────────────────────────

    def version_diff(
        self, artifact_id: str, from_version: int, to_version: int
    ) -> dict:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        from_ver = self.storage.get_version(artifact_id, from_version)
        if from_ver is None:
            raise ValueError(
                f"Version {from_version} not found for artifact: {artifact_id}"
            )
        to_ver = self.storage.get_version(artifact_id, to_version)
        if to_ver is None:
            raise ValueError(
                f"Version {to_version} not found for artifact: {artifact_id}"
            )
        from_lines = from_ver.content.splitlines(keepends=True)
        to_lines = to_ver.content.splitlines(keepends=True)
        diff_lines = list(
            difflib.unified_diff(
                from_lines,
                to_lines,
                fromfile=f"v{from_version}",
                tofile=f"v{to_version}",
            )
        )
        diff_text = "".join(diff_lines)
        lines_added = sum(1 for dl in diff_lines if dl.startswith("+") and not dl.startswith("+++"))
        lines_removed = sum(1 for dl in diff_lines if dl.startswith("-") and not dl.startswith("---"))
        logger.info(
            "Version diff %s v%d->v%d: +%d -%d lines",
            artifact_id,
            from_version,
            to_version,
            lines_added,
            lines_removed,
        )
        return {
            "diff": diff_text,
            "lines_added": lines_added,
            "lines_removed": lines_removed,
        }

    # ── #37: render / check_safety / inject / interact / sync ───

    async def render_artifact(
        self,
        content: str,
        session_id: str,
        lang_hint: str = "",
        project_id: str | None = None,
    ) -> dict:
        # 边界说明：本方法不执行浏览器渲染，只做 (1) 阈值判定 (2) 类型检测
        # (3) 存储为 artifact (4) 返回 render_type 提示 + 原始 content。
        # 浏览器渲染由调用方 (fusion-studio) 负责；服务端渲染场景见
        # _render_share_html (公开分享只读预览)。
        from fusion_artifacts_engine.auto_identifier import (
            detect_artifact_type,
            detect_renderable_type,
            extract_name_hint,
            should_create_artifact,
        )

        if not content:
            logger.info("render_artifact: empty content, skipping")
            return {"created": False, "reason": "empty_content"}
        if not should_create_artifact(content, content_type="text"):
            logger.info("render_artifact: content below threshold, skipping")
            return {"created": False, "reason": "below_threshold"}
        try:
            name = extract_name_hint(content, lang_hint)
            artifact_type = detect_artifact_type(name, content)
            render_type = detect_renderable_type(content, name) or artifact_type
            renderable_types = {"html", "react", "markdown", "svg", "mermaid"}
            is_renderable = render_type in renderable_types
            artifact, _version, ref_text = await self.create_artifact(
                session_id=session_id,
                name=name,
                artifact_type=artifact_type,
                content=content,
                summary=_truncate_summary(content),
                change_log="Created via artifact.render",
                project_id=project_id,
            )
            logger.info(
                "render_artifact: created %s type=%s render_type=%s renderable=%s",
                artifact.id,
                artifact_type,
                render_type,
                is_renderable,
            )
            return {
                "created": True,
                "artifact": artifact.model_dump(),
                "render_type": render_type if is_renderable else None,
                "content": content,
                "ref_text": ref_text,
            }
        except Exception as e:
            logger.exception("render_artifact failed")
            return {"created": False, "reason": str(e)}

    def check_safety(
        self, messages: list[dict], output_budget: int | None = None
    ) -> dict:
        current_tokens = 0
        for msg in messages:
            content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
            current_tokens += count_tokens(content)
        effective_budget = (
            output_budget
            if output_budget and output_budget > 0
            else self.config.context_budget_default
        )
        remaining = effective_budget - current_tokens
        safe = remaining >= 0
        logger.info(
            "check_safety: current=%d budget=%d remaining=%d safe=%s",
            current_tokens,
            effective_budget,
            remaining,
            safe,
        )
        return {
            "safe": safe,
            "current_tokens": current_tokens,
            "remaining_tokens": remaining,
        }

    def inject(
        self, messages: list[dict], output_budget: int | None = None
    ) -> dict:
        # NOTE: inject 当前仅做 token 预算检查，不注入也不修改 messages。
        # 调用方应据返回的 safe 标志自行决定是否压缩，勿依赖注入副作用。
        total_tokens = 0
        for msg in messages:
            content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
            total_tokens += count_tokens(content)
        effective_budget = (
            output_budget
            if output_budget and output_budget > 0
            else self.config.context_budget_default
        )
        safe = total_tokens <= effective_budget
        logger.info(
            "inject: messages=%d total_tokens=%d budget=%d safe=%s",
            len(messages),
            total_tokens,
            effective_budget,
            safe,
        )
        return {
            "messages": messages,
            "total_tokens": total_tokens,
            "safe": safe,
            "injected": False,
            "note": "no-op: budget check only, messages unchanged",
        }

    def interact_artifact(
        self,
        artifact_id: str,
        action: str,
        payload: dict | None = None,
        session_id: str | None = None,
    ) -> dict:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        event = self.emit_event(
            "artifact.interaction",
            artifact_id=artifact_id,
            session_id=session_id,
            payload={"action": action, "payload": payload or {}},
        )
        logger.info(
            "interact_artifact: %s action=%s event=%s", artifact_id, action, event.event_id
        )
        return {
            "ok": True,
            "artifact_id": artifact_id,
            "action": action,
            "event_id": event.event_id,
            "dispatched": False,
            "note": "stub: records interaction event only, no action dispatch",
        }

    async def sync_artifact_file(
        self, artifact_id: str, file_path: str, direction: str
    ) -> dict:
        if direction not in ("artifact_to_code", "code_to_artifact"):
            raise ValueError(
                "direction must be 'artifact_to_code' or 'code_to_artifact'"
            )
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        path = Path(file_path).expanduser()
        # C-1: 路径必须落在配置的 sync_root 内，杜绝任意文件写/读 (LFI)
        sync_root = self.config.sync_root
        if sync_root is None:
            raise PermissionError(
                "sync_root not configured; refuse sync_artifact_file"
            )
        root_resolved = sync_root.resolve()
        try:
            target_resolved = path.resolve()
            target_resolved.relative_to(root_resolved)
        except ValueError as e:
            logger.warning(
                "sync path %s escapes sync_root %s", file_path, sync_root
            )
            raise PermissionError(
                f"file_path must be within sync_root: {file_path}"
            ) from e
        if direction == "artifact_to_code":
            version = self.get_version_content(artifact_id)
            if version is None:
                raise ValueError(f"No version found for artifact: {artifact_id}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(version.content, encoding="utf-8")
            logger.info(
                "sync_artifact_file: artifact %s -> %s (%d bytes)",
                artifact_id,
                file_path,
                len(version.content),
            )
        else:
            if not path.exists():
                raise ValueError(f"File not found: {file_path}")
            new_content = path.read_text(encoding="utf-8")
            await self.create_version(
                artifact_id, new_content, f"sync from {file_path}"
            )
            logger.info(
                "sync_artifact_file: %s -> artifact %s (%d bytes)",
                file_path,
                artifact_id,
                len(new_content),
            )
        return {
            "ok": True,
            "direction": direction,
            "artifact_id": artifact_id,
            "file_path": file_path,
        }

    def close(self) -> None:
        # A-4: 优雅停机——先通知 EventBus 关流让 SSE handler 退出，再关 storage，
        # 避免线程悬在已关闭 storage handle 上
        try:
            self.event_bus.shutdown()
        except Exception:
            logger.exception("EventBus shutdown error during close")
        self.storage.close()
        logger.info("ArtifactEngine closed")
