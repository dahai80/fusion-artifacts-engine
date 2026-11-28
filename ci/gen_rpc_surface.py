# Issue #141: generate the JSON-RPC surface contract consumed by BOTH repos.
# Extracts method names from RPCHandler._build_methods via AST (no engine
# instantiation needed) and writes contract/rpc-surface.json. The TS client
# contract test in client-rpc/ asserts its wrapper map against this file, so
# contract drift fails the PR that caused it.
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "fusion_artifacts_engine" / "rpc" / "methods.py"
TARGET = ROOT / "contract" / "rpc-surface.json"


def extract_methods() -> list[str]:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_build_methods":
            ret = next(n for n in ast.walk(node) if isinstance(n, ast.Return))
            return sorted(k.value for k in ret.value.keys)
    raise RuntimeError("_build_methods not found in methods.py")


def main() -> None:
    methods = extract_methods()
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    payload = {"methods": methods, "count": len(methods)}
    TARGET.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {TARGET} ({len(methods)} methods)")


if __name__ == "__main__":
    main()
