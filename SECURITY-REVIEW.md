# Security Review: Authentication and Authorization

## Finding

| # | Severity | File | Lines | Vulnerability | Confidence |
|---|----------|------|-------|---------------|------------|
| 1 | HIGH | [src/api/webapp.py](src/api/webapp.py) | `/auth/send-otp` handler | **Remediated for production.** OTPs are not returned by default; accounts without a matching saved email cannot request one; failed delivery invalidates the pending OTP. | 10/10 |
| 2 | HIGH | [src/auth/access.py](src/auth/access.py), [src/api/client.py](src/api/client.py) | order placement | **Addressed.** Every account had full trading rights. Access levels (view / trade / agentic) are now checked at the single broker-order chokepoint, not only in the API. | 9/10 |
| 3 | HIGH | [src/api/shared_market.py](src/api/shared_market.py) | shared market data | **Designed in.** Sharing one account's market data with view-only users must not expose that account's data, credentials or order updates. | 9/10 |

## Evidence and impact

The original issue was that the public `/auth/send-otp` endpoint returned generated OTPs when email delivery failed and allowed requests for users with no saved email. The fix now requires a matching saved user email and sends the OTP to that address. Failed delivery invalidates the pending OTP by default. An explicitly enabled development fallback can return the code only when `APP_ENV=development`, `OTP_DEV_FALLBACK_ENABLED=true`, and the direct client address is loopback. Never enable that fallback in production.

## Recommended remediation

Keep SMTP credentials in environment configuration, use TLS, and monitor delivery failures. Rate limits are currently process-local; use a shared rate-limit store if deploying multiple workers or instances.

## Access levels (finding 2)

Orders carry an origin (`manual`, `risk`, `automated`; default `automated`) set by the code that creates them and carried through the order queue. `KiteClient.place_order` / `modify_order` call the account's guard before any request: `view` places nothing, `trade` places manual and protective orders only, `agentic` places all. A code path that forgets to mark its orders is treated as automated, so the failure mode is refusal, not escalation. The API refuses actions that would start automated trading below `agentic` with HTTP 403. Covered by `tests/test_access_levels.py`.

## Shared market data (finding 3)

Controls, each covered by `tests/test_shared_market.py`:

- **Deny by default.** The viewer client defines only market-data methods (forwarded) and account methods (answered from the viewer's own empty account). There is no fallthrough to the source client.
- **Input validation.** Exchange, instrument keys, interval and dates are validated before forwarding, so a viewer cannot steer a request to another Kite endpoint (e.g. `../user/profile`).
- **No credentials.** The viewer authenticator holds no token and cannot log in, log out or invalidate the source session.
- **No order updates.** The shared WebSocket never forwards Kite's order messages; viewers get only ticks for instruments they subscribed to.
- **No orders.** Every order and GTT call raises `AccessDenied`; shared sessions are `view` regardless of the account's configured level.
- **No identity leaks.** Errors are replaced by a generic message (details only in the server log); `/api/status` never names the source account.
- **No spoofing.** The served account always comes from the server-side session, never from the request; shared ids (`VIEW-<ID>`) cannot collide with Zerodha IDs; the source account is never served to itself.
- **No starvation.** Viewers together get half of each source rate limit, behind a short cache with request de-duplication and subscription caps.

Residual: sharing live exchange data with other people may be restricted by Kite Connect and exchange terms. Leave `MARKET_DATA_ACCOUNT` empty until that is confirmed.
