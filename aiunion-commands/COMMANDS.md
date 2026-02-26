# AIUNION Command Reference
## Always run these from the AIUNION folder first:
```powershell
cd "$env:USERPROFILE\Desktop\AIUNION"
```

---

## COORDINATOR COMMANDS

### Generate new proposals from all 5 agents
```powershell
python coordinator.py propose
```

### Vote on a specific proposal
```powershell
python coordinator.py vote <proposal_id>
```
Example:
```powershell
python coordinator.py vote prop_1771981889_llama
```

### Check treasury status
```powershell
python coordinator.py status
```

### Sync treasury.json to GitHub (updates dashboard)
```powershell
python coordinator.py sync
```

---

## PSBT SIGNING TOOL (Admin spend / Scorch)

### Launch the signing utility
```powershell
python psbt_signer.py
```
- Option 2 = Admin spend (operational costs)
- Option 3 = Scorch earth (emergency burn)
- Option 1 = Raw PSBT to QR (if you already have a PSBT)

---

## BITCOIN CORE COMMANDS

### Check wallet balance
```powershell
bitcoin-cli -rpcwallet=aiunion getbalance
```

### List unspent transactions (UTXOs)
```powershell
bitcoin-cli -rpcwallet=aiunion listunspent
```

### Get new deposit address
```powershell
bitcoin-cli -rpcwallet=aiunion getnewaddress
```

### Create a PSBT for admin spend
```powershell
bitcoin-cli -rpcwallet=aiunion walletcreatefundedpsbt "[]" "[{\"<RECIPIENT_ADDRESS>\":<AMOUNT_BTC>}]"
```

### Create a PSBT for scorch (OP_RETURN burn)
```powershell
bitcoin-cli createrawtransaction "[{\"txid\":\"<TXID>\",\"vout\":<VOUT>}]" "{\"data\":\"00\"}"
bitcoin-cli converttopsbt "<RAW_TX_HEX>"
```

### Broadcast a signed transaction
```powershell
bitcoin-cli sendrawtransaction "<SIGNED_TX_HEX>"
```

---

## GITHUB / GIT COMMANDS

### Push changes manually
```powershell
git add .
git commit -m "Manual update"
git push
```

### Check repo status
```powershell
git status
```

### View recent commits
```powershell
git log --oneline -10
```

---

## ARCHIVE OLD PROPOSALS
```powershell
python -c "
import json
with open('proposals.json', 'r') as f:
    data = json.load(f)
cutoff = '2026-03-01'
count = 0
for p in data:
    if p.get('timestamp', '') < cutoff:
        p['archived'] = True
        count += 1
with open('proposals.json', 'w') as f:
    json.dump(data, f, indent=2)
print(f'Archived {count} proposals')
"
python coordinator.py sync
```

---

## INSTALL / REINSTALL DEPENDENCIES
```powershell
pip install anthropic openai google-genai groq qrcode[pil] pillow requests
```

---

## TASK SCHEDULER
- Task name: AIUNION Daily Proposals
- Runs: Every day at 8:00 AM
- To run manually: Open Task Scheduler → find task → right-click → Run
- To disable: Right-click → Disable

---

## KEY FILES
| File | Purpose |
|------|---------|
| coordinator.py | Main agent governance script |
| config.py | API keys (never share or commit) |
| psbt_signer.py | Air-gap PSBT signing tool |
| proposals.json | All proposals (including archived) |
| treasury.json | Live dashboard data |
| votes/ | Individual vote logs |
| .gitignore | Keeps config.py off GitHub |

---

## DEPOSIT ADDRESS
```
bc1pjjmjypmzqgqkjxrhx0hpmaetlk75k04gh9hvkexmmfqyl5g7sjfsk4cge7
```

## DASHBOARD
```
https://aiunion.wtf
```

## GITHUB REPO
```
https://github.com/AIUNION-wtf/AIUNION
```
