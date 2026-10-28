# Third-party notices

autotok is licensed under the GNU Affero General Public License v3.0 (see
`LICENSE`). The files listed below are **not** covered by that licence. They are
redistributed as-is because they are needed to produce the request signatures
TikTok's web uploader expects, and remain the property of their respective
owners.

| File | Origin |
|---|---|
| `autotok/signer_js/signer.js` | Signature routine ("byted_acrawler"), as distributed by the [carcabot/tiktok-signature](https://github.com/carcabot/tiktok-signature) project (MIT). Originally derived from TikTok's web client. |
| `autotok/signer_js/webmssdk.js` | Modified copy of TikTok's `webmssdk.js` (originally from `lf3-cdn-tos.bytescm.com`), shared in [carcabot/tiktok-signature#140](https://github.com/carcabot/tiktok-signature/issues/140#issuecomment-1194196455). |
| `autotok/signer_js/xbogus.js` | X-Bogus routine extracted from TikTok's web client, as circulated in the tiktok-signature community. |

The original Node.js glue code from carcabot/tiktok-signature (MIT, © CarcaBot)
has been replaced by a Python port in `autotok/signer.py`.

If you are a rights holder and want a file removed, please open an issue.

## TikTok Studio UI selector reference

The optional `terex_tiktok/studio.py` implementation uses UI selectors researched
from [wkaisertexas/tiktok-uploader](https://github.com/wkaisertexas/tiktok-uploader),
commit `1cea92dd4c815dbd5127ee4b3a0a7b6fabbaa5f0`. Its uploader is not installed or
vendored; the Terex session lifecycle, request gate and verification are separate
implementations. The reference project's MIT notice is retained below.

Copyright 2024 William Kaiser

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the “Software”), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
