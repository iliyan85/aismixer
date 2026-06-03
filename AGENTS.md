# AGENTS.md

## Project: aismixer

`aismixer` is a Python service for receiving, normalizing, deduplicating, tagging, and forwarding AIS NMEA 0183 streams.

The project’s main purpose is to combine AIS data from multiple receiver inputs into clean logical streams that can be forwarded to marine platforms or downstream consumers.

Core responsibilities:

* receive AIS NMEA 0183 messages, primarily `!AIVDM` / `!AIVDO` and compatible variants;
* support multiple UDP inputs;
* support secure UDP inputs and forwarders;
* remove duplicate AIS messages in near real time;
* preserve or rewrite NMEA 4.0 TAG block metadata according to configuration;
* forward cleaned streams to one or more configured destinations;
* run reliably as a long-running production service, including under `systemd`.

This repository is not a toy example. Treat it as production-oriented infrastructure code for maritime telemetry.

---

## General behavior for Codex

When working in this repository:

1. Read existing code before proposing changes.
2. Prefer small, reviewable diffs.
3. Do not rewrite large parts of the project unless explicitly asked.
4. Do not change public behavior silently.
5. Preserve backward compatibility unless the task explicitly says otherwise.
6. Avoid speculative refactors.
7. Do not introduce new dependencies unless clearly justified.
8. Do not remove comments or documentation that explain protocol behavior.
9. Do not claim tests passed unless you actually ran them.
10. If the repository state is unclear, report what you found and ask for the smallest necessary clarification.

Before editing files, summarize:

* what you believe the task is;
* which files are likely affected;
* what risks exist;
* what checks you plan to run.

---

## Safety and permissions

Default mode should be conservative.

Do not run destructive commands such as:

* `rm -rf`
* forced git resets
* forced checkouts
* commands that rewrite history
* commands that delete databases, logs, captured samples, or configuration files

Do not access the network unless explicitly needed and approved.

Do not modify secrets, private keys, certificates, or local production configuration.

Do not commit changes automatically. Prepare changes for human review.

---

## Git workflow

Before making changes, inspect the current state:

```bash
git status -sb
```

If there are existing user changes, do not overwrite them. Treat uncommitted changes as belonging to the user.

Branch responsibilities:

* `main` is the primary runtime/development branch.
* `website` is the dedicated GitHub Pages branch.
* GitHub Pages deploys from the `website` branch, using `/docs` as the site root.
* `docs/` must not be reintroduced on `main`.
* `tests/` must remain on `main`; tests are part of development and CI.
* Production Python code changes normally happen on `main`.
* Website, Jekyll, and GitHub Pages changes normally happen on `website`.

Before any commit, pull, merge, delete, reset, branch switch, or other branch-sensitive operation, run:

```bash
git status -sb
```

Read and report the current branch before proceeding. If the requested operation involves a commit or push, the instructions and response must explicitly name the target branch.

When presenting work, summarize:

* files changed;
* purpose of each change;
* tests or checks run;
* remaining risks.

Do not create commits unless explicitly instructed.

---

## Architecture principles

`aismixer` should remain modular.

Keep the following concerns separated as much as possible:

* input receivers;
* NMEA sentence extraction;
* NMEA checksum handling;
* TAG block parsing and generation;
* deduplication;
* source identification;
* secure transport;
* forwarding;
* configuration;
* logging;
* service/runtime behavior.

Avoid coupling protocol parsing directly to network I/O when a pure function or testable helper would work better.

Prefer deterministic behavior over implicit magic.

---

## AIS and NMEA rules

AIS/NMEA handling is the most sensitive part of the project.

Do not casually change:

* sentence extraction;
* multipart message handling;
* checksum validation or generation;
* TAG block parsing;
* deduplication keys;
* ordering behavior;
* source attribution behavior.

The parser must handle realistic AIS input, including:

* single NMEA sentences;
* multipart AIS messages;
* pasted or concatenated raw lines;
* vendor or software-added metadata;
* NMEA 4.0 TAG blocks;
* `!AIVDM`, `!AIVDO`, and compatible AIS talker variants when supported by existing logic.

When touching parsing logic, add or update tests with real-looking examples.

---

## NMEA TAG block policy

The project uses NMEA 4.0 TAG block concepts, especially:

* `s` — source identifier;
* `c` — timestamp;
* `g` — grouping/message relation identifier.

Do not change TAG behavior without explicit instruction.

Expected source identifier policy:

1. Prefer configured station/source identifier when present and non-empty.
2. Then prefer per-input configured identifier.
3. Then use alias mapping where configured.
4. Only then fall back to safe defaults.

The `s` value may be sticky across multipart messages when the protocol logic requires it.

The `c` timestamp may be preserved from ingress or replaced by server time depending on configuration.

The `g` group identifier may be preserved or regenerated depending on configuration.

Generated `g` values should be safe, numeric when configured that way, fixed-length if configured, and should avoid leading zeros when that is part of the existing policy.

Do not introduce unbounded in-memory growth for source/tag tracking. This service is expected to run indefinitely.

---

## Deduplication rules

Deduplication must be near-real-time and safe for continuous operation.

When changing dedup logic:

* preserve existing TTL/cache behavior unless explicitly asked;
* avoid unbounded memory growth;
* be careful with multipart messages;
* avoid false deduplication across genuinely distinct AIS messages;
* avoid forwarding duplicate messages caused by multiple receiver inputs.

If deduplication keys are changed, explain the compatibility impact.

---

## Secure UDP / cryptographic direction

The project direction includes secure UDP communication between AIS stations and `aismixer`.

Preferred cryptographic direction:

* asymmetric station identity;
* ECDSA-oriented signatures;
* compact keys suitable for Raspberry Pi-class devices;
* user-friendly station-side setup;
* explicit replay protection;
* clear handshake context.

For secure protocol work, preserve these design expectations:

* station private key filename: `station_private.pem`;
* station public key filename: `station_public.pem`;
* signed handshake context string should include a protocol-specific prefix such as `NMEA-AUTH-v1`;
* handshake should include anti-replay protection using nonce and/or timestamp;
* final negotiated parameters should be bound by a final hash or equivalent transcript confirmation;
* do not rely only on transport-level protection when payload signing or message authenticity is required.

Do not add cryptographic code casually. Prefer standard, reviewed libraries. Avoid custom crypto primitives.

---

## Multi-user / vhost direction

The project may evolve toward multiple logical subscribers or tenants.

Do not hard-code assumptions that only one global stream, one subscriber, or one output exists.

Prefer designs that can later support:

* multiple input groups;
* multiple dedup clusters;
* multiple output targets;
* per-subscriber forwarding policy;
* secure and non-secure inputs side by side;
* clear mapping from ingress source to logical output stream.

Do not implement the full multi-tenant model unless explicitly asked. Keep current changes compatible with that direction.

---

## Configuration principles

Configuration should remain explicit and understandable.

When adding configuration options:

* provide safe defaults;
* document the option;
* avoid ambiguous names;
* avoid changing existing defaults unless explicitly requested;
* keep examples updated when behavior changes.

Do not break existing config files without a migration note.

---

## Logging principles

Logs should help operate a long-running AIS service.

Prefer logs that explain:

* input startup;
* forwarder startup;
* configuration problems;
* malformed input;
* secure handshake failures;
* dedup/cache behavior when relevant;
* destination forwarding errors.

Avoid logging private keys, secrets, full credentials, or sensitive operational details.

Do not flood logs per AIS sentence unless debug mode is explicitly enabled.

---

## Testing expectations

When changing code, run the smallest relevant checks first.

If the project has tests, prefer:

```bash
python -m pytest
```

or the documented project test command.

If there are no tests for the touched area, add focused tests when practical.

For parser and protocol changes, include test cases covering:

* plain single-sentence AIS input;
* TAG-prefixed input;
* multipart input;
* concatenated or pasted raw lines;
* invalid or malformed input;
* checksum-relevant behavior if touched.

Do not claim that the full project is validated if only partial tests were run.

---

## Documentation expectations

Update documentation when behavior changes.

Important documentation areas include:

* README usage examples;
* configuration examples;
* secure UDP setup;
* station key generation;
* systemd deployment;
* NMEA TAG behavior;
* deduplication behavior;
* sample input/output behavior.

Documentation should be practical and operator-friendly.

Assume some station operators may be sailors, radio amateurs, fishermen, or enthusiasts without deep Linux/networking expertise.

---

## Style expectations

Use clear, maintainable Python.

Prefer:

* simple functions;
* explicit names;
* type hints where useful;
* minimal global state;
* standard library when sufficient;
* small helpers for protocol parsing;
* tests for edge cases.

Avoid:

* unnecessary framework changes;
* broad rewrites;
* hidden background threads without lifecycle control;
* silent exception swallowing;
* excessive cleverness;
* global mutable state that can grow forever.

---

## Production assumptions

`aismixer` may run as a daemon under `systemd`.

Therefore:

* avoid memory leaks;
* avoid unbounded caches;
* handle malformed input robustly;
* avoid crashing on one bad packet;
* handle destination failures gracefully;
* keep shutdown behavior clean where applicable;
* prefer explicit timeouts for network operations where relevant.

---

## First-response checklist for Codex

For any non-trivial task, begin with:

1. repository files inspected;
2. understanding of the requested change;
3. proposed implementation plan;
4. affected files;
5. risks;
6. tests/checks to run.

Then wait for confirmation if the task is large or risky.

For small, clearly requested fixes, proceed with a minimal diff and report the result.

---

## Review checklist

Before finishing a change, verify:

* the change is minimal;
* behavior matches the request;
* existing public behavior is preserved unless intentionally changed;
* NMEA/TAG/dedup behavior is not accidentally affected;
* tests were added or updated when appropriate;
* relevant tests/checks were run;
* documentation was updated if needed;
* no secrets or local-only paths were introduced.

---

## Suggested response format after changes

When reporting back, use this structure:

```text
Summary:
- ...

Changed files:
- ...

Checks run:
- ...

Notes / risks:
- ...
```

Be honest about anything not tested.
