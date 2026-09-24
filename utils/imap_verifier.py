import asyncio
import datetime
import email
import imaplib
import os
import re
import ssl
from email import policy
from email.utils import parseaddr, parsedate_to_datetime

from config import logger


IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
IMAP_MAILBOX = "INBOX"
IMAP_LOOKBACK_HOURS = 48
OFFICIAL_SENDER_DOMAINS = ("famapp.in", "fampay.in", "famapp.co.in")


def get_imap_credentials():
    username = os.getenv("GMAIL_USERNAME", "").strip()
    password = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    missing = [name for name, value in (("GMAIL_USERNAME", username), ("GMAIL_APP_PASSWORD", password)) if not value]
    if missing:
        logger.error("Gmail IMAP verification disabled: missing %s", ", ".join(missing))
        return "", ""
    return username, password


MAX_PAYMENT_AGE_MINUTES = 30


def _sync_verify_utr(utr_query, max_age_mins=MAX_PAYMENT_AGE_MINUTES):
    """Keep the existing Manual UPI UTR contract separate from Auto UPI."""
    username, password = get_imap_credentials()
    utr = str(utr_query or "").strip()
    if not username or not password:
        return False, "Gateway configuration error."
    if len(utr) < 6:
        return False, "Invalid UTR / Transaction ID length."
    client = None
    try:
        client = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ssl.create_default_context())
        client.login(username, password)
        client.select(IMAP_MAILBOX)
        since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)).strftime("%d-%b-%Y")
        status, data = client.search(None, "SINCE", since)
        if status != "OK" or not data or not data[0]:
            return False, "Payment not found or not settled yet."
        now = datetime.datetime.now(datetime.timezone.utc)
        for message_id in reversed(data[0].split()[-5:]):
            status, fetched = client.fetch(message_id, "(RFC822)")
            if status != "OK":
                continue
            for part in fetched:
                if not isinstance(part, tuple):
                    continue
                message = email.message_from_bytes(part[1], policy=policy.default)
                sender = parseaddr(str(message.get("From", "")))[1].lower()
                if not _is_official_sender(sender):
                    continue
                received_at = _as_utc(message.get("Date"), None)
                if received_at is None or (now - received_at).total_seconds() > max_age_mins * 60:
                    continue
                parsed = parse_payment_email(part[1])
                if parsed and utr.casefold() in f"{parsed['subject']} {parsed['body']}".casefold():
                    return True, {
                        "utr": utr,
                        "email_msg_id": parsed["email_msg_id"],
                        "amount": parsed["amount"] or 0,
                        "sender": parsed["sender"],
                        "raw": parsed["body"][:200],
                    }
        return False, "Transaction details could not be verified."
    except Exception as exc:
        logger.error("IMAP verification exception: %s", exc)
        return False, "Payment verification server error."
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


async def verify_payment_utr(utr_query, max_age_mins=MAX_PAYMENT_AGE_MINUTES):
    return await asyncio.to_thread(_sync_verify_utr, utr_query, max_age_mins)


def _as_utc(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            value = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                value = parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError):
                return default
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def _purpose_matches(received, expected):
    if "-" not in str(expected):
        return str(received) == str(expected)
    received = re.sub(r"[^A-Za-z0-9]", "", str(received)).casefold()
    expected = re.sub(r"[^A-Za-z0-9]", "", str(expected)).casefold()
    return bool(received and expected and received == expected)


def parse_payment_email(raw_message):
    """Parse the received-payment fields used by the reference bot."""
    try:
        message = email.message_from_bytes(raw_message, policy=policy.default)
        parts = []
        if message.is_multipart():
            for part in message.walk():
                if part.get_content_disposition() == "attachment":
                    continue
                if part.get_content_type() in {"text/plain", "text/html"}:
                    parts.append(str(part.get_content()))
        else:
            parts.append(str(message.get_content()))
        body = "\n".join(parts)
        clean_body = re.sub(r"<[^<]+?>", " ", body)
        clean_body = re.sub(r"\s+", " ", clean_body).strip()
        combined = f"{message.get('Subject', '')}\n{clean_body}"
        purpose_match = re.search(
            r"purpose\s*:\s*([A-Za-z0-9-]+)", combined, re.IGNORECASE,
        )
        amount_match = re.search(
            r"(?:₹|INR)\s*([0-9][0-9,]*(?:\.\d+)?)", combined, re.IGNORECASE,
        )
        return {
            "sender": parseaddr(str(message.get("From", "")))[1].lower(),
            "subject": str(message.get("Subject", "")),
            "body": clean_body,
            "purpose": purpose_match.group(1) if purpose_match else None,
            "amount": float(amount_match.group(1).replace(",", "")) if amount_match else None,
            "date": message.get("Date"),
            "email_msg_id": (message.get("Message-ID") or "").strip(),
        }
    except Exception:
        logger.exception("Unable to parse payment email")
        return None


def _is_official_sender(sender):
    return any(sender == domain or sender.endswith("@" + domain) for domain in OFFICIAL_SENDER_DOMAINS)


def _sync_verify_auto_upi_order(order):
    username, password = get_imap_credentials()
    if not username or not password:
        return False, "Gateway configuration error."

    expected_purpose = str(order.get("purpose") or order.get("order_id") or "").strip()
    expected_amount = float(order.get("payable_amount", order.get("amount", 0)))
    now = datetime.datetime.now(datetime.timezone.utc)
    created_at = _as_utc(order.get("created_at"), now - datetime.timedelta(hours=IMAP_LOOKBACK_HOURS))
    expires_at = _as_utc(order.get("expires_at"), now)
    if not expected_purpose or expected_amount <= 0:
        return False, "Invalid Auto UPI order."
    if now >= expires_at:
        return False, "Payment order has expired."

    client = None
    try:
        client = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ssl.create_default_context())
        status, _ = client.login(username, password)
        if status != "OK":
            return False, "Payment verification server error."
        status, _ = client.select(IMAP_MAILBOX)
        if status != "OK":
            return False, "Payment verification server error."
        since = max(created_at, now - datetime.timedelta(hours=IMAP_LOOKBACK_HOURS)).strftime("%d-%b-%Y")
        status, data = client.search(None, "SINCE", since)
        if status != "OK" or not data or not data[0]:
            return False, "Payment not found or not settled yet."

        for message_id in reversed(data[0].split()):
            fetch_status, fetched = client.fetch(message_id, "(RFC822)")
            if fetch_status != "OK":
                continue
            for part in fetched:
                if not isinstance(part, tuple) or len(part) < 2:
                    continue
                parsed = parse_payment_email(part[1])
                if not parsed or not _is_official_sender(parsed["sender"]):
                    continue
                received = f"{parsed['subject']}\n{parsed['body']}".lower()
                if "your payment" in received or "successfully paid" in received:
                    continue
                if not (re.search(r"you\s+received\s*(?:₹|inr)\s*[0-9]", received) or
                        "you have successfully received" in received):
                    continue
                if not parsed["purpose"] or not _purpose_matches(parsed["purpose"], expected_purpose):
                    continue
                if parsed["amount"] != expected_amount:
                    continue
                received_at = _as_utc(parsed["date"], None)
                if (received_at is None or
                    received_at < created_at - datetime.timedelta(seconds=2) or
                    received_at > now or received_at >= expires_at):
                    continue
                return True, {
                    "email_msg_id": parsed["email_msg_id"],
                    "amount": int(parsed["amount"]) if parsed["amount"].is_integer() else parsed["amount"],
                    "purpose": expected_purpose,
                    "received_at": received_at,
                    "sender": parsed["sender"],
                }
        return False, "Payment not found or not settled yet."
    except Exception as exc:
        logger.error("Auto UPI IMAP verification exception: %s", exc)
        return False, "Payment verification server error."
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


async def verify_auto_upi_order(order):
    return await asyncio.to_thread(_sync_verify_auto_upi_order, order)