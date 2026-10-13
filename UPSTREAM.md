# Adapted upstream core

The `src/autotok` package is adapted from makiisthenes/TiktokAutoUploader,
release v2.0.1, commit `08bfaea493b35df4e1477c3999ac25f414267634` (2026-10-03).
Original author: Michael Peres. Core license: AGPL-3.0-only.

LT-Inc changes (2026-10-13): split preparation, media transfer, request preparation
and final submission; durable barrier/cancellation guard; classify ambiguous
server responses; pinned Phantomwright browser runtime. The original CLI,
MCP server, web application and scheduler are not included. Terex owns the
calendar, metadata, attempt authorization and publication history.

Preserve LICENSE and THIRD_PARTY_NOTICES.md. Three `signer_js` assets retain
their separately documented origins/rights and are not uniformly covered by
the core AGPL declaration. This service offers its corresponding source through
the repository URL. Repository updates must preserve notices and document the
applied upstream changes; do not blindly upgrade the private protocol.
