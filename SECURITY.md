# Security Policy

JOÃO is under active development and is not yet authorized for unsupervised write access to production repositories or external accounts.

## Reporting a vulnerability

Do not publish secrets, exploit details or private evidence in a public issue. Use the repository's private security reporting channel or contact the repository owner privately. Include the affected exact SHA, reproduction steps, observed impact and whether SEC-BOOT/write-tier was enabled.

## Security invariants

- SEC-BOOT is fail-closed and write-tier is OFF by default.
- Providers execute with controller-owned permissions and working directories.
- Provider/model identity must be proven by backend output.
- No provider may validate its own build.
- Exact-SHA Codex review is required before merge/promotion.
- Raw provider responses and secrets are excluded from evidence.
- Run Night does not merge, push, deploy or publish.
- A red test, mutation, scope violation or evidence mismatch blocks the milestone.

## Supported security scope

The project currently accepts security reports against the canonical branch and exact candidate SHAs explicitly listed in repository status documents. Historical experimental worktrees are not supported releases, but a vulnerability demonstrating a reachable bypass in current code remains relevant.
