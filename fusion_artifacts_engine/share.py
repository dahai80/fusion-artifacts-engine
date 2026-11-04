import logging
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

from fusion_artifacts_engine.models import ArtifactShare
from fusion_artifacts_engine.render import render_share_html

logger = logging.getLogger(__name__)


def _is_expired(expires_at: str | None) -> bool:
    # L-1: fail-closed - only a missing expiry passes; unparseable values are always
    # treated as expired and access is denied
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
    # L-6: parse expires_at into an aware datetime; None passes, unparseable raises ValueError
    if not expires_at:
        return None
    try:
        exp = datetime.fromisoformat(expires_at)
    except ValueError as e:
        raise ValueError(f"Invalid expires_at format: {expires_at!r}") from e
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    return exp


# H7: share access control + R2 memory buffering extracted from engine.py into this
# standalone class. The engine holds a ShareManager instance and delegates. Behavior unchanged.


class ShareManager:
    def __init__(self, engine):
        # engine reference for looking back into storage/config/get_version_content/emit_event
        self.engine = engine
        # R2: in-memory buffer for share access_count - public GETs only bump the in-memory
        # counter, flushed to disk in batches by a background thread, so viral traffic does
        # not take _write_lock and write SQLite on every public access (write-amplification DoS).
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
        # R2: background thread periodically flushes share access deltas. Daemon thread,
        # exits via the stop event.
        while not self._stop.wait(self._flush_interval):
            try:
                self._flush()
            except Exception as e:
                logger.warning("share access flush failed: %s", e)

    def _flush(self) -> None:
        # R2: atomically take the buffered deltas, then batch UPDATE access_count += delta
        # plus last_access_at=now.
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
                # On a per-row failure, backfill the buffer and retry next flush; no count loss
                logger.warning("flush share %s delta=%d failed: %s", share_id, delta, e)
                with self._lock:
                    self._buffer[share_id] = self._buffer.get(share_id, 0) + delta
        if flushed:
            logger.debug("share access flushed: %d share(s)", flushed)

    def buffered_access_count(self, share_id: str, persisted: int) -> int:
        # R2: total access count including the buffer, for the E2 max_accesses cap check
        with self._lock:
            return persisted + self._buffer.get(share_id, 0)

    def try_consume_access(
        self, share_id: str, persisted: int, max_accesses: int | None
    ) -> int | None:
        # P1-2/H5: atomic check-and-increment. Check and increment are merged into a single
        # _lock-held section, closing the TOCTOU window in get_public_share between
        # buffered_access_count (check) and _buffer_access (bump) - under a concurrent burst,
        # N requests could all pass the check before each bumped the count, exceeding
        # max_accesses. Returns the effective_count after the bump (allowed); None means the
        # cap is reached (denied).
        with self._lock:
            buffered = self._buffer.get(share_id, 0)
            effective_count = persisted + buffered
            if max_accesses is not None and effective_count >= max_accesses:
                return None
            self._buffer[share_id] = buffered + 1
            new_count = effective_count + 1
        if self._buffer.get(share_id, 0) >= self._flush_threshold:
            self._flush()
        return new_count

    def shutdown(self) -> None:
        # R2: final flush before shutting down the flush thread, so no counts are lost
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
        permission: str = "view",
    ) -> ArtifactShare:
        # #77: permission validation up front (early failure point beyond the model validator)
        if permission not in ("view", "remix"):
            raise ValueError(f"share permission must be 'view' or 'remix', got: {permission!r}")
        artifact = self._storage.get_artifact(artifact_id)
        if artifact is None:
            raise ValueError(f"Artifact not found: {artifact_id}")
        # C-9: owner-only sharing - when the artifact has owner_user_id, created_by must match
        if artifact.owner_user_id and created_by != artifact.owner_user_id:
            logger.warning(
                "Share denied: caller %s != owner %s for artifact %s",
                created_by,
                artifact.owner_user_id,
                artifact_id,
            )
            raise PermissionError(f"Only the owner can share artifact {artifact_id}")
        # E2: validate max_accesses - must be a positive integer, None = unlimited
        if max_accesses is not None and max_accesses < 1:
            logger.warning("create_share bad max_accesses %r", max_accesses)
            raise ValueError(f"max_accesses must be >= 1 or null: {max_accesses}")
        # L-6: validate expires_at format/range - reject past dates and beyond max TTL
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
            # #77: permission change on the same link - with an existing share, upsert the
            # permission so it takes effect on the next open
            if existing.permission != permission:
                self._storage.update_share_permission(existing.share_id, permission)
                existing = self._storage.get_share(existing.share_id)
                logger.info(
                    "Updated existing share %s permission=%s for artifact %s",
                    existing.share_id, permission, artifact_id,
                )
            else:
                logger.info(
                    "Returning existing share %s for artifact %s",
                    existing.share_id,
                    artifact_id,
                )
            return existing
        share_id = f"shr_{uuid.uuid4().hex[:12]}"
        # E1: created_at as a float epoch, consistent with Artifact/Version
        share = ArtifactShare(
            share_id=share_id,
            artifact_id=artifact_id,
            created_by=created_by,
            created_at=time.time(),
            expires_at=expires_at,
            max_accesses=max_accesses,
            permission=permission,
        )
        self._storage.save_share(share)
        self._storage.set_artifact_share_id(artifact_id, share_id)
        logger.info(
            "Created share %s for artifact %s max_accesses=%s permission=%s",
            share_id, artifact_id, max_accesses, permission,
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
        # L-2: artifact existence check comes before the access count, otherwise a deleted
        # artifact would pollute access stats.
        artifact = self._storage.get_artifact(share.artifact_id)
        if artifact is None:
            return None
        # P1-2/H5: atomic check-and-increment - a single conditional UPDATE atomically does
        # "bump only if under the cap", closing the read-access_count-then-increment TOCTOU
        # (concurrent bursts could exceed max_accesses).
        if not self._storage.try_increment_share_access(share_id, share.max_accesses):
            logger.info("Share %s exhausted: access_count >= max_accesses %s", share_id, share.max_accesses)
            return None
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
        # L-2: artifact existence check comes before the access count, otherwise a deleted
        # artifact would pollute access stats. No count change happens here, only the artifact
        # read, so moving it before try_consume_access does not break L-2.
        artifact = self._storage.get_artifact(share.artifact_id)
        if artifact is None:
            logger.warning("Public share %s: artifact %s missing", share_id, share.artifact_id)
            return {"status": "not_found"}
        # P1-2/H5: atomic check-and-increment - the check and the buffer bump are merged into
        # one _lock section, closing the TOCTOU window where, under a concurrent burst, many
        # requests all pass the check before each bumps the count and max_accesses is exceeded.
        # R2: the public endpoint uses the memory buffer (no write lock), flushed in batches
        # by a background thread.
        new_count = self.try_consume_access(share_id, share.access_count, share.max_accesses)
        if new_count is None:
            logger.info(
                "Public share %s exhausted: access_count >= max_accesses %d",
                share_id, share.max_accesses,
            )
            return {"status": "gone", "reason": "exhausted"}
        version = self.engine.get_version_content(share.artifact_id)
        raw_content = version.content if version else ""
        rendered_html = render_share_html(artifact, raw_content)
        logger.info(
            "Public share %s served artifact %s (rendered, source isolated)",
            share_id,
            share.artifact_id,
        )
        # C-5: the public unauthenticated endpoint returns only display fields, never leaking
        # internal PII such as session_id/project_id/owner_user_id/metadata/content_hash/
        # folder_id/created_by
        public_share = {
            "share_id": share.share_id,
            "expires_at": share.expires_at,
            "revoked": share.revoked,
            "max_accesses": share.max_accesses,
            "access_count": new_count,
            "permission": share.permission,
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
