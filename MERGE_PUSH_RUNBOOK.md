# Merge and Push Runbook

This runbook is executed only after Codex `ACCEPT` and GPT `PASS` are both bound to the same exact candidate SHA/tree and audit-package SHA-256.

## 1. Verify local authority

```bash
export JOAO_REPO="$HOME/joao-orchestrator"
export CANDIDATE_SHA="<approved 40-char SHA>"

cd "$JOAO_REPO"
git cat-file -e "$CANDIDATE_SHA^{commit}"
git status --porcelain=v1
git show --no-patch --format='%H %T %P' "$CANDIDATE_SHA"
```

The worktree must be clean. Compare the exact values with Codex and GPT certificates.

## 2. Verify remote/authentication

```bash
gh auth status
git remote -v
```

If the canonical repository does not exist, create it **private first**. Do not make it public before `PUBLIC_RELEASE_GATE.md` is complete.

```bash
# Run only after confirming the intended GitHub owner/name.
gh repo create <owner>/joao-orchestrator --private --source "$JOAO_REPO" --remote origin
```

## 3. Fast-forward only

```bash
git fetch origin --prune
git switch main

git merge --ff-only "$CANDIDATE_SHA"
git diff --check
git status --porcelain=v1
git push origin main
```

If `--ff-only` fails, stop. Do not create an automatic conflict-resolution merge.

## 4. Verify remote exactness and CI

```bash
LOCAL_SHA=$(git rev-parse HEAD)
REMOTE_SHA=$(git ls-remote origin refs/heads/main | awk '{print $1}')
test "$LOCAL_SHA" = "$REMOTE_SHA"
gh run list --branch main --limit 5
```

Do not change repository visibility while CI or the public-release gate is red.

## 5. Profile update

Confirm the GitHub username, then create or update the profile repository separately. Copy `github-profile/README.md`, review the rendered page and push. Update repository description/topics and pin JOÃO only after the repository is sanitized and understandable from a clean clone.
