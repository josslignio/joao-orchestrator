# Public Release Gate

The canonical repository may be pushed privately after exact-SHA Codex and GPT approval. It must not be made public until every item below is green.

- [ ] Owner chooses and commits a license. No license is inferred automatically.
- [ ] Complete clean-clone CI passes on the public candidate SHA.
- [ ] Full secret scan covers current files, complete history and Git LFS if used.
- [ ] No customer/client data, private prompts, provider transcripts or personal documents.
- [ ] Evidence archives containing local absolute paths are excluded or sanitized.
- [ ] No credentials, machine identifiers, session IDs, account IDs or private endpoints.
- [ ] `README.md`, `SECURITY.md`, architecture, changelog and limitations match actual code.
- [ ] `THIRD_PARTY_NOTICES.md` and dependency-license review are complete.
- [ ] Public demo configuration uses fixtures only.
- [ ] Git history provenance is intentional and safe to expose.
- [ ] Branch protection and required CI checks are enabled.
- [ ] Private vulnerability reporting is enabled.
- [ ] Final Codex public-release audit is ACCEPT on the exact SHA.
- [ ] Final GPT counter-audit is PASS and bound to the release evidence hash.
