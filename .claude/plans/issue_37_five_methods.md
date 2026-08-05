# Issue #37 — Register 5 unregistered RPC methods (render/check_safety/inject/interact/sync)

Direction: **引擎补齐（推翻#22）** — user-approved. Register and implement all 5 methods in the engine.

## Signatures (per #37 studio call sites, NOT old impl)

| Method | Params | Return |
|---|---|---|
| `artifact.render` | `content, session_id, lang_hint, project_id?` | `{created, artifact, render_type, content, ref_text}` (or `{created:false, reason}`) |
| `artifact.check_safety` | `messages, output_budget` | `{safe, current_tokens, remaining_tokens}` |
| `artifact.inject` | `messages, output_budget` | `{messages, total_tokens, safe}` |
| `artifact.interact` | `artifact_id, action, payload, session_id?` | `{ok, artifact_id, action, event_id}` |
| `artifact.sync` | `artifact_id, file_path, direction` | `{ok, direction, artifact_id, file_path}` |

`direction` ∈ `{"artifact_to_code","code_to_artifact"}`. `messages` = list of `{role, content}` dicts.

## Implementation

### 1. `engine.py` — add 5 methods (reuse existing primitives, no LLM/HTTP calls)

- `render_artifact(content, session_id, lang_hint, project_id)` — reuse `auto_identifier.{should_create_artifact, detect_artifact_type, detect_renderable_type, extract_name_hint}` + `create_artifact()`. Threshold check; if below → `{created:false, reason:"below_threshold"}`. Empty content → `{created:false, reason:"empty_content"}`. On success return artifact + `render_type` (from `detect_renderable_type` or artifact type) + content + ref_text. Wrap in try/except → `{created:false, reason:str(e)}`.

- `check_safety(messages, output_budget)` — `count_tokens` on each message content sum = `current_tokens`. `remaining = output_budget - current_tokens`. `safe = remaining >= 0`. Reuse `self.config.context_budget_default` if `output_budget` is None/0. Return `{safe, current_tokens, remaining_tokens}`.

- `inject(messages, output_budget)` — token accounting + passthrough (no session_id param → can't list session artifacts). Sum tokens, `safe = total <= output_budget`, return messages + `{total_tokens, safe}`. Matches old `_inject` return shape. Unblocks studio without re-adding LLM-dependent injection heuristics.

- `interact_artifact(artifact_id, action, payload, session_id)` — verify artifact exists (raise if not), emit event via existing `emit_event("artifact.interaction", artifact_id, session_id, payload)` → reuse `ArtifactEvent` + `storage.save_event`. Return `{ok:true, artifact_id, action, event_id}`.

- `sync_artifact_file(artifact_id, file_path, direction)` — verify artifact exists. `artifact_to_code`: write current version content to `file_path`. `code_to_artifact`: read `file_path`, `create_version` with new content. Return `{ok, direction, artifact_id, file_path}`. `file_path` is an external code file (studio-side), no storage-root constraint.

### 2. `rpc/methods.py` — register + 5 handlers

Register in `_build_methods()`:
```
"artifact.render": self._render,
"artifact.check_safety": self._check_safety,
"artifact.inject": self._inject,
"artifact.interact": self._interact,
"artifact.sync": self._sync,
```
Handlers forward params to engine methods. `_render` publishes `artifact.rendered` event.

### 3. `tests/test_render_inject_interact_sync.py` — new test file

Tests:
- `test_render_creates_artifact` — html content → created=true, render_type set
- `test_render_below_threshold` — short content → created=false, reason="below_threshold"
- `test_render_empty_content` — → created=false, reason="empty_content"
- `test_render_with_lang_hint` — lang_hint="python" → name has .py
- `test_check_safety_safe` — small messages, large budget → safe=true
- `test_check_safety_unsafe` — large messages, small budget → safe=false
- `test_check_safety_default_budget` — output_budget=None → uses context_budget_default
- `test_inject_passthrough` — messages returned, total_tokens counted, safe flag set
- `test_interact_records_event` — interact → ok=true, event_id set
- `test_interact_artifact_not_found` — raises ValueError
- `test_sync_artifact_to_code` — write content to tmp file; verify file contents
- `test_sync_code_to_artifact` — write file, sync → new version created
- `test_sync_invalid_direction` — raises ValueError
- `test_sync_artifact_not_found` — raises ValueError
- `test_*_rpc_dispatch` — end-to-end via `RPCHandler.dispatch` for each method

### 4. Docs + version

- `pyproject.toml`: 0.3.3 → 0.3.4
- `README.md` / `README_CN.md`: add 5 rows to RPC table; update test count; note #22 reversal

### 5. Verify

- `ruff check .` → 0 issues
- `pytest tests/ -q` → all pass (338 + ~15 new)
- commit, push, close #37 with comment

## Constraints
- No docstrings, 4-space indent, logging on every method
- Reuse existing `count_tokens`, `auto_identifier`, `emit_event`, `create_version`, `get_version_content`
- No LLM/HTTP calls in engine (render = type detection + storage, not actual rendering)
- Match #37 param names exactly (output_budget, file_path, lang_hint)
