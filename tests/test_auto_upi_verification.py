import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import mongomock

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test-hash")
os.environ.setdefault("BOT_TOKEN", "1:test-token")
os.environ.setdefault("ADMIN_ID", "1")
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")

from mongo_repository import MongoRepository
from utils import auto_upi_verifier
from utils.auto_upi_verifier import payment_not_found_text
from utils.imap_verifier import verify_auto_upi_order
from plugins.deposit import register_deposit


class FakeImap:
    raw_messages = []

    def __init__(self, *_args, **_kwargs):
        self.raw_messages = type(self).raw_messages

    def login(self, *_args):
        return "OK", []

    def select(self, _mailbox):
        return "OK", []

    def search(self, *_args):
        return "OK", [b" ".join(str(index + 1).encode() for index in range(len(self.raw_messages)))]

    def fetch(self, message_id, _query):
        index = int(message_id) - 1
        return "OK", [(b"header", self.raw_messages[index])]

    def logout(self):
        return "BYE", []


def payment_email(purpose, amount, when, sender="no-reply@famapp.in", outgoing=False):
    rupee = chr(0x20B9)
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = "Payment notification"
    message["Date"] = when.strftime("%a, %d %b %Y %H:%M:%S %z")
    if outgoing:
        body = f"Your payment of {rupee}{amount} was successfully paid. Purpose: {purpose}"
    else:
        body = f"You have successfully received {rupee}{amount}. Purpose: {purpose}"
    message.set_content(body)
    return message.as_bytes()


def pending_order(purpose="ORD-TEST-123", amount=100, created_at=None, expires_at=None):
    now = datetime.now(timezone.utc)
    return {
        "_id": purpose,
        "order_id": purpose,
        "purpose": purpose,
        "payable_amount": amount,
        "created_at": created_at or now - timedelta(minutes=5),
        "expires_at": expires_at or now + timedelta(minutes=5),
        "status": "pending",
        "user_id": 7,
    }


class AutoUpiVerificationTests(unittest.TestCase):
    def verify_email(self, order, raw_message):
        FakeImap.raw_messages = [raw_message]
        with patch("utils.imap_verifier.get_imap_credentials", return_value=("test@example.com", "test-password")), \
                patch("utils.imap_verifier.imaplib.IMAP4_SSL", FakeImap):
            return asyncio.run(verify_auto_upi_order(order))

    def test_matches_current_order_purpose_and_amount(self):
        order = pending_order()
        result = self.verify_email(
            order,
            payment_email(order["purpose"], order["payable_amount"], datetime.now(timezone.utc)),
        )
        self.assertEqual(result[0], True)
        self.assertEqual(result[1]["purpose"], order["purpose"])
        self.assertEqual(result[1]["amount"], order["payable_amount"])

    def test_rejects_wrong_amount_and_wrong_purpose(self):
        order = pending_order()
        now = datetime.now(timezone.utc)
        wrong_amount = self.verify_email(order, payment_email(order["purpose"], 99, now))
        wrong_purpose = self.verify_email(order, payment_email("ORD-OTHER-999", 100, now))
        self.assertEqual(wrong_amount[0], False)
        self.assertEqual(wrong_purpose[0], False)

    def test_rejects_old_email_and_expired_order(self):
        order = pending_order()
        old_email = payment_email(order["purpose"], 100, order["created_at"] - timedelta(seconds=1))
        expired_order = pending_order(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        self.assertEqual(self.verify_email(order, old_email)[0], False)
        self.assertEqual(self.verify_email(expired_order, old_email)[1], "Payment order has expired.")

    def test_rejects_outgoing_payment_notification(self):
        order = pending_order()
        result = self.verify_email(
            order,
            payment_email(order["purpose"], order["payable_amount"], datetime.now(timezone.utc), outgoing=True),
        )
        self.assertEqual(result[0], False)

    def test_rejects_untrusted_sender_domain(self):
        order = pending_order()
        result = self.verify_email(
            order,
            payment_email(
                order["purpose"], order["payable_amount"], datetime.now(timezone.utc),
                sender="no-reply@famapp.in.example.com",
            ),
        )
        self.assertEqual(result[0], False)

    def test_repeated_and_concurrent_verification_credit_once(self):
        repository = MongoRepository(client=mongomock.MongoClient(), database_name="auto_upi_verification_test")
        repository.ensure_indexes()
        repository.ensure_user(7)
        order = repository.create_auto_upi_order(
            7, 100, 100, "ORD-CONCURRENT-1", repository._now() + timedelta(minutes=5),
        )

        async def verified_payment(_order):
            await asyncio.sleep(0)
            return True, {"amount": 100, "purpose": order["purpose"], "email_msg_id": "<one@example.com>"}

        async def exercise():
            with patch.object(auto_upi_verifier, "repository", repository), \
                    patch.object(auto_upi_verifier, "verify_auto_upi_order", new=AsyncMock(side_effect=verified_payment)):
                first, second = await asyncio.gather(
                    auto_upi_verifier.verify_pending_order(order, notify=False),
                    auto_upi_verifier.verify_pending_order(order, notify=False),
                )
                repeated = await auto_upi_verifier.verify_pending_order(order, notify=False)
            return first, second, repeated

        first, second, repeated = asyncio.run(exercise())
        self.assertEqual(sum(result["credited"] for result in (first, second)), 1)
        self.assertEqual(repeated["credited"], False)
        self.assertEqual(repository.get_user(7)["balance"], 100)
        self.assertEqual(repository.db.upi_orders.find_one({"_id": order["_id"]})["status"], "paid")

    def test_repeated_check_skips_identical_edit_but_runs_verification(self):
        class CallbackBot:
            def __init__(self):
                self.handlers = []

            def on(self, _pattern):
                def decorator(handler):
                    self.handlers.append(handler)
                    return handler
                return decorator

        callback_bot = CallbackBot()
        register_deposit(callback_bot)
        callback = next(handler for handler in callback_bot.handlers if handler.__name__ == "cb_auto_upi_check")
        event = SimpleNamespace(
            sender_id=7,
            message=SimpleNamespace(message=payment_not_found_text()),
            edit=AsyncMock(),
            answer=AsyncMock(),
        )
        pending_result = {"status": "pending", "credited": False}
        with patch("plugins.deposit.repository.get_current_pending_auto_upi_order", return_value=pending_order()), \
                patch("plugins.deposit.verify_current_user_order", new=AsyncMock(return_value=pending_result)) as verify:
            asyncio.run(callback(event))
            asyncio.run(callback(event))

        self.assertEqual(verify.await_count, 2)
        event.edit.assert_not_awaited()
        self.assertEqual(event.answer.await_count, 2)


if __name__ == "__main__":
    unittest.main()
