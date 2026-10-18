# Security Review: Authentication and Authorization

## Finding

| # | Severity | File | Lines | Vulnerability | Confidence |
|---|----------|------|-------|---------------|------------|
| 1 | HIGH | [src/api/webapp.py](src/api/webapp.py) | `/auth/send-otp` handler | **Remediated for production.** OTPs are not returned by default; accounts without a matching saved email cannot request one; failed delivery invalidates the pending OTP. | 10/10 |

## Evidence and impact

The original issue was that the public `/auth/send-otp` endpoint returned generated OTPs when email delivery failed and allowed requests for users with no saved email. The fix now requires a matching saved user email and sends the OTP to that address. Failed delivery invalidates the pending OTP by default. An explicitly enabled development fallback can return the code only when `APP_ENV=development`, `OTP_DEV_FALLBACK_ENABLED=true`, and the direct client address is loopback. Never enable that fallback in production.

## Recommended remediation

Keep SMTP credentials in environment configuration, use TLS, and monitor delivery failures. Rate limits are currently process-local; use a shared rate-limit store if deploying multiple workers or instances.
