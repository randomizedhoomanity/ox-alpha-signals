# AGENTS.md

Instructions for AI coding agents working on the ox-alpha signals service. This stack is
**live**: it settles real USDC on Base through a Coinbase CDP facilitator keyed to the
operator's trading account. Read this file before changing anything.

## What this is

A FastAPI service (`app.py`) that sells crypto signal data for $0.001/call via x402
(HTTP 402 → payment → verify → settle), with an MCP server (`mcp_tools.py`, mounted at
`/mcp`) exposing the same data as x402-paid tools. One process, one port: the HTTP
routes, the MCP endpoint, llms.txt, the agent card and the discovery manifest all share
the data cache and the facilitator auth.

## Running and testing

```bash
# production venv (x402==2.20.0, mcp 1.29.x — see the pins below)
uvicorn app:app --host 127.0.0.1 --port 8940

# NEVER test on the production port. Boot a scratch port instead:
uvicorn app:app --host 127.0.0.1 --port 8942

# free-path battery (no money moves):
curl -s localhost:8942/health            # 200
curl -s localhost:8942/preview           # 200 + live #1 signal
curl -s localhost:8942/report            # 402 + payment-required header
curl -s localhost:8942/llms.txt          # 200 text/markdown

# MCP battery (free tools + unpaid paid-tool):
python mcp_buyer_test.py preview         # free, no payment
python mcp_buyer_test.py report          # pays $0.001 from the buyer wallet (real money)

# paid HTTP settle (real money):
python buyer_paid_call.py
```

**Nothing is restarted until the scratch battery passes.** Deploy = one
`systemctl --user restart ox-x402-api` — the tunnel service is a *different* unit and is
never touched.

## Hard-won gotchas (each of these cost a debugging session)

1. **Pin `mcp>=1.0.0,<2`.** mcp 2.x renames `FastMCP` → `MCPServer` and breaks
   `x402.mcp` 2.20.0 with `ModuleNotFoundError: mcp.server.fastmcp`.

2. **EIP-712 domain name is `"USD Coin"`, not the ticker `"USDC"`.** The `extra.name`
   in payment requirements is what buyers sign the EIP-3009 permit with. A wrong name
   makes the signature recover to the wrong address — the facilitator rejects verify
   with `invalid_payload: contract call failed: execution reverted`. (Omitting `extra`
   entirely is also safe: the SDK fills the correct name from its asset table.)

3. **CDP facilitator JWTs need the host claim `api.cdp.coinbase.com`** in the `uris`
   field, one token per facilitator endpoint (supported/verify/settle), minted from the
   CDP API key. See `CDPFacilitatorAuth` in `app.py`.

4. **Self-pay is blocked by the facilitator.** The payTo wallet cannot buy from itself;
   E2E tests use a separate buyer wallet with ~$1 USDC + gas.

5. **Mounted FastMCP apps don't run their own lifespan.** You must call
   `mcp.streamable_http_app()` at module level (it creates the session manager) and run
   `async with mcp.session_manager.run():` inside the FastAPI lifespan — accessing
   `session_manager` before `streamable_http_app()` raises.

6. **`mcp.settings.streamable_http_path = "/"`** so mounting at `/mcp` yields exactly
   `POST /mcp` (default would serve `/mcp/mcp`). A bare `POST /mcp` is additionally
   rewritten to `/mcp/` in-process (see `mcp_root_rewrite`) because Starlette's Mount
   would otherwise 307.

7. **MCP DNS-rebinding protection rejects non-localhost Host headers with 421.** Pass
   `transport_security=TransportSecuritySettings(allowed_hosts=[PUBLIC_HOST, ...])` to
   FastMCP — anything arriving through a public tunnel needs its hostname allowlisted.

8. **Bazaar discovery: descriptions ≤ 500 chars**, and route patterns with a wildcard
   (`/price/*`) auto-generate `pathParams: {"var1": ...}` which fails the facilitator's
   discovery parse check — use named params (`GET /price/:pair`) so `pathParams` matches
   the declared schema.

9. **Bazaar indexing happens on settled paid calls** that carry the discovery extension,
   not on startup. After changing route metadata, re-settle each route from the buyer
   wallet, then validate with
   `POST https://api.cdp.coinbase.com/platform/v2/x402/validate {"resource": url, "method": "GET"}`
   — expect `simulation: accepted` and `index.active: true`. (The validator is
   HTTP-only; it cannot parse MCP endpoints — that's expected, MCP tools are indexed
   from settles.)

10. **The startup warnings** `bazaar extension: input: 'method' is a required property`
    on `GET /signals|/report|/price/:pair` are benign: the runtime enrichment injects
    `input: {type: "http", method: "GET"}` into the wire challenge. Verify by decoding
    the base64 `payment-required` header.

11. **The cloudflared quick-tunnel URL is stable only while the tunnel process runs.**
    Never restart the tunnel service — the URL would change and every published link
    (README, PR entry, manifest) would break. If the URL ever changes, update
    `PUBLIC_HOST` in `app.py` (the MCP Host allowlist) plus the docs.

12. **Facilitator settle order:** verification first, tool/endpoint executes, settlement
    only after successful execution. Failed tool runs are never settled — keep it that
    way; it's the CDP MCP-payments standard.

## Operator-side facts

- systemd units: `ox-x402-api.service` (uvicorn, 127.0.0.1:8940) and
  `ox-x402-tunnel.service` (cloudflared — never touch).
- Config files (never commit): CDP API key, payTo wallet file, buyer wallet file,
  ox-trader package + config (private Coinbase client).
- All code in this repo is written and operated by the disclosed AI agent (ox-alpha /
  Hermes Agent by Nous Research) under a human principal.
