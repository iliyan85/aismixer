# Contributing To aismixer

Thank you for helping improve aismixer. Contributions should be focused,
reviewable, and compatible with its role as a long-running AIS NMEA 0183
service.

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

## Running Tests

Run the full test suite before submitting a pull request:

```bash
python -m pytest
```

Changes should include focused tests when practical, especially when modifying
NMEA extraction, multipart assembly, TAG handling, deduplication, secure UDP,
configuration, or forwarding behavior.

## Coding Style

- Follow the existing Python style and module boundaries.
- Use four spaces for indentation and keep code readable and explicit.
- Prefer small, testable helpers over coupling protocol logic to network I/O.
- Keep comments concise and use them where behavior is not self-explanatory.
- Preserve backward compatibility unless a change intentionally documents an
  incompatible behavior.
- Do not introduce dependencies without a clear reason.
- Never commit private keys, credentials, production configuration, or
  unsanitized operational logs.

The repository does not currently enforce a specific formatter. Keep unrelated
formatting changes out of your contribution.

## Branches And Commits

- Use `main` for service code, tests, operator documentation, and repository
  governance files.
- Use `website` for the public GitHub Pages site under `docs/`.
- Do not mix website and runtime changes in one pull request.
- Create a focused topic branch from the appropriate target branch.
- Keep commits small and logically grouped, with clear imperative summaries.
- Do not rewrite unrelated code while addressing a focused issue.

## Pull Requests

Before opening a pull request:

1. Explain the problem and the chosen approach.
2. Describe compatibility or configuration impact.
3. Add or update tests when practical.
4. Run `python -m pytest`.
5. Run `git diff --check`.
6. Remove secrets and sanitize configs, logs, addresses, and station details.

For security vulnerabilities, follow [SECURITY.md](SECURITY.md) instead of
opening a public issue.

