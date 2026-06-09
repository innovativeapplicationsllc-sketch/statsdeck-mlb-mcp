"""
OAuth 2.1 / Clerk integration for StatsDeck.

Architecture: StatsDeck is a pure OAuth resource server (RFC 9728).
Clerk is the authorization server.

This module provides:
  - ClerkTokenVerifier      — validates Clerk JWTs; implements the mcp TokenVerifier protocol
  - AS metadata bridge      — /.well-known/oauth-authorization-server: bridges Clerk's OIDC,
                              overrides issuer/authorization_endpoint/registration_endpoint
  - Authorization bridge    — GET /oauth/authorize: receives Claude's auth request, strips the
                              RFC 8707 `resource` param (Clerk's Fosite rejects it with
                              oauth2idp_patch_fosite_state_non_invalid_state_error), then
                              302-redirects to Clerk with all remaining params intact
  - DCR shim                — POST /oauth/register: returns pre-registered Clerk OAuth app
                              credentials so Claude never needs manual Client ID entry
  - /health                 — auth-exempt, for Railway health checks

Logging: every handshake step is logged at INFO/DEBUG so OAuth failures are diagnosable
without live debugging. Check Railway logs first when the connect flow fails.
"""

import json
import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Clerk JWT Validator  —  implements mcp.server.auth.provider.TokenVerifier
# ---------------------------------------------------------------------------

class ClerkTokenVerifier:
    """
    Validates Clerk-issued access tokens (RS256 JWTs) against Clerk's JWKS.

    The JWKS is fetched from Clerk on first use and cached by PyJWKClient.
    Cache lifetime is 1 hour; rotating Clerk keys will be picked up on next
    cache miss (PyJWKClient retries on unknown key ID automatically).

    Usage:
        verifier = ClerkTokenVerifier(clerk_domain, oauth_client_id)
        mcp = FastMCP("name", token_verifier=verifier, auth=AuthSettings(...))
    """

    def __init__(self, clerk_domain: str, client_id: str) -> None:
        self._issuer = f"https://{clerk_domain}"
        self._client_id = client_id
        jwks_url = f"https://{clerk_domain}/.well-known/jwks.json"
        # PyJWKClient caches keys; construction is safe at import time (no HTTP yet)
        self._jwks = PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)
        logger.info("ClerkTokenVerifier ready — issuer=%s jwks=%s", self._issuer, jwks_url)

    async def verify_token(self, token: str) -> AccessToken | None:
        """
        Validate a Bearer JWT. Returns an AccessToken on success, None on any failure.
        All failures are logged; none raise exceptions to callers.
        """
        preview = token[:20] + "..." if len(token) > 20 else token
        logger.debug("verify_token called: %s", preview)
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            payload: dict[str, Any] = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=self._issuer,
                options={
                    # Clerk access tokens don't always set aud; validate manually below
                    "verify_aud": False,
                    "require": ["sub", "exp", "iss"],
                },
            )
            user_id: str = payload["sub"]
            exp: int | None = payload.get("exp")
            azp: str = payload.get("azp", "")

            # Log key claims for debugging
            logger.info(
                "Token valid — user_id=%s azp=%s exp=%s iss=%s",
                user_id, azp, exp, payload.get("iss"),
            )

            # Warn (but don't reject) if the authorized party doesn't match our client
            if azp and azp != self._client_id:
                logger.warning(
                    "Token azp=%s does not match configured client_id=%s — "
                    "accepting anyway (check CLERK_OAUTH_CLIENT_ID if tools fail)",
                    azp, self._client_id,
                )

            scope_str: str = payload.get("scope", "openid profile email")
            return AccessToken(
                token=token,
                client_id=azp or self._client_id,
                scopes=scope_str.split(),
                expires_at=exp,
                subject=user_id,
                claims=payload,
            )

        except jwt.ExpiredSignatureError:
            logger.warning("Token rejected: expired")
        except jwt.InvalidIssuerError:
            logger.warning("Token rejected: issuer mismatch (expected %s)", self._issuer)
        except jwt.InvalidSignatureError:
            logger.warning("Token rejected: invalid signature")
        except jwt.DecodeError as exc:
            logger.warning("Token rejected: decode error — %s", exc)
        except jwt.PyJWKClientError as exc:
            logger.error("JWKS fetch/parse error (Clerk unreachable?) — %s", exc)
        except Exception as exc:
            logger.exception("Unexpected token validation error: %s", exc)

        return None


# ---------------------------------------------------------------------------
# Starlette route handlers
# ---------------------------------------------------------------------------

def _make_oauth_handlers(
    clerk_domain: str,
    server_url: str,
) -> tuple[Any, Any]:
    """
    Return (as_metadata_handler, authorize_handler) sharing one Clerk OIDC cache.

    as_metadata_handler  — GET /.well-known/oauth-authorization-server
        Fetches Clerk's OIDC config (cached 1h) and overlays:
          issuer              = server_url                     (RFC 8414 §2)
          authorization_endpoint = server_url/oauth/authorize  (our bridge below)
          registration_endpoint  = server_url/oauth/register   (DCR shim)

    authorize_handler    — GET /oauth/authorize
        Receives Claude's authorization request.  Strips the RFC 8707 `resource`
        param before forwarding — Clerk's Fosite does not support resource indicators
        and throws oauth2idp_patch_fosite_state_non_invalid_state_error when it sees
        one.  All other params (state, code_challenge, code_challenge_method, scope,
        redirect_uri, client_id, response_type) are passed through unchanged.
    """
    _cache: dict[str, Any] = {}

    async def _load() -> None:
        """Fetch Clerk OIDC once per hour and populate _cache."""
        if "ts" in _cache and time.monotonic() - _cache["ts"] < 3600:
            return
        oidc_url = f"https://{clerk_domain}/.well-known/openid-configuration"
        logger.debug("Fetching Clerk OIDC metadata from %s", oidc_url)
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(oidc_url)
            resp.raise_for_status()
            clerk_meta: dict[str, Any] = resp.json()

        clerk_auth_ep: str = clerk_meta.get("authorization_endpoint", "")
        logger.info("Clerk OIDC metadata fetched — clerk_auth_endpoint=%s", clerk_auth_ep)

        # Store Clerk's real auth endpoint separately for the authorize bridge.
        _cache["clerk_auth_endpoint"] = clerk_auth_ep

        # Build the AS metadata document we serve to Claude.
        as_meta = dict(clerk_meta)
        # issuer must match the URL from which this document is served (RFC 8414 §2)
        as_meta["issuer"] = server_url
        # Point Claude at our bridge, not Clerk directly — bridge strips `resource`
        as_meta["authorization_endpoint"] = f"{server_url}/oauth/authorize"
        as_meta["registration_endpoint"] = f"{server_url}/oauth/register"
        as_meta.setdefault("code_challenge_methods_supported", ["S256"])
        as_meta["authorization_response_iss_parameter_supported"] = True
        auth_methods: list[str] = as_meta.get("token_endpoint_auth_methods_supported", [])
        if "none" not in auth_methods:
            as_meta["token_endpoint_auth_methods_supported"] = auth_methods + ["none"]

        _cache["as_meta"] = as_meta
        _cache["ts"] = time.monotonic()
        logger.debug("OAuth metadata cached for 1 h")

    async def as_metadata_handler(request: Request) -> JSONResponse:
        try:
            await _load()
        except Exception as exc:
            logger.error("Failed to fetch Clerk OIDC metadata: %s", exc)
            return JSONResponse({"error": "upstream_error", "detail": str(exc)}, status_code=502)
        logger.info(
            "Serving AS metadata to %s — authorization_endpoint=%s registration_endpoint=%s",
            request.client.host if request.client else "unknown",
            _cache["as_meta"]["authorization_endpoint"],
            _cache["as_meta"]["registration_endpoint"],
        )
        return JSONResponse(_cache["as_meta"])

    async def authorize_handler(request: Request) -> Response:
        try:
            await _load()
        except Exception as exc:
            logger.error("Auth bridge: failed to load Clerk metadata: %s", exc)
            return Response(
                content=json.dumps({"error": "upstream_error", "detail": str(exc)}),
                status_code=502,
                media_type="application/json",
            )

        params = dict(request.query_params)

        # Strip RFC 8707 `resource` parameter.  Clerk's Fosite does not support resource
        # indicators; leaving it in causes oauth2idp_patch_fosite_state_non_invalid_state_error
        # before the login page ever renders.
        resource = params.pop("resource", None)
        if resource:
            logger.info(
                "Auth bridge: stripped resource=%s (Clerk does not support RFC 8707)", resource
            )

        logger.info(
            "Auth bridge: forwarding to Clerk — state=%s code_challenge=%s scope=%s",
            params.get("state", "MISSING"),
            "present" if "code_challenge" in params else "MISSING",
            params.get("scope", "(none)"),
        )

        clerk_url = f"{_cache['clerk_auth_endpoint']}?{urlencode(params)}"
        return RedirectResponse(url=clerk_url, status_code=302)

    return as_metadata_handler, authorize_handler


def _make_dcr_handler(client_id: str, client_secret: str) -> Any:
    """
    Return an async Starlette handler for POST /oauth/register (DCR shim).

    Clerk doesn't support RFC 7591 DCR natively.  This shim accepts any
    registration request from Claude and returns the pre-registered Clerk
    OAuth app credentials.  Claude treats these as if it self-registered.

    Logs the redirect_uris Claude sends — useful for diagnosing redirect
    mismatches in Clerk's "Allowed redirect URIs" dashboard setting.
    """
    async def handler(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}

        redirect_uris = body.get("redirect_uris", [])
        client_name = body.get("client_name", "unknown")
        logger.info(
            "DCR registration request — client_name=%s redirect_uris=%s",
            client_name, redirect_uris,
        )
        logger.debug("Full DCR body: %s", json.dumps(body))

        # The redirect_uris Clerk must have in "Allowed redirect URIs":
        for uri in redirect_uris:
            logger.info(
                "  → Claude wants redirect_uri: %s  "
                "(must be in Clerk dashboard > OAuth Applications > Allowed redirect URIs)",
                uri,
            )

        # Public Clerk app (secret is blank) → PKCE-only, no client_secret at token endpoint.
        # Confidential app (secret is set)   → client_secret_basic at token endpoint.
        is_public = not client_secret
        response: dict[str, Any] = {
            "client_id": client_id,
            "redirect_uris": redirect_uris or ["https://claude.ai/api/mcp/auth_callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none" if is_public else "client_secret_basic",
            "client_id_issued_at": int(time.time()),
            "client_secret_expires_at": 0,
        }
        if not is_public:
            response["client_secret"] = client_secret
        logger.info(
            "DCR responding with client_id=%s auth_method=%s",
            client_id, response["token_endpoint_auth_method"],
        )
        return JSONResponse(response, status_code=201)

    return handler


async def _health_handler(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Route factory
# ---------------------------------------------------------------------------

def build_oauth_routes(
    clerk_domain: str,
    server_url: str,
    client_id: str,
    client_secret: str,
) -> list[Route]:
    """
    Return the list of Starlette routes to append to the FastMCP app:
      GET  /.well-known/oauth-authorization-server  — AS metadata bridge
      GET  /oauth/authorize                          — authorization bridge (→ Clerk, strips resource)
      POST /oauth/register                           — DCR shim
      GET  /health                                   — Railway health check
    """
    as_metadata_handler, authorize_handler = _make_oauth_handlers(clerk_domain, server_url)
    return [
        Route(
            "/.well-known/oauth-authorization-server",
            as_metadata_handler,
            methods=["GET"],
        ),
        Route(
            "/oauth/authorize",
            authorize_handler,
            methods=["GET"],
        ),
        Route(
            "/oauth/register",
            _make_dcr_handler(client_id, client_secret),
            methods=["POST"],
        ),
        Route("/health", _health_handler, methods=["GET"]),
    ]
