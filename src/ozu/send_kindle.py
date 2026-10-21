"""Send-to-Kindle delivery for the weekly digest PDF.

The only outbound message the pipeline sends, and it goes to the owner's own
device: one email, PDF attached, to their @kindle.com address via SMTP
credentials in ~/ozu-data/.env. All three of OZU_KINDLE_EMAIL, OZU_SMTP_USER and
OZU_SMTP_PASS must be set or this is a no-op and the digest stays a local
file — the sender address must also be on Amazon's approved list
(amazon.com/sendtokindle → Manage Your Content and Devices → Preferences).
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path

from .config import get_config

log = logging.getLogger("ozu.send_kindle")


def send_pdf(path: Path) -> bool:
    """Email one PDF to the Kindle. Returns False when not configured."""
    cfg = get_config()
    if not (cfg.kindle_email and cfg.smtp_user and cfg.smtp_pass):
        log.info("send-to-kindle not configured; digest stays local")
        return False
    msg = EmailMessage()
    msg["From"] = cfg.smtp_user
    msg["To"] = cfg.kindle_email
    msg["Subject"] = path.stem
    # The body is never read on the device — Send-to-Kindle keeps the
    # attachment and discards it — but "Weekly Ozu digest attached" on a Build
    # result or a research brief is wrong in the sender's own Sent folder, which
    # is where a delivery is chased when one goes missing.
    msg.set_content(f"{path.stem} attached.")
    msg.add_attachment(path.read_bytes(), maintype="application",
                       subtype="pdf", filename=path.name)
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=60) as smtp:
        smtp.starttls()
        smtp.login(cfg.smtp_user, cfg.smtp_pass)
        smtp.send_message(msg)
    # The file, never the address: the @kindle.com address is half of a
    # delivery credential (whoever has it and passes as an approved sender
    # puts documents on the device), and anything that greps the journal
    # would print it.
    log.info("%s sent to Send-to-Kindle", path.name)
    return True
