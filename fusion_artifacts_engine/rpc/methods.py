import asyncio
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Any

from fusion_artifacts_engine import ai_client, long_context
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.office_ingest import maybe_write_back
from fusion_artifacts_engine.rpc.errors import (
    BusinessRuleError,
    NotFoundError,
    NotImplementedError,
    PermissionError,
    RpcError,
)
from fusion_artifacts_engine.section_index import (
    build_toc,
    find_section_bounds,
    read_line_range,
)
from fusion_artifacts_engine.utils import deep_link_url, get_package_version

logger = logging.getLogger(__name__)


class RPCHandler:
    def __init__(self, engine: ArtifactEngine):
        self.engine = engine
        self._method_map = self._build_methods()
        # v0.7.1 #90: per-version cache for artifact.toc (key=(artifact_id, version_num));
        # a new version = new key auto-invalidates; LRU cap keeps many artifacts from exhausting memory.
        self._toc_cache: OrderedDict[tuple[str, int], list[dict]] = OrderedDict()
        # v0.7.1 #91: strong-reference set for background long-context tasks (prevents create_task GC)
        self._bg_tasks: set[asyncio.Task] = set()

    def _spawn_long_context(self, pre, version_num: int) -> None:
        # #91: rolling summary + RAG archival are both background tasks - the mlx 60s / rag 30s
        # timeouts must never block the patch/update response. Failures are logged inside the
        # task and never affect the RPC result.
        superseded = pre.current_version
        for coro in (
            long_context.maybe_archive_superseded(self.engine, pre, superseded),
            long_context.maybe_generate_summary(self.engine, pre, version_num),
        ):
            task = asyncio.create_task(coro)
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

    async def dispatch(self, method: str, params: dict) -> Any:
        # P2-5/H12(trace): the rpc.server.duration span covers the whole method execution (storage I/O included).
        # dispatch is the single routing entry for all RPCs (server _handle / REST / direct calls all go
        # through it), so the span lives here rather than at the server layer - every call path captures
        # the causal chain. tracing.span is a no-op when tracing is not enabled, zero overhead.
        from fusion_artifacts_engine import tracing

        with tracing.span("rpc.server.duration", rpc_method=method):
            handler = self._method_map.get(method)
            if handler is None:
                raise RpcError(-32601, f"Method not found: {method}")
            return await handler(params)

    async def _publish(self, event_type: str, aid: str | None, **extra) -> None:
        # SSE ?kind= filtering is by artifact kind (app/code/...), not by event name.
        # Every artifact event must carry kind, otherwise subscribers filtering by kind silently drop it.
        # L-3: callers may pass kind= via extra to override (delete events use the pre-delete kind,
        # A-2: use the engine instance's EventBus to avoid cross-engine stream leakage
        # E10: log a warning when kind is still None after override + re-read, so silent event loss stays debuggable
        # P2-2/M1+F2: the kind re-read is offloaded via to_thread so the event loop is not blocked
        kind = extra.get("kind")
        if kind is None and aid:
            artifact = await asyncio.to_thread(
                self.engine.storage.get_artifact, aid
            )
            if artifact is not None:
                kind = artifact.kind
        if kind is None:
            logger.warning(
                "publish %s for artifact %s has no kind (SSE kind-filter will drop)",
                event_type, aid,
            )
        payload = {"event_type": event_type, "kind": kind}
        payload.update(extra)
        self.engine.event_bus.publish(event_type, payload)
        # #100-2: events are also persisted - list_events/audit depend on durable events. Previously they
        # only went to in-memory SSE and artifact.list_events returned empty after create/update/rollback;
        # persistence failure does not block the main operation.
        if aid:
            try:
                await asyncio.to_thread(
                    self.engine.emit_event,
                    event_type,
                    aid,
                    extra.get("session_id"),
                    payload,
                )
            except Exception as e:
                logger.error("Persist event failed (non-fatal): %s type=%s aid=%s", e, event_type, aid)

    def _enforce_owner(self, params: dict, artifact) -> None:
        # P2-3/MEDIUM-4: IDOR protection. Write ops + get verify caller identity ownership.
        # Single-tenant default omits caller_user_id (None) -> skipped, keeping backward compatibility.
        # Multi-tenant deployments inject caller_user_id per request; a mismatch with the artifact
        # owner_user_id raises PermissionError(-32006). When either side is None, no check is made (missing
        # ownership or missing identity both pass, avoiding false positives on single-tenant defaults).
        caller = params.get("caller_user_id")
        if not caller:
            return
        if artifact is None:
            return
        owner = getattr(artifact, "owner_user_id", None)
        if owner is None:
            return
        if caller != owner:
            logger.warning(
                "IDOR denied: caller=%s owner=%s artifact_id=%s",
                caller, owner, getattr(artifact, "id", "?"),
            )
            raise PermissionError(
                f"Permission denied: caller {caller} is not owner of artifact"
            )

    def _build_methods(self) -> dict:
        return {
            "artifact.create": self._create,
            "artifact.get": self._get,
            "artifact.get_content": self._get_content,
            "artifact.list": self._list,
            "artifact.delete": self._delete,
            "artifact.update": self._update,
            "artifact.version_list": self._version_list,
            "artifact.version_rollback": self._version_rollback,
            "artifact.export": self._export,
            "artifact.export_session": self._export_session,
            "artifact.import": self._import_artifact,
            "artifact.export_code": self._export_code,
            "artifact.import_code": self._import_code,
            "artifact.watch": self._watch,
            # P1: lifecycle + global repo
            "artifact.rename": self._rename,
            "artifact.star": self._star,
            "artifact.pin": self._pin,
            "artifact.duplicate": self._duplicate,
            "artifact.remix": self._remix,
            "artifact.list_all": self._list_all,
            # P1: recycle bin
            "artifact.list_recycle": self._list_recycle,
            "artifact.restore": self._restore,
            "artifact.purge_expired": self._purge_expired,
            # P2: snapshots
            "artifact.create_snapshot": self._create_snapshot,
            "artifact.list_snapshots": self._list_snapshots,
            # P1: share
            "artifact.create_share": self._create_share,
            "artifact.get_shared": self._get_shared,
            "artifact.revoke_share": self._revoke_share,
            # P2: folders
            "artifact.create_folder": self._create_folder,
            "artifact.list_folders": self._list_folders,
            "artifact.rename_folder": self._rename_folder,
            "artifact.delete_folder": self._delete_folder,
            "artifact.move_to_folder": self._move_to_folder,
            # P4: tags
            "artifact.add_tag": self._add_tag,
            "artifact.remove_tag": self._remove_tag,
            "artifact.list_tags": self._list_tags,
            "artifact.list_artifact_tags": self._list_artifact_tags,
            # P4: events
            "artifact.emit_event": self._emit_event,
            # #119: workflow-facing facade (cowork product_lifecycle)
            "artifact.workflow.create": self._workflow_create,
            "artifact.workflow.revision": self._workflow_revision,
            "artifact.comment": self._comment,
            "artifact.list_comments": self._list_comments,
            "artifact.list_events": self._list_events,
            # P3: project KB
            "artifact.move_to_project_kb": self._move_to_project_kb,
            # external module
            "artifact.create_external": self._create_external,
            "artifact.list_by_source": self._list_by_source,
            # AE-1: patch_artifact
            "artifact.patch": self._patch,
            # v0.7.1 #90: chunked read path (TOC + line-range/section views)
            "artifact.toc": self._toc,
            "artifact.view": self._view,
            # AE-2: load_artifact
            "artifact.load": self._load,
            # AE-6: context_budget
            "context.budget": self._budget,
            # AE-7: auto_compact
            "artifact.auto_compact": self._auto_compact,
            # #36: version_diff
            "artifact.version_diff": self._version_diff,
            # #37: render / check_safety / inject / interact / sync
            "artifact.render": self._render,
            "artifact.check_safety": self._check_safety,
            "artifact.inject": self._inject,
            "artifact.interact": self._interact,
            "artifact.sync": self._sync,
            # #58/#59/#60: visual engine — DSL parse + 8 primitive compilers + 3-level fallback
            "render.visual": self._render_visual,
            # v0.7.5 #97: webui AI generate/iterate entry (via the fusion-mlx OpenAI-compatible API)
            "ai.generate": self._ai_generate,
            "ai.list_models": self._ai_list_models,
            "ping": self._ping,
        }

    async def _create(self, params: dict) -> dict:
        valid_types = ("code", "markdown", "html", "react", "svg", "mermaid", "data")
        if params.get("type") not in valid_types:
            raise ValueError(f"Invalid type, must be one of {valid_types}")
        valid_kinds = ("app", "code", "document", "game", "tool", "template")
        kind = params.get("kind")
        if kind is not None and kind not in valid_kinds:
            raise ValueError(f"Invalid kind, must be one of {valid_kinds}")
        artifact, version, ref_text = await self.engine.create_artifact(
            session_id=params["session_id"],
            name=params["name"],
            artifact_type=params["type"],
            content=params["content"],
            summary=params.get("summary", ""),
            change_log=params.get("change_log", "Initial version"),
            kind=kind,
            project_id=params.get("project_id"),
            metadata=params.get("metadata"),
            # P2-3/MEDIUM-4: owner_user_id can be injected at creation (multi-tenant ownership);
            # default None = single-tenant
            owner_user_id=params.get("owner_user_id"),
            ownership_type=params.get("ownership_type"),
        )
        await self._publish(
            "artifact.created", artifact.id, artifact_id=artifact.id, kind=artifact.kind
        )
        return {
            "artifact": artifact.model_dump(),
            "version": version.model_dump(),
            "ref_text": ref_text,
        }

    async def _get(self, params: dict) -> dict:
        # P2-2/M1+F2: read path offloaded via to_thread so the event loop is not blocked
        artifact = await asyncio.to_thread(
            self.engine.get_artifact,
            params["artifact_id"], project_id=params.get("project_id"),
        )
        if artifact is None:
            raise NotFoundError(f"Artifact not found: {params['artifact_id']}")
        # P2-3/MEDIUM-4: IDOR - verify ownership before read (active when caller_user_id is
        # injected for multi-tenant)
        self._enforce_owner(params, artifact)
        return {"artifact": artifact.model_dump()}

    async def _get_content(self, params: dict) -> dict:
        # P2-3/MEDIUM-4: IDOR - fetch the artifact and verify ownership before reading content (active
        # when caller_user_id is injected for multi-tenant)
        if params.get("caller_user_id"):
            art = await asyncio.to_thread(
                self.engine.storage.get_artifact, params["artifact_id"]
            )
            self._enforce_owner(params, art)
        version = params.get("version")
        if isinstance(version, str) and version != "latest":
            try:
                version = int(version)
            except ValueError:
                pass
        if version == "latest":
            version = None
        # P2-2/M1+F2: content read offloaded via to_thread
        result = await asyncio.to_thread(
            self.engine.get_version_content, params["artifact_id"], version
        )
        if result is None:
            raise NotFoundError("Version not found")
        return {
            "content": result.content,
            "token_count": result.token_count,
            "version": result.version_num,
        }

    async def _list(self, params: dict) -> dict:
        # P2-2/M1+F2: list read offloaded via to_thread
        artifacts = await asyncio.to_thread(
            self.engine.list_artifacts,
            params["session_id"],
            params.get("include_deleted", False),
            project_id=params.get("project_id"),
            metadata_filter=params.get("metadata_filter"),
        )
        return {"artifacts": [a.model_dump() for a in artifacts]}

    async def _delete(self, params: dict) -> dict:
        aid = params["artifact_id"]
        # L-3: cache kind before delete; after a hard delete get_artifact returns None, dropping
        # P2-2/M1+F2: read offloaded via to_thread
        pre_artifact = await asyncio.to_thread(self.engine.storage.get_artifact, aid)
        pre_kind = pre_artifact.kind if pre_artifact is not None else None
        # P2-3/MEDIUM-4: IDOR - verify ownership before delete (active when caller_user_id is
        # injected for multi-tenant)
        self._enforce_owner(params, pre_artifact)
        # P2-2/M1+F2: writes are also offloaded via to_thread (delete holds the write lock; blocking the
        # event loop is equally bad)
        ok = await asyncio.to_thread(
            self.engine.delete_artifact,
            aid,
            params.get("soft_delete", True),
            project_id=params.get("project_id"),
        )
        # L-4: emit the delete event only when the delete succeeded, otherwise a phantom event fires
        # for an artifact that was not deleted
        if ok:
            await self._publish(
                "artifact.deleted", aid, artifact_id=aid, kind=pre_kind
            )
        else:
            logger.warning("artifact.delete no-op for %s, skip publish", aid)
        return {"ok": ok}

    async def _update(self, params: dict) -> dict:
        valid_sources = ("manual", "ai_generation")
        source = params.get("source", "manual")
        if source not in valid_sources:
            raise ValueError(f"Invalid source, must be one of {valid_sources}")
        # P-6: read kind via to_thread so the event loop is not blocked; cache it before the
        # change for _publish reuse
        pre = await asyncio.to_thread(
            self.engine.storage.get_artifact, params["artifact_id"]
        )
        # P2-3/MEDIUM-4: IDOR - verify ownership before update (active when caller_user_id is
        # injected for multi-tenant)
        self._enforce_owner(params, pre)
        version, ref_text = await self.engine.create_version(
            params["artifact_id"],
            params["content"],
            params.get("change_log", ""),
            source=source,
            expected_content_hash=params.get("expected_content_hash"),
        )
        await self._publish(
            "artifact.updated",
            params["artifact_id"],
            artifact_id=params["artifact_id"],
            kind=pre.kind if pre is not None else None,
        )
        # v0.7.0: after an office-sourced artifact update, write back to .outbox (failure is
        # logged only and does not affect the result)
        if pre is not None:
            await asyncio.to_thread(maybe_write_back, self.engine, pre, version.version_num)
            self._spawn_long_context(pre, version.version_num)
        return {"version": version.model_dump(), "ref_text": ref_text}

    async def _version_list(self, params: dict) -> dict:
        # P2-2/M1+F2: read offloaded via to_thread
        versions = await asyncio.to_thread(
            self.engine.list_versions,
            params["artifact_id"],
            page=int(params.get("page", 1)),
            page_size=int(params.get("page_size", 200)),
            include_content=bool(params.get("include_content", True)),
        )
        return {"versions": [v.model_dump() for v in versions]}

    async def _version_rollback(self, params: dict) -> dict:
        # P2-3/MEDIUM-4: IDOR - verify ownership before rollback (active when caller_user_id is
        # injected for multi-tenant)
        if params.get("caller_user_id"):
            art = await asyncio.to_thread(
                self.engine.storage.get_artifact, params["artifact_id"]
            )
            self._enforce_owner(params, art)
        version, ref_text = await self.engine.rollback_version(
            params["artifact_id"], params["target_version"]
        )
        await self._publish(
            "artifact.rolled_back",
            params["artifact_id"],
            artifact_id=params["artifact_id"],
            target_version=params["target_version"],
        )
        return {"version": version.model_dump(), "ref_text": ref_text}

    async def _export(self, params: dict) -> dict:
        # P2-2/M1+F2: read offloaded via to_thread
        artifact = await asyncio.to_thread(
            self.engine.get_artifact, params["artifact_id"]
        )
        if artifact is None:
            raise NotFoundError("Artifact not found")
        include_versions = params.get("include_versions", False)
        data = {"artifact": artifact.model_dump()}
        if include_versions:
            versions = await asyncio.to_thread(
                self.engine.list_versions, params["artifact_id"]
            )
            data["versions"] = [v.model_dump() for v in versions]
        else:
            latest = await asyncio.to_thread(
                self.engine.get_version_content, params["artifact_id"]
            )
            if latest:
                data["content"] = latest.content
        return {"data": data}

    async def _export_session(self, params: dict) -> dict:
        storage_root = Path(self.engine.config.storage_root).resolve()
        output_dir = Path(params["output_dir"]).resolve()
        try:
            output_dir.relative_to(storage_root)
        except ValueError:
            # P1-10/M18: error messages must not leak the storage_root filesystem path to callers;
            # the internal log keeps the path for ops localization, the external reply is a generic hint
            logger.warning(
                "export_session rejected: output_dir %s outside storage_root %s",
                output_dir, storage_root,
            )
            raise ValueError("output_dir must be under storage root (path traversal denied)")
        output_dir.mkdir(parents=True, exist_ok=True)
        # P2-2/M1+F2: list read offloaded via to_thread
        artifacts = await asyncio.to_thread(
            self.engine.list_artifacts, params["session_id"]
        )
        count = 0
        for art in artifacts:
            content = await asyncio.to_thread(
                self.engine.get_version_content, art.id
            )
            if content:
                safe_name = (
                    art.name.replace("/", "_")
                    .replace("\\", "_")
                    .replace("..", "_")
                    .replace("\x00", "_")
                )
                path = output_dir / safe_name
                try:
                    path.resolve().relative_to(storage_root)
                except ValueError:
                    logger.warning(
                        "Skipping artifact name that escapes export dir: %s", art.name
                    )
                    continue
                # P2-2/M1+F2: file write offloaded via to_thread (disk I/O blocking the event loop is equally bad)
                await asyncio.to_thread(
                    path.write_text, content.content, "utf-8"
                )
                count += 1
        return {"count": count, "path": str(output_dir)}

    async def _import_artifact(self, params: dict) -> dict:
        valid_types = ("code", "markdown", "html", "react", "data")
        data = params["data"]
        artifact_data = data.get("artifact", data)
        content = data.get("content", "")
        artifact_type = artifact_data.get("type", "code")
        if artifact_type not in valid_types:
            raise ValueError(f"Invalid type, must be one of {valid_types}")
        valid_kinds = ("app", "code", "document", "game", "tool", "template")
        kind = artifact_data.get("kind")
        if kind is not None and kind not in valid_kinds:
            raise ValueError(f"Invalid kind, must be one of {valid_kinds}")
        artifact, _version, ref_text = await self.engine.create_artifact(
            session_id=params["session_id"],
            name=artifact_data.get("name", "imported"),
            artifact_type=artifact_type,
            content=content,
            summary=artifact_data.get("summary", ""),
            kind=kind,
            project_id=params.get("project_id"),
            metadata=artifact_data.get("metadata"),
        )
        await self._publish(
            "artifact.created", artifact.id, artifact_id=artifact.id, kind=artifact.kind
        )
        return {"artifact": artifact.model_dump(), "ref_text": ref_text}

    async def _export_code(self, params: dict) -> dict:
        language = params.get("language", "")
        # P2-2/M1+F2: read offloaded via to_thread
        result = await asyncio.to_thread(
            self.engine.export_code, params["artifact_id"], language
        )
        return result

    async def _import_code(self, params: dict) -> dict:
        artifact, version, ref_text = await self.engine.import_code(
            session_id=params["session_id"],
            code=params["code"],
            language=params.get("language", ""),
            name=params.get("name", ""),
            metadata=params.get("metadata"),
        )
        await self._publish(
            "artifact.created", artifact.id, artifact_id=artifact.id, kind=artifact.kind
        )
        return {
            "artifact": artifact.model_dump(),
            "version": version.model_dump(),
            "ref_text": ref_text,
        }

    async def _watch(self, params: dict) -> dict:
        artifact_id = params["artifact_id"]
        action = params.get("action", "poll")
        since_version = params.get("since_version", 0)
        watcher_id = params.get("watcher_id", "")
        if action == "register":
            if not watcher_id:
                raise ValueError("watcher_id required for register action")
            # P2-2/M1+F2: write offloaded via to_thread
            await asyncio.to_thread(
                self.engine.register_watcher, artifact_id, watcher_id
            )
            return {"registered": True, "artifact_id": artifact_id}
        elif action == "unregister":
            if not watcher_id:
                raise ValueError("watcher_id required for unregister action")
            await asyncio.to_thread(
                self.engine.unregister_watcher, artifact_id, watcher_id
            )
            return {"unregistered": True, "artifact_id": artifact_id}
        elif action == "poll":
            # P2-2/M1+F2: read offloaded via to_thread
            events = await asyncio.to_thread(
                self.engine.get_watch_events, artifact_id, since_version
            )
            return {"artifact_id": artifact_id, "events": events}
        else:
            raise ValueError(
                f"Invalid action: {action}, must be register/unregister/poll"
            )

    async def _ping(self, params: dict) -> dict:
        return {"pong": True, "version": get_package_version()}

    async def _rename(self, params: dict) -> dict:
        # P2-2/M1+F2: write offloaded via to_thread
        ok = await asyncio.to_thread(
            self.engine.rename_artifact, params["artifact_id"], params["new_name"]
        )
        await self._publish(
            "artifact.renamed",
            params["artifact_id"],
            artifact_id=params["artifact_id"],
            new_name=params["new_name"],
        )
        return {"ok": ok}

    async def _star(self, params: dict) -> dict:
        # P2-2/M1+F2: write offloaded via to_thread
        ok = await asyncio.to_thread(
            self.engine.star_artifact,
            params["artifact_id"], params.get("starred", True),
        )
        await self._publish(
            "artifact.starred",
            params["artifact_id"],
            artifact_id=params["artifact_id"],
            starred=params.get("starred", True),
        )
        return {"ok": ok}

    async def _pin(self, params: dict) -> dict:
        # P2-2/M1+F2: write offloaded via to_thread
        ok = await asyncio.to_thread(
            self.engine.pin_artifact,
            params["artifact_id"],
            chat_id=params.get("chat_id"),
            pinned=params.get("pinned", True),
        )
        await self._publish(
            "artifact.pinned",
            params["artifact_id"],
            artifact_id=params["artifact_id"],
            pinned=params.get("pinned", True),
        )
        return {"ok": ok}

    async def _duplicate(self, params: dict) -> dict:
        # P2-2/M1+F2: duplicate (read + write) offloaded via to_thread
        dup = await asyncio.to_thread(
            self.engine.duplicate_artifact,
            params["artifact_id"], params.get("new_name"),
        )
        if dup is None:
            raise ValueError("Failed to duplicate artifact")
        await self._publish("artifact.created", dup.id, artifact_id=dup.id, kind=dup.kind)
        return {"artifact": dup.model_dump()}

    async def _remix(self, params: dict) -> dict:
        # #77: remix derived copy - parent_artifact_id lineage, lands in the caller's library
        dup = await asyncio.to_thread(
            self.engine.remix_artifact,
            params["artifact_id"], params.get("new_name"),
        )
        if dup is None:
            raise ValueError("Failed to remix artifact")
        await self._publish(
            "artifact.remixed",
            dup.id,
            artifact_id=dup.id,
            parent_artifact_id=params["artifact_id"],
        )
        return {"artifact": dup.model_dump()}

    async def _list_all(self, params: dict) -> dict:
        sort = params.get("sort", "updated_at")
        # P2-2/M1+F2: read offloaded via to_thread
        artifacts, total = await asyncio.to_thread(
            self.engine.list_all_artifacts,
            filters=params.get("filters"),
            sort=sort,
            page=params.get("page", 1),
            page_size=params.get("page_size", 20),
            cursor=params.get("cursor"),
        )
        # P2-7/F6: cursor pagination - when the result fills a page, build next_cursor from the last
        # row for the client to continue. Only updated_at/created_at sorting supports cursors (storage
        # falls back to OFFSET internally). Use _clamp_pagination for the normalized page_size in the
        # full-page check, guarding against non-int strings in params.
        from fusion_artifacts_engine.storage.sqlite_storage import (
            _clamp_pagination,
            _encode_cursor,
        )
        _, eff_page_size = _clamp_pagination(params.get("page", 1), params.get("page_size", 20))
        next_cursor = None
        if artifacts and len(artifacts) >= eff_page_size and sort in ("updated_at", "created_at"):
            last = artifacts[-1]
            col_val = getattr(last, sort, None)
            if col_val is not None and getattr(last, "id", None):
                next_cursor = _encode_cursor(float(col_val), last.id)
        return {
            "artifacts": [a.model_dump() for a in artifacts],
            "total": total,
            "next_cursor": next_cursor,
        }

    # ── P1: recycle bin ────────────────────────────────────────

    async def _list_recycle(self, params: dict) -> dict:
        # P2-2/M1+F2: read offloaded via to_thread
        artifacts, total = await asyncio.to_thread(
            self.engine.list_recycle,
            page=params.get("page", 1),
            page_size=params.get("page_size", 20),
        )
        return {"artifacts": [a.model_dump() for a in artifacts], "total": total}

    async def _restore(self, params: dict) -> dict:
        # P2-2/M1+F2: write offloaded via to_thread
        ok = await asyncio.to_thread(
            self.engine.restore_artifact, params["artifact_id"]
        )
        await self._publish(
            "artifact.restored",
            params["artifact_id"],
            artifact_id=params["artifact_id"],
        )
        return {"ok": ok}

    async def _purge_expired(self, params: dict) -> dict:
        # P2-2/M1+F2: write offloaded via to_thread
        count = await asyncio.to_thread(self.engine.purge_expired)
        await self._publish("artifacts.purged", None, purged=count)
        return {"purged": count}

    # ── P2: snapshots ──────────────────────────────────────────

    async def _create_snapshot(self, params: dict) -> dict:
        snapshot = await self.engine.create_snapshot(
            params["artifact_id"],
            label=params.get("label"),
            author=params.get("author"),
        )
        await self._publish(
            "artifact.snapshot_created",
            params["artifact_id"],
            artifact_id=params["artifact_id"],
            snapshot_id=snapshot.version_num,
        )
        return {"version": snapshot.model_dump()}

    async def _list_snapshots(self, params: dict) -> dict:
        # P2-2/M1+F2: read offloaded via to_thread
        snapshots = await asyncio.to_thread(
            self.engine.list_snapshots,
            params["artifact_id"],
            page=int(params.get("page", 1)),
            page_size=int(params.get("page_size", 200)),
            include_content=bool(params.get("include_content", True)),
        )
        return {"snapshots": [s.model_dump() for s in snapshots]}

    # ── P1: share ──────────────────────────────────────────────

    async def _create_share(self, params: dict) -> dict:
        max_accesses = params.get("max_accesses")
        if max_accesses is not None:
            max_accesses = int(max_accesses)
        # P2-2/M1+F2: write offloaded via to_thread
        share = await asyncio.to_thread(
            self.engine.create_share,
            params["artifact_id"],
            created_by=params.get("created_by"),
            expires_at=params.get("expires_at"),
            max_accesses=max_accesses,
            permission=params.get("permission", "view"),
        )
        await self._publish(
            "artifact.shared",
            params["artifact_id"],
            artifact_id=params["artifact_id"],
            share_id=share.share_id,
        )
        # #83: return a directly openable public page URL (not a JSON endpoint)
        share_url = f"{self.engine.config.share_base_url}/s/{share.share_id}"
        return {"share": share.model_dump(), "share_url": share_url}

    async def _get_shared(self, params: dict) -> dict:
        # P2-2/M1+F2: read offloaded via to_thread
        result = await asyncio.to_thread(
            self.engine.get_shared_artifact, params["share_id"]
        )
        if result is None:
            raise NotFoundError("Shared artifact not found or access denied")
        return result

    async def _revoke_share(self, params: dict) -> dict:
        # P2-2/M1+F2: write offloaded via to_thread
        ok = await asyncio.to_thread(
            self.engine.revoke_share, params["share_id"]
        )
        await self._publish(
            "artifact.share_revoked",
            None,
            share_id=params["share_id"],
        )
        return {"ok": ok}

    # ── P2: folders ────────────────────────────────────────────

    async def _create_folder(self, params: dict) -> dict:
        # P2-2/M1+F2: write offloaded via to_thread
        folder = await asyncio.to_thread(
            self.engine.create_folder,
            params["name"],
            parent_id=params.get("parent_id"),
            project_id=params.get("project_id"),
        )
        await self._publish("folder.created", None, folder_id=folder.folder_id)
        return {"folder": folder.model_dump()}

    async def _list_folders(self, params: dict) -> dict:
        # P2-2/M1+F2: read offloaded via to_thread
        folders = await asyncio.to_thread(
            self.engine.list_folders, params.get("project_id")
        )
        return {"folders": [f.model_dump() for f in folders]}

    async def _rename_folder(self, params: dict) -> dict:
        # P2-2/M1+F2: write offloaded via to_thread
        ok = await asyncio.to_thread(
            self.engine.rename_folder, params["folder_id"], params["new_name"]
        )
        await self._publish("folder.renamed", None, folder_id=params["folder_id"])
        return {"ok": ok}

    async def _delete_folder(self, params: dict) -> dict:
        # P2-2/M1+F2: write offloaded via to_thread
        ok = await asyncio.to_thread(
            self.engine.delete_folder, params["folder_id"]
        )
        await self._publish("folder.deleted", None, folder_id=params["folder_id"])
        return {"ok": ok}

    async def _move_to_folder(self, params: dict) -> dict:
        # P2-2/M1+F2: write offloaded via to_thread
        ok = await asyncio.to_thread(
            self.engine.move_to_folder, params["artifact_id"], params.get("folder_id")
        )
        await self._publish(
            "artifact.moved",
            params["artifact_id"],
            artifact_id=params["artifact_id"],
            folder_id=params.get("folder_id"),
        )
        return {"ok": ok}

    # ── P4: tags ───────────────────────────────────────────────

    async def _add_tag(self, params: dict) -> dict:
        # P2-2/M1+F2: write offloaded via to_thread
        tag = await asyncio.to_thread(
            self.engine.add_tag,
            params["artifact_id"],
            params["tag_name"],
            color=params.get("color"),
        )
        await self._publish(
            "artifact.tagged",
            params["artifact_id"],
            artifact_id=params["artifact_id"],
            tag_name=params["tag_name"],
        )
        return {"tag": tag.model_dump()}

    async def _remove_tag(self, params: dict) -> dict:
        # P2-2/M1+F2: write offloaded via to_thread
        ok = await asyncio.to_thread(
            self.engine.remove_tag, params["artifact_id"], params["tag_name"]
        )
        await self._publish(
            "artifact.untagged",
            params["artifact_id"],
            artifact_id=params["artifact_id"],
            tag_name=params["tag_name"],
        )
        return {"ok": ok}

    async def _list_tags(self, params: dict) -> dict:
        # L-7: filter by scope; omitted returns everything
        scope = params.get("scope")
        # P2-2/M1+F2: read offloaded via to_thread
        tags = await asyncio.to_thread(self.engine.list_tags, scope)
        return {"tags": [t.model_dump() for t in tags]}

    async def _list_artifact_tags(self, params: dict) -> dict:
        # P2-2/M1+F2: read offloaded via to_thread
        tags = await asyncio.to_thread(
            self.engine.list_artifact_tags, params["artifact_id"]
        )
        return {"tags": [t.model_dump() for t in tags]}

    # ── P4: events ─────────────────────────────────────────────

    async def _emit_event(self, params: dict) -> dict:
        # P2-2/M1+F2: write offloaded via to_thread
        event = await asyncio.to_thread(
            self.engine.emit_event,
            params["event_type"],
            artifact_id=params.get("artifact_id"),
            session_id=params.get("session_id"),
            payload=params.get("payload"),
        )
        await self._publish(
            event.event_type,
            event.artifact_id,
            event_id=event.event_id,
            artifact_id=event.artifact_id,
            session_id=event.session_id,
            payload=event.payload,
        )
        return {"event": event.model_dump()}

    async def _list_events(self, params: dict) -> dict:
        # P2-2/M1+F2: read offloaded via to_thread
        events, total = await asyncio.to_thread(
            self.engine.list_events,
            artifact_id=params.get("artifact_id"),
            session_id=params.get("session_id"),
            since_ts=params.get("since_ts"),
            page=params.get("page", 1),
            page_size=params.get("page_size", 50),
        )
        return {"events": [e.model_dump() for e in events], "total": total}

    # ── P3: project KB ─────────────────────────────────────────

    async def _move_to_project_kb(self, params: dict) -> dict:
        # P2-2/M1+F2: write offloaded via to_thread
        ok = await asyncio.to_thread(
            self.engine.move_to_project_kb,
            params["artifact_id"], params["project_id"],
        )
        await self._publish(
            "artifact.moved_to_kb",
            params["artifact_id"],
            artifact_id=params["artifact_id"],
            project_id=params["project_id"],
        )
        return {"ok": ok}

    # ── external module ─────────────────────────────────────────

    async def _create_external(self, params: dict) -> dict:
        valid_types = ("code", "markdown", "html", "react", "data")
        artifact_type = params.get("type", "code")
        if artifact_type not in valid_types:
            raise ValueError(f"Invalid type, must be one of {valid_types}")
        valid_kinds = ("app", "code", "document", "game", "tool", "template")
        kind = params.get("kind")
        if kind is not None and kind not in valid_kinds:
            raise ValueError(f"Invalid kind, must be one of {valid_kinds}")
        artifact, version, ref_text = await self.engine.create_external_artifact(
            source_module=params["source_module"],
            workspace_id=params["workspace_id"],
            name=params["name"],
            artifact_type=artifact_type,
            content=params["content"],
            workflow_run_id=params.get("workflow_run_id"),
            summary=params.get("summary", ""),
            kind=kind,
            project_id=params.get("project_id"),
            metadata=params.get("metadata"),
        )
        await self._publish(
            "artifact.created",
            artifact.id,
            artifact_id=artifact.id,
            source_module=artifact.source_module,
        )
        return {
            "artifact": artifact.model_dump(),
            "version": version.model_dump(),
            "ref_text": ref_text,
        }

    async def _list_by_source(self, params: dict) -> dict:
        # P2-2/M1+F2: read offloaded via to_thread
        artifacts = await asyncio.to_thread(
            self.engine.list_by_source,
            source_module=params["source_module"],
            workspace_id=params.get("workspace_id"),
            workflow_run_id=params.get("workflow_run_id"),
            page=int(params.get("page", 1)),
            page_size=int(params.get("page_size", 200)),
        )
        return {"artifacts": [a.model_dump() for a in artifacts]}

    # ── #119: workflow-facing facade (cowork product_lifecycle template) ──

    async def _workflow_create(self, params: dict) -> dict:
        # One-call contract for pipeline nodes: create the artifact from a node
        # output, stamp project/stage tags, return artifact_id + version + url.
        artifact_type = params.get("type", "markdown")
        if artifact_type not in ("code", "markdown", "html", "react", "data"):
            raise ValueError("Invalid type, must be one of (code, markdown, html, react, data)")
        artifact, version, ref_text = await self.engine.create_external_artifact(
            source_module=params.get("source_module", "cowork"),
            workspace_id=params["workspace_id"],
            name=params["name"],
            artifact_type=artifact_type,
            content=params["content"],
            workflow_run_id=params.get("workflow_run_id"),
            summary=params.get("summary", ""),
            project_id=params.get("project_id"),
            metadata=params.get("metadata"),
        )
        aid = artifact.id
        project_slug = params.get("project_slug")
        if project_slug:
            await asyncio.to_thread(self.engine.add_tag, aid, f"project:{project_slug}")
        stage = params.get("stage")
        if stage:
            await asyncio.to_thread(self.engine.add_tag, aid, f"stage:{stage}")
        await self._publish("artifact.created", aid, artifact_id=aid, kind=artifact.kind)
        if project_slug or stage:
            logger.info(
                "workflow create %s project_slug=%s stage=%s", aid, project_slug, stage
            )
        return {
            "artifact_id": aid,
            "version": version.version_num,
            "url": deep_link_url(self.engine.config, aid, version.version_num),
            "ref_text": ref_text,
            "artifact": artifact.model_dump(),
        }

    async def _workflow_revision(self, params: dict) -> dict:
        # Revision round (gate A/B rejection loops, reviewer findings): land the
        # new content as the next version with the reason as change_log.
        version, ref_text = await self.engine.create_version(
            params["artifact_id"],
            params["content"],
            params.get("reason", "workflow revision"),
            source="manual",
            expected_content_hash=params.get("expected_content_hash"),
        )
        aid = params["artifact_id"]
        await self._publish(
            "artifact.updated", aid, artifact_id=aid, version_num=version.version_num
        )
        return {
            "artifact_id": aid,
            "version": version.version_num,
            "url": deep_link_url(self.engine.config, aid, version.version_num),
            "ref_text": ref_text,
        }

    async def _comment(self, params: dict) -> dict:
        # Review-comment surface per artifact version — stored as a persisted
        # event (kind review_comment), so gate A/B discussions attach to the doc
        # without new storage. _publish both fans out to SSE and persists the
        # event (#100-2) — emitting through the engine here would double-save.
        version = int(params["version"])
        comment = params["comment"]
        author = params.get("author", "")
        await self._publish(
            "review_comment",
            params["artifact_id"],
            version=version,
            comment=comment,
            author=author,
        )
        return {"ok": True, "version": version}

    async def _list_comments(self, params: dict) -> dict:
        events, _total = await asyncio.to_thread(
            self.engine.list_events,
            artifact_id=params["artifact_id"],
            page=int(params.get("page", 1)),
            page_size=int(params.get("page_size", 200)),
        )
        version = params.get("version")
        comments = []
        for e in events:
            if e.event_type != "review_comment":
                continue
            p = e.payload or {}
            if version is not None and int(p.get("version", 0)) != int(version):
                continue
            comments.append(
                {
                    "event_id": e.event_id,
                    "version": p.get("version"),
                    "comment": p.get("comment"),
                    "author": p.get("author", ""),
                    "created_at": e.created_at,
                }
            )
        comments.sort(key=lambda c: (c["version"] or 0, c["created_at"] or 0))
        return {"comments": comments}

    # ── AE-1: patch_artifact ────────────────────────────────────

    async def _patch(self, params: dict) -> dict:
        valid_ops = ("replace_section", "append", "prepend", "delete_section")
        operation = params.get("operation", "")
        if operation not in valid_ops:
            raise ValueError(f"Invalid operation, must be one of {valid_ops}")
        # P-6: read kind via to_thread so the event loop is not blocked; cache it before the
        # change for _publish reuse
        pre = await asyncio.to_thread(
            self.engine.storage.get_artifact, params["artifact_id"]
        )
        # P2-3/MEDIUM-4: IDOR - verify ownership before patch (active when caller_user_id is
        # injected for multi-tenant)
        self._enforce_owner(params, pre)
        version, patch_info = await self.engine.patch_artifact(
            artifact_id=params["artifact_id"],
            operation=operation,
            anchor=params.get("anchor", ""),
            content=params.get("content", ""),
            expected_version=params.get("expected_version"),
        )
        await self._publish(
            "artifact.patched",
            params["artifact_id"],
            artifact_id=params["artifact_id"],
            kind=pre.kind if pre is not None else None,
        )
        # v0.7.0: after an office-sourced artifact patch, write back to .outbox (failure is
        # logged only and does not affect the result)
        if pre is not None:
            await asyncio.to_thread(maybe_write_back, self.engine, pre, version.version_num)
            self._spawn_long_context(pre, version.version_num)
        return {"version": version.model_dump(), "patch_info": patch_info}

    def _resolve_version_sync(self, artifact_id: str, version_num: int | None):
        # v0.7.1 #90: version defaults to the current version; a nonexistent specified version ->
        # NotFoundError
        if version_num is not None:
            version = self.engine.storage.get_version(artifact_id, version_num)
            if version is None:
                raise NotFoundError(f"version {version_num} of {artifact_id} not found")
            return version
        artifact = self.engine.storage.get_artifact(artifact_id)
        if artifact is None:
            raise NotFoundError(f"artifact {artifact_id} not found")
        version = self.engine.storage.get_version(artifact_id, artifact.current_version)
        if version is None:
            raise NotFoundError(f"current version of {artifact_id} not found")
        return version

    async def _toc(self, params: dict) -> dict:
        artifact_id = params["artifact_id"]
        version_num = params.get("version")
        # P2-2/M1+F2: read offloaded via to_thread
        version = await asyncio.to_thread(
            self._resolve_version_sync, artifact_id, version_num
        )
        artifact = await asyncio.to_thread(self.engine.storage.get_artifact, artifact_id)
        art_type = artifact.type if artifact else "text"
        key = (artifact_id, version.version_num)
        cached = self._toc_cache.get(key)
        if cached is None:
            cached = await asyncio.to_thread(build_toc, version.content, art_type)
            self._toc_cache[key] = cached
            self._toc_cache.move_to_end(key)
            while len(self._toc_cache) > 128:
                self._toc_cache.popitem(last=False)
        return {
            "artifact_id": artifact_id,
            "version": version.version_num,
            "type": art_type,
            "toc": cached,
            "total_lines": version.content.count("\n") + 1,
            "total_bytes": len(version.content.encode("utf-8")),
            "tokens": version.token_count,
        }

    async def _view(self, params: dict) -> dict:
        artifact_id = params["artifact_id"]
        version_num = params.get("version")
        section = params.get("section")
        offset = params.get("offset")
        limit = params.get("limit")
        if section is None and offset is None:
            raise ValueError("artifact.view requires 'section' or 'offset' (+optional 'limit')")
        version = await asyncio.to_thread(
            self._resolve_version_sync, artifact_id, version_num
        )
        if section is not None:
            # #90: section view - O(section) instead of O(artifact); only matching lines are joined
            content = version.content
            artifact = await asyncio.to_thread(
                self.engine.storage.get_artifact, artifact_id
            )
            art_type = artifact.type if artifact else "text"
            bounds = await asyncio.to_thread(
                find_section_bounds, content, section, art_type
            )
            if not bounds:
                raise NotFoundError(f"section '{section}' not found in {artifact_id}")
            start, end, _lvl = bounds[0]
            lines = content.split("\n")
            body = "\n".join(lines[start:end])
            return {
                "artifact_id": artifact_id,
                "version": version.version_num,
                "section": section,
                "content": body,
                "start_line": start + 1,
                "end_line": end,
                "lines": end - start,
                "total_lines": content.count("\n") + 1,
                "total_bytes": len(content.encode("utf-8")),
                "tokens": version.token_count,
            }
        # #90: line-range view. Empty limit = read to the end (caller pages via total_lines)
        chunk = await asyncio.to_thread(
            read_line_range, version.content, int(offset or 0), int(limit) if limit else None
        )
        chunk["artifact_id"] = artifact_id
        chunk["version"] = version.version_num
        chunk["tokens"] = version.token_count
        return chunk

    async def _load(self, params: dict) -> dict:
        # P2-2/M1+F2: read offloaded via to_thread
        result = await asyncio.to_thread(
            self.engine.load_artifact,
            artifact_id=params["artifact_id"],
            preview_only=params.get("preview_only", True),
            section=params.get("section"),
        )
        return result

    async def _budget(self, params: dict) -> dict:
        context_window = params.get("context_window")
        if context_window is not None:
            context_window = int(context_window)
        # P2-2/M1+F2: read offloaded via to_thread
        result = await asyncio.to_thread(
            self.engine.context_budget,
            session_id=params.get("session_id"),
            context_window=context_window,
        )
        return result

    async def _auto_compact(self, params: dict) -> dict:
        result = await self.engine.auto_compact(
            artifact_id=params["artifact_id"],
            token_budget=params["token_budget"],
        )
        await self._publish(
            "artifact.compacted",
            params["artifact_id"],
            artifact_id=params["artifact_id"],
        )
        return result

    async def _version_diff(self, params: dict) -> dict:
        # P2-2/M1+F2: read (both version contents) offloaded via to_thread
        result = await asyncio.to_thread(
            self.engine.version_diff,
            artifact_id=params["artifact_id"],
            from_version=params["from_version"],
            to_version=params["to_version"],
        )
        return result

    # ── #37: render / check_safety / inject / interact / sync ───

    async def _render(self, params: dict) -> dict:
        result = await self.engine.render_artifact(
            content=params.get("content", ""),
            session_id=params.get("session_id", ""),
            lang_hint=params.get("lang_hint", ""),
            project_id=params.get("project_id"),
        )
        if result.get("created"):
            rid = result["artifact"]["id"]
            rkind = result["artifact"].get("kind")
            await self._publish(
                "artifact.created",
                rid,
                artifact_id=rid,
                kind=rkind,
            )
            await self._publish(
                "artifact.rendered",
                rid,
                artifact_id=rid,
                kind=rkind,
            )
        return result

    async def _check_safety(self, params: dict) -> dict:
        # P2-2/M1+F2: read offloaded via to_thread
        result = await asyncio.to_thread(
            self.engine.check_safety,
            messages=params.get("messages", []),
            output_budget=params.get("output_budget"),
        )
        return result

    async def _inject(self, params: dict) -> dict:
        # Ops-6: the inject placeholder is retired. The method registration is kept (backward compatible,
        # old clients do not hit method not found); execution always rejects with -32005 NotImplementedError.
        # Callers should switch to context.budget + check_safety.
        raise NotImplementedError(
            "artifact.inject is not implemented; use context.budget and "
            "check_safety for token budget management"
        )

    async def _interact(self, params: dict) -> dict:
        # Ops-6: the interact placeholder is retired. Registration kept (backward compatible); execution
        # always rejects with -32005 NotImplementedError. No interaction event is recorded anymore (no side effects).
        raise NotImplementedError(
            "artifact.interact is not implemented; action dispatch is not supported"
        )

    async def _sync(self, params: dict) -> dict:
        result = await self.engine.sync_artifact_file(
            artifact_id=params["artifact_id"],
            file_path=params["file_path"],
            direction=params["direction"],
        )
        await self._publish(
            "artifact.synced",
            params["artifact_id"],
            artifact_id=params["artifact_id"],
            direction=params["direction"],
            file_path=params["file_path"],
        )
        return result

    async def _render_visual(self, params: dict) -> dict:
        # #58/#59/#60: visual rendering RPC. Accepts raw_text (raw model output) or dsl (already-parsed dict).
        # Returns self-contained HTML plus visual_type and valid/errors metadata.
        raw_text = params.get("raw_text")
        dsl = params.get("dsl")
        progress = params.get("progress", 1.0)
        try:
            progress = float(progress)
        except (TypeError, ValueError):
            progress = 1.0
        result = await asyncio.to_thread(
            self.engine.render_visual,
            raw_text=raw_text,
            dsl=dsl,
            progress=progress,
        )
        await self._publish(
            "visual.rendered",
            None,
            visual_type=result.get("visual_type"),
            valid=result.get("valid"),
            html_len=len(result.get("html") or ""),
        )
        return result

    def _resolve_ai_model(self, requested: str | None) -> str:
        cfg = self.engine.config
        model = (requested or "").strip() or cfg.ai_model.strip()
        return model

    async def _ai_generate(self, params: dict) -> dict:
        # #97: webui AI generate/iterate. The GUI composes the prompt (generate/iterate modes); the engine
        # is a thin proxy to the fusion-mlx OpenAI-compatible API. Empty model = auto-pick the first from /v1/models.
        prompt = str(params.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("prompt is required")
        cfg = self.engine.config
        model = self._resolve_ai_model(params.get("model"))
        try:
            if not model:
                models = await ai_client.list_models(
                    cfg.ai_base_url, api_key=cfg.ai_api_key or None
                )
                if not models:
                    raise BusinessRuleError(
                        f"No models available at {cfg.ai_base_url}"
                    )
                model = models[0]
            messages = []
            system = params.get("system")
            if system:
                messages.append({"role": "system", "content": str(system)})
            messages.append({"role": "user", "content": prompt})
            text = await ai_client.chat(
                cfg.ai_base_url,
                model,
                messages,
                timeout=cfg.ai_timeout,
                api_key=cfg.ai_api_key or None,
                max_tokens=int(params.get("max_tokens", 4096)),
            )
        except RpcError:
            raise
        except Exception as e:
            logger.error("AI generate failed: base_url=%s model=%s err=%s", cfg.ai_base_url, model, e)
            raise BusinessRuleError(f"AI backend unavailable: {e}")
        return {"text": text, "model": model}

    async def _ai_list_models(self, params: dict) -> dict:
        cfg = self.engine.config
        try:
            models = await ai_client.list_models(cfg.ai_base_url, api_key=cfg.ai_api_key or None)
        except Exception as e:
            logger.error("AI list models failed: base_url=%s err=%s", cfg.ai_base_url, e)
            raise BusinessRuleError(f"AI backend unavailable: {e}")
        return {"models": models, "default": cfg.ai_model}
