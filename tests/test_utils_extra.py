import logging
from fusion_artifacts_engine.utils import setup_logging, get_package_version, generate_artifact_id


def test_setup_logging():
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    root.setLevel(logging.WARNING)
    setup_logging(logging.DEBUG)
    assert root.level <= logging.DEBUG


def test_get_package_version():
    v = get_package_version()
    assert isinstance(v, str)
    assert len(v) > 0


def test_generate_artifact_id_default():
    aid = generate_artifact_id()
    assert aid.startswith("art_")
    assert len(aid) == 12


def test_generate_artifact_id_custom_prefix():
    aid = generate_artifact_id("custom_")
    assert aid.startswith("custom_")
