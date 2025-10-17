from audiohooksipbridge.uui import encode_uui, decode_uui, metadata_to_uui, uui_to_metadata, UUIConfig


def test_encode_decode_ascii():
    cfg = UUIConfig(encoding="ascii", purpose="foo", content="bar")
    header = encode_uui("hello-123", cfg)
    assert ";purpose=foo" in header and ";content=bar" in header
    value, params = decode_uui(header)
    assert value == "hello-123"
    assert params.get("purpose") == "foo"


def test_encode_decode_hex():
    cfg = UUIConfig(encoding="hex", purpose="p")
    header = encode_uui("héx✓", cfg)
    assert ";encoding=hex" in header
    value, params = decode_uui(header)
    assert value == "héx✓"


def test_metadata_mapping_prefers_uui_key():
    cfg = UUIConfig(encoding="ascii")
    md = {"externalId": "ext-1", "uui": "abc", "context.trace": "xyz"}
    header = metadata_to_uui(md, cfg)
    assert header and header.startswith("abc;")
    back = uui_to_metadata(header, cfg)
    assert back["uui"] == "abc"
