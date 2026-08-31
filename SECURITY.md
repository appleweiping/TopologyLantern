# Security Policy

## Supported versions

Security fixes target the latest released minor line. Before the first stable
release, only the current development version is supported.

## Private reports

Use GitHub's private vulnerability reporting feature for path handling, denial
of service, unintended execution, parser confusion, or output injection. Do not
attach confidential PDK data, proprietary designs, credentials, or active
payloads to a public issue.

Include the affected version, operating system, minimal inert specification or
report, observed boundary failure, and expected result. Acknowledgement, repair,
and disclosure timing depends on maintainer availability, impact, and reproducibility.

## Boundary

Specifications and result reports are untrusted data. Generation must remain
offline, bounded, deterministic, and free of simulator or process execution.
Bad circuit quality is an engineering limitation unless it also violates this
software boundary or misrepresents a structural check as verified performance.
