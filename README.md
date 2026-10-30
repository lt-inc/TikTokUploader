# TikTokUploader for Terex

Separate Python 3.12 service for TikTok sessions and publication execution. Terex
owns flows, the common calendar, rendered revisions, metadata and publication
history. This service accepts only an authorized, ready publication and receives
one temporary media URL; it has no MinIO keys or YouTube credentials.

The HTTP upload core is adapted from autotok 2.0.1. See [UPSTREAM.md](UPSTREAM.md),
[LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). This is an
experimental TikTok web integration, not TikTok's official Content Posting API.
Compatibility and actual private uploads require a stage pilot; unit tests do
not establish current TikTok compatibility or guaranteed reach.

## Connect your account

1. Add a TikTok account in Terex and generate its ten-minute connection code.
2. Install `extension/` as an unpacked extension in Chrome or Edge (developer
   mode on the browser's extension page). Open `https://www.tiktok.com` and log
   in to the intended account normally, including any verification.
3. Click **Terex TikTok Connect**, check the Terex address, paste the connection
   code, and click **Export TikTok session and connect**. Grant access only to
   your own Terex address. Export happens only after this explicit click.
4. Return to Terex and check the account. The server verifies the actual TikTok
   identity before storing a connected session. Reconnection cannot replace a
   previously bound account with another identity.
5. If verification is required, open **Browser verification** in Terex, complete
   the challenge in the temporary browser and press **I have finished**. Return
   to the account and press **Check** to refresh the session version.

The extension reads only TikTok cookies, including HttpOnly session cookies. It
does not save exports or codes on disk and does not inspect other browser
accounts. A consumed/expired code must be regenerated. If the response is lost,
check the account in Terex before exporting again. Logging out/revoking TikTok
sessions can invalidate the connection; reconnect the same account.

## Publishing behavior

Point a flow at the TikTok account, enable short-form outputs and configure its
TikTok slots in Terex. Calendar times determine when Terex starts an upload;
native TikTok scheduling is not used. The ready vertical H.264/AAC MP4 is reused.
Caption is required and is limited to 2,200 characters. Private/public are
supported; music search, custom covers and platform-specific long compilations
are not part of this pilot.

Default HTTP execution: Claim → identity/SHA check → prepare → 5 MiB sequential HTTP transfer
→ persist media ID → prepare final request → durable Terex checkpoint immediately
before final POST → verify the actual post → Complete. One worker and one browser
permit keep resource use bounded. Lease renewals run every twenty seconds. An
expired lease, changed session version or changed account fences further sends.

A transfer ID is not a post ID. After a lost final response, timeout or restart,
the journal permits only read-only reconciliation; the file is not uploaded again.
The authenticated Studio list must identify one exact caption, account and
publication-time match, plus a returned post ID or an exact creation-ID link. Caption/time
alone only identify a candidate and never automatically confirm publication. Missing/ambiguous
evidence stays **needs attention**. Check TikTok and use Terex's reconciliation
action. An absent post in a single list response is not proof that retry is safe.
Only an explicit operator confirmation of absence may authorize a new publication
version for retry; old broker messages and automatic attempts cannot clear uncertainty.

Before submission, HTTP 429 from upload endpoints and ApplyUploadInner's
`CodeN=30411` pause uploads
for the account for at least thirty minutes, or longer when `Retry-After` asks
for it. The encrypted cooldown survives worker restarts and prevents other queued
videos from immediately contacting TikTok. A rate-limit response after the final
send never authorizes an automatic repeat. `30411` alone does not establish
whether the cause is frequency, the account, the network route or web-client
compatibility. See [upload diagnosis and alternatives](docs/UPLOAD-DIAGNOSIS.ru.md).
Deploy the companion Terex change that recognizes `tiktok_account_cooldown` as a
deferral before this worker: waiting for another video's cooldown must not use
up the publication's failure budget. Actual TikTok refusals still use that budget.

### Experimental local Studio browser mode

Set `TIKTOK_UPLOAD_MODE=studio` to use the existing local Phantomwright/Chromium
runtime for the whole upload form. The default remains `http`. Studio's own
JavaScript transfers the video and constructs the publication request; no paid
browser service, third-party posting API or TikTok developer app is required.

The worker imports the verified account's cookies, selects the video, fills the
caption and explicitly selects public/private. It observes the actual upload
IDs, then validates the native publish payload before allowing one final POST.
The durable Terex checkpoint runs in the browser request gate immediately before
that send. Browser retries cannot issue a second final POST. The browser-generated
creation ID replaces the HTTP worker's local project ID before the checkpoint.
The browser is closed before the normal authenticated post reconciliation.

This mode is a stage pilot, not a verified TikTok workaround. UI/protocol changes,
unexpected dialogs, missing upload IDs or mismatched privacy stop the attempt.
Login/challenge screens require the existing Browser verification flow before
another pre-submit attempt. There is no automatic switch between transports after
an uncertain result. Confirm one private post and its visibility before testing
public reach. Both modes respect the same account cooldown and final-send fence.

## Stage deployment and secrets

Infrastructure and image rollout declarations live in **lt-inc/k3s**. This
repository builds/publishes the image; its CI never creates cluster resources.
Use the concrete SHA image `registry.legendary-trio.me/lt-inc/terex-tiktok-uploader:<commit>`.

Required environment:

| Variable | Purpose |
|---|---|
| `TIKTOK_UPLOADER_API_TOKEN` | Main-be → uploader service authentication |
| `TIKTOK_WORKER_TOKEN` | Uploader → main-be scoped TikTok worker authentication |
| `TIKTOK_SESSION_ENCRYPTION_KEY` | Base64url-encoded 32-byte AES-GCM key; preserve while sessions exist |
| `TIKTOK_MAIN_BE_URL` | Internal main-be HTTP address |
| `RABBITMQ_URL` | Dedicated publisher user restricted to `tiktok.workflow.v1.publish` |
| `APP_PUBLIC_URL` | HTTPS Terex origin, used for browser-help links and Origin checks |
| `TIKTOK_MEDIA_HOSTS` | Comma-separated allowlist of actual temporary media URL hosts |

The two service-token variables may map to the same Infisical service token;
main-be still scopes the worker endpoints to TikTok attempts. Never give this
user access to YouTube publication commands or infrastructure credentials.

Runtime paths: `TIKTOK_STATE_DIR=/data/sessions` on a persistent encrypted-state
volume; `TIKTOK_SCRATCH_DIR=/tmp/tiktok` on ephemeral `/tmp`. Session records and
restart journals are AES-GCM encrypted; atomic writes preserve them across
restarts. One publisher holds an exclusive OS ownership lock on this volume for
its entire lifetime. On restart, only acquisition of this lock proves the old
local process ended: its registry operations are then released without clearing
uncertain publication outcomes or re-uploading. Lease expiry alone never releases
a running operation. Deployment uses one owner/Recreate and the local persistent
volume; do not share this state through a filesystem with unreliable file locks.
Scratch is cleaned when each attempt completes/cancels/fails. A media
download is bounded to 512 MiB (`TIKTOK_MAX_MEDIA_BYTES`) and five minutes, with
SHA-256 verification and at least two GiB of spare temporary disk space. Whole
attempt deadline is fifteen minutes. Persistent state is never scratch cleanup.

Optional: `TIKTOK_MEDIA_DOWNLOAD_ORIGIN` (e.g. `http://minio.infra.svc:9000`) is a
trusted, deployment-controlled transport origin for media downloads. It must be an
HTTP(S) origin with a hostname and no credentials, path, query or fragment; invalid
values fail startup without echoing the value. The source URL must still pass the
`TIKTOK_MEDIA_HOSTS` allowlist and carry no credentials. Only its scheme/authority
are replaced; the signed path/query are preserved and the original `Host` header is
sent so the SigV4 signature stays valid. No S3 credentials are added, redirects stay
disabled, and SHA-256, size/time limits and fencing are unchanged. There is no
automatic fallback to the public URL and no extra retry; a failure follows the normal
retryable media-download failure. Download logs include `media_route=internal|public`.
It does not affect TikTok/account proxy routing.

Optional: `TIKTOK_PROXY_URL` is the server's default TikTok route. Each account's
Terex panel can select **server default**, **direct (no proxy)** or **custom
proxy**. The custom URL is encrypted with the session state and is never returned
to the panel; only its scheme/host/port and a configured flag are exposed. Turning
it off preserves the encrypted URL for later reuse. Direct mode also ignores
ambient HTTP(S) proxy variables for TikTok HTTP requests. Identity, browser,
upload and reconciliation operations all use the selected account route.

Save the route before reconnecting or opening Browser verification. A route
change increments the session version and fences an in-progress attempt before
another send; an already submitted post remains subject to reconciliation.
Check the session after changing the route, or reconnect the same account if
TikTok requires it. This setting controls the uploader and its helper browser;
your local Chrome/Edge connection is configured separately.

`TIKTOK_CAPTCHA_API_KEY`, `TIKTOK_CAPTCHA_MODEL_ID`, and
`TIKTOK_CAPTCHA_OBJECT_MODEL_ID` configure **your own** Roboflow credentials/models.
No API keys or weights are bundled. The model helper supports only recognized
image challenges, at most two attempts within sixty seconds; unsupported or
uncertain results go to the same operator browser. No model runs on gpu-be.

Only HTTP 8080 is exposed. Route the exact public connection export to main-be,
and `/api/tiktok/browser-help/` to this service. Every browser-help HTTP/WS asset
request requires its ten-minute, account/version-bound token. WebSocket and
changing requests also validate the Terex Origin. The browser, native VNC and
websockify ports bind locally inside the pod. Do not expose CDP/5900/6080.

`/healthz` is liveness; `/readyz` reports API readiness and broker state separately.
Broker outages do not remove account-connection UI from Service endpoints. The
worker reconnects and reconciles saved unknown outcomes. `/metrics` exposes
bounded metrics for attempt outcome, stages, transfer bytes, active work, session
states, broker connection and operator help. IDs and cookies are not metric
labels. HTTP access logs are disabled so helper tokens and presigned URLs are
not logged. `/source` identifies the servicing revision and corresponding source.

## Local verification

Install `.[test]`, then run `python -m compileall -q src` and `pytest -q`. The
critical checks cover account identity/single-use pairing and encryption, the
durable final-send barrier/expiry, unknown-result redelivery, post verification,
the actual Go/Python HTTP ID/completion/release contract, session continuity
across the upload clients, and account cooldown without repeating a final send.
After installing Chromium with `phantomwright_driver install chromium`, run
`TIKTOK_STUDIO_BROWSER_TESTS=1 pytest -q` to include real local-browser fixtures.
They exercise the Studio form, identity mismatch, rejected checkpoints and
blocked preparation/publication redirects without contacting TikTok.
The production Docker build installs pinned Phantomwright 0.2.0 and driver 1.58.3,
Chromium, Xvfb and the operator helper. It requires a running Linux Docker daemon.

Before enabling public publication on stage, connect one dedicated account and
perform a private video pilot: verify actual identity, caption/visibility, post
ID, lost-answer behavior, lease cancellation and broker restart. Do not infer
posting success from a browser redirect, media ID or provider HTTP 200 alone.
