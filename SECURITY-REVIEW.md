# Security Review: Authentication and Authorization

## Finding

| # | Severity | File | Lines | Vulnerability | Confidence |
|---|----------|------|-------|---------------|------------|
| 1 | HIGH | [src/api/webapp.py](src/api/webapp.py) | 170-174; [src/auth/otp_auth.py](src/auth/otp_auth.py) 151-153, 184-186 | When email delivery is unavailable, the public OTP endpoint returns the generated OTP in its response. A requester can use it to obtain an authenticated session. If no email is configured for the account, the submitted email is not matched to a configured address. | 10/10 |

## Evidence and impact

`/auth/send-otp` is public. After generating an OTP, it returns the OTP to the caller when `send_otp_email` reports failure. That function reports failure when SMTP credentials are missing or email delivery raises an exception. The public `/auth/verify-otp` endpoint accepts the OTP and creates a session cookie. As a result, an unauthenticated requester who knows a registered Zerodha ID can authenticate when delivery is unavailable. The send endpoint also skips email matching when that user's configured email is empty.

## Recommended remediation

Fail closed when OTP delivery is unavailable: never return an authentication code in an API response. Require a configured, verified email address before issuing an OTP, and ensure that address is the one used for delivery.
