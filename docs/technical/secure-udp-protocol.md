---
layout: default
title: Secure UDP Protocol
permalink: /technical/secure-udp-protocol/
---

# Secure UDP Protocol

This page describes the current secure UDP behavior used by AISMixer and its
station-side proxy.

## Packet Prefixes

- `NMEA-H`: handshake packet.
- `NMEA-D`: encrypted AIS/NMEA data packet.
- `KEEPALIVE`: plaintext session keepalive packet.

## Handshake

The station sends a text handshake packet:

```text
NMEA-H|<station_id>|<unix_timestamp>|<base64(client_signature)>
```

The current client signing payload is not the text packet above. It is:

```python
b"NMEA-H" + station_id.encode() + timestamp.to_bytes(8, "big")
```

The server verifies the client signature against the authorized public key for
`station_id`. The server accepts only timestamps within 30 seconds of server
time.

After signature verification, the server checks a process-local handshake replay
cache. The replay key is based on the station id, timestamp, and exact client
signature bytes. The cache TTL is 60 seconds and it is bounded to 100000
entries. Replayed verified handshakes are rejected before a session is created
or an `OK` response is sent.

On success, the server signs the same current handshake payload and responds:

```text
OK|<base64(server_signature)>
```

## Session Key

The station and server derive a shared secret with ECDH. The AES session key is
derived with SHA-256 over a protocol label, the ECDH shared secret, the client
signature, and the server signature.

The server stores sessions by UDP peer address. Each session currently contains
the station id, AES-GCM object, creation time, last-seen time, and a per-session
data nonce cache.

## Encrypted Data

The station encrypts each extracted AIS/NMEA sentence as JSON:

```json
{
  "type": "nmea",
  "payload": "<NMEA sentence>",
  "timestamp": 1234567890,
  "source_id": "<station_id>"
}
```

The UDP packet layout is:

```text
NMEA-D || 12-byte AES-GCM nonce || ciphertext || 16-byte GCM tag
```

AES-GCM uses AAD:

```python
b"NMEA"
```

The server rejects malformed `NMEA-D` packets that are too short to contain the
prefix, 12-byte nonce, and GCM tag-sized payload.

## Data Nonce Replay Protection

Data nonce tracking is per session. The nonce key is the raw 12-byte AES-GCM
nonce from the packet.

For an active session, the server checks whether the nonce is already recorded
before decrypting. Duplicate nonces are rejected without decrypting, touching the
session, or enqueueing data.

For a new nonce, the server decrypts, parses JSON, and verifies that
`source_id` matches the authenticated session station id. Only after those
checks pass does it record the nonce. The data nonce cache TTL currently matches
the session TTL, 300 seconds, and is bounded to 100000 entries per session.

Failed decrypts, invalid JSON, malformed packets, and `source_id` mismatches do
not record nonces.

## Session TTL And Keepalive

Sessions expire when `last_seen` is more than 300 seconds old. Expired sessions
are removed when looked up.

The station sends plaintext keepalive packets every 30 seconds:

```text
KEEPALIVE|<station_id>|<unix_timestamp>
```

The server updates `last_seen` only when an active session exists for the UDP
peer address and the keepalive `station_id` matches the session station id.
Keepalive packets do not create sessions and do not enqueue AIS data.

## Station Identity

The handshake station id selects the authorized public key. Encrypted data must
carry a JSON `source_id` that matches the station id stored in the active
session. A mismatch is rejected and does not refresh the session or record the
data nonce.

When accepted data is queued, the secure input source identifier is the
configured secure input id if provided, otherwise the station id.

## Current Limitations

- `NMEA-AUTH-v1` transcript helper code exists, but v1 transcript binding is
  not wired into the live handshake.
- The handshake packet format is still the current `NMEA-H|...` format; public
  key material is not currently transcript-bound by the live protocol.
- The JSON payload `timestamp` is carried but freshness is not currently
  validated.
- Secure data replay protection tracks AES-GCM nonce reuse only within the
  active process/session cache. It is not persistent across service restarts.
- Station-side key naming currently uses the legacy `station_private.key`
  filename in code and sample config.
