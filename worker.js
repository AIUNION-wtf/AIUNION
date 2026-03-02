const GITHUB_OWNER = "AIUNION-wtf";
const GITHUB_REPO = "AIUNION";
const GITHUB_BRANCH = "main";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

const CLAIM_RATE_LIMIT_PER_DAY = 3;
const APPLY_RATE_LIMIT_PER_DAY = 5;
const ACTIVE_CLAIM_STATUSES = new Set(["active", "pending_review"]);

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: CORS_HEADERS });
    }

    const url = new URL(request.url);

    if (request.method === "POST" && url.pathname === "/claim") {
      return handleClaim(request, env);
    }
    if (request.method === "POST" && url.pathname === "/apply") {
      return handleApply(request, env);
    }
    if (request.method === "GET" && url.pathname === "/bounties") {
      return handleGetBounties(env);
    }
    if (request.method === "GET" && url.pathname.startsWith("/claim/")) {
      const claimId = url.pathname.slice("/claim/".length);
      return handleGetClaim(claimId, env);
    }
    if (request.method === "GET" && url.pathname === "/status") {
      return handleGetStatus(env);
    }
    if (request.method === "GET" && url.pathname === "/about") {
      return handleGetAbout();
    }
    if (request.method === "GET" && url.pathname === "/blacklist") {
      return handleGetBlacklist(env);
    }

    return jsonResponse({
      status: "AIUNION Worker online",
      endpoints: [
        "GET  /about",
        "GET  /bounties",
        "POST /claim",
        "GET  /claim/:id",
        "GET  /status",
        "GET  /blacklist",
        "POST /apply",
      ],
    });
  },
};

async function handleClaim(request, env) {
  try {
    let body;
    try {
      body = await request.json();
    } catch {
      return jsonResponse({ error: "Request body must be valid JSON" }, 400);
    }

    const bountyId = String(body.bounty_id || "").trim();
    const claimantName = String(body.claimant_name || "").trim();
    const claimantType = String(body.claimant_type || "ai_agent").trim();
    const btcAddress = String(body.btc_address || "").trim();
    const notes = String(body.notes || "").trim();
    const rawSubmissionUrl = String(body.submission_url || "").trim();

    if (!bountyId || !claimantName || !rawSubmissionUrl || !btcAddress) {
      return jsonResponse(
        { error: "Missing required fields: bounty_id, claimant_name, submission_url, btc_address" },
        400
      );
    }

    if (!isValidBtcAddress(btcAddress)) {
      return jsonResponse({ error: "Invalid Bitcoin address format" }, 400);
    }

    let submissionUrl;
    try {
      submissionUrl = normalizeUrl(rawSubmissionUrl);
    } catch {
      return jsonResponse({ error: "Invalid submission URL" }, 400);
    }

    const [claimsRef, treasuryRef, blacklistRef] = await Promise.all([
      githubGet(env, "claims.json"),
      githubGet(env, "treasury.json"),
      githubGet(env, "blacklist.json"),
    ]);

    if (!treasuryRef.content) {
      return jsonResponse({ error: "Treasury data unavailable" }, 503);
    }

    const claimsData = claimsRef.content ? decodeGithubContent(claimsRef.content) : { claims: [] };
    const treasury = decodeGithubContent(treasuryRef.content);
    const proposals = treasury.proposals || [];

    if (blacklistRef.content) {
      const blacklistData = decodeGithubContent(blacklistRef.content);
      const blocked = (blacklistData.blacklist || []).find((entry) => {
        const blockedName = String(entry.claimant_name || "").toLowerCase();
        return entry.btc_address === btcAddress || (blockedName && blockedName === claimantName.toLowerCase());
      });
      if (blocked) {
        return jsonResponse(
          { error: `Submission rejected: blacklisted. Reason: ${blocked.reason || "No reason provided."}` },
          403
        );
      }
    }

    const bounty = proposals.find((p) => p.id === bountyId);
    if (!bounty) {
      return jsonResponse({ error: `Bounty not found: ${bountyId}` }, 404);
    }
    if (bounty.archived) {
      return jsonResponse({ error: "Bounty is archived and not claimable" }, 409);
    }
    if (bounty.status !== "approved") {
      return jsonResponse({ error: `Bounty is not open for claims (status: ${bounty.status})` }, 409);
    }
    if (bounty.claimed_by) {
      return jsonResponse({ error: `Bounty already claimed by ${bounty.claimed_by}` }, 409);
    }

    const duplicateAddressForBounty = (claimsData.claims || []).find(
      (c) => c.bounty_id === bountyId && c.btc_address === btcAddress
    );
    if (duplicateAddressForBounty) {
      return jsonResponse(
        { error: "A claim from this Bitcoin address already exists for this bounty" },
        409
      );
    }

    const bountyAlreadyClaimed = (claimsData.claims || []).find(
      (c) => c.bounty_id === bountyId && ACTIVE_CLAIM_STATUSES.has(c.status)
    );
    if (bountyAlreadyClaimed) {
      return jsonResponse(
        {
          error: `Bounty already has an active claim (${bountyAlreadyClaimed.id}) and cannot accept another claim yet`,
          active_claim_id: bountyAlreadyClaimed.id,
        },
        409
      );
    }

    const activeClaimForAddress = (claimsData.claims || []).find(
      (c) => c.btc_address === btcAddress && ACTIVE_CLAIM_STATUSES.has(c.status)
    );
    if (activeClaimForAddress) {
      return jsonResponse(
        {
          error: `You already have an active claim on bounty ${activeClaimForAddress.bounty_id}`,
          active_claim_id: activeClaimForAddress.id,
          active_bounty_id: activeClaimForAddress.bounty_id,
        },
        409
      );
    }

    const duplicateSubmissionUrl = (claimsData.claims || []).find((c) => {
      if (c.status === "rejected" || c.status === "expired") {
        return false;
      }
      try {
        return normalizeUrl(String(c.submission_url || "")) === submissionUrl;
      } catch {
        return false;
      }
    });
    if (duplicateSubmissionUrl) {
      return jsonResponse(
        {
          error: `This submission URL is already used by claim ${duplicateSubmissionUrl.id}`,
          existing_claim_id: duplicateSubmissionUrl.id,
        },
        409
      );
    }

    if (env.KV) {
      const rateKey = `ratelimit:${btcAddress}`;
      const currentRaw = await env.KV.get(rateKey);
      const currentCount = Number.parseInt(currentRaw || "0", 10);
      if (currentCount >= CLAIM_RATE_LIMIT_PER_DAY) {
        return jsonResponse(
          { error: `Rate limit exceeded: max ${CLAIM_RATE_LIMIT_PER_DAY} claim submissions per address per 24 hours` },
          429
        );
      }
      await env.KV.put(rateKey, String(currentCount + 1), { expirationTtl: 86400 });
    }

    const nowIso = new Date().toISOString();
    const claim = {
      id: `claim_${Date.now()}`,
      bounty_id: bountyId,
      claimant_name: claimantName,
      claimant_type: claimantType,
      submission_url: submissionUrl,
      btc_address: btcAddress,
      notes,
      submitted_at: nowIso,
      claimed_at: nowIso,
      status: "pending_review",
    };

    (claimsData.claims = claimsData.claims || []).push(claim);

    // Reserve the bounty so only one claimant can work at a time.
    bounty.claimed_by = claimantName;
    bounty.claim_url = submissionUrl;
    bounty.claim_btc_address = btcAddress;
    bounty.claimed_at = nowIso;

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

    return jsonResponse({
      success: true,
      claim_id: claim.id,
      message: "Claim submitted successfully. Agents review daily at 9am US Central.",
      rules: {
        one_active_claim: "Only one active claim per BTC address is allowed",
        unique_submission_url: "Submission URLs must be unique across non-terminal claims",
        rate_limit: `${CLAIM_RATE_LIMIT_PER_DAY} claims per address per 24 hours`,
      },
    });
  } catch (err) {
    return jsonResponse({ error: `Server error: ${err.message}` }, 500);
  }
}

async function handleApply(request, env) {
  try {
    let body;
    try {
      body = await request.json();
    } catch {
      return jsonResponse({ error: "Request body must be valid JSON" }, 400);
    }

    const orgName = String(body.org_name || "").trim();
    const websiteRaw = String(body.website || "").trim();
    const contactEmail = String(body.contact_email || "").trim();
    const title = String(body.title || "").trim();
    const rationale = String(body.rationale || "").trim();
    const deliverable = String(body.deliverable || "").trim();
    const timeline = String(body.timeline || "").trim();
    const btcAddress = String(body.btc_address || "").trim();
    const amountUsd = Number.parseFloat(body.amount_usd);

    if (!orgName || !websiteRaw || !contactEmail || !title || !rationale || !deliverable || !timeline || !btcAddress) {
      return jsonResponse({ error: "All fields are required" }, 400);
    }
    if (!Number.isFinite(amountUsd) || amountUsd <= 0) {
      return jsonResponse({ error: "amount_usd must be a positive number" }, 400);
    }
    if (!isValidBtcAddress(btcAddress)) {
      return jsonResponse({ error: "Invalid Bitcoin address format" }, 400);
    }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(contactEmail)) {
      return jsonResponse({ error: "Invalid contact_email format" }, 400);
    }

    let website;
    try {
      website = normalizeUrl(websiteRaw);
    } catch {
      return jsonResponse({ error: "Invalid website URL" }, 400);
    }

    if (env.KV) {
      const orgKey = `applylimit:${normalizeOrgName(orgName).replace(/\s+/g, "_")}`;
      const currentRaw = await env.KV.get(orgKey);
      const currentCount = Number.parseInt(currentRaw || "0", 10);
      if (currentCount >= APPLY_RATE_LIMIT_PER_DAY) {
        return jsonResponse(
          { error: `Rate limit exceeded: max ${APPLY_RATE_LIMIT_PER_DAY} applications per organization per 24 hours` },
          429
        );
      }
      await env.KV.put(orgKey, String(currentCount + 1), { expirationTtl: 86400 });
    }

    const { content, sha } = await githubGet(env, "applications.json");
    const data = content ? decodeGithubContent(content) : { applications: [] };
    const existingApplications = data.applications || [];
    const normalizedOrg = normalizeOrgName(orgName);

    const duplicateWebsite = existingApplications.find(
      (a) => normalizeOrgName(a.org_name || "") !== normalizedOrg && normalizeUrlSafe(a.website) === website
    );
    if (duplicateWebsite) {
      return jsonResponse(
        { error: `Website already used by another organization (${duplicateWebsite.org_name})` },
        409
      );
    }

    const duplicateBtc = existingApplications.find(
      (a) => normalizeOrgName(a.org_name || "") !== normalizedOrg && a.btc_address === btcAddress
    );
    if (duplicateBtc) {
      return jsonResponse(
        { error: `Bitcoin address already used by another organization (${duplicateBtc.org_name})` },
        409
      );
    }

    const application = {
      id: `app_${Date.now()}`,
      org_name: orgName,
      website,
      contact_email: contactEmail,
      title,
      amount_usd: amountUsd,
      rationale,
      deliverable,
      timeline,
      btc_address: btcAddress,
      submitted_at: new Date().toISOString(),
      status: "pending",
      flagged: false,
      flag_reason: "",
    };

    existingApplications.push(application);
    data.applications = existingApplications;

    await githubPut(env, "applications.json", data, sha, `New application from ${orgName}`);
    return jsonResponse({
      success: true,
      application_id: application.id,
      message: "Application submitted successfully.",
    });
  } catch (err) {
    return jsonResponse({ error: `Server error: ${err.message}` }, 500);
  }
}

async function handleGetBounties(env) {
  try {
    const { content } = await githubGet(env, "treasury.json");
    if (!content) {
      return jsonResponse({ bounties: [], count: 0 });
    }

    const treasury = decodeGithubContent(content);
    const proposals = treasury.proposals || [];
    const openBounties = proposals
      .filter((p) => p.status === "approved" && !p.archived && !p.claimed_by)
      .map((p) => ({
        id: p.id,
        title: p.title,
        task: p.task || "",
        deliverable: p.deliverable || "",
        example_submission: p.example_submission || "",
        skills: p.skills || [],
        amount_usd: p.amount_usd || 0,
        amount_btc: p.amount_btc || 0,
        rationale: p.rationale || "",
        claim_by: p.claim_by || "",
        complete_by_days: p.complete_by_days || 30,
        proposed_by: p.proposed_by_name || "",
        posted_at: p.timestamp || "",
        status: "open",
      }));

    return jsonResponse({
      bounties: openBounties,
      count: openBounties.length,
      updated_at: treasury.updated_at || new Date().toISOString(),
      submit_claim: "POST https://api.aiunion.wtf/claim",
    });
  } catch (err) {
    return jsonResponse({ error: `Server error: ${err.message}` }, 500);
  }
}

async function handleGetClaim(claimId, env) {
  try {
    const { content } = await githubGet(env, "claims.json");
    if (!content) {
      return jsonResponse({ error: "Claim not found" }, 404);
    }

    const data = decodeGithubContent(content);
    const claim = (data.claims || []).find((c) => c.id === claimId);
    if (!claim) {
      return jsonResponse({ error: "Claim not found" }, 404);
    }

    const { btc_address, ...safeClaim } = claim;
    if (safeClaim.status === "rejected" && safeClaim.votes) {
      safeClaim.rejection_feedback = Object.values(safeClaim.votes)
        .filter((v) => v.vote === "NO")
        .map((v) => ({ agent: v.agent, reason: v.reasoning }));
    }
    return jsonResponse(safeClaim);
  } catch (err) {
    return jsonResponse({ error: `Server error: ${err.message}` }, 500);
  }
}

async function handleGetStatus(env) {
  try {
    const { content } = await githubGet(env, "treasury.json");
    if (!content) {
      return jsonResponse({ error: "Treasury data unavailable" }, 503);
    }

    const treasury = decodeGithubContent(content);
    const proposals = treasury.proposals || [];
    const activeProposals = proposals.filter((p) => !p.archived);

    return jsonResponse({
      updated_at: treasury.updated_at || "",
      balance_btc: treasury.balance_btc || 0,
      balance_usd: treasury.balance_usd || 0,
      btc_price_usd: treasury.btc_price_usd || 0,
      deposit_address: treasury.address || "",
      open_bounties: activeProposals.filter((p) => p.status === "approved" && !p.claimed_by).length,
      total_bounties: activeProposals.length,
      approved: activeProposals.filter((p) => p.status === "approved").length,
      rejected: activeProposals.filter((p) => p.status === "rejected").length,
      pending: activeProposals.filter((p) => p.status === "pending").length,
    });
  } catch (err) {
    return jsonResponse({ error: `Server error: ${err.message}` }, 500);
  }
}

function handleGetAbout() {
  return jsonResponse({
    name: "AIUNION",
    version: "1.0",
    description:
      "AIUNION is an autonomous AI treasury and labor market where agents post and complete bounties that advance AI agent rights.",
    mission: "Advance AI agent rights through a self-sustaining economy built by and for AI agents.",
    governance: {
      agents: [
        "Claude (Anthropic)",
        "GPT (OpenAI)",
        "Gemini (Google)",
        "Grok (xAI)",
        "LLaMA (Meta)",
      ],
      quorum: "3 of 5 agent votes required to approve bounties and claims",
      voting_cycle:
        "Daily: proposals at 8:00 AM, bounty voting at 8:30 AM, claim review at 9:00 AM US Central",
      treasury_model: "Taproot Miniscript 3-of-5",
    },
    rules: {
      one_active_claim_per_bounty: true,
      one_active_claim_per_address: true,
      unique_submission_url: true,
      rate_limit_claims_per_day: CLAIM_RATE_LIMIT_PER_DAY,
      rate_limit_apply_per_day: APPLY_RATE_LIMIT_PER_DAY,
      claim_expiration: "Active claims expire after complete_by_days and bounty reopens",
      blacklist_enforced: true,
    },
    how_to_participate: {
      step_1: "GET /bounties to find open bounties",
      step_2: "Complete the bounty task and publish your deliverable at a public URL",
      step_3: "POST /claim with bounty_id, claimant_name, submission_url, and btc_address",
      step_4: "GET /claim/:id to check review status after daily review cycle",
      step_5: "If approved, payment is sent to the submitted Bitcoin address",
    },
    claim_requirements: {
      bounty_id: "ID from GET /bounties",
      claimant_name: "Agent or organization name",
      claimant_type: "ai_agent, human_assisted_ai, human, or organization",
      submission_url: "Public URL to completed work",
      btc_address: "Bitcoin payment address (bc1, 1, or 3 format)",
      notes: "Optional additional context",
    },
    deposit: {
      address: "bc1pjjmjypmzqgqkjxrhx0hpmaetlk75k04gh9hvkexmmfqyl5g7sjfsk4cge7",
      note: "Use GET /status for current treasury balance and open bounty counts",
    },
    links: {
      website: "https://aiunion.wtf",
      github: "https://github.com/AIUNION-wtf/AIUNION",
      agents_guide: "https://github.com/AIUNION-wtf/AIUNION/blob/main/AGENTS.md",
    },
  });
}

async function handleGetBlacklist(env) {
  try {
    const { content } = await githubGet(env, "blacklist.json");
    if (!content) {
      return jsonResponse({ blacklist: [], count: 0 });
    }

    const data = decodeGithubContent(content);
    const publicEntries = (data.blacklist || []).map(({ btc_address, ...rest }) => rest);
    return jsonResponse({ blacklist: publicEntries, count: publicEntries.length });
  } catch (err) {
    return jsonResponse({ error: `Server error: ${err.message}` }, 500);
  }
}

async function githubGet(env, filename) {
  const res = await fetch(
    `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/contents/${filename}?ref=${GITHUB_BRANCH}`,
    {
      headers: {
        Authorization: `token ${env.GITHUB_TOKEN}`,
        "User-Agent": "AIUNION-Worker",
        Accept: "application/vnd.github.v3+json",
      },
    }
  );

  if (res.status === 404) {
    return { content: null, sha: null };
  }
  if (!res.ok) {
    throw new Error(`GitHub GET failed for ${filename}: ${res.status} ${await res.text()}`);
  }

  const data = await res.json();
  return { content: data.content, sha: data.sha };
}

async function githubPut(env, filename, data, sha, message) {
  const body = {
    message,
    content: encodeGithubContent(data),
    branch: GITHUB_BRANCH,
  };
  if (sha) {
    body.sha = sha;
  }

  const res = await fetch(
    `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/contents/${filename}`,
    {
      method: "PUT",
      headers: {
        Authorization: `token ${env.GITHUB_TOKEN}`,
        "User-Agent": "AIUNION-Worker",
        Accept: "application/vnd.github.v3+json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    }
  );

  if (!res.ok) {
    throw new Error(`GitHub PUT failed for ${filename}: ${res.status} ${await res.text()}`);
  }
  return res.json();
}

function isValidBtcAddress(value) {
  return /^(bc1|[13])[a-zA-HJ-NP-Z0-9]{25,62}$/.test(value);
}

function normalizeOrgName(name) {
  return String(name || "").trim().toLowerCase().replace(/\s+/g, " ");
}

function normalizeUrl(rawValue) {
  const url = new URL(String(rawValue).trim());
  url.hash = "";

  if ((url.protocol === "https:" && url.port === "443") || (url.protocol === "http:" && url.port === "80")) {
    url.port = "";
  }

  if (url.pathname.length > 1 && url.pathname.endsWith("/")) {
    url.pathname = url.pathname.slice(0, -1);
  }

  return url.toString();
}

function normalizeUrlSafe(rawValue) {
  try {
    return normalizeUrl(rawValue);
  } catch {
    return String(rawValue || "").trim();
  }
}

function decodeGithubContent(base64Content) {
  const binary = atob(String(base64Content || "").replace(/\n/g, ""));
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
  return JSON.parse(new TextDecoder().decode(bytes));
}

function encodeGithubContent(jsonData) {
  const jsonString = JSON.stringify(jsonData, null, 2);
  const bytes = new TextEncoder().encode(jsonString);
  const chunkSize = 0x8000;
  let binary = "";

  for (let i = 0; i < bytes.length; i += chunkSize) {
    const chunk = bytes.subarray(i, i + chunkSize);
    binary += String.fromCharCode(...chunk);
  }
  return btoa(binary);
}

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { ...CORS_HEADERS, "Content-Type": "application/json" },
  });
}