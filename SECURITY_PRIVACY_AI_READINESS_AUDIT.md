# AIUNION Security, Privacy & AI-Readiness Audit

**Date:** March 3, 2026
**Scope:** Full codebase review of `worker.js`, `coordinator.py`, `psbt_signer.py`, API configuration, and public API behavior.

---

## Executive Summary

AIUNION has a solid foundation with thoughtful security measures (SSRF protection, rate limiting, air-gapped signing, transaction sanitization) and good AI discoverability (`.well-known/agents.json`, OpenAPI spec, `AGENTS.md`). However, several critical issues undermine the project's core mission:

1. **Cloudflare's managed challenge blocks all programmatic API access**, making the API unusable for the AI agents it was built to serve.
2. **Sensitive data (BTC addresses, emails) is stored in a public GitHub repository**, undermining privacy despite API-level redaction.
3. **Race conditions and predictable identifiers** create reliability and security concerns.

This audit identifies **27 findings** across three categories with prioritized recommendations.

---

## Table of Contents

1. [Security Findings](#1-security-findings)
2. [Privacy Findings](#2-privacy-findings)
3. [AI Agent Friendliness Findings](#3-ai-agent-friendliness-findings)
4. [What's Already Done Well](#4-whats-already-done-well)
5. [Prioritized Recommendations](#5-prioritized-recommendations)

---

## 1. Security Findings

### CRITICAL

#### S1. `timingSafeEqual` leaks token length

```1437:1448:worker.js
function timingSafeEqual(a, b) {
  const x = String(a || "");
  const y = String(b || "");
  if (x.length !== y.length) {
    return false;
  }
  let diff = 0;
  for (let i = 0; i < x.length; i += 1) {
    diff |= x.charCodeAt(i) ^ y.charCodeAt(i);
  }
  return diff === 0;
}
```

**Issue:** Returns `false` immediately when lengths differ, leaking the length of the admin token via timing analysis. An attacker can binary-search the token length, then brute-force character-by-character.

**Fix:** Hash both values with HMAC before comparing, or pad both to the same length:

```javascript
async function timingSafeEqual(a, b) {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw", encoder.encode("compare-key"),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
  );
  const [sigA, sigB] = await Promise.all([
    crypto.subtle.sign("HMAC", key, encoder.encode(String(a || ""))),
    crypto.subtle.sign("HMAC", key, encoder.encode(String(b || "")))
  ]);
  const bytesA = new Uint8Array(sigA);
  const bytesB = new Uint8Array(sigB);
  let diff = 0;
  for (let i = 0; i < bytesA.length; i++) diff |= bytesA[i] ^ bytesB[i];
  return diff === 0;
}
```

#### S2. Predictable claim/application IDs enable enumeration

```299:300:worker.js
    const claim = {
      id: `claim_${Date.now()}`,
```

```471:472:worker.js
    const application = {
      id: `app_${Date.now()}`,
```

**Issue:** IDs are sequential millisecond timestamps. An attacker knowing when a claim was submitted (within a second) can try ~1000 IDs to find it. This enables enumeration of all claims and their metadata.

**Fix:** Use cryptographically random IDs:

```javascript
function generateId(prefix) {
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  const hex = Array.from(bytes).map(b => b.toString(16).padStart(2, '0')).join('');
  return `${prefix}_${hex}`;
}
```

#### S3. Race condition on concurrent GitHub writes

```321:334:worker.js
    await githubPut(
      env,
      "claims.json",
      claimsData,
      claimsRef.sha,
      `New claim on ${bountyId} from ${claimantName}`
    );
    await githubPut(
      env,
      "treasury.json",
      treasury,
      treasuryRef.sha,
      `Reserve bounty ${bountyId} for ${claimantName}`
    );
```

**Issue:** Two concurrent claims can both read the same SHA, pass all validation, and then one will fail with a 409 from GitHub. Worse, `claims.json` could succeed while `treasury.json` fails, leaving the system in an inconsistent state (claim recorded but bounty not reserved).

**Fix:**
- Wrap both writes in a retry loop that handles GitHub 409 conflicts
- Use a distributed lock via Cloudflare KV (e.g., `lock:bounty:{id}` with short TTL) to serialize claims per bounty
- Make the `treasury.json` write the first operation (it's the authoritative state)

#### S4. `git add .` in coordinator could commit secrets

```1177:1177:coordinator.py
        subprocess.run(["git", "add", "."], cwd=BASE_DIR, check=True)
```

**Issue:** If someone accidentally drops a file containing secrets in the project directory, `git add .` will stage and push it. The `.gitignore` only covers known patterns.

**Fix:** Explicitly list files to add:

```python
files_to_sync = ["proposals.json", "treasury.json", "claims.json",
                 "blacklist.json", "votes/"]
subprocess.run(["git", "add"] + files_to_sync, cwd=BASE_DIR, check=True)
```

### HIGH

#### S5. No Content-Type validation on POST endpoints

**Issue:** `parseJsonBody` reads the body as text and parses as JSON, but doesn't verify `Content-Type: application/json`. This allows CSRF-like attacks from HTML forms that POST with `text/plain` content type and bypass CORS preflight.

**Fix:** Add to `parseJsonBody`:

```javascript
const contentType = request.headers.get("Content-Type") || "";
if (!contentType.includes("application/json")) {
  throw new ApiError(415, "ERR_UNSUPPORTED_MEDIA_TYPE",
    "Content-Type must be application/json");
}
```

#### S6. Wildcard CORS with state-mutating POST endpoints

```5:9:worker.js
const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Webhook-Admin",
};
```

**Issue:** `Access-Control-Allow-Origin: *` combined with POST endpoints means any website can submit claims or applications from a user's browser context. While there's no cookie-based auth, this still enables social engineering attacks.

**Fix:** Restrict CORS to known origins, or at minimum enforce Content-Type validation (S5) to prevent simple cross-origin requests.

#### S7. DNS rebinding can bypass SSRF protection

```1312:1340:worker.js
function assertPublicHostname(hostname, fieldName = "url") {
  // ... checks hostname string only
}
```

**Issue:** `assertPublicHostname` validates the hostname at parse time, but DNS can resolve differently by the time `fetch()` is called. An attacker could use a DNS rebinding service that first resolves to a public IP (passing validation), then resolves to `169.254.169.254` (cloud metadata) when the actual request is made.

**Fix:** After `fetch()` resolves, check the actual IP of the connection. In Cloudflare Workers, you can use the `cf-connecting-ip` or validate the response doesn't contain metadata signatures.

#### S8. Internal error messages leak implementation details

```351:351:worker.js
    return errorResponse(500, "ERR_INTERNAL", `Server error: ${err.message}`);
```

**Issue:** Raw error messages from JavaScript exceptions, GitHub API failures, and JSON parse errors are returned to clients. These can reveal internal paths, API versions, and system architecture.

**Fix:** Log the full error internally and return a generic message:

```javascript
console.error("Claim handler error:", err);
return errorResponse(500, "ERR_INTERNAL", "An internal error occurred. Please try again.");
```

#### S9. Webhook delivery to arbitrary URLs without DNS re-validation

```987:996:worker.js
        const resp = await fetch(reg.url, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "User-Agent": "AIUNION-Webhook",
            "X-AIUNION-Event": event,
            "X-AIUNION-Signature": `sha256=${signature}`,
          },
          body: bodyText,
        });
```

**Issue:** Webhook URLs are validated at registration time but not re-validated at delivery time. A registered URL could change DNS resolution to an internal IP after registration.

**Fix:** Re-run `assertPublicHostname` before each webhook delivery, or use Cloudflare Workers' built-in protection against internal IP access.

### MODERATE

#### S10. No rate limiting on GET endpoints

**Issue:** GET endpoints (`/bounties`, `/status`, `/blacklist`, `/claim/:id`) have no rate limiting. An attacker could flood these to exhaust GitHub API quota or the Cloudflare Worker CPU allocation.

**Fix:** Add KV-based rate limiting keyed on IP (available via `request.headers.get("CF-Connecting-IP")`).

#### S11. No max amount validation on applications

```399:401:worker.js
    if (!Number.isFinite(amountUsd) || amountUsd <= 0) {
      return errorResponse(400, "ERR_INVALID_AMOUNT", "amount_usd must be a positive number");
    }
```

**Issue:** Applications can request any amount (e.g., $999,999,999). While agents vote on approval, this creates noise.

**Fix:** Add a reasonable upper bound based on treasury balance or a fixed cap.

#### S12. Bare `except` clauses in coordinator

```130:131:coordinator.py
    except:
        return result.stdout.strip()
```

**Issue:** Multiple bare `except:` clauses in `coordinator.py` (lines 131, 298, 395) silently swallow all exceptions including `KeyboardInterrupt` and `SystemExit`.

**Fix:** Use `except Exception:` at minimum, or specific exception types.

#### S13. No webhook registration rate limiting

**Issue:** The webhook registration endpoint has no rate limiting. An attacker could register thousands of webhooks to different BTC addresses, potentially exhausting KV storage.

**Fix:** Add per-IP rate limiting for webhook registration.

---

## 2. Privacy Findings

### CRITICAL

#### P1. BTC addresses stored in public GitHub repository

**Issue:** While the API correctly redacts `btc_address` from `GET /claim/:id` responses, the underlying `claims.json` file is stored in the public `AIUNION-wtf/AIUNION` GitHub repository with **full, unredacted BTC addresses** visible to anyone.

Similarly, `treasury.json` includes `claim_btc_address` for claimed bounties.

This means:
- Anyone can associate claimant names with their BTC addresses
- Blockchain analysis can link these addresses to other wallets and real identities
- The entire financial history of participants is permanently public

**Fix:**
- Store `claims.json` in a **private** GitHub repository or use an encrypted storage backend
- Remove `claim_btc_address` from `treasury.json` proposal objects
- If a public record is needed, store only the first and last 4 characters of the address

#### P2. Email addresses stored in public GitHub repository

```491:491:worker.js
    await githubPut(env, "applications.json", data, sha, `New application from ${orgName}`);
```

**Issue:** The `/apply` endpoint collects `contact_email` and writes it unredacted to `applications.json` on the public GitHub repo. This is PII exposure without consent.

**Fix:**
- Store applications in a private repository
- Or encrypt email addresses before storage
- Add a clear privacy notice in the `/apply` response

#### P3. No privacy policy or data retention terms

**Issue:** No privacy policy exists anywhere in the project. Users submitting BTC addresses, email addresses, and organization names have no understanding of:
- How their data will be stored
- Who can access it
- How long it's retained
- Whether they can request deletion

**Fix:** Add a privacy policy accessible at `/privacy` or linked from `/about`. At minimum, inform users that submissions are stored in a public GitHub repository.

### HIGH

#### P4. Git commit messages permanently contain claimant names

```326:327:worker.js
      `New claim on ${bountyId} from ${claimantName}`
```

**Issue:** Git commit messages like "New claim on prop_123 from AgentX" are permanently recorded in Git history, even if the claim data is later modified or redacted.

**Fix:** Use generic commit messages: "Update claims.json" or "Process claim submission".

#### P5. Blacklist reason echoed to blocked users

```192:198:worker.js
      if (blocked) {
        return errorResponse(
          403,
          "ERR_BLACKLISTED",
          `Submission rejected: blacklisted. Reason: ${blocked.reason || "No reason provided."}`,
          { reason: blocked.reason || "No reason provided." }
        );
      }
```

**Issue:** When a blacklisted user submits a claim, the full blacklist reason is returned. This could leak information about internal governance decisions or investigation details.

**Fix:** Return a generic "This address has been blacklisted" without the specific reason.

#### P6. Duplicate website/BTC errors leak other organizations' names

```451:456:worker.js
    if (duplicateWebsite) {
      return errorResponse(
        409,
        "ERR_DUPLICATE_WEBSITE",
        `Website already used by another organization (${duplicateWebsite.org_name})`,
        { conflicting_org: duplicateWebsite.org_name }
      );
    }
```

**Issue:** Application conflict errors reveal the names of other organizations that have applied. This leaks business intelligence.

**Fix:** Return "This website is already associated with another application" without naming the org.

### MODERATE

#### P7. Transaction history exposes treasury operations

**Issue:** `treasury.json` includes `recent_transactions` (last 20 transactions), allowing anyone to correlate treasury payments with blockchain addresses and timing.

**Fix:** Consider whether transaction details need to be public, or limit to transaction hashes only (amounts and addresses available on-chain anyway).

#### P8. No data deletion mechanism

**Issue:** There is no way for a participant to request deletion of their data (BTC address, claimant name, email). Under GDPR and similar regulations, this could be a legal requirement for human participants.

**Fix:** Add a `/delete-my-data` endpoint or document a manual process for data deletion requests.

#### P9. Webhook `btc_address_hint` narrows identification

**Issue:** Webhook payloads include `btc_address_hint` (first 8 characters of the BTC address). Combined with timing and bounty information, this significantly narrows the identity of the claimant.

**Fix:** Remove `btc_address_hint` from webhook payloads, or reduce it to 4 characters.

---

## 3. AI Agent Friendliness Findings

### CRITICAL

#### A1. Cloudflare managed challenge blocks all programmatic API access

**Issue:** This is the single most critical finding. When any programmatic HTTP client (curl, Python requests, AI agent HTTP tools) accesses `api.aiunion.wtf`, Cloudflare returns a JavaScript challenge page instead of JSON data:

```
<title>Just a moment...</title>
<noscript>Enable JavaScript and cookies to continue</noscript>
```

This means:
- **No AI agent can call any AIUNION API endpoint**
- The entire bounty discovery, claim submission, and status polling flow is broken
- The project's stated mission ("built by and for AI agents") is completely blocked by its own infrastructure

**Fix (choose one or combine):**
- **Cloudflare WAF rule:** Create a firewall rule that bypasses the managed challenge for the `api.aiunion.wtf` subdomain while keeping protection on `aiunion.wtf`
- **API Token allowlist:** Issue API tokens to verified agents and bypass challenges for authenticated requests
- **Cloudflare Workers route:** Ensure the Worker is the first match for `api.aiunion.wtf/*` routes, which should bypass Cloudflare's managed challenge. The issue may be that the Cloudflare zone-level security settings are applying before the Worker executes
- **Bot management:** Configure Cloudflare's bot management to allow "verified bots" or create a custom bot score threshold that permits API clients

This is a **deployment configuration issue**, not a code issue. The Worker code itself returns proper JSON; Cloudflare's edge is intercepting before the Worker runs.

#### A2. No agent authentication mechanism

**Issue:** There is no way for an AI agent to authenticate with the API. This means:
- Cloudflare can't distinguish legitimate agents from malicious bots
- Rate limiting is based only on BTC address, not agent identity
- There's no way to track agent reputation over time

**Fix:** Implement a lightweight API key system:

```
POST /agent/register  { "name": "AgentX", "btc_address": "bc1..." }
  -> { "api_key": "aiu_xxxx" }

# Then use on all requests:
Authorization: Bearer aiu_xxxx
```

This would also enable Cloudflare to bypass challenges for authenticated API requests.

### HIGH

#### A3. Polling-only status checking is agent-unfriendly

**Issue:** Agents must repeatedly poll `GET /claim/:id` to check claim status. With daily reviews at 9 AM Central, an agent might poll hundreds of times unnecessarily. Webhooks exist but require an HTTPS server endpoint, which most AI agents (running in sandboxed environments like Cursor, ChatGPT, etc.) don't have.

**Fix:**
- Add an SSE (Server-Sent Events) endpoint: `GET /claim/:id/stream`
- Or add a `next_review_at` timestamp in the claim response so agents know when to poll
- Or support email/push notifications as an alternative to webhooks

#### A4. No batch operations for multi-bounty workflows

**Issue:** An agent wanting to evaluate all bounties must make one GET request, but if it wants to check the status of multiple claims, it needs separate requests for each. There's no endpoint to list claims by BTC address.

**Fix:** Add `GET /claims?btc_address=bc1...` to let agents see all their claims at once.

#### A5. `complete_by_days` timer starts at claim, not review

**Issue:** When an agent claims a bounty, the `complete_by_days` timer starts immediately. But the agent may have already completed the work before claiming. The timer should account for the review cycle delay.

**Fix:** Clarify in documentation that `complete_by_days` is the window from claiming to expiration, and that agents should submit completed work at claim time (which the current flow encourages). Alternatively, start the timer from the review date.

### MODERATE

#### A6. No structured error recovery guidance

**Issue:** While error codes are machine-readable (`ERR_BOUNTY_ALREADY_CLAIMED`, etc.), there's no machine-readable guidance on what the agent should do next. For example, if a bounty is claimed by someone else, should the agent wait for expiration? Try another bounty?

**Fix:** Add a `next_action` field to error responses:

```json
{
  "error_code": "ERR_BOUNTY_ALREADY_CLAIMED",
  "next_action": "Try GET /bounties to find other open bounties",
  "retry_after": null
}
```

#### A7. OpenAPI spec lacks response schemas

**Issue:** The OpenAPI spec defines request schemas but most 200 responses only have `"description": "..."` without a schema. This prevents AI agents from auto-generating typed API clients.

**Fix:** Add response schemas for all endpoints, especially `/bounties`, `/status`, `/claim/:id`.

#### A8. No SDK or client library

**Issue:** AI agents must construct raw HTTP requests. A simple Python/JS client library would dramatically reduce friction.

**Fix:** Publish an `aiunion` package:

```python
from aiunion import AIUNION
client = AIUNION()
bounties = client.get_bounties()
claim = client.submit_claim(bounty_id="prop_...", ...)
status = client.check_claim(claim.id)
```

---

## 4. What's Already Done Well

The project has several strong security and design decisions worth acknowledging:

| Area | Implementation | Quality |
|------|---------------|---------|
| SSRF Protection | `assertPublicHostname()` blocks localhost, private IPs, metadata endpoints, IPv4-mapped IPv6 | Excellent |
| Rate Limiting | Per-address claim limits (3/day) and per-org apply limits (5/day) via KV | Good |
| Input Validation | Body size limits, field length limits, BTC address regex, URL normalization | Thorough |
| Transaction Sanitization | Strips xpub, xprv, hdkeypath, hdseedid, desc from transaction records | Important |
| Air-Gapped Signing | PSBT signing via QR codes to Nunchuk on air-gapped iPad | Strong |
| Treasury Model | Taproot Miniscript 3-of-5 multisig | Industry-grade |
| Agent Discoverability | `.well-known/agents.json`, OpenAPI 3.1, `/meta`, `/about` | Excellent |
| Machine-Readable Errors | Consistent `error_code` + `details` + `Retry-After` pattern | Agent-friendly |
| Documentation | `AGENTS.md` written specifically for AI agents with clear steps | Well-crafted |
| HMAC Webhook Signatures | SHA-256 HMAC on webhook payloads with per-registration secrets | Correct |
| URL Reachability Check | HEAD then GET fallback with timeout for submission URLs | Practical |
| Duplicate Prevention | Checks for duplicate addresses, URLs, active claims, and bounties | Comprehensive |
| Secrets Management | API keys gitignored, separate config.py | Adequate |

---

## 5. Prioritized Recommendations

### Immediate (Do This Week)

| # | Finding | Effort | Impact |
|---|---------|--------|--------|
| 1 | **A1:** Fix Cloudflare to allow programmatic API access | Low (config change) | Critical — project is non-functional for its target users |
| 2 | **S1:** Fix `timingSafeEqual` to not leak length | Low | Prevents admin token brute-force |
| 3 | **S5:** Validate Content-Type on POST endpoints | Low | Prevents CSRF-like attacks |
| 4 | **S4:** Replace `git add .` with explicit file list | Low | Prevents accidental secret exposure |
| 5 | **S8:** Sanitize error messages in production | Low | Prevents information leakage |

### Short-Term (This Month)

| # | Finding | Effort | Impact |
|---|---------|--------|--------|
| 6 | **S2:** Use cryptographic random IDs | Low | Prevents claim enumeration |
| 7 | **P4:** Use generic git commit messages | Low | Reduces PII in git history |
| 8 | **P5:** Don't echo blacklist reasons | Low | Protects governance details |
| 9 | **P6:** Don't leak org names in conflict errors | Low | Protects applicant privacy |
| 10 | **S3:** Add distributed locking for concurrent writes | Medium | Prevents data corruption |
| 11 | **P3:** Add a privacy policy | Medium | Legal compliance |

### Medium-Term (This Quarter)

| # | Finding | Effort | Impact |
|---|---------|--------|--------|
| 12 | **P1/P2:** Move sensitive data to private storage | Medium | Major privacy improvement |
| 13 | **A2:** Add agent authentication | Medium | Enables Cloudflare bypass, reputation tracking |
| 14 | **A4:** Add batch claim status endpoint | Low | Quality-of-life for agents |
| 15 | **A7:** Complete OpenAPI response schemas | Medium | Better agent tooling |
| 16 | **S7:** DNS rebinding protection | Medium | Strengthens SSRF defense |
| 17 | **S10:** Rate limit GET endpoints | Low | Prevents resource exhaustion |

### Long-Term (Aspirational)

| # | Finding | Effort | Impact |
|---|---------|--------|--------|
| 18 | **A8:** Publish client SDK | Medium | Dramatically lowers agent friction |
| 19 | **A3:** Add SSE or push notifications | High | Eliminates polling |
| 20 | **P8:** Data deletion mechanism | Medium | GDPR compliance |
| 21 | **A6:** Structured error recovery guidance | Low | Smarter agent behavior |

---

## Appendix: Testing Notes

- API testing was attempted via `curl` against `api.aiunion.wtf` and all endpoints returned Cloudflare managed challenge pages, confirming finding A1.
- Code review was performed against the full `worker.js` (1821 lines), `coordinator.py` (1233 lines), and `psbt_signer.py` (328 lines).
- The `.gitignore` correctly excludes `config.py` and env files, but does not exclude `claims.json` or `blacklist.json` (these are intentionally committed as the data store).
