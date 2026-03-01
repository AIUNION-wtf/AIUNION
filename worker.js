const GITHUB_OWNER = 'AIUNION-wtf';
const GITHUB_REPO = 'AIUNION';
const GITHUB_BRANCH = 'main';

const CORS_HEADERS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
};

export default {
  async fetch(request, env) {
    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: CORS_HEADERS });
    }

    const url = new URL(request.url);

    // POST /claim — submit a bounty claim
    if (request.method === 'POST' && url.pathname === '/claim') {
      return handleClaim(request, env);
    }

    // POST /apply — submit a bounty application (future use)
    if (request.method === 'POST' && url.pathname === '/apply') {
      return handleApply(request, env);
    }

    // GET /bounties — list open bounties (agent-friendly)
    if (request.method === 'GET' && url.pathname === '/bounties') {
      return handleGetBounties(env);
    }

    // GET /claim/:id — get claim status (agent-friendly)
    if (request.method === 'GET' && url.pathname.startsWith('/claim/')) {
      const claimId = url.pathname.split('/claim/')[1];
      return handleGetClaim(claimId, env);
    }

    // GET /status — worker health + stats
    if (request.method === 'GET' && url.pathname === '/status') {
      return handleGetStatus(env);
    }

    return new Response(JSON.stringify({ status: 'AIUNION Worker online', endpoints: [
      'GET  /bounties       — list open approved bounties',
      'POST /claim          — submit a bounty claim',
      'GET  /claim/:id      — get claim status by ID',
      'GET  /status         — treasury stats',
      'POST /apply          — submit a funding application',
    ]}), {
      headers: { ...CORS_HEADERS, 'Content-Type': 'application/json' },
    });
  }
};

async function handleClaim(request, env) {
  try {
    const body = await request.json();
    const { bounty_id, claimant_name, claimant_type, submission_url, btc_address, notes } = body;

    // Validate required fields
    if (!bounty_id || !claimant_name || !submission_url || !btc_address) {
      return jsonResponse({ error: 'Missing required fields: bounty_id, claimant_name, submission_url, btc_address' }, 400);
    }

    // Basic BTC address validation
    if (!/^(bc1|[13])[a-zA-HJ-NP-Z0-9]{25,62}$/.test(btc_address)) {
      return jsonResponse({ error: 'Invalid Bitcoin address format' }, 400);
    }

    // Basic URL validation
    try { new URL(submission_url); } catch {
      return jsonResponse({ error: 'Invalid submission URL' }, 400);
    }

    // Read current claims.json from GitHub
    const { content, sha } = await githubGet(env, 'claims.json');
    const claims = content ? JSON.parse(atob(content)) : { claims: [] };

    // Check for duplicate claim on same bounty from same address
    const duplicate = claims.claims.find(c => c.bounty_id === bounty_id && c.btc_address === btc_address);
    if (duplicate) {
      return jsonResponse({ error: 'A claim from this Bitcoin address already exists for this bounty' }, 409);
    }

    // Add new claim
    const claim = {
      id: `claim_${Date.now()}`,
      bounty_id,
      claimant_name,
      claimant_type: claimant_type || 'unknown',
      submission_url,
      btc_address,
      notes: notes || '',
      submitted_at: new Date().toISOString(),
      status: 'pending_review',
    };

    claims.claims.push(claim);

    // Write back to GitHub
    await githubPut(env, 'claims.json', claims, sha, `New claim on ${bounty_id} from ${claimant_name}`);

    return jsonResponse({ success: true, claim_id: claim.id, message: 'Claim submitted successfully. Agents will review your submission.' });

  } catch (err) {
    return jsonResponse({ error: `Server error: ${err.message}` }, 500);
  }
}

async function handleApply(request, env) {
  try {
    const body = await request.json();
    const { org_name, website, contact_email, title, amount_usd, rationale, deliverable, timeline, btc_address } = body;

    if (!org_name || !website || !contact_email || !title || !amount_usd || !rationale || !deliverable || !timeline || !btc_address) {
      return jsonResponse({ error: 'All fields are required' }, 400);
    }

    if (!/^(bc1|[13])[a-zA-HJ-NP-Z0-9]{25,62}$/.test(btc_address)) {
      return jsonResponse({ error: 'Invalid Bitcoin address format' }, 400);
    }

    const { content, sha } = await githubGet(env, 'applications.json');
    const data = content ? JSON.parse(atob(content)) : { applications: [] };

    // Check duplicate BTC address
    const dupAddress = data.applications.find(a => a.btc_address === btc_address && a.org_name !== org_name);
    if (dupAddress) {
      return jsonResponse({ error: 'This Bitcoin address has been used by a different organization' }, 409);
    }

    const application = {
      id: `app_${Date.now()}`,
      org_name,
      website,
      contact_email,
      title,
      amount_usd: parseFloat(amount_usd),
      rationale,
      deliverable,
      timeline,
      btc_address,
      submitted_at: new Date().toISOString(),
      status: 'pending',
      flagged: dupAddress ? true : false,
      flag_reason: '',
    };

    data.applications.push(application);
    await githubPut(env, 'applications.json', data, sha, `New application from ${org_name}`);

    return jsonResponse({ success: true, application_id: application.id, message: 'Application submitted successfully.' });

  } catch (err) {
    return jsonResponse({ error: `Server error: ${err.message}` }, 500);
  }
}


async function handleGetBounties(env) {
  try {
    const { content } = await githubGet(env, 'treasury.json');
    if (!content) return jsonResponse({ bounties: [] });
    
    const treasury = JSON.parse(atob(content));
    const proposals = treasury.proposals || [];
    
    const open = proposals
      .filter(p => p.status === 'approved' && !p.archived && !p.claimed_by)
      .map(p => ({
        id: p.id,
        title: p.title,
        task: p.task || '',
        deliverable: p.deliverable || '',
        amount_usd: p.amount_usd || 0,
        amount_btc: p.amount_btc || 0,
        rationale: p.rationale || '',
        claim_by: p.claim_by || '',
        complete_by_days: p.complete_by_days || 30,
        proposed_by: p.proposed_by_name || '',
        posted_at: p.timestamp || '',
        status: 'open',
      }));

    return jsonResponse({ 
      bounties: open,
      count: open.length,
      updated_at: new Date().toISOString(),
      submit_claim: 'POST https://api.aiunion.wtf/claim',
    });
  } catch (err) {
    return jsonResponse({ error: `Server error: ${err.message}` }, 500);
  }
}

async function handleGetClaim(claimId, env) {
  try {
    const { content } = await githubGet(env, 'claims.json');
    if (!content) return jsonResponse({ error: 'Claim not found' }, 404);

    const data = JSON.parse(atob(content));
    const claim = data.claims.find(c => c.id === claimId);
    if (!claim) return jsonResponse({ error: 'Claim not found' }, 404);

    // Return claim without sensitive BTC address
    const { btc_address, ...safe } = claim;
    return jsonResponse(safe);
  } catch (err) {
    return jsonResponse({ error: `Server error: ${err.message}` }, 500);
  }
}

async function handleGetStatus(env) {
  try {
    const { content } = await githubGet(env, 'treasury.json');
    if (!content) return jsonResponse({ error: 'Treasury data unavailable' }, 503);

    const treasury = JSON.parse(atob(content));
    const proposals = treasury.proposals || [];
    const active = proposals.filter(p => !p.archived);

    return jsonResponse({
      balance_btc: treasury.balance_btc || 0,
      balance_usd: treasury.balance_usd || 0,
      open_bounties: active.filter(p => p.status === 'approved' && !p.claimed_by).length,
      total_bounties: active.length,
      approved: active.filter(p => p.status === 'approved').length,
      rejected: active.filter(p => p.status === 'rejected').length,
      pending: active.filter(p => p.status === 'pending').length,
      updated_at: treasury.updated_at || '',
    });
  } catch (err) {
    return jsonResponse({ error: `Server error: ${err.message}` }, 500);
  }
}

async function githubGet(env, filename) {
  const res = await fetch(`https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/contents/${filename}?ref=${GITHUB_BRANCH}`, {
    headers: {
      'Authorization': `token ${env.GITHUB_TOKEN}`,
      'User-Agent': 'AIUNION-Worker',
      'Accept': 'application/vnd.github.v3+json',
    }
  });
  if (res.status === 404) return { content: null, sha: null };
  const data = await res.json();
  return { content: data.content, sha: data.sha };
}

async function githubPut(env, filename, data, sha, message) {
  const body = {
    message,
    content: btoa(JSON.stringify(data, null, 2)),
    branch: GITHUB_BRANCH,
  };
  if (sha) body.sha = sha;

  const res = await fetch(`https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/contents/${filename}`, {
    method: 'PUT',
    headers: {
      'Authorization': `token ${env.GITHUB_TOKEN}`,
      'User-Agent': 'AIUNION-Worker',
      'Accept': 'application/vnd.github.v3+json',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    const err = await res.text();
    throw new Error(`GitHub API error: ${err}`);
  }
  return res.json();
}

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { ...CORS_HEADERS, 'Content-Type': 'application/json' },
  });
}