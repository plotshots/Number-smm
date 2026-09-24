import imaplib
import ssl
import email
import email.utils
import datetime
import re
import asyncio
import os
from config import logger

def get_imap_credentials():
    user = os.getenv("GMAIL_USERNAME", "").strip()
    password = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    missing = []
    if not user:
        missing.append("GMAIL_USERNAME")
    if not password:
        missing.append("GMAIL_APP_PASSWORD")
    if missing:
        logger.error("Gmail IMAP verification disabled: missing %s", ", ".join(missing))
        return "", ""
    return user, password

# Strict whitelist of official FamPay / Payment provider domains
OFFICIAL_SENDER_DOMAINS = [
    "@famapp.in",
    "@fampay.in",
    "@famapp.co.in",
    "no-reply@famapp.in"
]

# Maximum allowed age for auto-verification email (in minutes)
# Strict 30-minute window to completely block old payments/emails from past days!
MAX_PAYMENT_AGE_MINUTES = 30

def _sync_verify_utr(utr_query, max_age_mins=MAX_PAYMENT_AGE_MINUTES):
    email_user, email_pass = get_imap_credentials()
    if not email_user or not email_pass:
        return False, "Gateway configuration error."

    utr_clean = utr_query.strip()
    if len(utr_clean) < 6:
        return False, "Invalid UTR / Transaction ID length."

    try:
        context = ssl.create_default_context()
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=context)
        mail.login(email_user, email_pass)
        mail.select("INBOX")

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        # Search emails from today/yesterday only
        since_date = (now_utc - datetime.timedelta(days=1)).strftime("%d-%b-%Y")
        
        # Primary search: within recent date range
        status, data = mail.search(None, f'(SINCE "{since_date}" TEXT "{utr_clean}")')
        if status != "OK" or not data or not data[0]:
            # Secondary search: check if email exists at all
            status, data = mail.search(None, f'(TEXT "{utr_clean}")')
            if status != "OK" or not data or not data[0]:
                mail.logout()
                return False, "Payment not found or not settled yet."

        mail_ids = data[0].split()
        # Check matching emails starting from most recent
        for m_id in reversed(mail_ids[-5:]):
            status, msg_data = mail.fetch(m_id, "(RFC822)")
            for part in msg_data:
                if isinstance(part, tuple):
                    msg = email.message_from_bytes(part[1])
                    
                    # 1. STRICT SENDER AUTHENTICATION: Must be from official FamPay email!
                    from_header = (msg.get("From") or "").lower()
                    if not any(domain in from_header for domain in OFFICIAL_SENDER_DOMAINS):
                        logger.warning(f"Ignored fake/unauthorized payment email from: {from_header}")
                        continue

                    # 2. STRICT DATE / EXPIRATION CHECK: Prevent using old payment emails!
                    date_header = msg.get("Date")
                    if date_header:
                        try:
                            email_dt = email.utils.parsedate_to_datetime(date_header)
                            if email_dt.tzinfo is None:
                                email_dt = email_dt.replace(tzinfo=datetime.timezone.utc)
                            
                            age_seconds = (now_utc - email_dt.astimezone(datetime.timezone.utc)).total_seconds()
                            age_minutes = age_seconds / 60.0
                            
                            if age_minutes > max_age_mins:
                                logger.warning(f"Rejected old UTR {utr_clean}: email date was {email_dt} ({age_minutes:.1f} mins old, limit is {max_age_mins} mins)")
                                mail.logout()
                                return False, "Payment transaction expired or not recent."
                        except Exception as dt_err:
                            logger.error(f"Error parsing email date: {dt_err}")

                    body = ""
                    if msg.is_multipart():
                        for p in msg.walk():
                            payload = p.get_payload(decode=True)
                            if payload:
                                body += payload.decode(errors="ignore") + " "
                    else:
                        payload = msg.get_payload(decode=True)
                        if payload:
                            body = payload.decode(errors="ignore")

                    clean_text = re.sub("<[^<]+?>", " ", body)
                    clean_text = re.sub(r"\s+", " ", clean_text)

                    if utr_clean.lower() in clean_text.lower():
                        # Extract Message-ID header
                        msg_id = (msg.get("Message-ID") or "").strip()

                        # Extract amount (e.g. received ₹35.0 or ₹35)
                        amt_match = re.search(r"received\s*₹\s*([0-9]+(?:\.[0-9]+)?)", clean_text, re.I)
                        if not amt_match:
                            amt_match = re.search(r"₹\s*([0-9]+(?:\.[0-9]+)?)", clean_text)
                        
                        amt = float(amt_match.group(1)) if amt_match else 0.0
                        
                        # Extract sender
                        sender_match = re.search(r"from\s+([^.]+?)\s*(?:\.|\sat|\swith)", clean_text, re.I)
                        sender = sender_match.group(1).strip() if sender_match else "User"

                        # Extract all identifiers to prevent claiming once with UTR and once with TxnID!
                        utr_match = re.search(r"UTR[:\s]*([0-9]{6,15})", clean_text, re.I)
                        detected_utr = utr_match.group(1).strip() if utr_match else (utr_clean if utr_clean.isdigit() else "")

                        txnid_match = re.search(r"transaction\s*id[:\s]*([A-Za-z0-9]+)", clean_text, re.I)
                        detected_txnid = txnid_match.group(1).strip() if txnid_match else (utr_clean if not utr_clean.isdigit() else "")

                        mail.logout()
                        return True, {
                            "utr": utr_clean,
                            "email_msg_id": msg_id,
                            "detected_utr": detected_utr,
                            "detected_txnid": detected_txnid,
                            "amount": int(amt) if amt == int(amt) else amt,
                            "sender": sender,
                            "raw": clean_text[:200]
                        }

        mail.logout()
        return False, "Transaction details could not be verified."
    except Exception as e:
        logger.error(f"IMAP verification exception: {e}")
        return False, "Payment verification server error."

async def verify_payment_utr(utr_query, max_age_mins=MAX_PAYMENT_AGE_MINUTES):
    """Asynchronous wrapper for IMAP payment verification."""
    return await asyncio.to_thread(_sync_verify_utr, utr_query, max_age_mins)


def _sync_verify_auto_upi_order(order):
    """Find a recent official payment email for one pending Auto UPI order."""
    email_user, email_pass = get_imap_credentials()
    if not email_user or not email_pass:
        return False, "Gateway configuration error."

    purpose = str(order.get("purpose") or order.get("order_id") or "").strip()
    order_id = str(order.get("order_id") or "").strip()
    expected_amount = float(order.get("payable_amount", order.get("amount", 0)))
    if not purpose or expected_amount <= 0:
        return False, "Invalid Auto UPI order."

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    created_at = order.get("created_at") or now_utc
    expires_at = order.get("expires_at") or now_utc
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=datetime.timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
    if now_utc >= expires_at:
        return False, "Payment order has expired."

    try:
        context = ssl.create_default_context()
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=context)
        mail.login(email_user, email_pass)
        mail.select("INBOX")
        since_date = max(created_at, now_utc - datetime.timedelta(days=1)).strftime("%d-%b-%Y")
        status, data = mail.search(None, f'(SINCE "{since_date}" TEXT "{purpose}")')
        if status != "OK" or not data or not data[0]:
            return False, "Payment not found or not settled yet."

        for mail_id in reversed(data[0].split()[-20:]):
            status, msg_data = mail.fetch(mail_id, "(RFC822)")
            if status != "OK":
                continue
            for part in msg_data:
                if not isinstance(part, tuple):
                    continue
                msg = email.message_from_bytes(part[1])
                from_header = (msg.get("From") or "").lower()
                if not any(domain in from_header for domain in OFFICIAL_SENDER_DOMAINS):
                    continue

                date_header = msg.get("Date")
                if not date_header:
                    continue
                try:
                    email_dt = email.utils.parsedate_to_datetime(date_header)
                    if email_dt.tzinfo is None:
                        email_dt = email_dt.replace(tzinfo=datetime.timezone.utc)
                    email_dt = email_dt.astimezone(datetime.timezone.utc)
                except (TypeError, ValueError, OverflowError):
                    continue
                if email_dt < created_at or email_dt >= expires_at or email_dt > now_utc:
                    continue

                body = ""
                if msg.is_multipart():
                    for body_part in msg.walk():
                        payload = body_part.get_payload(decode=True)
                        if payload:
                            body += payload.decode(errors="ignore") + " "
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        body = payload.decode(errors="ignore")
                clean_text = re.sub(r"<[^<]+?>", " ", body)
                clean_text = re.sub(r"\s+", " ", clean_text)
                if purpose.lower() not in clean_text.lower() and order_id.lower() not in clean_text.lower():
                    continue

                amount_match = re.search(
                    r"(?:received|amount|payment)\s*[:=-]?\s*(?:₹|INR)?\s*([0-9]+(?:\.[0-9]+)?)"
                    r"|₹\s*([0-9]+(?:\.[0-9]+)?)",
                    clean_text,
                    re.I,
                )
                if not amount_match:
                    continue
                amount = float(amount_match.group(1) or amount_match.group(2))
                if amount != expected_amount:
                    continue

                return True, {
                    "email_msg_id": (msg.get("Message-ID") or "").strip(),
                    "amount": int(amount) if amount.is_integer() else amount,
                    "purpose": purpose,
                    "received_at": email_dt,
                    "sender": from_header,
                }
        return False, "Payment not found or not settled yet."
    except Exception as exc:
        logger.error("Auto UPI IMAP verification exception: %s", exc)
        return False, "Payment verification server error."
    finally:
        try:
            mail.logout()
        except Exception:
            pass


async def verify_auto_upi_order(order):
    """Asynchronously verify one pending order against the linked Gmail inbox."""
    return await asyncio.to_thread(_sync_verify_auto_upi_order, order)
