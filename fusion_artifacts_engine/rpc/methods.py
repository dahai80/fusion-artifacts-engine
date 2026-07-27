import logging
from typing import Any, Optional
from pathlib import Path
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.rpc.errors import RpcError
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
            "artifact.inject": self._inject,
            "artifact.check_safety": self._check_safety,
            "artifact.export": self._export,
            "artifact.export_session": self._export_session,
            "artifact.import": self._import_artifact,
            "artifact.export_code": self._export_code,
            "artifact.import_code": self._import_code,
            "artifact.watch": self._watch,
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
        )
        return {"artifact": artifact.model_dump(), "version": version.model_dump(), "ref_text": ref_text}

    async def _get(self, params: dict) -> dict:
        artifact = self.engine.get_artifact(params["artifact_id"])
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
            raise ValueError(f"Version not found")
        return {"content": result.content, "token_count": result.token_count, "version": result.version_num}

    async def _list(self, params: dict) -> dict:
        artifacts = self.engine.list_artifacts(params["session_id"], params.get("include_deleted", False))
        return {"artifacts": [a.model_dump() for a in artifacts]}

    async def _delete(self, params: dict) -> dict:
        ok = self.engine.delete_artifact(params["artifact_id"], params.get("soft_delete", True))
        return {"ok": ok}

    async def _update(self, params: dict) -> dict:
        valid_sources = ("manual", "ai_generation")
        source = params.get("source", "manual")
        if source not in valid_sources:
            raise ValueError(f"Invalid source, must be one of {valid_sources}")
        version, ref_text = await self.engine.create_version(
            params["artifact_id"], params["content"],
            params.get("change_log", ""), source=source,
        )
        return {"version": version.model_dump(), "ref_text": ref_text}

    async def _version_list(self, params: dict) -> dict:
        versions = self.engine.list_versions(params["artifact_id"])
        return {"versions": [v.model_dump() for v in versions]}

    async def _version_rollback(self, params: dict) -> dict:
        version, ref_text = await self.engine.rollback_version(
            params["artifact_id"], params["target_version"]
        )
        return {"version": version.model_dump(), "ref_text": ref_text}

    async def _inject(self, params: dict) -> dict:
        messages, total, safe = await self.engine.inject(
            params["messages"], params.get("max_context")
        )
        return {"messages": messages, "total_tokens": total, "safe": safe}

    async def _check_safety(self, params: dict) -> dict:
        safe, current, remaining = await self.engine.check_safety(
            params["messages"], params.get("max_context")
        )
        return {"safe": safe, "current_tokens": current, "remaining_tokens": remaining}

    async def _export(self, params: dict) -> dict:
        artifact = self.engine.get_artifact(params["artifact_id"])
        if artifact is None:
            raise ValueError(f"Artifact not found")
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
                safe_name = art.name.replace("/", "_").replace("\\", "_").replace("..", "_").replace("\x00", "_")
                path = output_dir / safe_name
                try:
                    path.resolve().relative_to(storage_root)
                except ValueError:
                    logger.warning("Skipping artifact name that escapes export dir: %s", art.name)
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
        artifact, version, ref_text = await self.engine.create_artifact(
            session_id=params["session_id"],
            name=artifact_data.get("name", "imported"),
            artifact_type=artifact_type,
            content=content,
            summary=artifact_data.get("summary", ""),
            kind=kind,
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
        return {"artifact": artifact.model_dump(), "version": version.model_dump(), "ref_text": ref_text}

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
            raise ValueError(f"Invalid action: {action}, must be register/unregister/poll")

    async def _sync(self, params: dict) -> dict:
        direction = params.get("direction", "artifact_to_code")
        if direction not in ("artifact_to_code", "code_to_artifact"):
            raise ValueError("direction must be 'artifact_to_code' or 'code_to_artifact'")
        result = await self.engine.sync_artifact_file(
            params["artifact_id"], params["code_path"], direction
        )
        return result

    async def _ping(self, params: dict) -> dict:
        return {"pong": True, "version": get_package_version()}
