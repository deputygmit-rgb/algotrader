"""Plain SMTP sender for notifications (same SMTP_* settings as the OTP email)."""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from typing import Optional

import certifi

from src.utils.logger import get_logger

logger = get_logger(__name__)


def send_mail(to: str, subject: str, text: str, reply_to: Optional[str] = None) -> bool:
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
