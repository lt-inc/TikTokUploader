# Adapted upstream core

The `src/autotok` package is adapted from makiisthenes/TiktokAutoUploader,
release v2.0.1, commit `08bfaea493b35df4e1477c3999ac25f414267634` (2026-10-03).
Original author: Michael Peres. Core license: AGPL-3.0-only.

LT-Inc changes (2026-10-13): split preparation, media transfer, request preparation
and final submission; durable barrier/cancellation guard; classify ambiguous
server responses; pinned Phantomwright browser runtime; Terex caption preparation
keeps email addresses and @names as plain text without a verified mention mapping.
Browser session routing cookies are preserved without an invented datacenter.
LT-Inc change (2026-10-16): ApplyUploadInner mirrors TikTok Studio's request: a fresh
random `s` per upload (upstream: constant) plus `device_platform=web`,
`business_tag=tiktok_video_submission_web`, `ClientBestHosts` and `X-Amz-Expires`.
LT-Inc change (2026-10-16): ApplyUploadInner is sent with Studio's headers over a Chrome
TLS/HTTP2 fingerprint (`curl_cffi`) and an own SigV4 signer (`autotok/impersonate.py`);
upstream's plain `requests` call is answered with CodeN 30411. CommitUploadInner keeps
the upstream `requests` signer.
LT-Inc change (2026-10-27): preserve the scoped upload cookie jar, including
server-side cookie rotation/deletion, across requests and curl_cffi; do not
follow redirects on the signed ApplyUploadInner request. Preserve structured
upload error codes and Retry-After timing without logging response bodies or
network exception text. The worker applies a durable per-account cooldown only
before the final submission; uncertain results remain fenced from repeat sends.
LT-Inc change (2026-10-27): add an independent, optional local Studio UI backend
in `terex_tiktok/studio.py`. The site performs upload/signing; a request gate
validates metadata, checkpoints the native creation ID and permits one publish
request. Existing worker ownership, session fencing and post reconciliation apply.
LT-Inc change (2026-10-28): add encrypted per-account proxy overrides with
server-default/direct/custom modes, apply the route to identity, browser, upload
and reconciliation, and fence changed routes through session versioning. Direct
HTTP sessions do not inherit an ambient proxy. Delayed failed session checks
cannot overwrite a newer connection or proxy selection.
The original CLI,
MCP server, web application and scheduler are not included. Terex owns the
calendar, metadata, attempt authorization and publication history.

LT-Inc change (2026-10-29): identity fallback preserves cookie scopes and expiry;
extension exports the active tab cookie store and reports rejected sessions
explicitly. Identity status parsing fails closed and diagnostics exclude remote
text. Upload diagnostics correlate account/session/route/attempt and bounded
provider response metadata without storing credentials or response bodies.
Live investigation found route-dependent session acceptance; the existing HTTP
transport completed a media transfer for the repaired account. No transport
default was changed and no final publication was sent by the diagnostic.

Preserve LICENSE and THIRD_PARTY_NOTICES.md. Three `signer_js` assets retain
their separately documented origins/rights and are not uniformly covered by
the core AGPL declaration. This service offers its corresponding source through
the repository URL. Repository updates must preserve notices and document the
applied upstream changes; do not blindly upgrade the private protocol.
