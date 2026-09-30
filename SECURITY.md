# Security policy

## Deployment boundary

Gufo Dashboard is intended for trusted local machines and private networks.
It has no built-in authentication, authorization, TLS, or request-size limit.
Anyone who can reach it can read statistics and captured text, clear history,
and access upstream Gufo endpoints. It does not make an exposed Gufo server safer.

The default listen address is `0.0.0.0`. Set `DASHBOARD_HOST=127.0.0.1` for
local-only Python or host-network Docker deployments. For bridge networking,
restrict the published port's host address. If remote access is needed, place an
authenticated TLS gateway in front of the whole service, including `/api/*` and
proxied routes, and restrict direct access to Gufo as well.

Client credentials are forwarded to Gufo. `GUFO_API_KEY` is used only for status
polls; it is not a dashboard password. Database records contain identifiers,
usage metadata, and optionally captured questions and answers. Anyone who can
reach the dashboard can read or delete that content; keep dashboard access and
database backups private. See [Privacy](docs/privacy.md).

## Reporting a vulnerability

Use the repository's **Security → Report a vulnerability** feature when private
vulnerability reporting is enabled. Include the affected version, reproduction,
impact, and a minimal sanitized example. Do not submit a public exploit or secret.

If private reporting is unavailable, open an issue asking for a private reporting
channel without including vulnerability details. Maintainers should enable
private reporting before public launch.

## Maintenance scope

Security fixes are intended for the latest development version. Older releases
have no separate support commitment. No response-time or patch-time guarantee is
currently published.
