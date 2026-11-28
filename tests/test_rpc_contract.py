import json

from ci.gen_rpc_surface import ROOT, extract_methods

CONTRACT = ROOT / "contract" / "rpc-surface.json"


def test_contract_matches_method_map():
    live = extract_methods()
    assert CONTRACT.exists(), (
        "contract/rpc-surface.json missing - run: python ci/gen_rpc_surface.py"
    )
    committed = json.loads(CONTRACT.read_text())["methods"]
    added = sorted(set(live) - set(committed))
    removed = sorted(set(committed) - set(live))
    assert not added and not removed, (
        f"RPC surface drift. added={added} removed={removed}. "
        "Run: python ci/gen_rpc_surface.py and commit the result"
    )


def test_contract_count_consistent():
    data = json.loads(CONTRACT.read_text())
    assert data["count"] == len(data["methods"])
