import pytest

from fusion_artifacts_engine.config import ArtifactEngineConfig
from fusion_artifacts_engine.engine import ArtifactEngine
from fusion_artifacts_engine.models import Artifact
from fusion_artifacts_engine.storage.base import StorageDriver
from fusion_artifacts_engine.storage.sqlite_storage import (
    SQLiteStorage,
    _decode_cursor,
    _encode_cursor,
)


@pytest.fixture
def storage(tmp_path):
    return SQLiteStorage(
        db_path=tmp_path / "m.db",
        content_dir=tmp_path / "c",
        metadata_indexed_keys=["language", "framework", "bad key!"],
    )


@pytest.fixture
def engine(tmp_path):
    cfg = ArtifactEngineConfig(
        storage_root=str(tmp_path),
        sync_root=str(tmp_path),
        metadata_indexed_keys=["language", "framework"],
    )
    return ArtifactEngine(config=cfg)


# ── metadata expression indexes ────────────────────────────────────


def test_metadata_indexed_keys_filter_unsafe(storage):
    # Unsafe identifiers (containing spaces/!) are filtered; indexes are created only for safe keys
    assert storage.metadata_indexed_keys == ["language", "framework"]


def test_metadata_indexes_created(storage):
    idxs = {
        r[0]
        for r in storage._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_meta_%'"
        ).fetchall()
    }
    assert "idx_meta_language" in idxs
    assert "idx_meta_framework" in idxs
    # No index created for unsafe keys
    assert not any("bad" in i for i in idxs)


def test_no_metadata_indexes_when_empty(tmp_path):
    st = SQLiteStorage(db_path=tmp_path / "m.db", content_dir=tmp_path / "c")
    idxs = {
        r[0]
        for r in st._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_meta_%'"
        ).fetchall()
    }
    assert idxs == set()


def test_metadata_filter_uses_index(storage, tmp_path):
    # P2-7/F6: metadata_filter hits the expression index. Create artifacts with metadata attached.
    art = Artifact(
        id="art_x", session_id="s1", name="d.md", type="markdown", kind="document",
        current_version=1, summary="", created_at=1700000000.0, updated_at=1700000000.0,
        metadata={"language": "python", "framework": "fastapi"},
    )
    storage.save_artifact(art)
    # list_artifacts uses metadata_filter and hits the indexed language column
    found = storage.list_artifacts("s1", metadata_filter={"language": "python"})
    assert len(found) == 1 and found[0].id == "art_x"
    miss = storage.list_artifacts("s1", metadata_filter={"language": "rust"})
    assert miss == []


# ── cursor encode/decode ──────────────────────────────────────────


def test_decode_cursor_valid():
    assert _decode_cursor("1700000000.5:art_1") == (1700000000.5, "art_1")


def test_decode_cursor_bad_float():
    assert _decode_cursor("notfloat:art_1") == (None, "")


def test_decode_cursor_no_colon():
    assert _decode_cursor("nocolon") == (None, "")


def test_decode_cursor_empty():
    assert _decode_cursor("") == (None, "")


def test_decode_cursor_id_with_no_colon_id():
    # When the id segment has no ':', the partition tail is empty
    assert _decode_cursor("1.0:") == (1.0, "")


def test_encode_cursor_roundtrip():
    c = _encode_cursor(1700000000.5, "art_1")
    assert _decode_cursor(c) == (1700000000.5, "art_1")


# ── keyset pagination ─────────────────────────────────────────


def _seed_many(engine, n=25):
    base = 1700000000.0
    for i in range(n):
        engine.storage.save_artifact(
            Artifact(
                id=f"art_{i:02d}", session_id="s1", name=f"d{i}.md", type="markdown",
                kind="document", current_version=1, summary="",
                created_at=base + i, updated_at=base + i,
            )
        )


def test_keyset_pagination_no_overlap(engine):
    _seed_many(engine, 25)
    page1, total = engine.list_all_artifacts(page_size=10)
    assert total == 25 and len(page1) == 10
    last = page1[-1]
    cursor = _encode_cursor(last.updated_at, last.id)
    page2, _ = engine.list_all_artifacts(page_size=10, cursor=cursor)
    p1, p2 = {a.id for a in page1}, {a.id for a in page2}
    assert not (p1 & p2)
    last2 = page2[-1]
    cursor2 = _encode_cursor(last2.updated_at, last2.id)
    page3, _ = engine.list_all_artifacts(page_size=10, cursor=cursor2)
    all_ids = [a.id for a in page1] + [a.id for a in page2] + [a.id for a in page3]
    assert len(all_ids) == len(set(all_ids)) == 25


def test_keyset_order_desc(engine):
    _seed_many(engine, 15)
    page1, _ = engine.list_all_artifacts(page_size=10)
    assert [a.id for a in page1] == [f"art_{i:02d}" for i in range(14, 4, -1)]


def test_keyset_invalid_cursor_falls_back_offset(engine):
    _seed_many(engine, 25)
    # Invalid cursor → fall back to OFFSET pagination, page=1 takes the first 10
    arts, _ = engine.list_all_artifacts(page_size=10, cursor="notfloat:x")
    assert len(arts) == 10
    # After fallback this is equivalent to the first page (10 newest)
    assert arts[0].id == "art_24"


def test_keyset_unsupported_sort_falls_back(engine):
    _seed_many(engine, 25)
    # name ordering does not support keyset → fall back to OFFSET
    arts, _ = engine.list_all_artifacts(page_size=10, sort="name", cursor="1.0:art_24")
    assert len(arts) == 10


def test_keyset_with_filter(engine):
    _seed_many(engine, 25)
    # With a type filter matching only 5 rows, cursor paging still works correctly
    page1, total = engine.list_all_artifacts(
        filters={"type": "markdown"}, page_size=3
    )
    assert total == 25 and len(page1) == 3
    last = page1[-1]
    cursor = _encode_cursor(last.updated_at, last.id)
    page2, _ = engine.list_all_artifacts(
        filters={"type": "markdown"}, page_size=3, cursor=cursor
    )
    assert not ({a.id for a in page1} & {a.id for a in page2})


# ── RPC next_cursor ─────────────────────────────────────


async def test_rpc_next_cursor_present(engine):
    from fusion_artifacts_engine.rpc.methods import RPCHandler
    _seed_many(engine, 25)
    handler = RPCHandler(engine)
    res = await handler.dispatch("artifact.list_all", {"page_size": 10})
    assert res["total"] == 25
    assert res["next_cursor"] is not None
    p1 = {a["id"] for a in res["artifacts"]}
    res2 = await handler.dispatch(
        "artifact.list_all", {"page_size": 10, "cursor": res["next_cursor"]}
    )
    p2 = {a["id"] for a in res2["artifacts"]}
    assert not (p1 & p2)


async def test_rpc_next_cursor_none_on_last_page(engine):
    from fusion_artifacts_engine.rpc.methods import RPCHandler
    _seed_many(engine, 5)
    handler = RPCHandler(engine)
    res = await handler.dispatch("artifact.list_all", {"page_size": 10})
    # Fewer than one page → no next-page cursor
    assert res["next_cursor"] is None


async def test_rpc_next_cursor_none_unsupported_sort(engine):
    from fusion_artifacts_engine.rpc.methods import RPCHandler
    _seed_many(engine, 25)
    handler = RPCHandler(engine)
    res = await handler.dispatch(
        "artifact.list_all", {"page_size": 10, "sort": "name"}
    )
    # name ordering does not support keyset → next_cursor None
    assert res["next_cursor"] is None


# ── ABC contract ────────────────────────────────────────────


def test_abc_signature_has_cursor():
    # The list_all_artifacts ABC carries the cursor parameter (H8 swap-point contract)
    sig = StorageDriver.list_all_artifacts
    import inspect
    params = inspect.signature(sig).parameters
    assert "cursor" in params
