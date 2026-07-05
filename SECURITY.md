# Security Policy

## Supported Development State

AISMixer has not yet declared a formally supported stable release line. Until
the first versioned release is declared, security fixes target the current
`main` branch.

Historical commits, forks, local modifications, and unmaintained deployments
are not automatically supported. Operators should track current security
updates and review deployment-specific configuration, key handling, and network
exposure.

## Reporting A Vulnerability

Please report suspected security vulnerabilities privately. Do not open a
public GitHub issue or discussion for a vulnerability, and do not publish
exploit details before maintainers have had a reasonable opportunity to review
the report.

Use GitHub's **Security** tab and **Report a vulnerability** option when Private
Vulnerability Reporting is available for this repository. If that option is not
available, use a private contact method already listed on the repository
maintainer's GitHub profile and request a private reporting channel.

Include, when practical:

- the affected commit or deployment version;
- the affected component, such as UDP ingress, UDPSEC, `nmea_sproxy`, NMEA
  parsing, routing, runtime control, installation, or configuration handling;
- reproduction steps or a minimal proof of concept;
- expected and observed impact;
- relevant mitigations or workarounds;
- whether the issue is already publicly known.

Do not include real private keys, credentials, unsanitized configuration,
public IPs, sensitive station identifiers, or operational logs unless they have
been redacted appropriately.

## Security-Relevant Surfaces

Security-sensitive areas include:

- plain UDP ingress;
- UDPSEC authentication and encryption;
- `nmea_sproxy` station private keys;
- AISMixer server private keys;
- authorized station public keys in `authorized_keys.yaml`;
- NMEA sentence extraction, checksum-relevant handling, and multipart assembly;
- NMEA TAG `s`/`c`/`g` metadata handling;
- internal source identity and routing decisions;
- the POSIX Unix-domain routing-control socket;
- configuration files, installers, update scripts, and systemd deployment.

## Current Trust Boundaries

- Plain UDP ingress has no cryptographic authentication or confidentiality.
- UDP source IPs and UDP alias mappings are operational identifiers, not
  cryptographic identity.
- Authenticated UDPSEC station identity is distinct from the emitted NMEA TAG
  `s` value.
- Filesystem ownership, group membership, and mode bits are the current
  authorization boundary for the Unix control socket.
- The control protocol has no application-level authentication token.
- `expected_generation` prevents stale routing updates, but it is not
  authentication or authorization.
- Runtime routing state and generations are process-local and are not currently
  persisted across restart.
- UDPSEC authenticates and encrypts transport between a configured station and
  AISMixer, but it does not prove the semantic truth of AIS payloads.
- AIS spoof or anomaly detection is not currently implemented.

## Key And Secret Handling

- Never commit station private keys, AISMixer server private keys, credentials,
  or production-only configuration.
- Protect private keys and sensitive configuration files with restrictive
  filesystem permissions.
- Authorized public keys may be distributed, but files that grant station
  authorization still need controlled modification and review.
- Redact keys, hostnames, public IP addresses, station identifiers, credentials,
  and operational logs when sharing reports publicly.
- Treat local socket paths, runtime configuration, and logs as potentially
  sensitive when they reveal deployment topology.

## Response Expectations

AISMixer is an open-source project maintained on a best-effort basis. There is
no guaranteed response-time or remediation SLA. Maintainers will try to
acknowledge, investigate, fix, and coordinate disclosure for valid reports as
time permits.
