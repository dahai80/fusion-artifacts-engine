import asyncio
import json
import logging
import shutil
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# #84: fusion-design [Save to Artifacts] 的 file-IPC 消费端。
# 生产者契约（fusion-design fd-ecosystem EcosystemLink::send，已冻结）：
#   目录 <ipc_base>/fusion-artifacts-engine/
#   文件名 <ns>_<pid>_<seq>.json（上限 8MB）
#   消息 {"target":{"Custom":"fusion-artifacts-engine"},"action":"save_artifact",
#         "payload":{"source":"fusion-design","title":...,"session":...,"version":...,
#                    "document":"<PenDocument JSON>","design_system":...}}
# 处理策略：按 session 归并为一个 artifact（同一设计会话版本递进），处理成功移入
# .processed/，畸形/超限移入 .failed/，避免重启重复摄入。

_SOURCE_MODULE = "fusion-design"
_ACTION = "save_artifact"
_MAX_MESSAGE_BYTES = 8 * 1024 * 1024
_MAX_PARSE_ATTEMPTS = 3


class DesignIPCConsumer:
    # v0.7.0: 参数化为通用 file-IPC 消费端，子类只覆写来源与 _ingest。
    _source_module = "fusion-design"
    _thread_name = "fae-design-ipc"
    _log_tag = "design IPC"
    _session_key = "design_session"
    _version_key = "design_version"

    def __init__(
        self,
        engine,
        directory: Path,
        poll_interval: float = 3.0,
        max_message_bytes: int = _MAX_MESSAGE_BYTES,
    ) -> None:
        self._engine = engine
        self._dir = Path(directory)
        self._interval = max(0.5, float(poll_interval))
        self._max_bytes = max_message_bytes
        self._processed_dir = self._dir / ".processed"
        self._failed_dir = self._dir / ".failed"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._attempts: dict[str, int] = {}

    def start(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._loop, name=self._thread_name, daemon=True)
        self._thread.start()
        logger.info(
            "%s consumer started (dir=%s interval=%.1fs)", self._log_tag, self._dir, self._interval
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 2)
            self._thread = None
        logger.info("%s consumer stopped", self._log_tag)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.process_once()
            except Exception:  # noqa: BLE001
                logger.exception("%s poll failed", self._log_tag)

    def process_once(self) -> int:
        if not self._dir.is_dir():
            return 0
        ingested = 0
        for path in sorted(self._dir.glob("*.json")):
            if not path.is_file():
                continue
            try:
                if self._process_file(path):
                    ingested += 1
            except Exception:  # noqa: BLE001
                logger.exception("%s file failed: %s", self._log_tag, path)
                self._archive(path, self._failed_dir)
        return ingested

    def _process_file(self, path: Path) -> bool:
        size = path.stat().st_size
        if size > self._max_bytes:
            logger.warning(
                "%s message too large (%d bytes), rejecting: %s", self._log_tag, size, path
            )
            self._archive(path, self._failed_dir)
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            attempts = self._attempts.get(str(path), 0) + 1
            self._attempts[str(path)] = attempts
            logger.warning(
                "%s unreadable message (%d/%d): %s: %s",
                self._log_tag,
                attempts,
                _MAX_PARSE_ATTEMPTS,
                path,
                e,
            )
            if attempts >= _MAX_PARSE_ATTEMPTS:
                self._archive(path, self._failed_dir)
            return False
        self._attempts.pop(str(path), None)
        if not self._is_save_artifact(data):
            logger.warning(
                "%s unsupported message (action=%r), archiving", self._log_tag, data.get("action")
            )
            self._archive(path, self._processed_dir)
            return False
        payload = data.get("payload") or {}
        try:
            created = self._ingest(payload)
        except Exception:  # noqa: BLE001
            logger.exception("%s ingest failed: %s", self._log_tag, path)
            self._archive(path, self._failed_dir)
            return False
        self._archive(path, self._processed_dir)
        logger.info(
            "%s ingested %s (%s)", self._log_tag, path.name, "created" if created else "skipped"
        )
        return created

    def _is_save_artifact(self, data: dict) -> bool:
        if data.get("action") != _ACTION:
            return False
        target = data.get("target")
        if isinstance(target, dict):
            custom = target.get("Custom")
            if custom not in (None, "fusion-artifacts-engine"):
                return False
        return isinstance(data.get("payload"), dict)

    def _ingest(self, payload: dict) -> bool:
        session = str(payload.get("session") or "default")
        version = int(payload.get("version") or 1)
        title = str(payload.get("title") or "Design")
        document = payload.get("document")
        if isinstance(document, (dict, list)):
            document = json.dumps(document, ensure_ascii=False)
        if not isinstance(document, str) or not document:
            raise ValueError("save_artifact payload missing document")
        metadata_common = {
            "source": self._source_module,
            self._session_key: session,
            self._version_key: version,
        }

        existing = self._find_artifact(session)
        if existing is None:
            artifact, _version, _ref = asyncio.run(
                self._engine.create_external_artifact(
                    self._source_module,
                    session,
                    title,
                    "data",
                    document,
                    metadata=metadata_common,
                )
            )
            self._publish("artifact.created", artifact.id, artifact.kind)
            logger.info(
                "%s created artifact %s for session %s v%d",
                self._log_tag,
                artifact.id,
                session,
                version,
            )
            return True

        stored_version = int((existing.metadata or {}).get(self._version_key) or 0)
        if version <= stored_version:
            logger.info(
                "%s skip duplicate: session=%s version=%d <= stored=%d",
                self._log_tag,
                session,
                version,
                stored_version,
            )
            return False
        asyncio.run(
            self._engine.create_version(
                existing.id,
                document,
                change_log=f"Design save v{version}",
                source="ai_generation",
            )
        )
        refreshed = self._engine.storage.get_artifact(existing.id)
        if refreshed is not None:
            merged = dict(refreshed.metadata or {})
            merged.update(metadata_common)
            refreshed.metadata = merged
            self._engine.storage.save_artifact(refreshed)
        self._publish("artifact.version_created", existing.id, existing.kind)
        logger.info("%s bumped artifact %s to v%d", self._log_tag, existing.id, version)
        return True

    def _publish(self, event_type: str, artifact_id: str, kind: str | None) -> None:
        # SSE 订阅方按 kind 过滤：缺 kind 的事件会被静默丢弃（与 RPC _publish 同契约）
        try:
            self._engine.event_bus.publish(event_type, {"kind": kind, "artifact_id": artifact_id})
        except Exception:  # noqa: BLE001
            logger.exception("%s event publish failed: %s", self._log_tag, event_type)

    def _find_artifact(self, session: str):
        session_id = f"ext_{self._source_module}_{session}"
        artifacts = self._engine.storage.list_artifacts(session_id)
        matches = [a for a in artifacts if (a.metadata or {}).get(self._session_key) == session]
        if not matches:
            return None
        matches.sort(key=lambda a: a.updated_at, reverse=True)
        return matches[0]

    def _archive(self, path: Path, target_dir: Path) -> None:
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / path.name
            if target.exists():
                target = target_dir / f"{int(time.time() * 1000)}_{path.name}"
            shutil.move(str(path), str(target))
            logger.info("%s archived %s -> %s", self._log_tag, path.name, target_dir.name)
        except OSError as e:
            logger.error("%s archive failed %s: %s", self._log_tag, path, e)


class OfficeIPCConsumer(DesignIPCConsumer):
    # v0.7.0: fusion-office 文件摄取。payload 带 file_path（office 二进制不进
    # JSON），解析为带 # 标题的 markdown（section_index 可寻址），元数据记
    # office_format/office_source_path 供写回定位。
    _source_module = "fusion-office"
    _thread_name = "fae-office-ipc"
    _log_tag = "office IPC"
    _session_key = "office_session"
    _version_key = "office_version"

    def _ingest(self, payload: dict) -> bool:
        from fusion_artifacts_engine.office_ingest import parse_office

        session = str(payload.get("session") or "default")
        version = int(payload.get("version") or 1)
        title = str(payload.get("title") or "Document")
        file_path = str(payload.get("file_path") or "")
        if not file_path:
            raise ValueError("office save_artifact payload missing file_path")
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"office file not found: {file_path}")
        content, fmt, stats = parse_office(path)
        metadata_common = {
            "source": self._source_module,
            self._session_key: session,
            self._version_key: version,
            "office_format": fmt,
            "office_source_path": str(path),
            "office_stats": stats,
        }

        existing = self._find_artifact(session)
        if existing is None:
            artifact, _version, _ref = asyncio.run(
                self._engine.create_external_artifact(
                    self._source_module,
                    session,
                    title,
                    "markdown",
                    content,
                    metadata=metadata_common,
                )
            )
            self._publish("artifact.created", artifact.id, artifact.kind)
            logger.info(
                "%s created artifact %s for session %s v%d (%s)",
                self._log_tag,
                artifact.id,
                session,
                version,
                fmt,
            )
            return True

        stored_version = int((existing.metadata or {}).get(self._version_key) or 0)
        if version <= stored_version:
            logger.info(
                "%s skip duplicate: session=%s version=%d <= stored=%d",
                self._log_tag,
                session,
                version,
                stored_version,
            )
            return False
        asyncio.run(
            self._engine.create_version(
                existing.id,
                content,
                change_log=f"Office save v{version}",
                source="ai_generation",
            )
        )
        refreshed = self._engine.storage.get_artifact(existing.id)
        if refreshed is not None:
            merged = dict(refreshed.metadata or {})
            merged.update(metadata_common)
            refreshed.metadata = merged
            self._engine.storage.save_artifact(refreshed)
        self._publish("artifact.version_created", existing.id, existing.kind)
        logger.info("%s bumped artifact %s to v%d", self._log_tag, existing.id, version)
        return True
