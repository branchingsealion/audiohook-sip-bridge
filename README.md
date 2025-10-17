AudioHook ↔ SIP Bridge

Status: Native SIPS (TLS over TCP) SIP client and bidirectional AudioHook bridge 
with end-to-end secure transport when using SIPS.

Features

- Native SIPS (SIP over TLS) signaling implemented with Python's ssl and asyncio.
- Bidirectional audio bridge:
  - WebSocket binary frames (PCMU 8 kHz) → RTP to SIP peer.
  - Incoming RTP from SIP peer → forwarded as binary frames over WebSocket.
- PCMU (G.711 μ-law) helpers and 20 ms (160-byte) frame utilities in pure Python.
- SIP UUI encode/decode helpers; UUI can be mapped from AudioHook metadata and 
  sent as a SIP User-to-User header.
- Minimal health and metrics endpoints (/healthz, /metrics) over HTTP.
- Dockerfile provided for containerized deployment.

Quick start (local)

1) Create and activate a virtual environment
```bash
   uv venv venv && source venv/bin/activate
   uv pip install .
```

2) Run the bridge (HTTP + WS)
   export HTTP_PORT=8080 WS_PORT=8081 AUDIOHOOK_API_KEY=devkey
   # For SIPS (TLS) set SIP_TRANSPORT=tls and configure TLS verification below
   uv run -m bridge.app

3) Connect a client (example)
```bash
   uv run examples/demo_loopback.py
```

Docker

- Build: `docker build -t audiohook-bridge:dev .`
- Run: `docker run --rm -p 8080:8080 -p 8081:8081 -e AUDIOHOOK_API_KEY=devkey audiohook-bridge:dev`

Configuration

Core
- HTTP_HOST/HTTP_PORT: control plane server (health/metrics)
- WS_HOST/WS_PORT: AudioHook WebSocket server
- WS_TLS_CERTFILE/WS_TLS_KEYFILE: enable WSS for end-to-end encryption with SIPS
- AUDIOHOOK_API_KEY: static API key required on open messages (omit to disable)

SIP
- SIP_USERNAME, SIP_PASSWORD, SIP_DOMAIN: account credentials/realm
- SIP_PROXY: optional outbound proxy (host[:port])
- SIP_PORT: default 5060 (UDP/TCP) or 5061 (TLS)
- SIP_TRANSPORT: udp | tcp | tls
- SIP_TLS_VERIFY: true|false, verify server cert when using TLS (default true)
- SIP_TLS_CAFILE: optional path to a CA bundle file for TLS verification
- SIP_CONTACT_HOST: host/IP to advertise in Contact and Via (defaults to local bind IP)

SRTP (optional; scaffold)
- SRTP: true|false to enable
- SRTP_PROFILE: AES_CM_128_HMAC_SHA1_80 (default) or AES_CM_128_HMAC_SHA1_32
- SRTP_LOCAL_KEY / SRTP_REMOTE_KEY: base64 SDES keys for lab/testing; for production, prefer DTLS-SRTP

AudioHook protocol

- Text JSON messages:
```json
  {"type":"open","apiKey":"...","metadata":{...}}
  {"type":"update","metadata":{...}}
  {"type":"pause"} / {"type":"resume"}
  {"type":"ping"}
  {"type":"sipInvite","target":"sip:user@domain"}
  {"type":"sipBye"}
  {"type":"dtmf","digits":"123#*"}
  {"type":"close"}
```
- Binary frames: PCMU 8 kHz 20 ms (160 bytes) recommended. Frames are sent to SIP over RTP when a call is active; incoming RTP is forwarded to WS clients.
- Acks: openAck/updateAck/paused/resumed/pong/sipInviteAck/sipByeAck/dtmfAck.

Security

- End-to-end encryption when combining SIPS (TLS) on the SIP side and WSS (TLS) on the WebSocket side.
- Enable WSS by setting WS_TLS_CERTFILE and WS_TLS_KEYFILE to your certificate and key.
- Control SIP TLS verification via SIP_TLS_VERIFY and optionally SIP_TLS_CAFILE.

Tests

- Run: pytest -q
- Included tests for PCMU utilities and UUI helpers.

Notes

- Native SIPS implements a minimal INVITE/ACK/BYE and RTP; REGISTER and digest auth are not implemented.
- This project uses PCMU exclusively; no external sound libraries are required by default.
