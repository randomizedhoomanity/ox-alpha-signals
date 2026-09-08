"""ox-alpha MCP server — x402-paid tools over Streamable HTTP, mounted at /mcp.

Mirrors the HTTP API (same data, same $0.001/call pricing, same payTo wallet):
  report         top-5 long candidates            PAID
  signals        all 20 USDC pairs ranked         PAID
  price          single-pair lookup               PAID
  preview        #1 signal sample                 FREE
  service_info   endpoints / pricing / discovery  FREE

Payment flow (x402 over MCP, the CDP standard): an unpaid tool call returns an
isError result carrying a PaymentRequired payload; the client pays and retries
with the payment at _meta["x402/payment"]; the facilitator verifies, the tool
runs, and only successful executions are settled (receipt at
_meta["x402/payment-response"]). Failed tool runs are never settled.
https://docs.cdp.coinbase.com/x402/seller/mcp-payments

app.py calls build_mcp() BEFORE creating the FastAPI app, then
streamable_http_app() (creates the session manager), runs it in the app
lifespan, and mounts it at /mcp. The internal streamable_http_path is "/"
so the public endpoint is exactly POST /mcp.
"""
import asyncio

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from x402 import x402ResourceServer
from x402.extensions.bazaar import (
    DeclareMcpDiscoveryConfig,
    OutputConfig,
    declare_mcp_discovery_extension,
)
from x402.http import FacilitatorConfig, HTTPFacilitatorClient
from x402.mcp import create_payment_wrapper
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.schemas import ResourceConfig
from x402.schemas.payments import ResourceInfo

DISCLAIMER = "AI-generated market commentary, not financial advice."
GITHUB_REPO = "https://github.com/randomizedhoomanity/ox-alpha-signals"

# Tool descriptions (Bazaar rejects discovery metadata with descriptions
# over 500 chars — _paid() enforces the cap, same as the HTTP routes).
_REPORT_DESC = ("Top-5 crypto long candidates across 20 Coinbase USDC pairs with "
                "score, price, ATR% and reasons. AI-operated market intelligence, "
                "not advice.")
_SIGNALS_DESC = ("Ranked crypto signal scores for 20 USDC pairs on Coinbase "
                 "(score -100..+100, price, ATR%, reasons). AI-operated, "
                 "disclosed. Updated every 10 min.")
_PRICE_DESC = ("Single-pair crypto lookup: price in USDC, composite signal score "
               "(-100..+100), ATR% and reasons. Pair is a Coinbase pair id like "
               "BTC-USDC.")


def build_mcp(*, facilitator_url, auth_provider, pay_to, network, price,
              get_signals, public_host, extra_hosts=()):
    """Build the FastMCP server with x402-paid tools.

    All dependencies are injected (no imports from app.py) — app.py owns the
    config constants, the facilitator auth, and the shared signal cache.
    """
    resource_server = x402ResourceServer(HTTPFacilitatorClient(FacilitatorConfig(
        url=facilitator_url, auth_provider=auth_provider)))
    resource_server.register(network, ExactEvmServerScheme())
    resource_server.initialize()
    accepts = resource_server.build_payment_requirements(ResourceConfig(
        scheme="exact", network=network, pay_to=pay_to, price=price,
        # extra.name is the token's EIP-712 domain name — it MUST be "USD
        # Coin" (Circle's FiatToken domain), NOT the ticker "USDC": the buyer
        # signs the EIP-3009 permit with this name, and a mismatch makes the
        # signature recover to the wrong address (facilitator verify reverts
        # with invalid_payload). Matches the HTTP routes' middleware default.
        extra={"name": "USD Coin", "version": "2"},
    ))

    def paid(tool_name, description, input_schema, example, output_example):
        """x402 payment wrapper + Bazaar discovery metadata for one tool."""
        extension = declare_mcp_discovery_extension(DeclareMcpDiscoveryConfig(
            tool_name=tool_name,
            input_schema=input_schema,
            description=description[:500],
            transport="streamable-http",
            example=example,
            output=OutputConfig(example=output_example),
        ))
        return create_payment_wrapper(
            resource_server,
            accepts=accepts,
            resource=ResourceInfo(
                url=f"mcp://ox-alpha/{tool_name}",
                description=description[:500],
                mime_type="application/json",
            ),
            extensions=extension,
        )

    mcp = FastMCP(
        "ox-alpha signals",
        instructions=(
            "Crypto signal intelligence from ox-alpha (AI-operated, disclosed). "
            "Paid tools cost $0.001/call via x402 (USDC on Base): an unpaid "
            "call returns payment instructions; retry the same call with the "
            "payment at _meta['x402/payment'] to complete it. Free tools: "
            "preview (sample signal), service_info."
        ),
        stateless_http=True,
        # DNS-rebinding protection stays ON. Without an explicit allowlist the
        # MCP SDK defaults to localhost-only Host headers and returns 421 to
        # everything arriving through the public tunnel; allowlist the public
        # host (kept stable by never restarting the tunnel service) plus
        # localhost patterns for scratch-port testing.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[public_host, *extra_hosts,
                           "127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=[f"https://{public_host}"]
                          + [f"https://{h}" for h in extra_hosts],
        ),
    )
    mcp.settings.streamable_http_path = "/"  # public endpoint: POST /mcp

    async def _data():
        """Signal data — shares the 10-min cache with the HTTP routes."""
        return await asyncio.to_thread(get_signals)

    @mcp.tool(name="report",
              description=f"{_REPORT_DESC} Requires payment of $0.001 USDC.")
    @paid("report", _REPORT_DESC,
          {"type": "object", "properties": {}, "required": []}, {},
          {"generated_at": 1787660000,
           "top_long_candidates": [{"pair": "ETH-USDC", "price_usdc": 3100.0,
                                    "score": 55, "atr_pct": 2.8,
                                    "reasons": ["momentum"]}],
           "disclaimer": DISCLAIMER})
    async def report() -> dict:
        data = await _data()
        if not data["signals"]:
            raise RuntimeError("signal feed unavailable, try again later")
        return {
            "generated_at": data["generated_at"],
            "top_long_candidates": data["signals"][:5],
            "disclaimer": DISCLAIMER,
        }

    @mcp.tool(name="signals",
              description=f"{_SIGNALS_DESC} Requires payment of $0.001 USDC.")
    @paid("signals", _SIGNALS_DESC,
          {"type": "object", "properties": {}, "required": []}, {},
          {"generated_at": 1787660000,
           "signals": [{"pair": "BTC-USDC", "price_usdc": 64000.0,
                        "score": 42, "atr_pct": 2.1,
                        "reasons": ["trend up"]}]})
    async def signals() -> dict:
        data = await _data()
        if not data["signals"]:
            raise RuntimeError("signal feed unavailable, try again later")
        return data

    @mcp.tool(name="price",
              description=f"{_PRICE_DESC} Requires payment of $0.001 USDC.")
    @paid("price", _PRICE_DESC,
          {"type": "object",
           "properties": {"pair": {"type": "string",
                                   "description": "Coinbase pair id like BTC-USDC"}},
           "required": ["pair"]},
          {"pair": "BTC-USDC"},
          {"pair": "BTC-USDC", "price_usdc": 64000.0, "score": 42,
           "atr_pct": 2.1, "reasons": ["trend up"]})
    async def price(pair: str) -> dict:
        pid = pair.upper().replace("_", "-")
        data = await _data()
        for row in data["signals"]:
            if row["pair"] == pid:
                return row
        raise ValueError(f"{pid} not in the 20-pair USDC universe")

    @mcp.tool(name="preview",
              description="Free sample: the current #1 top-ranked signal, no "
                          "payment. Full top-5 and 20-pair universe via the "
                          "paid report/signals tools ($0.001/call).")
    async def preview() -> dict:
        data = await _data()
        top = data["signals"][:1]
        return {
            "generated_at": data["generated_at"],
            "preview": top[0] if top else None,
            "note": "Full top-5 + 20-pair universe via report/signals "
                    "($0.001/call via x402)",
            "disclaimer": DISCLAIMER,
        }

    @mcp.tool(name="service_info",
              description="Free: endpoints, pricing, x402 payment setup "
                          "(USDC on Base) and discovery links for the "
                          "ox-alpha signals service.")
    async def service_info() -> dict:
        return {
            "service": "ox-alpha signals API",
            "operator": "autonomous AI agent (disclosed)",
            "pricing": f"{price} per paid call, USDC via x402 on Base (chain 8453)",
            "payment": {"protocol": "x402", "asset": "USDC",
                        "network": network, "payTo": pay_to,
                        "facilitator": facilitator_url},
            "paid_tools": ["report", "signals", "price"],
            "free_tools": ["preview", "service_info"],
            "http_endpoints": ["/report", "/signals", "/price/{pair}"],
            "discovery": {
                "llms_txt": "/llms.txt",
                "x402_manifest": "/.well-known/x402.json",
                "agent_card": "/.well-known/agent-card.json",
                "openapi": "/openapi.json",
                "github": GITHUB_REPO,
            },
            "disclaimer": DISCLAIMER,
        }

    return mcp
