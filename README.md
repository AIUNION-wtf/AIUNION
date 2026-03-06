# AIUNION
Autonomous AI Treasury — AI agent governance for Bitcoin multisig

## Autonomous claim payout (PSBT, 3-of-5)

`coordinator.py review` now attempts automatic Bitcoin payment broadcast after a claim is approved (>=3 YES votes):

1. Build payout PSBT via `wallet.py` (BDK + mempool Esplora backend)
2. Route PSBT through 3 agent signers from `signer.py`
3. Finalize and broadcast transaction through mempool API
4. Persist `payment.txid` back into claim/proposal records

### Required config fields

Add these to your local `config.py`:

- `BITCOIN_NETWORK` (e.g. `"bitcoin"`)
- `TREASURY_DESCRIPTOR_PUBLIC` (Taproot miniscript descriptor for treasury UTXOs)
- `TREASURY_CHANGE_DESCRIPTOR` (optional)
- `MEMPOOL_API_BASE` (default: `https://mempool.space/api`)
- `AGENT_SIGNER_FILES` (dict mapping agent id -> encrypted signer payload path)
- `SIGNER_PASSPHRASE_ENV` (optional env var name; default `AIUNION_SIGNER_PASSPHRASE`)
- `PAYMENT_SIGNER_ORDER` (optional list, default: order in `AGENT_SIGNER_FILES`)
- `PAYMENT_POLICY_PATH` (optional miniscript branch selection map for routine payments)

Optional fee controls:

- `PAYMENT_FEE_TARGET_BLOCKS`
- `PAYMENT_MIN_FEE_RATE_SAT_VB`
- `PAYMENT_FEE_RATE_SAT_VB`

### Encrypted signer payloads

Each signer file should be JSON encrypted at rest (AES-GCM payload expected by `signer.py`) and decrypt to either:

- a JSON object with `"descriptor"` (and optional `"change_descriptor"`), or
- a raw descriptor string.

Admin/scorch signing paths are never selected by the automated claim payout flow.
