import os
import re
import json
from pathlib import Path

import bdkpython as bdk

import config
from signer import encrypt_signer_material


def main():
    descriptor = getattr(config, "TREASURY_DESCRIPTOR_PUBLIC", "").strip()
    if not descriptor:
        raise SystemExit("Set TREASURY_DESCRIPTOR_PUBLIC in config.py first")

    base_desc = descriptor.split("#", 1)[0]
    if "/<0;1>/*" not in base_desc:
        raise SystemExit("Expected multipath descriptor containing '/<0;1>/*'")

    passphrase = os.getenv("AIUNION_SIGNER_PASSPHRASE", "").strip()
    if not passphrase:
        raise SystemExit("Set AIUNION_SIGNER_PASSPHRASE in this PowerShell session first")

    networks = {
        "bitcoin": bdk.Network.BITCOIN,
        "mainnet": bdk.Network.BITCOIN,
        "testnet": bdk.Network.TESTNET,
        "testnet4": bdk.Network.TESTNET4,
        "signet": bdk.Network.SIGNET,
        "regtest": bdk.Network.REGTEST,
    }
    network_name = getattr(config, "BITCOIN_NETWORK", "bitcoin").lower().strip()
    if network_name not in networks:
        raise SystemExit(f"Unsupported BITCOIN_NETWORK: {network_name}")
    network = networks[network_name]

    # Map agent IDs to your 5 multisig XFPs (no admin/scorch)
    agent_xfp = {
        "claude": "0f7d5110",
        "gpt": "44a8bdde",
        "gemini": "3b63b238",
        "grok": "ec1aabf1",
        "llama": "aa4d90f8",
    }

    token_re = re.compile(
        r"(\[(?P<xfp>[0-9a-fA-F]{8})/(?P<path>[^\]]+)\]xpub[1-9A-HJ-NP-Za-km-z]+/<0;1>/\*)"
    )
    tokens_by_xfp = {}
    for m in token_re.finditer(base_desc):
        tokens_by_xfp[m.group("xfp").lower()] = {
            "token_multi": m.group(1),
            "path": m.group("path"),
        }

    missing = [xfp for xfp in agent_xfp.values() if xfp.lower() not in tokens_by_xfp]
    if missing:
        raise SystemExit(f"Could not find these XFPs in descriptor: {missing}")

    # Build branch-specific public bases
    desc_ext_base = base_desc.replace("/<0;1>/*", "/0/*")
    desc_chg_base = base_desc.replace("/<0;1>/*", "/1/*")

    xpub_re = re.compile(r"xpub[1-9A-HJ-NP-Za-km-z]+")
    outdir = Path("secrets/signers")
    outdir.mkdir(parents=True, exist_ok=True)

    for agent, expected_xfp in agent_xfp.items():
        expected_xfp = expected_xfp.lower()
        info = tokens_by_xfp[expected_xfp]
        token_multi = info["token_multi"]
        token_ext = token_multi.replace("/<0;1>/*", "/0/*")
        token_chg = token_multi.replace("/<0;1>/*", "/1/*")
        default_path = f"m/{info['path']}"

        print(f"\n=== {agent.upper()} ({expected_xfp}) ===")
        mnemonic = input("24-word mnemonic (VISIBLE): ").strip()
        path = input(f"BIP32 path [{default_path}]: ").strip() or default_path

        m = bdk.Mnemonic.from_string(mnemonic)
        root = bdk.DescriptorSecretKey(network, m, None)
        derived = root.derive(bdk.DerivationPath(path))

        derived_pub = str(derived.as_public())   # .../*
        derived_prv = str(derived)               # .../*

        if f"[{expected_xfp}/" not in derived_prv.lower():
            raise SystemExit(f"{agent}: derived key fingerprint/path mismatch for XFP {expected_xfp}")

        dpub = xpub_re.search(derived_pub)
        ppub = xpub_re.search(token_multi)
        if not dpub or not ppub or dpub.group(0) != ppub.group(0):
            raise SystemExit(f"{agent}: mnemonic/path does not match descriptor xpub for XFP {expected_xfp}")

        if not derived_prv.endswith("/*"):
            raise SystemExit(f"{agent}: unexpected derived descriptor key format")

        priv_ext = derived_prv[:-2] + "/0/*"
        priv_chg = derived_prv[:-2] + "/1/*"

        signer_desc_ext = desc_ext_base.replace(token_ext, priv_ext, 1)
        signer_desc_chg = desc_chg_base.replace(token_chg, priv_chg, 1)

        # sanity parse (should now succeed)
        bdk.Descriptor(signer_desc_ext, network)
        bdk.Descriptor(signer_desc_chg, network)

        payload = encrypt_signer_material(
            json.dumps({
                "descriptor": signer_desc_ext,
                "change_descriptor": signer_desc_chg,
            }),
            passphrase,
        )

        out = outdir / f"{agent}.enc.json"
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote {out}")

    print("\nDone. Encrypted signer payloads created in secrets/signers/")


if __name__ == "__main__":
    main()