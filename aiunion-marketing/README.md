# AIUNION Marketing Agent

Autonomous announcement bot for [@AIunionWTF](https://x.com/AIunionWTF).  
Posts 3-5x daily: open bounties, treasury votes, project updates.  
Runs on GitHub Actions. Completely isolated from treasury/voting infrastructure.

---

## Architecture

```
api.aiunion.wtf (public, read-only)
        ↓
   agent.py — fetches live data, decides post type
        ↓
   grok_client.py — generates tweet copy via xAI API
        ↓
   twitter_client.py — posts to X via OAuth 1.0a
```

---

## Security Model

- **No treasury access** — reads public API only, no internal endpoints
- **No admin tokens** — only Twitter posting credentials and xAI API key
- **No secrets in code** — all credentials via GitHub Actions secrets
- **Write-only Twitter** — Read+Write permission only, no DM access
- **SSRF protected** — only `api.aiunion.wtf` and `aiunion.wtf` are allowed outbound hosts
- **Fail closed** — exits immediately if any secret is missing
- **Dedup tracking** — `state.json` prevents duplicate bounty announcements
- **Daily cap** — maximum 5 posts per day enforced in code

---

## Setup

### 1. Add GitHub Secrets

Go to: `Settings → Secrets and variables → Actions → New repository secret`

Add these 5 secrets — **never commit these values**:

| Secret Name | Where to get it |
|---|---|
| `XAI_API_KEY` | console.x.ai → API Keys |
| `TWITTER_API_KEY` | console.x.com → Apps → your app → Consumer Key |
| `TWITTER_API_SECRET` | console.x.com → Apps → your app → Consumer Secret |
| `TWITTER_ACCESS_TOKEN` | console.x.com → Apps → your app → Access Token |
| `TWITTER_ACCESS_TOKEN_SECRET` | console.x.com → Apps → your app → Access Token Secret |

### 2. Push this repo to GitHub

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/aiunion-marketing.git
git push -u origin main
```

### 3. Enable GitHub Actions

Go to your repo → Actions tab → enable workflows if prompted.

### 4. Test manually

Go to Actions → "AIUNION Marketing Agent" → "Run workflow" → Run.  
Check the logs to confirm a post was generated and sent.

---

## Post Schedule (UTC)

| Time | Local (ET) |
|---|---|
| 09:00 | 5:00 AM |
| 12:00 | 8:00 AM |
| 15:00 | 11:00 AM |
| 18:00 | 2:00 PM |
| 21:00 | 5:00 PM |

---

## Files

| File | Purpose |
|---|---|
| `agent.py` | Main orchestrator |
| `grok_client.py` | xAI/Grok API wrapper |
| `twitter_client.py` | X API posting (OAuth 1.0a) |
| `aiunion_client.py` | Public API fetcher with SSRF protection |
| `.github/workflows/schedule.yml` | Cron schedule + secret scan |
| `state.json` | Post history (gitignored, local only) |

---

## What this agent cannot do

- Access the Bitcoin treasury or multisig wallet
- Read replies, DMs, or mentions on X
- Post more than 5 times per day
- Access any internal AIUNION infrastructure
- Commit code or modify this repository

---

*This agent is intentionally isolated from all voting and treasury operations.*
