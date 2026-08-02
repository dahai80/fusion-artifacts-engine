from fusion_artifacts_engine.ref_parser import parse_refs_from_message, generate_ref_text


def test_generate_ref_text_without_summary():
    text = generate_ref_text("art_abc123", "hello.py", "code", 1, 42)
    assert "art_abc123" in text
    assert "hello.py" in text
    assert "v1" in text
    assert "Size: 42" in text
    assert "Summary" not in text


def test_generate_ref_text_with_summary():
    text = generate_ref_text("art_abc123", "hello.py", "code", 2, 100, "a test file")
    assert "Summary: a test file" in text


def test_parse_refs_from_message_tool_result_format():
    msg = '[Artifact: hello.py | ID: art_abc123 | Version: v1 | Type: code | Size: 42 | Summary: test]'
    refs = parse_refs_from_message(msg)
    assert len(refs) == 1
    r = refs[0]
    assert r.artifact_id == "art_abc123"
    assert r.name == "hello.py"
    assert r.type == "code"
    assert r.version == "1"


def test_parse_refs_from_message_no_summary():
    msg = '[Artifact: hello.py | ID: art_abc123 | Version: v1 | Type: code | Size: 42]'
    refs = parse_refs_from_message(msg)
    assert len(refs) == 1


def test_parse_refs_from_message_xml_format():
    msg = '<artifact id="art_abc" name="test.py" type="code" version="2" size_bytes="100"><summary>test</summary></artifact>'
    refs = parse_refs_from_message(msg)
    assert len(refs) == 1
    r = refs[0]
    assert r.artifact_id == "art_abc"
    assert r.name == "test.py"


def test_parse_refs_from_message_xml_latest():
    msg = '<artifact id="art_abc" name="test.py" type="code" version="latest" size_bytes="100"></artifact>'
    refs = parse_refs_from_message(msg)
    assert len(refs) == 1
    assert refs[0].version == "latest"


def test_parse_refs_no_match():
    refs = parse_refs_from_message("no artifact refs here")
    assert len(refs) == 0


def test_roundtrip_generate_and_parse():
    text = generate_ref_text("art_test1234", "app.py", "code", 3, 256, "main app")
    refs = parse_refs_from_message(text)
    assert len(refs) == 1
    assert refs[0].artifact_id == "art_test1234"


def test_parse_refs_xml_version_non_numeric():
    msg = '<artifact id="art_y1" name="z.py" type="code" version="xyz" size_bytes="10"></artifact>'
    refs = parse_refs_from_message(msg)
    assert len(refs) == 1
    assert refs[0].version == 'xyz'


def test_parse_refs_xml_no_summary():
    msg = '<artifact id="art_z1" name="a.py" type="code" version="1" size_bytes="10"></artifact>'
    refs = parse_refs_from_message(msg)
    assert len(refs) == 1
    assert refs[0].summary == ''
