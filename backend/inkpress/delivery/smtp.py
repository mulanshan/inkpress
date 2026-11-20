from __future__ import annotations

import asyncio
import mimetypes
import re
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any

import aiosmtplib

from inkpress.logging import get_logger

logger = get_logger(__name__)

MAX_ATTACHMENT_BYTES = 24 * 1024 * 1024  # Safely under Gmail 25 MB limit


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip(" .")
    return cleaned[:160] or "InkPress_Document"


def parse_recipients(raw: str) -> list[str]:
    return [addr.strip() for addr in re.split(r"[,;\s]+", raw or "") if "@" in addr]


async def send_ebook_via_smtp(
    *,
    smtp_config: dict[str, Any],
    recipients: list[str],
    subject: str,
    body_text: str,
    attachments: list[Path],
    retries: int = 3,
) -> None:
    """Send one or more EPUB/PDF attachments to Kindle via async SMTP with retry."""
    host = str(smtp_config.get("host") or "").strip()
    port = int(smtp_config.get("port") or 465)
    use_ssl = bool(smtp_config.get("use_ssl", port == 465))
    username = str(smtp_config.get("username") or "").strip()
    password = str(smtp_config.get("password") or "")
    from_email = str(smtp_config.get("from_email") or username).strip()

    if not host or not from_email:
        raise ValueError("SMTP host and sender email must be configured before sending")
    if not recipients:
        raise ValueError("At least one Kindle recipient email address is required")

    total_bytes = sum(p.stat().st_size for p in attachments)
    if total_bytes > MAX_ATTACHMENT_BYTES:
        raise ValueError(
            f"Total attachment size ({total_bytes / 1024 / 1024:.1f} MB) exceeds 24 MB email limit"
        )

    msg = EmailMessage()
    msg["From"] = from_email
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_email.split("@")[-1])
    msg.set_content(body_text)

    for file_path in attachments:
        data = file_path.read_bytes()
        mime_type, _ = mimetypes.guess_type(file_path.name)
        if file_path.suffix.lower() == ".epub":
            maintype, subtype = "application", "epub+zip"
        elif file_path.suffix.lower() == ".pdf":
            maintype, subtype = "application", "pdf"
        elif mime_type and "/" in mime_type:
            maintype, subtype = mime_type.split("/", 1)
        else:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype,
            filename=sanitize_filename(file_path.name),
        )

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            await aiosmtplib.send(
                msg,
                hostname=host,
                port=port,
                username=username or None,
                password=password or None,
                use_tls=use_ssl,
                start_tls=not use_ssl,
                timeout=90,
            )
            logger.info(
                "smtp_sent_ok",
                recipients=recipients,
                subject=subject,
                files=[p.name for p in attachments],
                bytes=total_bytes,
            )
            return
        except Exception as exc:
            last_exc = exc
            logger.warning("smtp_attempt_failed", attempt=attempt, host=host, err=str(exc))
            if attempt < retries:
                await asyncio.sleep(2**attempt)

    raise RuntimeError(f"SMTP delivery failed after {retries} attempts: {last_exc}") from last_exc
