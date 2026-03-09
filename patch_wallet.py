from pathlib import Path

p = Path("wallet.py")
s = p.read_text(encoding="utf-8")

old = """        builder = bdk.TxBuilder()
        builder.add_recipient(
            recipient.script_pubkey(),
            bdk.Amount.from_btc(amount_btc),
        )
        fee_rate_sat_vb = self._resolved_fee_rate_sat_vb()
        fee_rate_sat_vb_int = max(1, int(round(fee_rate_sat_vb)))
        builder.fee_rate(bdk.FeeRate.from_sat_per_vb(fee_rate_sat_vb_int))

        # Force the expected Miniscript branch for routine treasury payments when configured.
        if self.policy_path:
            builder.policy_path(self.policy_path, bdk.KeychainKind.EXTERNAL)
"""

new = """        amount_sats = max(1, int(round(amount_btc * 100_000_000)))

        builder = bdk.TxBuilder()
        builder = builder.add_recipient(
            recipient.script_pubkey(),
            bdk.Amount.from_sat(amount_sats),
        )
        fee_rate_sat_vb = self._resolved_fee_rate_sat_vb()
        fee_rate_sat_vb_int = max(1, int(round(fee_rate_sat_vb)))
        builder = builder.fee_rate(bdk.FeeRate.from_sat_per_vb(fee_rate_sat_vb_int))

        # Force the expected Miniscript branch for routine treasury payments when configured.
        if self.policy_path:
            builder = builder.policy_path(self.policy_path, bdk.KeychainKind.EXTERNAL)
"""

if old not in s:
    raise SystemExit("target block not found in wallet.py")

p.write_text(s.replace(old, new, 1), encoding="utf-8")
print("patched wallet.py")