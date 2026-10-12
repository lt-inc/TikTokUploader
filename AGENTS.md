# Repository instructions

Implement only the TikTok account/session and publishing worker. Terex owns scheduling, admission, publications and history; infrastructure belongs to lt-inc/k3s.

Write tests only for critical invariants: session identity, attempt fencing and uncertain publish outcomes. Never blindly repeat a possibly submitted publication. Do not log cookies, bearer tokens or proxy credentials.

Preserve upstream license, attribution and third-party notices for adapted autotok code.
