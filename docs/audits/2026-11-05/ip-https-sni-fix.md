# Persistent HTTPS fallback for clients without SNI

Baseline: `7511d2f91cf86bbc094276e0e4632129daeed655`, branch `ccr-345833d2-sca9ob`.

The tracked Caddyfile lacked `default_sni`. IP clients typically omit the TLS SNI
extension; Caddy 2.8 could therefore fail certificate selection before HTTP routing.
Skipping certificate verification does not repair that handshake failure.

The tracked web image now starts through `infra/caddy/entrypoint.sh`, which derives
`XAGENT_DEFAULT_SNI` from the host of `XAGENT_SITE_ADDRESS` when the override is
empty or unset. Schemes, ports, and IPv6 brackets are stripped. Caddy uses the bare
host as its global `default_sni`. Domain-based sites and clients that send SNI keep
their normal configuration and routing. No production address is hardcoded.

Examples:

```dotenv
# IP installation; internal CA is NOT publicly trusted.
XAGENT_SITE_ADDRESS=https://192.0.2.10
XAGENT_TLS=internal
XAGENT_DEFAULT_SNI=
```

```dotenv
# Domain installation; existing ACME/email configuration remains unchanged.
XAGENT_SITE_ADDRESS=agent.example.com
XAGENT_TLS=you@example.com
XAGENT_DEFAULT_SNI=
```

An explicit override must be a bare certificate host (no scheme or port). The
installer documents the optional setting; Compose passes it to the web container.
Existing `.env` files do not need migration: the entrypoint derives the fallback
even when they have no new setting. The updater builds the tracked Dockerfile and
Caddyfile, and keeps using the existing shared `.env`. Updater scripts, image tag
selection, backup/rollback logic, database schemas and application code are unchanged.

## Verification

- Seven parametrized host-derivation regressions cover IP, domain, ports, IPv6,
  explicit override and localhost fallback.
- Full local Python suite: **333 passed, 0 failed, 0 skipped** (326 existing tests
  plus seven new cases), with system Chromium running the real-browser regressions.
  Python lint/compilation, shell syntax and whitespace checks passed. The current
  frontend production build and the complete web image build passed.
- `python3 tests/caddy_tls.py --image IMAGE` exercises the **actual built web image**
  in uniquely named disposable containers on loopback ports, with no VPS contact.
  This regression is also wired into the existing image-build CI job.
- Actual `caddy validate` and JSON adaptation verify the effective `default_sni`.
- Python TLS sockets use `server_hostname=None` to explicitly omit client SNI.
  IP, domain and explicit-override scenarios negotiate TLS and serve the built SPA,
  an SPA deep link, the API proxy, `/api/internal/*` rejection and service-worker
  cache headers through the published HTTPS listener. The API upstream is a tiny
  local static HTTP fixture, not a claim about the production backend.
- The domain scenario also verifies ordinary domain SNI. Restart repeats the
  no-SNI handshake and SPA request using the retained disposable CA storage.
- System-root verification rejects the fresh internal CA as expected; explicit
  trust of that disposable CA succeeds, including host verification. This proves
  TLS negotiation separately from certificate trust. Public ACME issuance is not
  tested by an internal-CA fixture.
- An image with the **original baseline Caddyfile** reproduces
  `TLSV1_ALERT_INTERNAL_ERROR` without SNI even with verification disabled. The
  patched image passes the equivalent probe.

The local full image build used a temporary Dockerfile with a BuildKit CA-secret
mount in the npm download step for this managed environment's proxy. TLS checking
remained enabled; the runtime Dockerfile instructions and repository source were
otherwise unchanged. The CA was not copied into runtime layers or repository files.

No deployment or VPS changes were made. Raw test/build evidence remains outside
the checkout under `/workspace/xagent-audit-results/sni-*`. Full suite outcomes and
the commit are reported with the delivery; the existing browser repairs are preserved.
