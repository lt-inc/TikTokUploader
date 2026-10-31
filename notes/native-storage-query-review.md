# Native storage query (`uploadid`) review

Read-only research for the blocked native storage POST
`POST https://{UploadHost}/upload/v1/{StoreUri}?device_platform=web&uploadid=...`.
No credentialed request, browser session or source change was used. No SDK
content with auth/session data is quoted here.

## Verdict

* **Not proven.** I found no primary source that defines how the TikTok Studio
  SDK derives `uploadid`, nor one that shows whether the first `/upload/v1/` POST
  is an init or the real transfer. The Studio upload SDK is not in the public
  static assets (see "Sources checked").
* Measured live (probes on `b2cd77b` / `3d89f14`, shape only): path
  `/upload/v1/[seg]/[seg]` is a registered Apply object, query keys are exactly
  `{device_platform, uploadid}`, `platform=web`, and `uploadid` did **not** equal
  `StoreInfos[].UploadID` (`uploadid_match=0`). Race ordering is fixed (Apply was
  registered before storage).
* Best-supported hypothesis: `uploadid` is **client-generated** by the SDK. This
  is an inference from `uploadid != Apply UploadID` plus the analogue in autotok
  (below), not from a source of the native SDK.

## Evidence (non-secret citations)

1. `src/autotok/uploader.py:_transfer`, line 407: `upload_id = str(uuid.uuid4())`.
   The legacy multipart client mints its own id; it never uses the Apply
   `StoreInfos[].UploadID`. Parts (line 420):
   `https://{host}/{store_uri}?partNumber={n}&uploadID={id}&phase=transfer`;
   finish (line 441): `...?uploadID={id}&phase=finish&uploadmode=part`;
   `CHUNK_SIZE = 5 MiB` (line 42). Upstream attribution: makiisthenes
   TiktokAutoUploader v2.0.1 (see `UPSTREAM.md`).
   This is a different route (`/{store_uri}`, camelCase `uploadID`/`partNumber`,
   `phase`) than the native one (`/upload/v1/{store_uri}`, lowercase `uploadid`,
   `device_platform`). It shows the server accepts a client-chosen id on the
   older route; it does not prove the same on `/upload/v1/`.
2. Public npm `tt-uploader@1.6.2` (`dist/index.js`, ByteDance veVod/veImageX web
   uploader, not TikTok Studio's bundled SDK): URL templates
   `{tosDomain}/{oid}?uploads`, `?partNumber={n}&uploadID={id}`,
   `?uploadID={id}`, `{tosDomain}/{oid}`. There the id is **server-issued** by the
   `?uploads` init (`payload.uploadID`), with the Apply `StoreInfo.UploadID`
   carried separately (`e.UploadID=u.StoreInfo.UploadID`, used for fallback
   nodes). It also has a "direct upload" mode (single POST to
   `{tosDomain}/{oid}`) and a `gatewayUpload` header (`x-storage-mode: gateway`).
   So in a related ByteDance SDK, Apply `UploadID` and the multipart/TOS
   `uploadID` are different identifiers, which is consistent with the measured
   mismatch. It has no `/upload/v1/`, `device_platform` or lowercase `uploadid`.
3. Gate code (current candidate): `src/terex_tiktok/studio.py`
   `_PublishGate._allowed_preparation` (line ~187) allows native storage only for
   exact `(authenticated UploadHost, /upload/v1/{StoreUri})` with **no query**;
   `storage_for` / `uploads_for` (lines 145/133) build the registered set from the
   in-memory-validated Apply body; `_storage_shape` (line 164) is the value-free
   diagnostic.

## Sources checked

* Public Studio shell + `creator-center-web` async chunks (108 chunks, ~3.8 MB):
  no `upload/v1`, `uploadid` or `ApplyUploadInner`. The upload UI is a
  module-federation remote (`creator_center`/`web-creation`); its chunks were not
  reachable from the public HTML, and I did not go through authenticated pages.
* GitHub code search for `"upload/v1" uploadid device_platform tiktok` and
  wkaisertexas/tiktok-uploader `upload/v1`: no hits. wkaisertexas uses browser
  automation, not the storage protocol. autotok 2.x only has the legacy route above.
* A generic web search returned plausible-looking but unsourced parameter lists
  (`part_number`, `phase=finalize`); they are disregarded as not evidence.

## Is a bounded client uuid sufficient?

Authorization-wise, yes if it is bounded, because `uploadid` carries no authority:
write permission comes from the Apply `Auth` header plus the exact
host/StoreUri, which the browser holds and the gate only checks. The Commit
`SessionKey`/`Vid` pairing and the final Publish fence do not depend on the
storage query. Allowing a query therefore adds no ability to name a different
object, host or video. Residual exposure, none of it new in kind:

* Any registered object (image **and** video) accepts the request. Same as today.
* The request body is not inspected (also true now).
* An id chosen by page JS can collide with another upload on the same object; this
  only damages that object, and Commit/Vid fences still reject it.
* More POSTs per object than a single transfer (multipart/retry).

## Recommended allowlist (if root decides to widen)

Only for `POST`, `https`, exact registered `(host, /upload/v1/{StoreUri})`:

1. Query keys exactly `{device_platform, uploadid}`, each once, no fragment,
   no duplicates (`parse_qs(..., keep_blank_values=True, strict_parsing=True)`).
2. `device_platform == "web"`.
3. `uploadid` shape-bounded: `^[A-Za-z0-9_-]{8,64}$` (covers UUID with dashes,
   hex, base64url). Do not require equality with `StoreInfos[].UploadID`
   (measured mismatch).
4. Pin one `uploadid` per `(host, path)` on first sight and reject a second
   different one; cap requests per object (e.g. 64) so a multipart upload is
   possible but unbounded loops are not.
5. Do **not** admit `phase`, `partNumber`/`part_number`, `uploadmode`,
   PUT or extra keys until an observed block shows they are needed. The one
   measured native request used only the two keys above, so nothing more is
   justified by evidence.
6. Keep everything else unchanged: authenticated Apply only, in-memory CDP
   validation, image/video separation, Commit/Publish fences, no logging of
   urls/ids/bodies.

## Uncertainty / next measurement

* Whether this POST is init, single-shot transfer, or the first of several parts
  is **unknown**. The 5 s synthetic video is small enough for a single-part
  upload, so one request is plausible, but not proven.
* Whether an `uploadid` is later expected back from a response (server-issued, as
  in `tt-uploader`) is unknown; the mismatch with Apply `UploadID` only says it is
  not that value. `uid_class`/`uid_len`/`uploadid_any_object` from probe 3 were
  cut off and should be captured (shape only) in the next bounded probe.
* A bounded probe that allows the request and logs only value-free shape of each
  subsequent storage request (method, key set, status, count) would settle
  init-vs-transfer and whether `phase`/part fields exist. Root owns that probe.
