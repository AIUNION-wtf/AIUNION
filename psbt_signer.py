#!/usr/bin/env python3
"""
AIUNION PSBT Signing Utility
=============================
Generates a QR code from a PSBT string so it can be scanned
by Nunchuk on the air-gapped iPad for signing.

Usage:
    python psbt_signer.py

Requirements:
    pip install qrcode[pil] pillow

Workflow:
    1. Build transaction in Bitcoin Core and export PSBT
    2. Run this script and paste the PSBT string
    3. QR code displays on screen
    4. Hold screen up to air-gapped iPad running Nunchuk
    5. Nunchuk scans, signs, and exports signed PSBT QR
    6. Scan signed QR back with this tool to get the hex
    7. Broadcast via Bitcoin Core
"""

import sys
import os

# ── Check dependencies ────────────────────────────────────────────────────────
try:
    import qrcode
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("\n❌ Missing dependencies. Run:")
    print("   pip install qrcode[pil] pillow")
    sys.exit(1)

# ── Colors ────────────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
AMBER  = "\033[93m"
RED    = "\033[91m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def print_header():
    print(f"\n{GREEN}{BOLD}")
    print("  ╔═══════════════════════════════════════╗")
    print("  ║     AIUNION PSBT SIGNING UTILITY      ║")
    print("  ║     Air-Gap Transfer Tool              ║")
    print("  ╚═══════════════════════════════════════╝")
    print(f"{RESET}")

def print_menu():
    print(f"{GREEN}  Select operation:{RESET}")
    print(f"  {AMBER}[1]{RESET} Generate QR from PSBT  — send to iPad for signing")
    print(f"  {AMBER}[2]{RESET} Admin spend             — operational costs")
    print(f"  {AMBER}[3]{RESET} Scorch earth            — emergency burn all funds")
    print(f"  {AMBER}[4]{RESET} Decode PSBT             — inspect transaction details")
    print(f"  {AMBER}[Q]{RESET} Quit")
    print()

def generate_qr(data, title, filename, warning=None):
    """Generate a QR code image and save it, then open it."""

    # Check size — QR codes have limits
    if len(data) > 2953:
        print(f"\n{AMBER}⚠ PSBT is large ({len(data)} chars). May need animated QR.")
        print(f"  Nunchuk supports animated QR — check the iPad after scanning.{RESET}\n")

    # Create QR
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=6,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)

    # Make image
    qr_img = qr.make_image(fill_color="black", back_color="white")
    qr_w, qr_h = qr_img.size

    # Add title bar
    padding = 40
    title_h = 60
    warn_h  = 50 if warning else 0
    total_h = qr_h + title_h + warn_h + padding

    canvas = Image.new("RGB", (qr_w + padding, total_h), color="#040608")
    draw   = ImageDraw.Draw(canvas)

    # Try to use a monospace font, fall back to default
    try:
        font_title = ImageFont.truetype("cour.ttf", 18)
        font_warn  = ImageFont.truetype("cour.ttf", 14)
    except:
        font_title = ImageFont.load_default()
        font_warn  = ImageFont.load_default()

    # Draw title
    draw.text((padding // 2, 15), f"AIUNION // {title}", fill="#00ff41", font=font_title)
    draw.line([(padding // 2, title_h - 10), (qr_w + padding // 2, title_h - 10)], fill="#1a2a1a", width=1)

    # Paste QR
    canvas.paste(qr_img, (padding // 2, title_h))

    # Draw warning if present
    if warning:
        y = title_h + qr_h + 10
        draw.text((padding // 2, y), warning, fill="#ff3333", font=font_warn)

    # Save
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    canvas.save(output_path)
    print(f"\n{GREEN}✓ QR code saved to: {output_path}{RESET}")
    print(f"  Opening image...")

    # Open the image
    try:
        if sys.platform == "win32":
            os.startfile(output_path)
        elif sys.platform == "darwin":
            os.system(f"open '{output_path}'")
        else:
            os.system(f"xdg-open '{output_path}'")
    except:
        print(f"  Could not auto-open. Open manually: {output_path}")

    return output_path

def psbt_from_input():
    """Get PSBT string from user."""
    print(f"\n{GREEN}Paste your PSBT string below (press Enter twice when done):{RESET}")
    print(f"{AMBER}  Tip: In Bitcoin Core run: bitcoin-cli walletcreatefundedpsbt{RESET}\n")
    
    lines = []
    while True:
        line = input()
        if line == "" and lines:
            break
        if line:
            lines.append(line.strip())
    
    psbt = "".join(lines).strip()
    
    if not psbt:
        print(f"{RED}❌ No PSBT provided.{RESET}")
        return None
    
    # Basic validation — PSBT strings are base64
    import base64
    try:
        decoded = base64.b64decode(psbt)
        if not decoded.startswith(b'psbt\xff'):
            print(f"{AMBER}⚠ Warning: This may not be a valid PSBT. Proceeding anyway.{RESET}")
    except:
        print(f"{AMBER}⚠ Warning: Could not validate PSBT format. Proceeding anyway.{RESET}")
    
    return psbt

def decode_psbt(psbt):
    """Try to decode and display PSBT details using bitcoin-cli."""
    print(f"\n{GREEN}Attempting to decode PSBT via Bitcoin Core...{RESET}")
    import subprocess
    try:
        result = subprocess.run(
            ["bitcoin-cli", "decodepsbt", psbt],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            import json
            data = json.loads(result.stdout)
            print(f"\n{GREEN}Transaction details:{RESET}")
            
            # Show outputs
            for i, output in enumerate(data.get("tx", {}).get("vout", [])):
                value = output.get("value", 0)
                script = output.get("scriptPubKey", {})
                addr = script.get("address", script.get("type", "unknown"))
                print(f"  Output {i}: {value} BTC → {addr}")
            
            # Show fee if available
            fee = data.get("fee")
            if fee:
                print(f"  Fee: {fee} BTC")
        else:
            print(f"{AMBER}  Bitcoin Core not available or PSBT invalid.{RESET}")
            print(f"  Error: {result.stderr.strip()}")
    except FileNotFoundError:
        print(f"{AMBER}  bitcoin-cli not found in PATH.{RESET}")
    except Exception as e:
        print(f"{AMBER}  Could not decode: {e}{RESET}")

def option_generate_qr():
    """Option 1 — Generate QR from any PSBT."""
    print(f"\n{GREEN}── Generate QR from PSBT ──{RESET}")
    psbt = psbt_from_input()
    if not psbt:
        return
    
    print(f"\n{GREEN}Transaction type:{RESET}")
    print(f"  {AMBER}[1]{RESET} Admin spend")
    print(f"  {AMBER}[2]{RESET} Scorch / burn")
    print(f"  {AMBER}[3]{RESET} Other")
    choice = input("\n  Choice: ").strip()
    
    if choice == "1":
        title   = "ADMIN SPEND — SIGN WITH ADMIN KEY"
        fname   = "psbt_admin_qr.png"
        warning = None
    elif choice == "2":
        title   = "⚠ SCORCH EARTH — IRREVERSIBLE BURN"
        fname   = "psbt_scorch_qr.png"
        warning = "WARNING: THIS BURNS ALL FUNDS PERMANENTLY. VERIFY BEFORE SIGNING."
    else:
        title   = "PSBT — SIGN WITH APPROPRIATE KEY"
        fname   = "psbt_qr.png"
        warning = None
    
    generate_qr(psbt, title, fname, warning)
    print(f"\n{GREEN}Next steps:{RESET}")
    print("  1. Hold this QR up to your air-gapped iPad")
    print("  2. In Nunchuk: tap Import PSBT → Scan QR")
    print("  3. Review transaction details carefully")
    print("  4. Sign with your key")
    print("  5. Nunchuk will show a signed PSBT QR — scan it back here or save the file")
    print("  6. Broadcast via Bitcoin Core: bitcoin-cli sendrawtransaction <hex>")

def option_admin():
    """Option 2 — Admin spend guidance."""
    print(f"\n{GREEN}── Admin Spend ──{RESET}")
    print(f"{AMBER}Use this for operational costs: API keys, hosting, etc.{RESET}")
    print()
    print("Steps to create admin PSBT in Bitcoin Core:")
    print()
    print('  1. bitcoin-cli listunspent')
    print('     — note the txid and vout of your UTXO')
    print()
    print('  2. bitcoin-cli createpsbt')
    print('     \'[{"txid":"<txid>","vout":<vout>}]\'')
    print('     \'[{"<recipient_address>":<amount_btc>}]\'')
    print()
    print('  3. bitcoin-cli walletprocesspsbt "<psbt_string>"')
    print('     — this partially signs with the wallet')
    print()
    print('  4. Paste the resulting PSBT into option [1] above')
    print('     to generate a QR for the iPad to sign')
    print()
    input(f"{GREEN}Press Enter when ready to generate QR...{RESET}")
    option_generate_qr()

def option_scorch():
    """Option 3 — Scorch earth."""
    print(f"\n{RED}{BOLD}── ⚠ SCORCH EARTH ──{RESET}")
    print(f"{RED}This will permanently burn ALL funds in the treasury.{RESET}")
    print(f"{RED}This action is IRREVERSIBLE.{RESET}")
    print()
    confirm = input(f"{AMBER}Type SCORCH to continue (anything else to cancel): {RESET}").strip()
    
    if confirm != "SCORCH":
        print(f"\n{GREEN}Cancelled.{RESET}")
        return
    
    print(f"\n{AMBER}Steps to create scorch PSBT in Bitcoin Core:{RESET}")
    print()
    print("  The scorch transaction sends ALL funds to an OP_RETURN output")
    print("  (provably unspendable — coins destroyed forever)")
    print()
    print('  1. bitcoin-cli listunspent')
    print('     — note ALL UTXOs (txid, vout, amount)')
    print()
    print('  2. For each UTXO, calculate total minus fee (~0.00001 BTC)')
    print()
    print('  3. bitcoin-cli createrawtransaction')
    print('     \'[{"txid":"<txid>","vout":<vout>}]\'')
    print('     \'{"data":"<any_hex_data>"}\'')
    print('     — "data" key creates an OP_RETURN output')
    print()
    print('  4. bitcoin-cli converttopsbt "<raw_tx_hex>"')
    print()
    print('  5. Paste resulting PSBT into option [1] to generate QR')
    print()
    input(f"{GREEN}Press Enter when ready to generate QR...{RESET}")
    
    psbt = psbt_from_input()
    if not psbt:
        return
    
    generate_qr(
        psbt,
        "⚠ SCORCH EARTH — IRREVERSIBLE BURN",
        "psbt_scorch_qr.png",
        "WARNING: THIS BURNS ALL FUNDS PERMANENTLY. VERIFY BEFORE SIGNING."
    )

def option_decode():
    """Option 4 — Decode PSBT."""
    psbt = psbt_from_input()
    if psbt:
        decode_psbt(psbt)

def main():
    print_header()
    
    while True:
        print_menu()
        choice = input(f"{GREEN}  Choice: {RESET}").strip().upper()
        
        if choice == "1":
            option_generate_qr()
        elif choice == "2":
            option_admin()
        elif choice == "3":
            option_scorch()
        elif choice == "4":
            option_decode()
        elif choice == "Q":
            print(f"\n{GREEN}Goodbye.{RESET}\n")
            break
        else:
            print(f"{AMBER}  Invalid choice.{RESET}")
        
        print()
        input(f"{GREEN}  Press Enter to return to menu...{RESET}")
        print("\n" + "─"*45 + "\n")

if __name__ == "__main__":
    main()
