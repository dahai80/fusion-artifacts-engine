import logging
from pathlib import Path
from typing import Any

from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.errors import RpcError
from fusion_artifacts_engine.rpc.event_bus import event_bus
from fusion_artifacts_engine.utils import get_package_version

logger = logging.getLogger(__name__)


class RPCHandler:
    def __init__(self, engine: ArtifactEngine):
        self.engine = engine
        self._method_map = self._build_methods()

    async def dispatch(self, method: str, params: dict) -> Any:
        handler = self._method_map.get(method)
        if handler is None:
            raise RpcError(-32601, f"Method not found: {method}")
        return await handler(params)

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
        )
        event_bus.publish(
            "artifact.created", {"artifact_id": artifact.id, "kind": artifact.kind}
        )
        return {
            "artifact": artifact.model_dump(),
            "version": version.model_dump(),
            "ref_text": ref_text,
        }

    async def _get(self, params: dict) -> dict:
        artifact = self.engine.get_artifact(
            params["artifact_id"], project_id=params.get("project_id")
        )
        if artifact is None:
            raise ValueError(f"Artifact not found: {params['artifact_id']}")
        return {"artifact": artifact.model_dump()}

    async def _get_content(self, params: dict) -> dict:
        version = params.get("version")
        if isinstance(version, str) and version != "latest":
            try:
                version = int(version)
            except ValueError:
                pass
        if version == "latest":
            version = None
        result = self.engine.get_version_content(params["artifact_id"], version)
        if result is None:
            raise ValueError("Version not found")
        return {
            "content": result.content,
            "token_count": result.token_count,
            "version": result.version_num,
        }

    async def _list(self, params: dict) -> dict:
        artifacts = self.engine.list_artifacts(
            params["session_id"],
            params.get("include_deleted", False),
            project_id=params.get("project_id"),
            metadata_filter=params.get("metadata_filter"),
        )
        return {"artifacts": [a.model_dump() for a in artifacts]}

    async def _delete(self, params: dict) -> dict:
        ok = self.engine.delete_artifact(
            params["artifact_id"],
            params.get("soft_delete", True),
            project_id=params.get("project_id"),
        )
        event_bus.publish("artifact.deleted", {"artifact_id": params["artifact_id"]})
        return {"ok": ok}

    async def _update(self, params: dict) -> dict:
        valid_sources = ("manual", "ai_generation")
        source = params.get("source", "manual")
        if source not in valid_sources:
            raise ValueError(f"Invalid source, must be one of {valid_sources}")
        version, ref_text = await self.engine.create_version(
            params["artifact_id"],
            params["content"],
            params.get("change_log", ""),
            source=source,
            expected_content_hash=params.get("expected_content_hash"),
        )
        event_bus.publish("artifact.updated", {"artifact_id": params["artifact_id"]})
        return {"version": version.model_dump(), "ref_text": ref_text}

    async def _version_list(self, params: dict) -> dict:
        versions = self.engine.list_versions(params["artifact_id"])
        return {"versions": [v.model_dump() for v in versions]}

    async def _version_rollback(self, params: dict) -> dict:
        version, ref_text = await self.engine.rollback_version(
            params["artifact_id"], params["target_version"]
        )
        return {"version": version.model_dump(), "ref_text": ref_text}

    async def _export(self, params: dict) -> dict:
        artifact = self.engine.get_artifact(params["artifact_id"])
        if artifact is None:
            raise ValueError("Artifact not found")
        include_versions = params.get("include_versions", False)
        data = {"artifact": artifact.model_dump()}
        if include_versions:
            versions = self.engine.list_versions(params["artifact_id"])
            data["versions"] = [v.model_dump() for v in versions]
        else:
            latest = self.engine.get_version_content(params["artifact_id"])
            if latest:
                data["content"] = latest.content
        return {"data": data}

    async def _export_session(self, params: dict) -> dict:
        storage_root = Path(self.engine.config.storage_root).resolve()
        output_dir = Path(params["output_dir"]).resolve()
        try:
            output_dir.relative_to(storage_root)
        except ValueError:
            raise ValueError(f"output_dir must be under storage root {storage_root}")
        output_dir.mkdir(parents=True, exist_ok=True)
        artifacts = self.engine.list_artifacts(params["session_id"])
        count = 0
        for art in artifacts:
            content = self.engine.get_version_content(art.id)
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
                path.write_text(content.content, encoding="utf-8")
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
        return {"artifact": artifact.model_dump(), "ref_text": ref_text}

    async def _export_code(self, params: dict) -> dict:
        language = params.get("language", "")
        result = self.engine.export_code(params["artifact_id"], language)
        return result

    async def _import_code(self, params: dict) -> dict:
        artifact, version, ref_text = await self.engine.import_code(
            session_id=params["session_id"],
            code=params["code"],
            language=params.get("language", ""),
            name=params.get("name", ""),
            metadata=params.get("metadata"),
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
            self.engine.register_watcher(artifact_id, watcher_id)
            return {"registered": True, "artifact_id": artifact_id}
        elif action == "unregister":
            if not watcher_id:
                raise ValueError("watcher_id required for unregister action")
            self.engine.unregister_watcher(artifact_id, watcher_id)
            return {"unregistered": True, "artifact_id": artifact_id}
        elif action == "poll":
            events = self.engine.get_watch_events(artifact_id, since_version)
            return {"artifact_id": artifact_id, "events": events}
        else:
            raise ValueError(
                f"Invalid action: {action}, must be register/unregister/poll"
            )

    async def _ping(self, params: dict) -> dict:
        return {"pong": True, "version": get_package_version()}

    async def _rename(self, params: dict) -> dict:
        ok = self.engine.rename_artifact(params["artifact_id"], params["new_name"])
        return {"ok": ok}

    async def _star(self, params: dict) -> dict:
        ok = self.engine.star_artifact(
            params["artifact_id"], params.get("starred", True)
        )
        return {"ok": ok}

    async def _pin(self, params: dict) -> dict:
        ok = self.engine.pin_artifact(
            params["artifact_id"],
            chat_id=params.get("chat_id"),
            pinned=params.get("pinned", True),
        )
        return {"ok": ok}

    async def _duplicate(self, params: dict) -> dict:
        dup = self.engine.duplicate_artifact(
            params["artifact_id"], params.get("new_name")
        )
        if dup is None:
            raise ValueError("Failed to duplicate artifact")
        return {"artifact": dup.model_dump()}

    async def _list_all(self, params: dict) -> dict:
        artifacts, total = self.engine.list_all_artifacts(
            filters=params.get("filters"),
            sort=params.get("sort", "updated_at"),
            page=params.get("page", 1),
            page_size=params.get("page_size", 20),
        )
        return {"artifacts": [a.model_dump() for a in artifacts], "total": total}

    # ── P1: recycle bin ────────────────────────────────────────

    async def _list_recycle(self, params: dict) -> dict:
        artifacts, total = self.engine.list_recycle(
            page=params.get("page", 1),
            page_size=params.get("page_size", 20),
        )
        return {"artifacts": [a.model_dump() for a in artifacts], "total": total}

    async def _restore(self, params: dict) -> dict:
        ok = self.engine.restore_artifact(params["artifact_id"])
        return {"ok": ok}

    async def _purge_expired(self, params: dict) -> dict:
        count = self.engine.purge_expired()
        return {"purged": count}

    # ── P2: snapshots ──────────────────────────────────────────

    async def _create_snapshot(self, params: dict) -> dict:
        snapshot = await self.engine.create_snapshot(
            params["artifact_id"],
            label=params.get("label"),
            author=params.get("author"),
        )
        return {"version": snapshot.model_dump()}

    async def _list_snapshots(self, params: dict) -> dict:
        snapshots = self.engine.list_snapshots(params["artifact_id"])
        return {"snapshots": [s.model_dump() for s in snapshots]}

    # ── P1: share ──────────────────────────────────────────────

    async def _create_share(self, params: dict) -> dict:
        share = self.engine.create_share(
            params["artifact_id"],
            created_by=params.get("created_by"),
            expires_at=params.get("expires_at"),
        )
        return {"share": share.model_dump()}

    async def _get_shared(self, params: dict) -> dict:
        result = self.engine.get_shared_artifact(params["share_id"])
        if result is None:
            raise ValueError("Shared artifact not found or access denied")
        return result

    async def _revoke_share(self, params: dict) -> dict:
        ok = self.engine.revoke_share(params["share_id"])
        return {"ok": ok}

    # ── P2: folders ────────────────────────────────────────────

    async def _create_folder(self, params: dict) -> dict:
        folder = self.engine.create_folder(
            params["name"],
            parent_id=params.get("parent_id"),
            project_id=params.get("project_id"),
        )
        return {"folder": folder.model_dump()}

    async def _list_folders(self, params: dict) -> dict:
        folders = self.engine.list_folders(params.get("project_id"))
        return {"folders": [f.model_dump() for f in folders]}

    async def _rename_folder(self, params: dict) -> dict:
        ok = self.engine.rename_folder(params["folder_id"], params["new_name"])
        return {"ok": ok}

    async def _delete_folder(self, params: dict) -> dict:
        ok = self.engine.delete_folder(params["folder_id"])
        return {"ok": ok}

    async def _move_to_folder(self, params: dict) -> dict:
        ok = self.engine.move_to_folder(params["artifact_id"], params.get("folder_id"))
        return {"ok": ok}

    # ── P4: tags ───────────────────────────────────────────────

    async def _add_tag(self, params: dict) -> dict:
        tag = self.engine.add_tag(
            params["artifact_id"],
            params["tag_name"],
            color=params.get("color"),
        )
        return {"tag": tag.model_dump()}

    async def _remove_tag(self, params: dict) -> dict:
        ok = self.engine.remove_tag(params["artifact_id"], params["tag_name"])
        return {"ok": ok}

    async def _list_tags(self, params: dict) -> dict:
        tags = self.engine.list_tags()
        return {"tags": [t.model_dump() for t in tags]}

    async def _list_artifact_tags(self, params: dict) -> dict:
        tags = self.engine.list_artifact_tags(params["artifact_id"])
        return {"tags": [t.model_dump() for t in tags]}

    # ── P4: events ─────────────────────────────────────────────

    async def _emit_event(self, params: dict) -> dict:
        event = self.engine.emit_event(
            params["event_type"],
            artifact_id=params.get("artifact_id"),
            session_id=params.get("session_id"),
            payload=params.get("payload"),
        )
        return {"event": event.model_dump()}

    async def _list_events(self, params: dict) -> dict:
        events, total = self.engine.list_events(
            artifact_id=params.get("artifact_id"),
            session_id=params.get("session_id"),
            since_ts=params.get("since_ts"),
            page=params.get("page", 1),
            page_size=params.get("page_size", 50),
        )
        return {"events": [e.model_dump() for e in events], "total": total}

    # ── P3: project KB ─────────────────────────────────────────

    async def _move_to_project_kb(self, params: dict) -> dict:
        ok = self.engine.move_to_project_kb(params["artifact_id"], params["project_id"])
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
        event_bus.publish(
            "artifact.created",
            {
                "artifact_id": artifact.id,
                "source_module": artifact.source_module,
                "kind": artifact.kind,
            },
        )
        return {
            "artifact": artifact.model_dump(),
            "version": version.model_dump(),
            "ref_text": ref_text,
        }

    async def _list_by_source(self, params: dict) -> dict:
        artifacts = self.engine.list_by_source(
            source_module=params["source_module"],
            workspace_id=params.get("workspace_id"),
            workflow_run_id=params.get("workflow_run_id"),
        )
        return {"artifacts": [a.model_dump() for a in artifacts]}

    # ── AE-1: patch_artifact ────────────────────────────────────

    async def _patch(self, params: dict) -> dict:
        valid_ops = ("replace_section", "append", "prepend", "delete_section")
        operation = params.get("operation", "")
        if operation not in valid_ops:
            raise ValueError(f"Invalid operation, must be one of {valid_ops}")
        version, patch_info = await self.engine.patch_artifact(
            artifact_id=params["artifact_id"],
            operation=operation,
            anchor=params.get("anchor", ""),
            content=params.get("content", ""),
            expected_version=params.get("expected_version"),
        )
        event_bus.publish("artifact.patched", {"artifact_id": params["artifact_id"]})
        return {"version": version.model_dump(), "patch_info": patch_info}

    async def _load(self, params: dict) -> dict:
        result = self.engine.load_artifact(
            artifact_id=params["artifact_id"],
            preview_only=params.get("preview_only", True),
            section=params.get("section"),
        )
        return result

    async def _budget(self, params: dict) -> dict:
        result = self.engine.context_budget(
            session_id=params["session_id"],
        )
        return result
