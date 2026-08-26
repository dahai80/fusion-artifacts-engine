import asyncio
import logging
from pathlib import Path
from typing import Any

from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.errors import (
    NotFoundError,
    NotImplementedError,
    PermissionError,
    RpcError,
)
from fusion_artifacts_engine.utils import get_package_version

logger = logging.getLogger(__name__)


class RPCHandler:
    def __init__(self, engine: ArtifactEngine):
        self.engine = engine
        self._method_map = self._build_methods()

    async def dispatch(self, method: str, params: dict) -> Any:
        # P2-5/H12(trace): rpc.server.duration span 覆盖整个方法执行（含 storage I/O）。
        # dispatch 是所有 RPC 的唯一路由入口（server _handle / REST / 直接调用皆经此），
        # 故 span 放这里而非 server 层——任何调用路径都能采到因果链。
        # tracing.span 未启用时为 no-op，零开销。
        from fusion_artifacts_engine import tracing

        with tracing.span("rpc.server.duration", rpc_method=method):
            handler = self._method_map.get(method)
            if handler is None:
                raise RpcError(-32601, f"Method not found: {method}")
            return await handler(params)

    async def _publish(self, event_type: str, aid: str | None, **extra) -> None:
        # SSE ?kind= 过滤按 artifact kind (app/code/...) 而非事件名。
        # 每个 artifact 事件都必须携带 kind，否则订阅者按 kind 过滤时静默丢弃。
        # L-3: 调用方可经 extra 传 kind= 覆盖（删除事件用删除前 kind，避免硬删后读 None）
        # A-2: 用 engine 实例的 EventBus，避免跨 engine 串流
        # E10: kind 经覆盖 + 回查仍为 None 时记 warning，避免静默丢事件难排查
        # P2-2/M1+F2: kind 回查读经 to_thread 卸载，不阻塞 event loop
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

    def _enforce_owner(self, params: dict, artifact) -> None:
        # P2-3/MEDIUM-4: IDOR 防护。写操作 + get 校验调用方身份归属。
        # 单租户默认不传 caller_user_id（None）→ 跳过，保持向后兼容。
        # 多租户部署按请求注入 caller_user_id，与产物 owner_user_id 不符即 PermissionError(-32006)。
        # 任一为 None 时不校验（未设置所有权或未注入身份均放行，避免误伤单租户默认）。
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
            "artifact.list_events": self._list_events,
            # P3: project KB
            "artifact.move_to_project_kb": self._move_to_project_kb,
            # external module
            "artifact.create_external": self._create_external,
            "artifact.list_by_source": self._list_by_source,
            # AE-1: patch_artifact
            "artifact.patch": self._patch,
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
            "ping": self._ping,
        }

    async def _create(self, params: dict) -> dict:
        valid_types = ("code", "markdown", "html", "react", "data")
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
            # P2-3/MEDIUM-4: 创建时可注入 owner_user_id（多租户归属），默认 None=单租户
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
        # P2-2/M1+F2: 读路径经 to_thread 卸载，不阻塞 event loop
        artifact = await asyncio.to_thread(
            self.engine.get_artifact,
            params["artifact_id"], project_id=params.get("project_id"),
        )
        if artifact is None:
            raise NotFoundError(f"Artifact not found: {params['artifact_id']}")
        # P2-3/MEDIUM-4: IDOR——读取也校验归属（多租户注入 caller_user_id 时生效）
        self._enforce_owner(params, artifact)
        return {"artifact": artifact.model_dump()}

    async def _get_content(self, params: dict) -> dict:
        # P2-3/MEDIUM-4: IDOR——取内容前先取 artifact 校验归属（多租户注入 caller_user_id 时生效）
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
        # P2-2/M1+F2: 读内容经 to_thread 卸载
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
        # P2-2/M1+F2: 列表读经 to_thread 卸载
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
        # L-3: 删除前缓存 kind，硬删后 get_artifact 返回 None 导致 kind-filter SSE 丢事件
        # P2-2/M1+F2: 读经 to_thread 卸载
        pre_artifact = await asyncio.to_thread(self.engine.storage.get_artifact, aid)
        pre_kind = pre_artifact.kind if pre_artifact is not None else None
        # P2-3/MEDIUM-4: IDOR——删除前校验归属（多租户注入 caller_user_id 时生效）
        self._enforce_owner(params, pre_artifact)
        # P2-2/M1+F2: 写也经 to_thread 卸载（delete 持写锁，阻塞 event loop 同理）
        ok = await asyncio.to_thread(
            self.engine.delete_artifact,
            aid,
            params.get("soft_delete", True),
            project_id=params.get("project_id"),
        )
        # L-4: 仅删除成功才发 delete 事件，否则对未删 artifact 发虚假事件
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
        # P-6: 经 to_thread 读 kind，不阻塞 event loop；变更前缓存供 _publish 复用
        pre = await asyncio.to_thread(
            self.engine.storage.get_artifact, params["artifact_id"]
        )
        # P2-3/MEDIUM-4: IDOR——更新前校验归属（多租户注入 caller_user_id 时生效）
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
        return {"version": version.model_dump(), "ref_text": ref_text}

    async def _version_list(self, params: dict) -> dict:
        # P2-2/M1+F2: 读经 to_thread 卸载
        versions = await asyncio.to_thread(
            self.engine.list_versions,
            params["artifact_id"],
            page=int(params.get("page", 1)),
            page_size=int(params.get("page_size", 200)),
            include_content=bool(params.get("include_content", True)),
        )
        return {"versions": [v.model_dump() for v in versions]}

    async def _version_rollback(self, params: dict) -> dict:
        # P2-3/MEDIUM-4: IDOR——回滚前校验归属（多租户注入 caller_user_id 时生效）
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
        # P2-2/M1+F2: 读经 to_thread 卸载
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
            # P1-10/M18: 错误消息不泄露 storage_root 文件系统路径给调用方；
            # 内部 log 保留路径供运维定位，对外只回通用提示
            logger.warning(
                "export_session rejected: output_dir %s outside storage_root %s",
                output_dir, storage_root,
            )
            raise ValueError("output_dir must be under storage root (path traversal denied)")
        output_dir.mkdir(parents=True, exist_ok=True)
        # P2-2/M1+F2: 列表读经 to_thread 卸载
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
                # P2-2/M1+F2: 文件写经 to_thread 卸载（磁盘 I/O 阻塞 event loop 同理）
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
        # P2-2/M1+F2: 读经 to_thread 卸载
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
            # P2-2/M1+F2: 写经 to_thread 卸载
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
            # P2-2/M1+F2: 读经 to_thread 卸载
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
        # P2-2/M1+F2: 写经 to_thread 卸载
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
        # P2-2/M1+F2: 写经 to_thread 卸载
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
        # P2-2/M1+F2: 写经 to_thread 卸载
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
        # P2-2/M1+F2: 复制（读+写）经 to_thread 卸载
        dup = await asyncio.to_thread(
            self.engine.duplicate_artifact,
            params["artifact_id"], params.get("new_name"),
        )
        if dup is None:
            raise ValueError("Failed to duplicate artifact")
        await self._publish("artifact.created", dup.id, artifact_id=dup.id, kind=dup.kind)
        return {"artifact": dup.model_dump()}

    async def _list_all(self, params: dict) -> dict:
        sort = params.get("sort", "updated_at")
        # P2-2/M1+F2: 读经 to_thread 卸载
        artifacts, total = await asyncio.to_thread(
            self.engine.list_all_artifacts,
            filters=params.get("filters"),
            sort=sort,
            page=params.get("page", 1),
            page_size=params.get("page_size", 20),
            cursor=params.get("cursor"),
        )
        # P2-7/F6: 游标分页——结果满页则据末行构造 next_cursor 供客户端续翻。
        # 仅 updated_at/created_at 排序支持游标（storage 内回退 OFFSET）。
        # 用 _clamp_pagination 取规范化 page_size 做满页判定，防 params 传非 int 字符串。
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
        # P2-2/M1+F2: 读经 to_thread 卸载
        artifacts, total = await asyncio.to_thread(
            self.engine.list_recycle,
            page=params.get("page", 1),
            page_size=params.get("page_size", 20),
        )
        return {"artifacts": [a.model_dump() for a in artifacts], "total": total}

    async def _restore(self, params: dict) -> dict:
        # P2-2/M1+F2: 写经 to_thread 卸载
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
        # P2-2/M1+F2: 写经 to_thread 卸载
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
        # P2-2/M1+F2: 读经 to_thread 卸载
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
        # P2-2/M1+F2: 写经 to_thread 卸载
        share = await asyncio.to_thread(
            self.engine.create_share,
            params["artifact_id"],
            created_by=params.get("created_by"),
            expires_at=params.get("expires_at"),
            max_accesses=max_accesses,
        )
        await self._publish(
            "artifact.shared",
            params["artifact_id"],
            artifact_id=params["artifact_id"],
            share_id=share.share_id,
        )
        return {"share": share.model_dump()}

    async def _get_shared(self, params: dict) -> dict:
        # P2-2/M1+F2: 读经 to_thread 卸载
        result = await asyncio.to_thread(
            self.engine.get_shared_artifact, params["share_id"]
        )
        if result is None:
            raise NotFoundError("Shared artifact not found or access denied")
        return result

    async def _revoke_share(self, params: dict) -> dict:
        # P2-2/M1+F2: 写经 to_thread 卸载
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
        # P2-2/M1+F2: 写经 to_thread 卸载
        folder = await asyncio.to_thread(
            self.engine.create_folder,
            params["name"],
            parent_id=params.get("parent_id"),
            project_id=params.get("project_id"),
        )
        await self._publish("folder.created", None, folder_id=folder.folder_id)
        return {"folder": folder.model_dump()}

    async def _list_folders(self, params: dict) -> dict:
        # P2-2/M1+F2: 读经 to_thread 卸载
        folders = await asyncio.to_thread(
            self.engine.list_folders, params.get("project_id")
        )
        return {"folders": [f.model_dump() for f in folders]}

    async def _rename_folder(self, params: dict) -> dict:
        # P2-2/M1+F2: 写经 to_thread 卸载
        ok = await asyncio.to_thread(
            self.engine.rename_folder, params["folder_id"], params["new_name"]
        )
        await self._publish("folder.renamed", None, folder_id=params["folder_id"])
        return {"ok": ok}

    async def _delete_folder(self, params: dict) -> dict:
        # P2-2/M1+F2: 写经 to_thread 卸载
        ok = await asyncio.to_thread(
            self.engine.delete_folder, params["folder_id"]
        )
        await self._publish("folder.deleted", None, folder_id=params["folder_id"])
        return {"ok": ok}

    async def _move_to_folder(self, params: dict) -> dict:
        # P2-2/M1+F2: 写经 to_thread 卸载
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
        # P2-2/M1+F2: 写经 to_thread 卸载
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
        # P2-2/M1+F2: 写经 to_thread 卸载
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
        # L-7: 按 scope 过滤；不传则返回全部
        scope = params.get("scope")
        # P2-2/M1+F2: 读经 to_thread 卸载
        tags = await asyncio.to_thread(self.engine.list_tags, scope)
        return {"tags": [t.model_dump() for t in tags]}

    async def _list_artifact_tags(self, params: dict) -> dict:
        # P2-2/M1+F2: 读经 to_thread 卸载
        tags = await asyncio.to_thread(
            self.engine.list_artifact_tags, params["artifact_id"]
        )
        return {"tags": [t.model_dump() for t in tags]}

    # ── P4: events ─────────────────────────────────────────────

    async def _emit_event(self, params: dict) -> dict:
        # P2-2/M1+F2: 写经 to_thread 卸载
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
        # P2-2/M1+F2: 读经 to_thread 卸载
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
        # P2-2/M1+F2: 写经 to_thread 卸载
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
        # P2-2/M1+F2: 读经 to_thread 卸载
        artifacts = await asyncio.to_thread(
            self.engine.list_by_source,
            source_module=params["source_module"],
            workspace_id=params.get("workspace_id"),
            workflow_run_id=params.get("workflow_run_id"),
            page=int(params.get("page", 1)),
            page_size=int(params.get("page_size", 200)),
        )
        return {"artifacts": [a.model_dump() for a in artifacts]}

    # ── AE-1: patch_artifact ────────────────────────────────────

    async def _patch(self, params: dict) -> dict:
        valid_ops = ("replace_section", "append", "prepend", "delete_section")
        operation = params.get("operation", "")
        if operation not in valid_ops:
            raise ValueError(f"Invalid operation, must be one of {valid_ops}")
        # P-6: 经 to_thread 读 kind，不阻塞 event loop；变更前缓存供 _publish 复用
        pre = await asyncio.to_thread(
            self.engine.storage.get_artifact, params["artifact_id"]
        )
        # P2-3/MEDIUM-4: IDOR——补丁前校验归属（多租户注入 caller_user_id 时生效）
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
        return {"version": version.model_dump(), "patch_info": patch_info}

    async def _load(self, params: dict) -> dict:
        # P2-2/M1+F2: 读经 to_thread 卸载
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
        # P2-2/M1+F2: 读经 to_thread 卸载
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
        # P2-2/M1+F2: 读（双版本内容）经 to_thread 卸载
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
        # P2-2/M1+F2: 读经 to_thread 卸载
        result = await asyncio.to_thread(
            self.engine.check_safety,
            messages=params.get("messages", []),
            output_budget=params.get("output_budget"),
        )
        return result

    async def _inject(self, params: dict) -> dict:
        # 运维6: inject 占位已下线。保留方法注册（向后兼容旧客户端不报 method not found），
        # 执行即拒，返回 -32005 NotImplementedError。调用方应改用 context.budget + check_safety。
        raise NotImplementedError(
            "artifact.inject is not implemented; use context.budget and "
            "check_safety for token budget management"
        )

    async def _interact(self, params: dict) -> dict:
        # 运维6: interact 占位已下线。保留方法注册（向后兼容），执行即拒，
        # 返回 -32005 NotImplementedError。不再记录 interaction event（无副作用）。
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
