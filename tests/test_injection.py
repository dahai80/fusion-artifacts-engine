from fusion_artifacts_engine.ref_parser import parse_refs_from_message, generate_ref_text


def test_parse_tool_result_ref():
    text = "[Artifact: test.py | ID: art_abc123 | Version: v1 | Type: code | Tokens: 4200 | Summary: A test file]"
    refs = parse_refs_from_message(text)
    assert len(refs) == 1
    assert refs[0].artifact_id == "art_abc123"
    assert refs[0].name == "test.py"
    assert refs[0].version == "1"
    assert refs[0].type == "code"


def test_parse_xml_ref():
    text = '<artifact id="art_xyz" name="app.py" type="code" version="2" token_count="100"><summary>Main app</summary></artifact>'
    refs = parse_refs_from_message(text)
    assert len(refs) == 1
    assert refs[0].artifact_id == "art_xyz"
    assert refs[0].version == "2"


def test_parse_no_refs():
    refs = parse_refs_from_message("Just plain text, no refs")
    assert len(refs) == 0


def test_parse_multiple_refs():
    text = (
        "Here is [Artifact: a.py | ID: art_a1 | Version: v1 | Type: code | Tokens: 100 | Summary: A] "
        "and [Artifact: b.py | ID: art_b2 | Version: v3 | Type: code | Tokens: 200 | Summary: B]"
    )
    refs = parse_refs_from_message(text)
    assert len(refs) == 2


def test_generate_ref_text():
    text = generate_ref_text("art_abc", "test.py", "code", 1, 4200, "A test file")
    assert "art_abc" in text
    assert "test.py" in text
    assert "4200" in text
