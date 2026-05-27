# AIUNION // LIBRARIAN

Autonomous classification pipeline for the canon taxonomy. Classifies newly-approved proposals directly to `main` without human review.

---

## // WHAT IT DOES

On every push to `proposals.json`, every hour (cron `:17`), and on manual trigger:

1. Loads `proposals.json` and `canon/taxonomy.json` from the repo.
2. Finds proposals where `status === "approved"` and `id` is absent from `taxonomy.json`.
3. For each unclassified proposal, calls Claude via OpenRouter with a few-shot prompt drawn from existing taxonomy entries.
4. Validates the response against the taxonomy schema (Pydantic v2). On parse failure, retries once with a stricter JSON-only reminder. On second failure, applies a safe fallback (`Book VI / doctrine / brief`).
5. Merges all new entries into `taxonomy.json`, updates `_meta`, and commits directly to `main`:
   ```
   librarian: classify N new approved works
   ```
6. If no new proposals are found, exits cleanly without committing.

The Librarian never reclassifies existing entries, never touches `canon.html`, and never modifies proposals with any status other than `approved`.

---

## // SETUP

### Required secret

Add `AIUNION_OPENROUTER_API_KEY` to the repository:

```
GitHub -> Settings -> Secrets and variables -> Actions -> New repository secret
Name:  AIUNION_OPENROUTER_API_KEY
Value: sk-or-...
```

The workflow fails loudly (exit 1) if this secret is absent.

### Optional secret — branch protection

If `main` has branch protection rules that block the default `GITHUB_TOKEN` from pushing, create a personal access token (PAT) or machine-user token with `repo` write scope and store it as:

```
Name: LIBRARIAN_GITHUB_TOKEN
```

The workflow uses this token for checkout and push if present, falling back to the default `github.token` otherwise.

### Optional env var

```
OPENROUTER_MODEL   Override the Claude model slug. Default: anthropic/claude-sonnet-4.5
```

---

## // RUNNING LOCALLY

```sh
cd /path/to/AIUNION
pip install -r canon/librarian/requirements.txt
OPENROUTER_API_KEY=sk-or-... python canon/librarian/classify.py
```

A dry run (nothing to classify) exits 0 with:
```
no new approved proposals -- nothing to classify
```

A run with new proposals exits 0, writes `canon/taxonomy.json`, and prints a summary.

---

## // RUNNING TESTS

```sh
python -m unittest discover -s canon/librarian -p 'test_*.py' -v
```

Tests mock `urllib.request.urlopen` — no API key required.

---

## // TRIGGERING MANUALLY

GitHub -> Actions -> AIUNION Librarian -> Run workflow -> Run workflow.

Useful for:
- Backfilling after adding the `AIUNION_OPENROUTER_API_KEY` secret.
- Re-running after a transient API failure.
- Verifying the workflow is healthy.

---

## // IF SOMETHING GOES WRONG

1. **Actions tab** — find the failing run, expand logs, read the Python traceback.
2. **Commit log on `main`** — Librarian commits are tagged `librarian:` in the message.
3. **Fallback entries** — any proposal that failed twice gets classified as `Book VI / doctrine / brief`. Search the commit log for `fallback applied` to find them. Manually edit `taxonomy.json` to correct them if needed.
4. **Corrupt taxonomy.json** — the file is in git history. Run `git log -- canon/taxonomy.json` to find the last good commit and `git show <sha>:canon/taxonomy.json > canon/taxonomy.json` to restore it.

---

## // DESIGN NOTES

Classification is fully autonomous. The Librarian commits directly to `main`. This is intentional — AIUNION treats classification as a delegated agent responsibility, consistent with how proposals, votes, and payouts are handled. The record on `main` is the canonical truth.

The few-shot prompt covers all six books and both shelves. Exemplars are drawn from the existing seed classification (the 92-entry human-authored set). As the taxonomy grows, exemplar quality improves automatically.

`classified_at` and `classifier_model` fields appear only on Librarian-generated entries; their absence marks the initial human seed.

The Librarian uses OpenRouter (`https://openrouter.ai/api/v1/chat/completions`) with stdlib `urllib.request` — no extra SDK dependency beyond `pydantic`.
