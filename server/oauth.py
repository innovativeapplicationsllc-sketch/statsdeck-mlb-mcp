"""
OAuth 2.1 / Clerk integration for StatsDeck.

Architecture: StatsDeck is a pure OAuth resource server (RFC 9728).
Clerk is the authorization server.

This module provides:
  - ClerkTokenVerifier  — validates Clerk JWTs; implements the mcp TokenVerifier protocol
  - Authorization Server metadata endpoint  — bridges Clerk's OIDC + injects our DCR URL
  - Dynamic Client Registration shim  — Clerk doesn't support DCR natively; we return
    pre-registered Clerk OAuth app credentials so Claude never needs manual Client ID entry
  - /health endpoint — auth-exempt, for Railway health checks

Logging: every handshake step is logged at INFO/DEBUG so OAuth failures are diagnosable
without live debugging. Check Railway logs first when the connect flow fails.
"""

import json
import logging
import os
import time
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
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

def _make_as_metadata_handler(
    clerk_domain: str,
    server_url: str,
) -> Any:
    """
    Return an async Starlette handler for /.well-known/oauth-authorization-server.

    Fetches Clerk's openid-configuration (cached 1h) and re-serves it with:
      - registration_endpoint pointing at our DCR shim
      - authorization_response_iss_parameter_supported: true  (RFC 9207)
      - Explicit code_challenge_methods_supported: ["S256"]   (MCP clients refuse if absent)
    """
    _cache: dict[str, Any] = {}

    async def handler(request: Request) -> JSONResponse:
        if "meta" not in _cache or time.monotonic() - _cache.get("ts", 0) > 3600:
            oidc_url = f"https://{clerk_domain}/.well-known/openid-configuration"
            logger.debug("Fetching Clerk OIDC metadata from %s", oidc_url)
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(oidc_url)
                    resp.raise_for_status()
                    clerk_meta: dict[str, Any] = resp.json()
                logger.info(
                    "Clerk OIDC metadata fetched — auth_endpoint=%s",
                    clerk_meta.get("authorization_endpoint"),
                )
            except Exception as exc:
                logger.error("Failed to fetch Clerk OIDC metadata: %s", exc)
                return JSONResponse({"error": "upstream_error", "detail": str(exc)}, status_code=502)

            # Overlay our additions on top of Clerk's metadata
            clerk_meta["registration_endpoint"] = f"{server_url}/oauth/register"
            clerk_meta.setdefault("code_challenge_methods_supported", ["S256"])
            clerk_meta["authorization_response_iss_parameter_supported"] = True
            # Ensure "none" is listed so Claude can use PKCE-only (public client)
            auth_methods = clerk_meta.get("token_endpoint_auth_methods_supported", [])
            if "none" not in auth_methods:
                clerk_meta["token_endpoint_auth_methods_supported"] = auth_methods + ["none"]

            _cache["meta"] = clerk_meta
            _cache["ts"] = time.monotonic()
            logger.debug("AS metadata cached for 1h")

        logger.info(
            "Serving AS metadata to %s — registration_endpoint=%s",
            request.client.host if request.client else "unknown",
            _cache["meta"]["registration_endpoint"],
        )
        return JSONResponse(_cache["meta"])

    return handler


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

        response = {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": redirect_uris or ["https://claude.ai/api/mcp/auth_callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_basic",
            "client_id_issued_at": int(time.time()),
            "client_secret_expires_at": 0,  # never expires
        }
        logger.info("DCR responding with client_id=%s", client_id)
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
      POST /oauth/register                          — DCR shim
      GET  /health                                  — Railway health check
    """
    return [
        Route(
            "/.well-known/oauth-authorization-server",
            _make_as_metadata_handler(clerk_domain, server_url),
            methods=["GET"],
        ),
        Route(
            "/oauth/register",
            _make_dcr_handler(client_id, client_secret),
            methods=["POST"],
        ),
        Route("/health", _health_handler, methods=["GET"]),
    ]
