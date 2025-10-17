"""
SIP UUI (User-to-User) header encodes/decodes helpers and mapping to/from AudioHook metadata.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class UUIConfig:
    encoding: str = "ascii"  # "ascii" or "hex"
    purpose: str = "isdn-uui"
    content: str = "content"


def encode_uui(value: str, cfg: UUIConfig | None = None) -> str:
    cfg = cfg or UUIConfig()
    if cfg.encoding == "hex":
        hex_str = value.encode("utf-8").hex()
        return f"{hex_str};encoding=hex;purpose={cfg.purpose};content={cfg.content}"
    else:
        # default ASCII per RFC 7433 (no encoding param implies UTF-8/ASCII)
        return f"{value};purpose={cfg.purpose};content={cfg.content}"


def decode_uui(uui_header: str) -> tuple[str, dict[str, str]]:
    # very simple parser; assumes a single value
    parts = [p.strip() for p in uui_header.split(";")]
    value = parts[0]
    params: dict[str, str] = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.strip()] = v.strip()
    if params.get("encoding") == "hex":
        try:
            value = bytes.fromhex(value).decode("utf-8", errors="replace")
        except ValueError:
            # leave as-is if invalid hex
            pass
    return value, params


AUDIOHOOK_UUI_KEYS = ("uui", "externalId")


def metadata_to_uui(metadata: dict[str, str], cfg: UUIConfig | None = None) -> str | None:
    cfg = cfg or UUIConfig()
    # pick the first present key
    for k in AUDIOHOOK_UUI_KEYS:
        if k in metadata:
            return encode_uui(str(metadata[k]), cfg)
    # context.* flatten support
    for k, v in metadata.items():
        if k.startswith("context."):
            return encode_uui(str(v), cfg)
    return None


def uui_to_metadata(uui_header: str, cfg: UUIConfig | None = None) -> dict[str, str]:
    cfg = cfg or UUIConfig()
    value, params = decode_uui(uui_header)
    md = {"uui": value, "uui.encoding": params.get("encoding", cfg.encoding)}
    if "purpose" in params:
        md["uui.purpose"] = params["purpose"]
    if "content" in params:
        md["uui.content"] = params["content"]
    return md
