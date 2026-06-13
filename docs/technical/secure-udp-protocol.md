---
layout: default
title: Secure UDP Protocol
description: AISMixer secure UDP protocol for authenticated, encrypted, NAT-friendly forwarding of AIS NMEA 0183 streams with nmea_sproxy.
permalink: /technical/secure-udp-protocol/
---

# Secure UDP Protocol

This page describes the implemented secure UDP behavior used between AISMixer
and the station-side `nmea_sproxy`.

- **AISMixer** is the mixer, deduplicator, tag-aware normalizer, and forwarder.
- **`nmea_sproxy`** is a client-side secure shovel/proxy. One process maps one
  local UDP input to one encrypted AISMixer SEC input; it does not mix streams.

The design assumes the station may be behind NAT, CGNAT, or a mobile network.
The station is the active side: it initiates the handshake and sends encrypted
ping traffic to a reachable AISMixer SEC input.

## Packet Prefixes

- `NMEA-H`: handshake packet.
- `OK`: successful signed handshake response.
- `NMEA-D`: AES-GCM authenticated encrypted JSON packet carrying AIS/NMEA data,
  a ping, or a pong.
- `NOSESSION`: unauthenticated reconnect hint.
- `KEEPALIVE`: legacy plaintext keepalive accepted by AISMixer for an existing
  session. The current `nmea_sproxy` lifecycle uses encrypted ping/pong instead.

## Handshake

The station initiates a text handshake packet:

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

After signature verification, AISMixer checks a process-local handshake replay
cache. The replay key is based on the station id, timestamp, and exact client
signature bytes. The cache TTL is 60 seconds and it is bounded to 100000
entries. A replayed verified handshake is rejected before a session is created
or an `OK` response is sent.

On success, AISMixer signs the same current handshake payload and responds:

```text
OK|<base64(server_signature)>
```

`nmea_sproxy` verifies that signature against its configured AISMixer public
key. The handshake therefore authenticates the station to AISMixer and proves
the configured server identity to the station.

## Session Key And Peer Binding

The station and server derive a shared secret with ECDH. The AES session key is
derived with SHA-256 over the `NMEA-SESSION` protocol label, ECDH shared secret,
client signature, and server signature.

AISMixer stores the session by the observed UDP peer address: the exact source
IP and source port. Each session contains the station id, AES-GCM object,
creation time, last-seen time, and a per-session nonce cache.

There is no session migration. NAT rebinding, changing networks, or changing
the client source port requires a new handshake.

## Authenticated Encrypted Packets

Every current AIS data, ping, and pong message is JSON inside an AES-GCM
authenticated encrypted `NMEA-D` packet. An AIS/NMEA data message looks like:

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

AISMixer rejects malformed `NMEA-D` packets that are too short to contain the
prefix, 12-byte nonce, and GCM tag-sized payload.

Supported encrypted message types are:

- `nmea`: carries one extracted AIS/NMEA sentence.
- `ping`: sent by `nmea_sproxy` with a sequence number.
- `pong`: returned by AISMixer with the matching sequence number.

All accepted encrypted messages must carry a `source_id` matching the station id
stored in the active session.

## Encrypted Ping/Pong Liveness

By default, `nmea_sproxy` sends an authenticated encrypted ping every 30 seconds
(`keepalive_interval: 30`). Valid ping traffic refreshes the AISMixer session
and helps keep NAT, CGNAT, and mobile-client UDP mappings alive.

AISMixer replies with an authenticated encrypted pong. `nmea_sproxy` treats a
pong as proof of liveness only when it:

- comes from the configured remote address;
- decrypts and authenticates with the active AES-GCM session key;
- carries the configured station id; and
- matches the expected ping sequence number.

Only a matching authenticated pong refreshes the client's peer-liveness timer.

## Recovery And Timeouts

The default client lifecycle settings are:

```yaml
reconnect_delay: 5
keepalive_interval: 30
peer_timeout: 90
session_refresh_interval: 0
```

- `peer_timeout` ends the client session and starts a new handshake when
  matching authenticated pongs stop arriving.
- `session_refresh_interval` optionally schedules a re-handshake. Its default
  value is `0`, which disables periodic refresh.
- `reconnect_delay` applies after handshake failures, socket failures,
  `peer_timeout`, and `NOSESSION`. A configured scheduled refresh re-handshakes
  immediately.

AISMixer expires an inactive server-side session after 300 seconds. When it
receives secure traffic for a session it no longer has, it may return:

```text
NOSESSION
NOSESSION|<station_id>
```

`NOSESSION` is not authenticated and creates no session. `nmea_sproxy` accepts
it only from the configured remote address, treats it only as a reconnect hint,
ends the local session, and attempts a new handshake after `reconnect_delay`.

This recovery model is friendly to clients behind NAT, CGNAT, and changing
mobile networks, but a changed source address still requires a fresh handshake.
It does not make UDP reliable or guarantee delivery of every AIS sentence.

## Nonce Replay Protection

Data nonce tracking is per session. The nonce key is the raw 12-byte AES-GCM
nonce from the packet.

For an active session, AISMixer checks whether the nonce is already recorded
before decrypting. Duplicate nonces are rejected without decrypting, touching the
session, enqueueing data, or returning a pong.

For a new nonce, the server decrypts, parses JSON, and verifies that
`source_id` matches the authenticated session station id. Only after those
checks pass does it record the nonce. The data nonce cache TTL currently matches
the session TTL, 300 seconds, and is bounded to 100000 entries per session.

Failed decrypts, invalid JSON, malformed packets, and `source_id` mismatches do
not record nonces.

## Station Identity

The handshake station id selects the authorized public key. Encrypted data must
carry a JSON `source_id` that matches the station id stored in the active
session. A mismatch is rejected and does not refresh the session or record the
data nonce.

The operator-facing station key-pair names are `station_private.pem` and
`station_public.pem`. The private key stays on the station; the public key is
used for the station entry in AISMixer's `authorized_keys.yaml`.

When accepted data is queued, the secure input source identifier is the
configured secure input id if provided, otherwise the station id.

## Current Guarantees And Limits

- Station authentication and the server handshake response use ECDSA.
- AIS data and current ping/pong liveness use AES-GCM authenticated encryption.
- Handshake replay and active-session AES-GCM nonce reuse are checked in
  bounded, process-local caches.
- The current live handshake does not yet bind the existing `NMEA-AUTH-v1`
  transcript helper or public-key material into a full versioned transcript.
- The handshake timestamp is checked against a 30-second server-time window.
  The encrypted JSON payload `timestamp` is carried but its freshness is not
  validated.
- Replay caches are not persistent across service restarts.
- `NOSESSION` is intentionally an unauthenticated reconnect hint, not proof of
  server identity.
- Sessions are bound to the observed source IP and port; no session migration
  is implemented.
- UDP packet loss remains possible, including while a session is recovering.

For operator setup, one-to-one relation configuration, systemd singleton and
template instances, and troubleshooting, see the current
[`nmea_sproxy` operator guide](https://github.com/iliyan85/aismixer/blob/main/nmea_sproxy/README.md).
