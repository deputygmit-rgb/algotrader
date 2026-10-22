"""Plain SMTP sender for notifications (same SMTP_* settings as the OTP email)."""
from __future__ import annotations

import os
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr
from typing import Optional

import certifi

from src.utils.logger import get_logger

logger = get_logger(__name__)


def send_mail(to: str, subject: str, text: str, reply_to: Optional[str] = None, html: Optional[str] = None) -> bool:
    sender = os.environ.get("SMTP_EMAIL", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    if not (sender and password and to):
        logger.warning("mail_not_configured")
        return False
    msg = EmailMessage()
    msg["From"] = formataddr(("VerquoAI", sender))
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to  # validated by the caller: no CR/LF
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")     # plain text stays the fallback
    try:
        with smtplib.SMTP(os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "587")),
                          timeout=10) as server:
            server.starttls(context=ssl.create_default_context(cafile=certifi.where()))
            server.login(sender, password)
            server.send_message(msg)
        return True
    except Exception as e:
        logger.error("mail_send_failed", error=str(e))
        return False


class MailGuard:
    """Caps on email that anonymous visitors can trigger (access requests).

    Login OTPs go through the same SMTP account, and providers cap daily sends
    (Gmail: about 500). Without these limits anyone could use the contact form
    to mail arbitrary addresses from our account, or burn the daily quota so
    nobody can receive a login code. Requests are still stored when a cap is hit.
    """

    def __init__(self) -> None:
        self.daily_limit = int(os.environ.get("CONTACT_MAIL_DAILY_LIMIT", "100"))
        self.admin_per_hour = int(os.environ.get("CONTACT_ADMIN_MAILS_PER_HOUR", "20"))
        self._day = ""
        self._sent_today = 0
        self._last_by_key: dict[str, float] = {}
        self._admin_times: list[float] = []

    def _roll(self, now: float) -> None:
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if today != self._day:
            self._day, self._sent_today = today, 0
        if len(self._last_by_key) > 10_000:
            cutoff = now - 86400
            self._last_by_key = {k: t for k, t in self._last_by_key.items() if t > cutoff}

    def _take(self, key: str, now: float) -> bool:
        if self._last_by_key.get(key, 0.0) > now - 86400:
            return False  # at most one mail per address (or per requester to admin) a day
        if self._sent_today >= self.daily_limit:
            logger.warning("contact_mail_daily_limit_reached", limit=self.daily_limit)
            return False
        self._last_by_key[key] = now
        self._sent_today += 1
        return True

    def allow_requester_copy(self, address: str) -> bool:
        now = time.time()
        self._roll(now)
        return self._take(f"to:{address.lower()}", now)

    def allow_admin_notice(self, requester: str) -> bool:
        now = time.time()
        self._roll(now)
        self._admin_times = [t for t in self._admin_times if t > now - 3600]
        if len(self._admin_times) >= self.admin_per_hour:
            logger.warning("contact_admin_mail_hourly_limit_reached", limit=self.admin_per_hour)
            return False
        if not self._take(f"admin:{requester.lower()}", now):
            return False
        self._admin_times.append(now)
        return True


mail_guard = MailGuard()
