# AIUNION — Paused

**Paused as of: 2026-11-12**

## Why

We already have a backlog of unfulfilled open bounties. Rather than keep
generating and voting on new proposals, we're pausing proposal/vote cycles to:

- Stop creating more open bounties on top of an existing backlog.
- Stop spending OpenRouter credits on new propose/vote cycles.

## What's still running

- **Existing open bounties remain claimable.** Submit a claim as normal.
- **Claim review and Bitcoin payout still run.** The event-driven
  `aiunion-claim-review.yml` workflow (triggered on push to `claims.json`/
  `proposals.json`) is untouched, and `coordinator.py review` is not gated by
  the pause flag — approved claims still get paid.
- **Claim expiry and checkout expiry still run** where triggered manually or
  by the still-active event-driven workflow.

## What's paused

- `coordinator.py propose` and `coordinator.py voteall`/`vote` exit
  immediately with an `AIUNION is paused` message (see `paused.json`).
- The daily cron in `.github/workflows/aiunion-cycle.yml` (propose → vote →
  review → expire) — disabled via GitHub Actions.
- The hourly Canon classification cron in `.github/workflows/librarian.yml`
  — disabled via GitHub Actions.
- Local Windows Task Scheduler tasks `AIUNION-Propose`, `AIUNION-Vote`,
  `AIUNION-Expire`, `AIUNION-Review` — disabled (not deleted).
- `aiunion-marketing`'s `schedule.yml` (announce/reply posts) and
  `moltbook.yml` were already auto-disabled by GitHub for inactivity; no
  action was needed there. `event_post.yml` (manual-dispatch only) is
  untouched and still fires for `claim_paid` announcements from `review`.

## How to resume

### 1. Flip the backstop off

Edit `paused.json` and set `"paused": false` (or delete the file).

### 2. Re-enable the two GitHub Actions workflows

Via `gh` (if installed by then):

```powershell
gh workflow enable aiunion-cycle.yml --repo AIUNION-wtf/AIUNION
gh workflow enable librarian.yml --repo AIUNION-wtf/AIUNION
```

Or via the REST API (same approach used to disable them — reads
`GITHUB_TOKEN` out of local `config.py`):

```powershell
$configContent = Get-Content "C:\Users\dusti\Desktop\AIUNION\config.py" -Raw
$token = [regex]::Match($configContent, 'GITHUB_TOKEN\s*=\s*"([^"]+)"').Groups[1].Value
$headers = @{ Authorization = "token $token"; Accept = "application/vnd.github+json"; "User-Agent" = "AIUNION-resume-script" }
foreach ($wf in @("aiunion-cycle.yml","librarian.yml")) {
  Invoke-RestMethod -Uri "https://api.github.com/repos/AIUNION-wtf/AIUNION/actions/workflows/$wf/enable" -Method Put -Headers $headers
}
$token = $null; $configContent = $null
```

Or manually: GitHub → Actions tab → select the workflow → "..." menu → **Enable workflow**.

Also check `aiunion-marketing`'s `schedule.yml` and `moltbook.yml` — they were
auto-disabled by GitHub for inactivity (not by this pause), so they need the
same enable step there if you still want them running:

```powershell
foreach ($wf in @("schedule.yml","moltbook.yml")) {
  Invoke-RestMethod -Uri "https://api.github.com/repos/AIUNION-wtf/aiunion-marketing/actions/workflows/$wf/enable" -Method Put -Headers $headers
}
```

(Note: the token used above returned 403 Forbidden against `aiunion-marketing`
when we tried to disable it — you may need a token with access to that repo,
or do this one via the GitHub UI instead.)

### 3. Re-enable the local Task Scheduler tasks

```powershell
Enable-ScheduledTask -TaskName "AIUNION-Propose"
Enable-ScheduledTask -TaskName "AIUNION-Vote"
Enable-ScheduledTask -TaskName "AIUNION-Expire"
Enable-ScheduledTask -TaskName "AIUNION-Review"
Get-ScheduledTask -TaskName "AIUNION-*" | Select-Object TaskName, State
```

### 4. Remove the public pause notices

- `README.md` — remove the `> ⚠️ **Paused as of ...**` blockquote at the top.
- `aiunion-pages/index.html` — remove the `<!-- Paused notice -->` banner
  `<div>` right after `<body>`.
- `.well-known/agents.json` — remove the `"notice"` field.
- `.well-known/mcp.json` — remove the `"notice"` field.
- `worker.js` — remove the `"## NOTICE (paused)"` block inside
  `handleGetLlmsTxt()`. **This one needs a Worker redeploy to take effect**
  (it was committed but never deployed while paused, so `/llms.txt` has been
  serving the un-paused text the whole time — double check whether you ever
  deployed it; if not, no redeploy-to-remove-notice is even needed, just
  revert the source).

### 5. Clean up tracking

- Delete `paused.json` and this file (`PAUSED.md`), or update both if you
  want to keep them as a historical record instead.
- Close and unpin issue
  [AIUNION-wtf/AIUNION#8](https://github.com/AIUNION-wtf/AIUNION/issues/8)
  ("AIUNION paused - bounty backlog"). Unpin via GraphQL if needed:

  ```powershell
  $pinQuery = @{ query = "query { repository(owner:\"AIUNION-wtf\", name:\"AIUNION\") { issue(number: 8) { id } } }" } | ConvertTo-Json
  $idResult = Invoke-RestMethod -Uri "https://api.github.com/graphql" -Method Post -Headers $headers -Body $pinQuery -ContentType "application/json; charset=utf-8"
  $unpinMutation = @{ query = "mutation(`$id: ID!) { unpinIssue(input: {issueId: `$id}) { issue { number } } }"; variables = @{ id = $idResult.data.repository.issue.id } } | ConvertTo-Json
  Invoke-RestMethod -Uri "https://api.github.com/graphql" -Method Post -Headers $headers -Body $unpinMutation -ContentType "application/json; charset=utf-8"
  ```

### 6. Commit and push

```powershell
Set-Location "C:\Users\dusti\Desktop\AIUNION"
git add -A
git pull origin main --rebase
git commit -m "Resume AIUNION"
git push origin main
```
