"""Paid MCP tool call through the public tunnel — x402 over Streamable HTTP.

Same buyer wallet and settle path as buyer_paid_call.py, but exercises the
MCP server at POST /mcp instead of the plain HTTP routes. Unpaid call ->
PaymentRequired result -> payment built by x402Client -> retried with the
payment at _meta['x402/payment'] -> facilitator verify -> tool runs -> settle
(receipt at _meta['x402/payment-response']).

Usage:
    python mcp_buyer_test.py              # paid 'report' tool ($0.001)
    python mcp_buyer_test.py price BTC-USDC
    python mcp_buyer_test.py preview      # free, no payment
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.environ.get("OX_TRADER_HOME", "/path/to/ox-trader"))

from eth_account import Account
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from x402 import x402Client
from x402.mcp import x402MCPSession
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact.client import ExactEvmScheme

TUNNEL = "https://starring-generated-forests-trader.trycloudflare.com"
MCP_URL = f"{TUNNEL}/mcp"

# Load buyer wallet (NOT the receive wallet — self-pay is blocked by the
# facilitator).
bk = json.load(open(os.path.expanduser(os.environ.get(
    "X402_BUYER_FILE", "~/.config/ox-alpha/x402_buyer.json"))))
priv = bk.get("private_key") or bk.get("key")
acct = Account.from_key(priv if priv.startswith("0x") else "0x" + priv)
print("buyer:", acct.address)

client = x402Client()
client.register("eip155:8453", ExactEvmScheme(EthAccountSigner(acct)))

tool = sys.argv[1] if len(sys.argv) > 1 else "report"
args = {}
if tool == "price" and len(sys.argv) > 2:
    args["pair"] = sys.argv[2]


async def main():
    # x402MCPSession is transport-agnostic: create_x402_mcp_client() only
    # ships an SSE transport, so wire it over Streamable HTTP ourselves.
    async with streamablehttp_client(MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            mcp = x402MCPSession(session, client)
            await mcp.initialize()
            print(f"calling MCP tool '{tool}' {args or ''}...")
            r = await mcp.call_tool(tool, args)
            print("payment_made:", r.payment_made, "| isError:", r.is_error)
            if r.payment_response is not None:
                pr = r.payment_response
                print("settle success:", getattr(pr, "success", pr))
                print("tx:", getattr(pr, "transaction", "?"))
                print("network:", getattr(pr, "network", "?"))
            body_raw = r.content[0].text if r.content else ""
            print("raw content:", body_raw[:400])
            try:
                body = json.loads(body_raw)
            except ValueError:
                body = None
            if r.is_error and body is None:
                sys.exit(f"tool error (no settle): {body_raw[:400]}")
            if tool == "report" and body:
                top = body["top_long_candidates"][0]
                print("top:", top["pair"], "score", top["score"])
            elif tool == "price":
                print(body["pair"], body["price_usdc"], "score", body["score"])
            elif tool == "preview":
                print("top:", body["preview"]["pair"], "score", body["preview"]["score"])
            else:
                print(json.dumps(body)[:400])


asyncio.run(main())
