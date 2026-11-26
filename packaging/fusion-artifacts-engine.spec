# PyInstaller spec: single-file binary per OS (windows/macos/ubuntu release matrix).
# Data files: default_config.yaml + webui_dist resolve via Path(__file__) relative to
# the package dir — inside the bundle __file__ lives under MEIPASS/fusion_artifacts_engine,
# so both data entries map there.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata

ROOT = Path(SPECPATH).parent

# importlib.metadata needs the dist-info on disk inside the bundle,
# otherwise get_package_version() falls back to its 0.1.0 placeholder.
METADATA_DATAS = copy_metadata("fusion-artifacts-engine")

a = Analysis(
    [str(ROOT / "fusion_artifacts_engine" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (ROOT / "fusion_artifacts_engine" / "default_config.yaml", "fusion_artifacts_engine"),
        (ROOT / "fusion_artifacts_engine" / "webui_dist", "fusion_artifacts_engine/webui_dist"),
        *METADATA_DATAS,
    ],
    hiddenimports=[
        # tiktoken loads its encoder registry through a plugin namespace
        "tiktoken_ext.openai_public",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "pip", "setuptools", "pkg_resources"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name="fusion-artifacts-engine",
    console=True,
    upx=False,
)
