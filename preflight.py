import os
import config
from signer import AgentPsbtSigner
from wallet import TreasuryWallet

if not os.getenv("AIUNION_SIGNER_PASSPHRASE", "").strip():
    raise SystemExit("Missing AIUNION_SIGNER_PASSPHRASE in this shell")

s = AgentPsbtSigner.from_config(config)
print("Configured signers:", list(s.signer_files.keys()))
for sid in s.signer_files:
    s._wallet_for_agent(sid)
    print("Signer OK:", sid)

w = TreasuryWallet.from_config(config)
print("Syncing wallet...")
w.sync()
print("BAL:", w.wallet.balance().total.to_btc())
print("PRE-FLIGHT PASSED")