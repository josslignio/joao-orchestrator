# Changelog

## Unreleased — V5 candidate

### Added

- exact-SHA global audit package builder with complete tracked source and Git history bundle;
- strict GitHub Actions collection, security, compatibility and full macOS test gates;
- repository-bounded Run Night execution roots;
- comprehensive NightArtifact secret scanning;
- watchdog signal/process-group cleanup;
- pytest-compatible SEC-BOOT canaries;
- removal of tracked Python bytecode and pytest cache artifacts;
- V5 candidate master specification, roadmap, backlog and architecture documentation;
- recruiter-facing repository and GitHub profile materials.

### Security

- blocks read-only providers from being pointed at arbitrary directories outside the authorized repository;
- rejects additional GitHub, OpenAI, Anthropic and other provider-token formats before artifact persistence;
- prevents child processes from surviving watchdog termination signals.

### Changed

- global audit evidence can no longer claim PASS when full tests are skipped or fail;
- signed V4 governance files remain unchanged; V5 is a separate candidate.
