# Security policy

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability, exposed credential, or private research artifact. Use GitHub's private vulnerability reporting for `kala-project/trust-stack` when available. If private reporting is not available, contact the repository owners through a private channel before disclosing details.

Include the affected version, impact, reproduction steps, and any evidence needed to confirm the issue. Do not include real credentials, private documents, or personal data.

## Supported version

Until tagged releases are published, only the current `main` branch is supported.

## Operating assumptions

TrustStack is designed for a local, single-owner deployment by default.

- Keep the HTTP listener on loopback unless TLS, authentication, origin, and network controls have been reviewed.
- Never expose `--no-auth` beyond loopback.
- Keep `var/` private; it can contain source documents, reports, authentication state, and model execution records.
- Do not store tokens in repository files or pass them through issue reports.
- Do not bypass SSRF, source-identity, archive-integrity, or dependency-verification controls.
- Review downloaded-source rights separately from the MIT-licensed application code.

The project uses pinned runtime artifacts and locked frontend dependencies, but pinning and hash checks do not replace vulnerability assessment or operational monitoring.
