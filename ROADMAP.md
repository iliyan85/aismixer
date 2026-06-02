# AISMixer Roadmap

This roadmap captures the current stabilization state and the next practical engineering steps.

## Completed Stabilization

- Added focused pytest coverage for AIS multipart assembly, TAG policy helpers, metadata writing, `nmea_sproxy` extraction, secure UDP helpers, and `aismixer.forward_loop`.
- Hardened multipart assembly against malformed fragment count fields.
- Documented current multipart behavior: assembly uses NMEA fragment fields, while TAG `g` is preserved or regenerated as metadata.
- Fixed `nmea_sproxy` forwarding of plain `!AIVDM` / `!AIVDO` datagrams.
- Added backward-compatible support for the legacy `aismixer_public_key` proxy config key.
- Added secure UDP packet structure parsing for `NMEA-D` packets.
- Added secure session TTL scaffolding and wired active-session expiry.
- Added plaintext KEEPALIVE handling for existing active secure sessions.
- Added `.gitattributes` to keep source files on stable LF line endings.

## Next Technical Areas

1. Secure UDP transcript binding
   - Bind the final session key to an explicit transcript including protocol context, station id, timestamp, client signature, server signature, and negotiated parameters.
   - Put the existing `NMEA-AUTH-v1` context string to active use.

2. Anti-replay protection
   - Add nonce and/or timestamp replay tracking for handshakes.
   - Add data-packet nonce reuse detection per active session.
   - Keep memory bounded with TTL caches.

3. Station key filename migration
   - Migrate station private key naming toward `station_private.pem`.
   - Keep compatibility with existing `station_private.key` installations during transition.
   - Document the migration clearly for operators.

4. Secure UDP integration tests
   - Add socket-free packet handling tests before socket-level tests.
   - Then add local loopback integration tests only if they can run reliably without external network dependencies.

5. Operator-friendly secure station setup docs
   - Document station key generation, server authorization, proxy config, and systemd deployment.
   - Include examples for Raspberry Pi-class station setups and common troubleshooting steps.
