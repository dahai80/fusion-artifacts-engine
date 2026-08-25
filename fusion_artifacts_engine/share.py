import logging
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

from fusion_artifacts_engine.models import ArtifactShare
from fusion_artifacts_engine.render import render_share_html

logger = logging.getLogger(__name__)


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


# H7: share 访问控制 + R2 内存缓冲从 engine.py 抽出为独立类。
# engine 持有 ShareManager 实例，方法委托。行为不变。


class ShareManager:
    def __init__(self, engine):
        # engine 引用用于回调查 storage/config/get_version_content/emit_event
        self.engine = engine
        # R2: share access_count 内存缓冲——公开 GET 只增内存计数，后台批量刷盘，
        # 避免病毒式传播时每次公开访问都持 _write_lock 写 SQLite（写放大 DoS）。
        self._buffer: dict[str, int] = {}
        self._lock = threading.Lock()
        self._flush_threshold = 50
        self._flush_interval = 5.0
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._flush_loop, name="share-access-flush", daemon=True
        )
        self._thread.start()

    @property
    def _storage(self):
        return self.engine.storage

    @property
    def _config(self):
        return self.engine.config

    def _flush_loop(self) -> None:
        # R2: 后台定期刷盘 share access 增量。daemon 线程，stop event 退出。
        while not self._stop.wait(self._flush_interval):
            try:
                self._flush()
            except Exception as e:
                logger.warning("share access flush failed: %s", e)

    def _flush(self) -> None:
        # R2: 原子取出缓冲增量，批量 UPDATE access_count += delta + last_access_at=now。
        with self._lock:
            if not self._buffer:
                return
            pending = self._buffer
            self._buffer = {}
        flushed = 0
        for share_id, delta in pending.items():
            try:
                self._storage.increment_share_access(share_id, delta=delta)
                flushed += 1
            except Exception as e:
                # 单条失败回填缓冲，下次再刷，不丢计数
                logger.warning("flush share %s delta=%d failed: %s", share_id, delta, e)
                with self._lock:
                    self._buffer[share_id] = self._buffer.get(share_id, 0) + delta
        if flushed:
            logger.debug("share access flushed: %d share(s)", flushed)

    def _buffer_access(self, share_id: str, share: ArtifactShare) -> None:
        # R2: 内存增计数。max_accesses 校验用 access_count（已持久化）+ buffer（未刷）。
        with self._lock:
            self._buffer[share_id] = self._buffer.get(share_id, 0) + 1
            buffered = self._buffer[share_id]
        if buffered >= self._flush_threshold:
            self._flush()

    def buffered_access_count(self, share_id: str, persisted: int) -> int:
        # R2: 含缓冲的总访问数，供 E2 max_accesses 上限校验
        with self._lock:
            return persisted + self._buffer.get(share_id, 0)

    def shutdown(self) -> None:
        # R2: 关停刷盘线程前最终 flush，防计数丢失
        self._stop.set()
        try:
            self._flush()
        except Exception as e:
            logger.warning("final share access flush failed: %s", e)

    def create_share(
        self,
        artifact_id: str,
        created_by: str | None = None,
        expires_at: str | None = None,
        max_accesses: int | None = None,
    ) -> ArtifactShare:
        artifact = self._storage.get_artifact(artifact_id)
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
            raise PermissionError(f"Only the owner can share artifact {artifact_id}")
        # E2: 校验 max_accesses——必须为正整数，None=不限
        if max_accesses is not None and max_accesses < 1:
            logger.warning("create_share bad max_accesses %r", max_accesses)
            raise ValueError(f"max_accesses must be >= 1 or null: {max_accesses}")
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
                max_ttl_days = self._config.share_max_ttl_days
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
        existing = self._storage.get_share_by_artifact(artifact_id)
        if existing is not None:
            logger.info(
                "Returning existing share %s for artifact %s",
                existing.share_id,
                artifact_id,
            )
            return existing
        share_id = f"shr_{uuid.uuid4().hex[:12]}"
        # E1: created_at 用 float epoch，与 Artifact/Version 一致
        share = ArtifactShare(
            share_id=share_id,
            artifact_id=artifact_id,
            created_by=created_by,
            created_at=time.time(),
            expires_at=expires_at,
            max_accesses=max_accesses,
        )
        self._storage.save_share(share)
        self._storage.set_artifact_share_id(artifact_id, share_id)
        logger.info(
            "Created share %s for artifact %s max_accesses=%s",
            share_id, artifact_id, max_accesses,
        )
        return share

    def get_shared_artifact(self, share_id: str) -> dict | None:
        share = self._storage.get_share(share_id)
        if share is None:
            return None
        if share.revoked:
            logger.info("Share %s is revoked", share_id)
            return None
        if _is_expired(share.expires_at):
            logger.info("Share %s expired", share_id)
            return None
        # E2: 超过 max_accesses 上限则拒绝（在 increment 前，含缓冲值）
        effective_count = self.buffered_access_count(share_id, share.access_count)
        if share.max_accesses is not None and effective_count >= share.max_accesses:
            logger.info(
                "Share %s exhausted: %d >= %d", share_id,
                effective_count, share.max_accesses,
            )
            return None
        artifact = self._storage.get_artifact(share.artifact_id)
        if artifact is None:
            return None
        # L-2: increment 必须在 artifact 存在性校验之后，否则已删 artifact 会污染 access 统计
        self._storage.increment_share_access(share_id)
        version = self.engine.get_version_content(share.artifact_id)
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
        ok = self._storage.revoke_share(share_id)
        logger.info("Revoked share %s ok=%s", share_id, ok)
        return ok

    def get_public_share(self, share_id: str) -> dict:
        share = self._storage.get_share(share_id)
        if share is None:
            logger.info("Public share %s not found", share_id)
            return {"status": "not_found"}
        if share.revoked:
            logger.info("Public share %s revoked", share_id)
            return {"status": "gone", "reason": "revoked"}
        if _is_expired(share.expires_at):
            logger.info("Public share %s expired", share_id)
            return {"status": "gone", "reason": "expired"}
        # E2: 超过 max_accesses 上限则返回 410 Gone（在 increment 前，含缓冲值）
        effective_count = self.buffered_access_count(share_id, share.access_count)
        if share.max_accesses is not None and effective_count >= share.max_accesses:
            logger.info(
                "Public share %s exhausted: %d >= %d", share_id,
                effective_count, share.max_accesses,
            )
            return {"status": "gone", "reason": "exhausted"}
        artifact = self._storage.get_artifact(share.artifact_id)
        if artifact is None:
            logger.warning("Public share %s: artifact %s missing", share_id, share.artifact_id)
            return {"status": "not_found"}
        # L-2: increment 必须在 artifact 存在性校验之后，否则已删 artifact 会污染 access 统计
        # R2: 公开端点走内存缓冲（不持写锁），后台批量刷盘
        self._buffer_access(share_id, share)
        version = self.engine.get_version_content(share.artifact_id)
        raw_content = version.content if version else ""
        rendered_html = render_share_html(artifact, raw_content)
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
            "max_accesses": share.max_accesses,
            "access_count": effective_count + 1,
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
