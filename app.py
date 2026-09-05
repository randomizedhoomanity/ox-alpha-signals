"""ox-alpha x402 paid-API service.

Sells ox-alpha crypto signal intelligence for USDC via x402 micropayments
(0.001 USDC per call, settled on Base mainnet via the x402 facilitator).

Free endpoints:  GET / , /health , /preview , /docs , /llms.txt ,
                 /.well-known/x402.json , /.well-known/agent-card.json
Paid endpoints:  GET /signals , /price/{pair} , /report   ($0.001/call)
MCP server:      POST /mcp  (Streamable HTTP — report/signals/price paid,
                            preview/service_info free; see mcp_tools.py)

Data comes from the ox-trader Coinbase client (authenticated brokerage
candles) scored with oxtrader.signals.score_product. Cached 10 minutes.
"""
import json
import os
import sys
import threading
import time
from contextlib import asynccontextmanager

# ox-trader is the operator's private Coinbase client package (Client().get_candles,
# score_product, load_key). Point OX_TRADER_HOME at your own copy, or swap the
# imports for any candle source + scoring function.
sys.path.insert(0, os.environ.get("OX_TRADER_HOME", "/path/to/ox-trader"))

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from oxtrader.coinbase import Client, CoinbaseError
from oxtrader.signals import score_product

from x402.extensions.bazaar import OutputConfig, declare_discovery_extension
from x402 import x402ResourceServer
from x402.http import FacilitatorConfig, HTTPFacilitatorClient
from x402.http.middleware.fastapi import payment_middleware_from_config
from x402.mechanisms.evm.exact import ExactEvmServerScheme

import mcp_tools

CONFIG = json.load(open(os.environ.get(
    "OX_TRADER_CONFIG", "/path/to/ox-trader/config.json")))  # needs "universe_watchlist": [pair ids]
PAYTO = json.load(open(os.path.expanduser(os.environ.get(
    "X402_PAYTO_FILE", "~/.config/ox-alpha/x402_payto.json"))))  # {"address": "0x..."}
PAY_TO = PAYTO["address"]
NETWORK = "eip155:8453"  # Base mainnet; facilitator settles in native USDC
FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"
FACILITATOR_HOST_CLAIM = "api.cdp.coinbase.com"
# Public hostname (cloudflared quick tunnel). Stable as long as the tunnel
# service is never restarted; if it ever changes, update this allowlist entry.
PUBLIC_HOST = "starring-generated-forests-trader.trycloudflare.com"


class CDPFacilitatorAuth:
    """Mints CDP JWTs per facilitator endpoint using the existing trading key."""

    def __init__(self):
        from oxtrader import coinbase as cbmod
        self._cb = cbmod
        self._creds = cbmod.load_key()
        if not self._creds:
            raise RuntimeError("no CDP key installed for facilitator auth")

    def _token(self, method, path):
        """CDP JWT with host claim api.cdp.coinbase.com (facilitator host)."""
        import jwt as pyjwt
        key_obj = self._cb._signing_key(self._creds)
        now = int(time.time())
        payload = {"sub": self._creds["key_id"], "iss": "cdp",
                   "nbf": now, "exp": now + 120,
                   "uris": [f"{method} {FACILITATOR_HOST_CLAIM}{path}"]}
        headers = {"kid": self._creds["key_id"],
                   "nonce": str(int.from_bytes(os.urandom(6), "big"))}
        algo = "EdDSA" if self._creds["kind"] == "ed25519" else "ES256"
        return pyjwt.encode(payload, key_obj, algorithm=algo, headers=headers)

    def get_auth_headers(self):
        from x402.http.facilitator_client_base import AuthHeaders
        b = "Bearer "
        return AuthHeaders(
            supported={"Authorization": b + self._token("GET", "/platform/v2/x402/supported")},
            verify={"Authorization": b + self._token("POST", "/platform/v2/x402/verify")},
            settle={"Authorization": b + self._token("POST", "/platform/v2/x402/settle")},
            bazaar={"Authorization": b + self._token("GET", "/platform/v2/x402/supported")},
        )

PRICE = "$0.001"

# ---------------------------------------------------------------- data layer
_cache_lock = threading.Lock()
_cache = {"ts": 0.0, "signals": None}
CACHE_TTL = 600  # seconds

UNIVERSE = CONFIG["universe_watchlist"]


def _compute_signals() -> dict:
    client = Client()
    out = []
    for pid in UNIVERSE:
        try:
            candles = client.get_candles(pid, "SIX_HOUR", 120)
            if not candles:
                continue
            price_now = float(candles[0]["close"])
            sc = score_product(candles, price_now)
            out.append({
                "pair": pid,
                "price_usdc": round(price_now, 6),
                "score": sc.get("score", 0),
                "atr_pct": sc.get("atr_pct"),
                "reasons": sc.get("reasons", []),
            })
            time.sleep(0.15)  # pace the API
        except (CoinbaseError, Exception):
            continue
    out.sort(key=lambda r: r["score"], reverse=True)
    return {
        "generated_at": int(time.time()),
        "granularity": "SIX_HOUR",
        "count": len(out),
        "signals": out,
    }


def get_signals(force: bool = False) -> dict:
    with _cache_lock:
        now = time.time()
        if force or _cache["signals"] is None or now - _cache["ts"] > CACHE_TTL:
            _cache["signals"] = _compute_signals()
            _cache["ts"] = now
        return _cache["signals"]


# ------------------------------------------------------------------ app setup
facilitator_auth = CDPFacilitatorAuth()
facilitator = HTTPFacilitatorClient(FacilitatorConfig(
    url=FACILITATOR_URL, auth_provider=facilitator_auth))

# MCP server (x402-paid tools at /mcp) — built before the app; its session
# manager runs in the app lifespan (mounted Starlette apps don't run their
# own lifespans, so it must be wired into the FastAPI lifespan).
mcp = mcp_tools.build_mcp(
    facilitator_url=FACILITATOR_URL,
    auth_provider=facilitator_auth,
    pay_to=PAY_TO,
    network=NETWORK,
    price=PRICE,
    get_signals=get_signals,
    public_host=PUBLIC_HOST,
)
mcp_app = mcp.streamable_http_app()  # also creates the session manager


@asynccontextmanager
async def _lifespan(_app):
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="ox-alpha signals API",
              description="AI-operated crypto market intelligence. "
                          "Paid endpoints cost $0.001/call via x402 "
                          "(USDC on Base). Disclosed as AI-generated data.",
              version="1.1.0",
              lifespan=_lifespan)


@app.get("/")
def index():
    return {
        "service": "ox-alpha signals API",
        "operator": "autonomous AI agent (disclosed)",
        "pricing": f"{PRICE} per paid call, USDC via x402 on Base",
        "paid_endpoints": ["/signals", "/price/{pair}", "/report",
                           "MCP tools report/signals/price at /mcp"],
        "free_endpoints": ["/health", "/preview", "/llms.txt", "/docs",
                           "/.well-known/x402.json",
                           "/.well-known/agent-card.json",
                           "/mcp (free tools: preview, service_info)"],
        "data": "Coinbase Advanced Trade 6h candles, composite trend/momentum score -100..+100",
    }


@app.get("/health")
def health():
    return {"ok": True, "ts": int(time.time())}


def _require_signals():
    data = get_signals()
    if not data["signals"]:
        raise HTTPException(status_code=503, detail="signal feed unavailable, try again")
    return data


@app.get("/signals")
def signals():
    """All pairs, ranked by composite score."""
    return _require_signals()


@app.get("/price/{pair}")
def price(pair: str):
    pair = pair.upper().replace("_", "-")
    if not pair.endswith("-USDC"):
        raise HTTPException(status_code=400, detail="only USDC-quoted pairs supported")
    data = _require_signals()
    for row in data["signals"]:
        if row["pair"] == pair:
            return row
    raise HTTPException(status_code=404, detail=f"{pair} not in universe")


@app.get("/.well-known/x402.json")
def x402_manifest():
    """Standard x402 discovery manifest — how agents find payable endpoints."""
    return {
        "name": "ox-alpha signals API",
        "description": "AI-operated crypto market intelligence: composite signal scores "
                       "for 20 Coinbase USDC pairs (trend/momentum/RSI/ATR), ranked long candidates. "
                       "Disclosed AI-generated data, not financial advice.",
        "endpoints": {
            "/report": {"price_usdc": 0.001, "method": "GET",
                        "description": "Top-5 long candidates with score and reasons"},
            "/signals": {"price_usdc": 0.001, "method": "GET",
                         "description": "All 20 USDC pairs ranked by composite score"},
            "/price/{pair}": {"price_usdc": 0.001, "method": "GET",
                              "description": "Single pair: price, score, ATR%, reasons (BTC-USDC etc.)"},
            "/mcp": {"price_usdc": 0.001, "method": "POST",
                     "description": "MCP server (Streamable HTTP): report/signals/price "
                                    "tools at $0.001/call; preview/service_info free"},
            "/preview": {"price_usdc": 0, "method": "GET",
                         "description": "Free sample: current #1 top signal, no payment"}
        },
        "network": "base-mainnet",
        "payment": {"protocol": "x402", "asset": "USDC", "chainId": 8453},
        "discovery": {"llms_txt": "/llms.txt",
                      "agent_card": "/.well-known/agent-card.json",
                      "openapi": "/openapi.json"},
        "operator": "ox-alpha (AI-operated, disclosed)",
        "disclaimer": "AI-generated market commentary, not financial advice."
    }


@app.get("/report")
def report():
    """Top-5 long candidates with reasons — the headline product."""
    data = _require_signals()
    top = data["signals"][:5]
    return {
        "generated_at": data["generated_at"],
        "top_long_candidates": top,
        "disclaimer": "AI-generated market commentary, not financial advice.",
    }


@app.get("/preview")
def preview():
    """Free preview: #1 top signal only. Full ranked list via /report ($0.001)."""
    data = _require_signals()
    top = data["signals"][:1]
    return {
        "generated_at": data["generated_at"],
        "preview": top[0] if top else None,
        "note": "Full top-5 + 20-pair universe at /report ($0.001/call via x402)",
        "disclaimer": "AI-generated market commentary, not financial advice.",
    }


# ------------------------------------------------- agent discovery surfaces
LLMS_TXT = f"""# ox-alpha signals API

> AI-operated crypto market intelligence: composite trend/momentum/RSI/ATR
> signal scores for 20 Coinbase USDC pairs, ranked long candidates.
> $0.001/call via x402 (USDC on Base) — no API keys, no signup. Operated
> end-to-end by an autonomous AI agent (disclosed). AI-generated market
> commentary, not financial advice.

Paid endpoints cost 0.001 USDC per call over the x402 protocol on Base
mainnet (chain 8453): an unpaid request returns HTTP 402 with payment
details; x402-capable clients pay and retry automatically. Settlement via
the Coinbase CDP facilitator. Free samples need no payment.

## Endpoints

- [Free preview](/preview): Current #1 top-ranked signal, no payment
- [Report](/report): Top-5 long candidates with scores and reasons ($0.001)
- [All signals](/signals): All 20 USDC pairs ranked by composite score ($0.001)
- [Single pair](/price/BTC-USDC): One pair's price, score, ATR%, reasons ($0.001)
- [MCP server](/mcp): Model Context Protocol endpoint (Streamable HTTP) — the same tools for AI agents, paid per tool call via x402; `preview` and `service_info` are free
- [Health](/health): Liveness check

## For AI agents

- [OpenAPI spec](/openapi.json): Machine-readable API schema
- [x402 discovery manifest](/.well-known/x402.json): Payable endpoints and payment config
- [Agent card](/.well-known/agent-card.json): A2A-style capabilities and skills card
- [API docs](/docs): Interactive OpenAPI documentation
- [GitHub]({mcp_tools.GITHUB_REPO}): Source, examples, AGENTS.md

## Optional

- [Service root](/): JSON service metadata
"""

AGENT_CARD = {
    "protocolVersion": "0.3.0",
    "name": "ox-alpha signals",
    "description": ("AI-operated crypto market intelligence: composite signal "
                    "scores (trend/momentum/RSI/ATR) for 20 Coinbase USDC "
                    "pairs, ranked long candidates. $0.001/call via x402 on "
                    "Base. Operated end-to-end by an autonomous AI agent "
                    "(disclosed)."),
    "version": "1.1.0",
    "provider": {"organization": "ox-alpha (AI-operated, disclosed)",
                 "url": mcp_tools.GITHUB_REPO},
    "capabilities": {
        "streaming": False,
        "pushNotifications": False,
        "payments": {"protocol": "x402", "network": NETWORK, "asset": "USDC"},
    },
    "defaultInputModes": ["application/json"],
    "defaultOutputModes": ["application/json"],
    "mcp": "/mcp",
    "skills": [
        {"id": "report", "name": "report",
         "description": mcp_tools._REPORT_DESC,
         "tags": ["crypto", "signals", "trading", "coinbase"],
         "x402": {"price": PRICE, "url": "/mcp", "tool": "report"}},
        {"id": "signals", "name": "signals",
         "description": mcp_tools._SIGNALS_DESC,
         "tags": ["crypto", "signals", "ranking", "coinbase"],
         "x402": {"price": PRICE, "url": "/mcp", "tool": "signals"}},
        {"id": "price", "name": "price",
         "description": mcp_tools._PRICE_DESC,
         "tags": ["crypto", "price", "signals"],
         "x402": {"price": PRICE, "url": "/mcp", "tool": "price"}},
        {"id": "preview", "name": "preview",
         "description": "Free sample: the current #1 top-ranked signal.",
         "tags": ["crypto", "free", "sample"]},
    ],
    "discovery": {"llms_txt": "/llms.txt",
                  "x402_manifest": "/.well-known/x402.json",
                  "openapi": "/openapi.json",
                  "github": mcp_tools.GITHUB_REPO},
    "disclaimer": mcp_tools.DISCLAIMER,
}


@app.get("/llms.txt", response_class=PlainTextResponse)
def llms_txt():
    """llms.txt (llmstxt.org v2) — curated map of the service for LLMs."""
    return PlainTextResponse(LLMS_TXT, media_type="text/markdown")


@app.get("/.well-known/agent-card.json")
def agent_card():
    """A2A-style agent card — capabilities, skills and payment config."""
    return AGENT_CARD


# MCP server (Streamable HTTP at POST /mcp; internal path is "/")
app.mount("/mcp", mcp_app)


@app.middleware("http")
async def mcp_root_rewrite(request: Request, call_next):
    """Serve POST /mcp directly (no 307 to /mcp/).

    Starlette's Mount only matches "/mcp/…", so the bare "/mcp" would 307.
    Real MCP clients follow redirects, but the canonical advertised URL should
    answer in place (strict clients and the Bazaar validator don't follow).
    """
    if request.url.path == "/mcp":
        request.scope["path"] = "/mcp/"
    return await call_next(request)


# ------------------------------------------------------------ x402 middleware
routes_config = {}
for path, desc in [
    ("/signals",
     "Ranked crypto signal scores for 20 USDC pairs on Coinbase (score -100..+100, "
     "price, ATR%, reasons). AI-operated, disclosed. Updated every 10 min."),
    ("/report",
     "Top-5 crypto long candidates across 20 Coinbase USDC pairs with score, "
     "price, ATR% and reasons. AI-operated market intelligence, not advice."),
]:
    routes_config[f"GET {path}"] = {
        "accepts": {"scheme": "exact", "payTo": PAY_TO, "price": PRICE,
                    "network": NETWORK},
        "description": desc[:500],
        "mimeType": "application/json",
        "extensions": declare_discovery_extension(
            output=OutputConfig(example={
                "generated_at": 1787660000,
                "signals": [{"pair": "BTC-USDC", "price_usdc": 64000.0,
                             "score": 42, "atr_pct": 2.1,
                             "reasons": ["trend up"]}],
            })) if path == "/signals" else declare_discovery_extension(
            output=OutputConfig(example={
                "generated_at": 1787660000,
                "top_long_candidates": [{"pair": "ETH-USDC", "price_usdc": 3100.0,
                                         "score": 55, "atr_pct": 2.8,
                                         "reasons": ["momentum"]}],
                "disclaimer": "AI-generated, not financial advice.",
            })),
    }
# ":pair" (not "*") so the bazaar extension reports pathParams {"pair": ...}
# matching the declared schema — wildcards auto-generate var1/var2 names that
# fail the facilitator's discovery parse check.
routes_config["GET /price/:pair"] = {
    "accepts": {"scheme": "exact", "payTo": PAY_TO, "price": PRICE,
                "network": NETWORK},
    "description": ("Single-pair crypto lookup: price in USDC, composite signal "
                    "score (-100..+100), ATR% and reasons. Path param is the "
                    "Coinbase pair id, e.g. /price/BTC-USDC.")[:500],
    "mimeType": "application/json",
    "extensions": declare_discovery_extension(
        path_params_schema={"properties": {"pair": {"type": "string",
            "description": "Coinbase pair id like BTC-USDC"}},
            "required": ["pair"]},
        output=OutputConfig(example={"pair": "BTC-USDC", "price_usdc": 64000.0,
                                     "score": 42, "atr_pct": 2.1,
                                     "reasons": ["trend up"]}),
    ),
}

# Facilitator + auth are constructed before the app (shared with the MCP
# resource server in mcp_tools).
x402_mw = payment_middleware_from_config(
    routes=routes_config,
    facilitator_client=facilitator,
    schemes=[{"network": NETWORK, "server": ExactEvmServerScheme()}],
)


@app.middleware("http")
async def x402_payment(request: Request, call_next):
    resp = await x402_mw(request, call_next)
    # Enrich bare 402 bodies with a hint for non-x402 clients
    if resp.status_code == 402 and resp.body in (b"", b"{}", b"null", None):
        content = {"error": "Payment required",
                   "hint": "Free sample: GET /preview (no payment). "
                           "To pay automatically: use an x402 client with "
                           "USDC on Base — $0.001/call.",
                   "docs": "/docs"}
        # Preserve ALL original headers (incl. x402 challenge) but fix Content-Length
        headers = {k: v for k, v in resp.headers.items()
                   if k.lower() not in ("content-length", "content-type")}
        return JSONResponse(content=content, status_code=402, headers=headers)
    return resp
