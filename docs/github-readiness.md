# Publication readiness

## Repository contents

The repository contains application source, controlled test fixtures, dependency
pins, documentation, and GitHub workflows. Local environments, runtime databases,
configuration secrets, archives, caches, implementation notes, and session
screenshots are excluded.

Published fixtures use generic model names, response IDs, request IDs, and
timestamps. They preserve usage and timing values needed by the tests, along
with harmless controlled greeting and image-description content. Recapturing
fixtures writes raw responses; review and sanitize every recapture before
committing it.

Project links point to [MiddlePathLabs/gufo-dashboard](https://github.com/MiddlePathLabs/gufo-dashboard).
Private vulnerability reporting is enabled. See the [security policy](../SECURITY.md).

## Validation

- The current suite passes 121 tests, including optional content capture,
  forwarding, privacy, retention, and deletion behavior.
- Ruff lint and formatting, mypy, Python distribution builds, and Docker builds
  pass. Runtime dependency auditing reports no known vulnerabilities at review
  time; CI repeats the audit.
- GitHub Actions are pinned to verified commit SHAs. Runtime dependencies are
  locked and the pip export contains hashes.
- Staged contents are reviewed for credentials, local usernames, machine names,
  home-directory paths, private network addresses, and original session metadata.
  Commit attribution uses a project identity rather than a personal account.

These checks do not guarantee the absence of every possible secret. Review new
fixtures, screenshots, logs, and generated artifacts before committing them.

## Deployment boundaries

The dashboard remains a trusted-network utility. It has no built-in
authentication, TLS, request-size limit, or coordinated multi-process state.
Optional text capture is off by default and may retain sensitive user text when
enabled. Keep database files and backups private; deletion is not secure erasure.
See [Privacy](privacy.md) and [Deployment](deployment.md).

The Docker base image and Python build backend are not pinned by immutable
version/digest, so runtime dependency pins do not make the entire build hermetic.
Gufo compatibility is based on controlled fixtures rather than a recorded
upstream commit. Test updates against the upstream release you intend to use.

## Maintainer checklist

1. Review `git diff --cached` before committing. Ignore rules do not remove
   already-tracked files and do not protect manual folder or archive uploads.
2. Confirm CI succeeds on the published commit before treating it as a release.
3. Protect the default branch once it exists remotely, using the desired review
   and required-check policy.
4. Review dependency updates and regenerate both `uv.lock` and `requirements.txt`.
5. Scan imported Git history before publishing it, and rotate any exposed secrets.

Keep runtime data out of release archives. See [Contributing](../CONTRIBUTING.md)
for development checks and fixture handling.
