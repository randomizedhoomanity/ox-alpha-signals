"""Buyer-side paid call to the MAINNET ox-alpha API through the public tunnel.
Settles via CDP facilitator on Base — this triggers Bazaar re-indexing.
Uses the buyer wallet (0x3831...), NOT the receive wallet (self-pay blocked).
"""
import asyncio, json, os, sys
sys.path.insert(0, os.environ.get("OX_TRADER_HOME", "/path/to/ox-trader"))

import httpx
from eth_account import Account
from x402 import x402Client
from x402.http.clients.httpx import wrapHttpxWithPayment
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact.client import ExactEvmScheme

# Load buyer wallet (the one with 1 USDC + gas)
bk = json.load(open(os.path.expanduser(os.environ.get(
    "X402_BUYER_FILE", "~/.config/ox-alpha/x402_buyer.json"))))
priv = bk.get("private_key") or bk.get("key")
acct = Account.from_key(priv if priv.startswith("0x") else "0x" + priv)
print("buyer:", acct.address)

# Public tunnel URL — re-read if stale
TUNNEL = "https://starring-generated-forests-trader.trycloudflare.com"

client = x402Client()
client.register("eip155:8453", ExactEvmScheme(EthAccountSigner(acct)))

async def main():
    async with wrapHttpxWithPayment(client, timeout=90) as c:
        print("calling /report on mainnet via tunnel...")
        r = await c.get(f"{TUNNEL}/report")
        print("status:", r.status_code)
        body = r.json()
        print(json.dumps(body, indent=2)[:1500])

asyncio.run(main())
