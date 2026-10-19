from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import smtplib
import ssl
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

import certifi
from dotenv import load_dotenv

from src.utils.logger import get_logger
from src.utils.exceptions import AuthenticationError

# Load .env file explicitly before reading environment variables
load_dotenv()

logger = get_logger(__name__)

# Security constants
OTP_EXPIRY_SECONDS = 300  # 5 minutes
SESSION_EXPIRY_SECONDS = 86400  # 24 hours - Kite access tokens expire daily anyway
STEP_UP_SECONDS = 900  # A fresh OTP unlocks risky actions for 15 minutes
MAX_OTP_ATTEMPTS = 3  # Max incorrect OTP attempts
# Per-account cap on OTP emails (all IPs together). The tighter per-(account, IP)
# limit lives in the web layer, so one IP cannot use up the account's allowance.
MAX_OTP_REQUESTS_PER_HOUR = 20
MAX_OTP_REQUESTS_PER_IP_PER_HOUR = 15  # Per-IP throttle across all Zerodha IDs
MAX_VERIFY_ATTEMPTS_PER_IP_PER_HOUR = 30  # Per-IP throttle for OTP verification
SESSION_FILE = os.path.expanduser("~/.kta_sessions.json")  # Home directory, not /tmp
STEP_UP_PURPOSE = "STEP-UP"


def _hash_token(token: str) -> str:
    """Sessions are stored by SHA-256 so the session file holds no usable cookies."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _is_token_hash(key: str) -> bool:
    return len(key) == 64 and all(c in "0123456789abcdef" for c in key)


class OTPAuth:
    def __init__(self) -> None:
        self._pending_otps: dict[str, dict] = {}
        self._sessions: dict[str, dict] = self._load_sessions()
        self._otp_request_count: dict[str, list[float]] = {}  # Track OTP requests per user
        self._ip_attempts: dict[str, list[float]] = {}  # Track requests per (bucket, ip)
        
        # SESSION_SECRET is REQUIRED in production
        self._secret = os.environ.get("SESSION_SECRET", "")
        if not self._secret:
            logger.warning("security_warning", msg="SESSION_SECRET not set - sessions will be invalidated on server restart")
            self._secret = secrets.token_hex(32)
        
        self._smtp_email = os.environ.get("SMTP_EMAIL", "")
        self._smtp_password = os.environ.get("SMTP_PASSWORD", "")
        self._smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
        self._smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        
        # Secure session file permissions
        self._secure_session_file()
        logger.info("otp_auth_initialized")

    def _secure_session_file(self) -> None:
        """Ensure session file has secure permissions."""
        if os.path.exists(SESSION_FILE):
            try:
                os.chmod(SESSION_FILE, 0o600)  # Read/write owner only
            except Exception as e:
                logger.warning("session_file_chmod_failed", error=str(e))

    def _load_sessions(self) -> dict[str, dict]:
        """Load and validate sessions from file, removing expired ones."""
        try:
            if os.path.exists(SESSION_FILE):
                with open(SESSION_FILE, "r") as f:
                    sessions = json.load(f)
                now = time.time()
                # Drop expired sessions and any stored before tokens were hashed.
                valid = {
                    k: v for k, v in sessions.items()
                    if _is_token_hash(k) and now - v.get("created_at", 0) < SESSION_EXPIRY_SECONDS
                }
                if len(valid) < len(sessions):
                    self._sessions = valid
                    self._save_sessions()
                return valid
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("session_load_failed", error=str(e))
        return {}

    def _save_sessions(self) -> None:
        """Save sessions to file with secure permissions."""
        try:
            # Create parent directory if needed
            os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
            # Write with restricted permissions
            with open(SESSION_FILE, "w") as f:
                json.dump(self._sessions, f)
            os.chmod(SESSION_FILE, 0o600)  # Read/write owner only
        except OSError as e:
            logger.error("session_save_failed", error=str(e))

    @staticmethod
    def _otp_key(zerodha_id: str, purpose: str = "") -> str:
        key = zerodha_id.upper()
        return f"{purpose}:{key}" if purpose else key

    def generate_otp(self, zerodha_id: str = "", purpose: str = "") -> str:
        """Generate OTP with rate limiting.
        
        Args:
            zerodha_id: Zerodha ID to generate OTP for
        
        Returns:
            6-digit OTP string
        
        Raises:
            AuthenticationError: If rate limit exceeded
        """
        now = time.time()
        hour_ago = now - 3600
        
        # Check rate limit: max MAX_OTP_REQUESTS_PER_HOUR requests per hour per user
        if zerodha_id not in self._otp_request_count:
            self._otp_request_count[zerodha_id] = []
        
        # Remove old requests outside 1-hour window
        self._otp_request_count[zerodha_id] = [
            t for t in self._otp_request_count[zerodha_id] if t > hour_ago
        ]
        
        if len(self._otp_request_count[zerodha_id]) >= MAX_OTP_REQUESTS_PER_HOUR:
            logger.warning("otp_rate_limit_exceeded", zerodha_id=zerodha_id)
            raise AuthenticationError("Too many OTP requests. Try again later.")
        
        self._otp_request_count[zerodha_id].append(now)
        
        # Generate cryptographically secure OTP
        otp = str(secrets.randbelow(900000) + 100000)
        key = self._otp_key(zerodha_id, purpose)
        self._pending_otps[key] = {
            "otp": otp,
            "created_at": now,
            "attempts": 0,
            "zerodha_id": zerodha_id.upper(),
        }
        logger.info("otp_generated", zerodha_id=zerodha_id)
        return otp

    def discard_pending_otp(self, zerodha_id: str, purpose: str = "") -> None:
        """Invalidate an OTP that could not be delivered."""
        self._pending_otps.pop(self._otp_key(zerodha_id, purpose), None)

    def send_otp_email(self, otp: str, recipient_email: str, zerodha_id: str = "") -> bool:
        """Send an OTP to the email address configured for the user.
        
        Args:
            otp: OTP code to send
            recipient_email: Verified email address from the user's configuration
            zerodha_id: Zerodha ID (for logging only)
        
        Returns:
            True if sent successfully
        """
        if not self._smtp_email or not self._smtp_password or not recipient_email:
            logger.error("smtp_not_configured", zerodha_id=zerodha_id)
            return False

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = "VerquoAI - your verification code"
            msg["From"] = self._smtp_email
            msg["To"] = recipient_email

            html = f"""
            <html>
            <body style="font-family: Arial, sans-serif; background: #0d1821; color: #e0e6ed; padding: 30px;">
                <div style="max-width: 400px; margin: 0 auto; background: #1a2332; border-radius: 12px; padding: 30px; border: 1px solid #2a3a4a;">
                    <h2 style="color: #00d4aa; margin-top: 0;">Kite Trading Agent</h2>
                    <p>Your one-time login code is:</p>
                    <div style="font-size: 36px; font-weight: bold; color: #00d4aa; text-align: center; padding: 20px; background: #0d1821; border-radius: 8px; letter-spacing: 8px; margin: 20px 0;">
                        {otp}
                    </div>
                    <p style="color: #8899aa; font-size: 13px;">This code expires in 5 minutes. Do not share it with anyone.</p>
                </div>
            </body>
            </html>
            """
            msg.attach(MIMEText(html, "html"))

            with smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=10) as server:
                server.starttls(context=ssl.create_default_context(cafile=certifi.where()))
                server.login(self._smtp_email, self._smtp_password)
                refused = server.sendmail(self._smtp_email, recipient_email, msg.as_string())
                if refused:
                    logger.error("otp_email_recipient_refused", zerodha_id=zerodha_id)
                    return False

            logger.info("otp_email_sent", zerodha_id=zerodha_id)
            return True
        except Exception as e:
            logger.error("otp_email_failed", zerodha_id=zerodha_id, error=str(e))
            return False

    def _consume_otp(self, otp: str, zerodha_id: str, purpose: str = "") -> Optional[dict]:
        """Check an OTP with expiry and attempt limiting; returns the pending entry on success."""
        key = self._otp_key(zerodha_id, purpose)
        pending = self._pending_otps.get(key)
        if not pending:
            logger.warning("otp_verify_no_pending", zerodha_id=zerodha_id)
            return None

        now = time.time()
        if now - pending["created_at"] > OTP_EXPIRY_SECONDS:
            del self._pending_otps[key]
            logger.warning("otp_expired", zerodha_id=zerodha_id)
            return None

        # Increment attempt counter
        pending["attempts"] += 1
        if pending["attempts"] > MAX_OTP_ATTEMPTS:
            del self._pending_otps[key]
            logger.warning("otp_max_attempts_exceeded", zerodha_id=zerodha_id, attempts=pending["attempts"])
            return None

        # Validate OTP (timing-safe comparison)
        if not secrets.compare_digest(pending["otp"], otp):
            logger.warning("otp_invalid", zerodha_id=zerodha_id, attempt=pending["attempts"])
            return None

        del self._pending_otps[key]
        return pending

    def verify_otp(self, otp: str, zerodha_id: str = "") -> Optional[str]:
        """Verify a login OTP; returns a new session token if valid, None otherwise."""
        pending = self._consume_otp(otp, zerodha_id)
        if not pending:
            return None
        session_token = self._create_session(pending.get("zerodha_id", ""))
        logger.info("otp_verified", zerodha_id=zerodha_id)
        return session_token

    def _create_session(self, zerodha_id: str = "") -> str:
        """Create cryptographically secure session token."""
        token = secrets.token_urlsafe(32)  # URL-safe base64 token
        self._sessions[_hash_token(token)] = {
            "zerodha_id": zerodha_id.upper(),
            "created_at": time.time(),
        }
        self._save_sessions()
        return token

    def _get_session(self, token: str) -> Optional[dict]:
        if not token:
            return None
        key = _hash_token(token)
        session = self._sessions.get(key)
        if not session:
            return None
        if time.time() - session["created_at"] > SESSION_EXPIRY_SECONDS:
            del self._sessions[key]
            return None
        return session

    def validate_session(self, token: str) -> bool:
        return self._get_session(token) is not None

    def get_session_user(self, token: str) -> Optional[str]:
        session = self._get_session(token)
        return session.get("zerodha_id", "") if session else None

    def logout(self, token: str) -> None:
        if token:
            self._sessions.pop(_hash_token(token), None)
        self._save_sessions()

    def logout_all(self, zerodha_id: str) -> int:
        """End every session of this user (all browsers and devices)."""
        uid = zerodha_id.upper()
        keys = [k for k, v in self._sessions.items() if v.get("zerodha_id") == uid]
        for k in keys:
            del self._sessions[k]
        self._save_sessions()
        logger.info("sessions_revoked", zerodha_id=uid, count=len(keys))
        return len(keys)

    def verify_step_up(self, token: str, otp: str) -> bool:
        """Confirm a fresh OTP for this session; unlocks risky actions for STEP_UP_SECONDS."""
        session = self._get_session(token)
        if not session:
            return False
        if not self._consume_otp(otp, session.get("zerodha_id", ""), STEP_UP_PURPOSE):
            return False
        session["step_up_until"] = time.time() + STEP_UP_SECONDS
        self._save_sessions()
        logger.info("step_up_verified", zerodha_id=session.get("zerodha_id", ""))
        return True

    def has_recent_step_up(self, token: str) -> bool:
        session = self._get_session(token)
        return bool(session) and session.get("step_up_until", 0) > time.time()

    def check_ip_rate_limit(self, bucket: str, ip: str, max_requests: int, window_seconds: int = 3600) -> bool:
        """Generic per-IP rate limiter shared by login/OTP endpoints.

        Returns True if the request is allowed (and records it), False if the
        caller has exceeded ``max_requests`` within ``window_seconds``.
        """
        key = f"{bucket}:{ip}"
        now = time.time()
        window_start = now - window_seconds
        attempts = [t for t in self._ip_attempts.get(key, []) if t > window_start]
        if len(attempts) >= max_requests:
            self._ip_attempts[key] = attempts
            logger.warning("ip_rate_limit_exceeded", bucket=bucket, ip=ip)
            return False
        attempts.append(now)
        self._ip_attempts[key] = attempts
        return True

    def get_csrf_token(self, session_token: str) -> str:
        """Derive a CSRF token bound to the session token (synchronizer pattern)."""
        if not session_token:
            return ""
        return hmac.new(self._secret.encode("utf-8"), session_token.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify_csrf_token(self, session_token: str, csrf_token: str) -> bool:
        if not session_token or not csrf_token:
            return False
        expected = self.get_csrf_token(session_token)
        return hmac.compare_digest(expected, csrf_token)


otp_auth = OTPAuth()
