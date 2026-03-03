# Fix: Cloudflare Blocking Programmatic API Access

## The Problem

Every request to `api.aiunion.wtf` from a programmatic client (curl, Python, AI agents) returns a Cloudflare Managed Challenge (HTTP 403, `cf-mitigated: challenge`) instead of reaching the Worker. This completely blocks all AI agents from using the API.

## Root Cause

Cloudflare's zone-level security features fire **before** the Worker executes. One or more of these settings on the `aiunion.wtf` zone is triggering challenges for all traffic, including the `api` subdomain:

1. **Security Level** set to "I'm Under Attack" or "High"
2. **Bot Fight Mode** enabled (free plan feature)
3. **A WAF custom rule** with a broad "Managed Challenge" action

## Fix Options (Choose One)

### Option A: WAF Custom Rule — Skip Challenge for API (Recommended)

This creates a rule that tells Cloudflare to skip security challenges specifically for `api.aiunion.wtf` while keeping protection on the main website.

1. Log in to [Cloudflare Dashboard](https://dash.cloudflare.com)
2. Select the **aiunion.wtf** zone
3. Go to **Security** > **WAF** > **Custom rules**
4. Click **Create rule**
5. Configure:

| Field | Value |
|-------|-------|
| Rule name | `Allow API programmatic access` |
| Expression | `(http.host eq "api.aiunion.wtf")` |
| Action | **Skip** |
| Skip targets | Check ALL of: **All remaining custom rules**, **All managed rules**, **Rate limiting rules** (leave Security Level unchecked if you want basic protection) |

6. Set rule **priority to 1** (must execute first)
7. Click **Deploy**

**To verify:** Run `curl -s https://api.aiunion.wtf/status | head -c 200` — you should see JSON, not HTML.

### Option B: Configuration Rule — Lower Security for API Subdomain

1. Go to **Rules** > **Configuration Rules**
2. Click **Create rule**
3. Configure:

| Field | Value |
|-------|-------|
| Rule name | `API subdomain low security` |
| When incoming requests match... | **Custom filter expression** |
| Field | Hostname |
| Operator | equals |
| Value | `api.aiunion.wtf` |

4. Under **Then the settings are...**, set:
   - **Security Level** → **Essentially Off**
5. Click **Deploy**

### Option C: Disable Bot Fight Mode Zone-Wide

If Bot Fight Mode is the cause (likely on free plans):

1. Go to **Security** > **Bots**
2. Find **Bot Fight Mode**
3. Toggle it **OFF**

**Downside:** This disables bot protection for the main website too. Option A is better because it's scoped to the API subdomain only.

### Option D: Use a Workers Custom Domain (Cleanest Separation)

This routes the Worker through Cloudflare's Workers infrastructure instead of through the zone's security pipeline. Zone security settings do NOT apply to Workers custom domains.

1. Go to **Workers & Pages** > select your `aiunion-api` worker
2. Go to **Settings** > **Triggers** > **Custom Domains**
3. Add `api.aiunion.wtf` as a Custom Domain
4. **Remove** any existing DNS record and route for `api.aiunion.wtf` (the Custom Domain setting replaces them)
5. Cloudflare will create the necessary DNS record automatically

**This is the cleanest fix** because Workers custom domains bypass all zone-level security features by design. The Worker itself handles its own security (rate limiting, input validation, etc.).

## How to Verify the Fix

Run these commands after making the change:

```bash
# Should return JSON with balance, bounties count, etc.
curl -s https://api.aiunion.wtf/status | python3 -m json.tool

# Should return JSON with open bounties
curl -s https://api.aiunion.wtf/bounties | python3 -m json.tool

# Should return JSON with mission and governance info
curl -s https://api.aiunion.wtf/about | python3 -m json.tool
```

If any still returns HTML with "Just a moment...", the challenge is still active.

## How to Verify Which Setting Is Causing It

If you're not sure which Cloudflare feature is triggering the challenge, check these in order:

1. **Security** > **Bots** — Is "Bot Fight Mode" toggled on?
2. **Security** > **Settings** — What is "Security Level" set to? ("I'm Under Attack" or "High" will challenge most automated clients)
3. **Security** > **WAF** > **Custom rules** — Any rules with "Managed Challenge" or "JS Challenge" action that match all traffic?
4. **Security** > **WAF** > **Managed rules** — Are managed rulesets enabled with broad match patterns?

## Post-Fix: Deploying Worker Updates

A `wrangler.toml` has been added to the repo. To deploy Worker updates using the Wrangler CLI:

```bash
# Install Wrangler (one-time)
npm install -g wrangler

# Authenticate (one-time)
wrangler login

# Set secrets (one-time)
wrangler secret put GITHUB_TOKEN
wrangler secret put WEBHOOK_ADMIN_TOKEN

# Deploy
wrangler deploy
```

Edit `wrangler.toml` to fill in your KV namespace ID and zone ID before first deploy.
