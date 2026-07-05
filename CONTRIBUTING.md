# Contributing To AISMixer

Thank you for helping improve AISMixer. Contributions should be focused,
reviewable, and compatible with its role as a long-running AIS NMEA 0183
service.

## Repository Boundaries

Every commit, pull request, or agent change instruction must explicitly name
its target branch or repository.

- `main`: runtime code, tests, examples, policies, templates, and the primary
  README.
- `website`: bilingual public website under `docs/`.
- `aismixer.wiki.git` `master`: detailed GitHub Wiki documentation.

Do not mix website, Wiki, and runtime repository changes in one pull request.

## Development Setup

Create and activate a Python virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
```

On Windows PowerShell, activate it with:

```powershell
.\.venv\Scripts\Activate.ps1
```

Install the runtime and development requirements:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

## Development Workflow

- Create a focused branch from the current `main` branch unless the change is
  explicitly for `website` or the Wiki repository.
- Keep changes atomic and reviewable.
- Avoid unrelated formatting churn.
- Do not commit automatically when operating through an agent.
- Inspect `git status -sb` and `git diff` before committing.
- Never commit secrets, private keys, credentials, production configuration, or
  unsanitized operational logs.
- Preserve existing comments and documentation that explain protocol behavior.

## Required Checks

Run the smallest relevant checks while developing, then include the commands and
results in the pull request.

For most repository changes:

```bash
python -m pytest
git diff --check
```

For Python changes, also run syntax checks for changed Python files when useful:

```bash
python -m py_compile <changed-python-files>
```

For YAML changes:

- parse edited YAML files with `yaml.safe_load`;
- validate routing/control YAML through the applicable loader where practical.

For POSIX Unix-domain control transport changes, verify on Linux, WSL,
Raspberry Pi OS, or another POSIX environment with asyncio Unix-socket support.
Use the focused POSIX command documented in the Wiki for that transport before
claiming Unix control integration is verified:

```bash
python -m pytest tests/test_routing_control_unix.py tests/test_routing_control_unix_client.py tests/test_runtime_control.py
```

Do not hardcode a test count in documentation or pull requests.

## Compatibility Expectations

- Preserve legacy broadcast behavior unless the change intentionally alters it
  and documents the impact.
- Consider both legacy mode and routing mode.
- Preserve the separation between internal `source_id` and emitted NMEA TAG
  `s`.
- Preserve `target_id` semantics and target-scoped deduplication in routing
  mode.
- Treat the Unix-domain control transport as POSIX-specific.
- Pure code and tests may run on Windows, but POSIX socket integration requires
  Linux-compatible verification.
- Avoid public behavior changes unless they are intentional, reviewed, tested,
  and documented.

## Documentation Expectations

User-facing changes may require coordinated updates to one or more of:

- `README.md`;
- examples under `examples/`;
- the GitHub Wiki;
- `SECURITY.md` or `ROADMAP.md`;
- the `website` branch.

Not every code change needs every document. Update the documents that operators
or contributors need in order to understand changed behavior.

## Canonical Terminology

Use the established terminology consistently:

- ingress;
- egress;
- UDPSEC;
- `nmea_sproxy`;
- `source_id`;
- `target_id`;
- logical zone;
- routing snapshot;
- generation;
- data plane;
- control plane;
- `aismixerctl`.

For security vulnerabilities, follow [SECURITY.md](SECURITY.md) instead of
opening a public issue.
