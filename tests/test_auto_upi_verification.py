import asyncio
import os
import urllib.parse
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
from utils.auto_upi_verifier import payment_not_found_text, payment_success_text
from utils.imap_verifier import verify_auto_upi_order
from plugins.deposit import (
    AUTO_UPI_CHECKING_TEXT,
    _build_auto_upi_uri,
    generate_auto_upi_order_id,
    register_deposit,
)


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
        return "OK", [(b"header", self.raw_messages[int(message_id) - 1])]

    def logout(self):
        return "BYE", []


def payment_email(purpose, amount, when, sender="no-reply@famapp.in", outgoing=False):
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = "Payment notification"
    message["Date"] = when.strftime("%a, %d %b %Y %H:%M:%S %z")
    rupee = chr(0x20B9)
    body = (
        f"Your payment of {rupee}{amount} was successfully paid. Purpose: {purpose}"
        if outgoing else
        f"You have successfully received {rupee}{amount}. Purpose: {purpose}"
    )
    message.set_content(body)
    return message.as_bytes()


def pending_order(purpose="ORD20260924TEST0001", amount=100):
    now = datetime.now(timezone.utc)
    return {
        "_id": purpose,
        "order_id": purpose,
        "purpose": purpose,
        "payable_amount": amount,
        "created_at": now - timedelta(minutes=5),
        "expires_at": now + timedelta(minutes=5),
        "status": "pending",
        "user_id": 7,
    }


class CallbackBot:
    def __init__(self):
        self.handlers = []
        self.send_message = AsyncMock(
            return_value=SimpleNamespace(id=999, delete=AsyncMock())
        )

    def on(self, _pattern):
        def decorator(handler):
            self.handlers.append(handler)
            return handler
        return decorator


class AutoUpiVerificationTests(unittest.TestCase):
    def verify_email(self, order, raw_message):
        FakeImap.raw_messages = [raw_message]
        with patch("utils.imap_verifier.get_imap_credentials", return_value=("test@example.com", "test-password")), \
                patch("utils.imap_verifier.imaplib.IMAP4_SSL", FakeImap):
            return asyncio.run(verify_auto_upi_order(order))

    def get_callback(self, callback_bot=None):
        callback_bot = callback_bot or CallbackBot()
        register_deposit(callback_bot)
        return callback_bot, next(handler for handler in callback_bot.handlers if handler.__name__ == "cb_auto_upi_check")

    def test_matches_current_order_purpose_and_amount(self):
        order = pending_order()
        result = self.verify_email(order, payment_email(order["purpose"], 100, datetime.now(timezone.utc)))
        self.assertTrue(result[0])
        self.assertEqual(result[1]["purpose"], order["purpose"])

    def test_rejects_wrong_amount_and_purpose(self):
        order = pending_order()
        now = datetime.now(timezone.utc)
        self.assertFalse(self.verify_email(order, payment_email(order["purpose"], 99, now))[0])
        self.assertFalse(self.verify_email(order, payment_email("ORD-OTHER-999", 100, now))[0])

    def test_rejects_outgoing_payment_notification(self):
        order = pending_order()
        self.assertFalse(self.verify_email(order, payment_email(order["purpose"], 100, datetime.now(timezone.utc), outgoing=True))[0])

    def test_qr_uri_and_order_id_are_canonical(self):
        order_id = generate_auto_upi_order_id(datetime(2026, 9, 24, tzinfo=timezone.utc), "CD937E82")
        self.assertEqual(order_id, "ORD20260924CD937E82")
        uri = _build_auto_upi_uri("merchant@upi", {"order_id": order_id, "payable_amount": 100})
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(uri).query)
        self.assertEqual(query["tn"], [order_id])
        self.assertEqual(query["tr"], [order_id])

    def test_repeated_and_concurrent_verification_credit_once(self):
        repository = MongoRepository(client=mongomock.MongoClient(), database_name="auto_upi_verification_test")
        repository.ensure_indexes()
        repository.ensure_user(7)
        order = repository.create_auto_upi_order(7, 100, 100, "ORD20260924CONCUR01", repository._now() + timedelta(minutes=5))

        async def verified_payment(_order):
            return True, {"amount": 100, "purpose": order["purpose"], "email_msg_id": "<one@example.com>"}

        async def exercise():
            with patch.object(auto_upi_verifier, "repository", repository), \
                    patch.object(auto_upi_verifier, "verify_auto_upi_order", new=AsyncMock(side_effect=verified_payment)):
                results = await asyncio.gather(
                    auto_upi_verifier.verify_pending_order(order, notify=False),
                    auto_upi_verifier.verify_pending_order(order, notify=False),
                )
                results.append(await auto_upi_verifier.verify_pending_order(order, notify=False))
                return results

        results = asyncio.run(exercise())
        self.assertEqual(sum(result["credited"] for result in results), 1)
        self.assertEqual(repository.get_user(7)["balance"], 100)

    def test_callback_answers_verifies_and_sends_new_not_found_for_each_click(self):
        callback_bot, callback = self.get_callback()
        order = pending_order()
        verification = AsyncMock(return_value={"status": "pending", "credited": False})
        events = [SimpleNamespace(sender_id=7, message=SimpleNamespace(edit=AsyncMock()), answer=AsyncMock()) for _ in range(2)]
        with patch("plugins.deposit.repository.get_current_pending_auto_upi_order", return_value=order), \
                patch("plugins.deposit.verify_pending_order", new=verification):
            for event in events:
                asyncio.run(callback(event))

        self.assertEqual(verification.await_count, 2)
        self.assertEqual(callback_bot.send_message.await_count, 4)
        self.assertEqual(
            [call.args[1] for call in callback_bot.send_message.await_args_list[::2]],
            [AUTO_UPI_CHECKING_TEXT] * 2,
        )
        self.assertEqual(
            [call.args[1] for call in callback_bot.send_message.await_args_list[1::2]],
            [payment_not_found_text()] * 2,
        )
        self.assertTrue(all(event.answer.await_count == 1 for event in events))
        self.assertTrue(all(event.message.edit.await_count == 0 for event in events))

    def test_repeated_checks_delete_only_the_previous_result_before_verifying(self):
        checking_messages = [
            SimpleNamespace(id=101, delete=AsyncMock()),
            SimpleNamespace(id=102, delete=AsyncMock()),
            SimpleNamespace(id=103, delete=AsyncMock()),
        ]
        result_messages = [
            SimpleNamespace(id=201, delete=AsyncMock()),
            SimpleNamespace(id=202, delete=AsyncMock()),
            SimpleNamespace(id=203, delete=AsyncMock()),
        ]
        callback_bot = CallbackBot()
        callback_bot.send_message = AsyncMock(
            side_effect=[message for pair in zip(checking_messages, result_messages) for message in pair]
        )
        callback_bot, callback = self.get_callback(callback_bot)
        order = pending_order()
        events = []

        async def verify(_order, _bot, notify=False):
            events.append("verify")
            return {"status": "pending", "credited": False}

        for _ in range(3):
            event = SimpleNamespace(
                sender_id=7,
                message=SimpleNamespace(delete=AsyncMock(), edit=AsyncMock()),
                answer=AsyncMock(),
            )
            events.append("click")
            with patch("plugins.deposit.repository.get_current_pending_auto_upi_order", return_value=order), \
                    patch("plugins.deposit.verify_pending_order", new=verify):
                asyncio.run(callback(event))

        self.assertEqual(callback_bot.send_message.await_count, 6)
        self.assertEqual(checking_messages[0].delete.await_count, 1)
        self.assertEqual(checking_messages[1].delete.await_count, 1)
        self.assertEqual(checking_messages[2].delete.await_count, 1)
        self.assertEqual(result_messages[0].delete.await_count, 1)
        self.assertEqual(result_messages[1].delete.await_count, 1)
        self.assertEqual(result_messages[2].delete.await_count, 0)
        self.assertEqual(events, ["click", "verify", "click", "verify", "click", "verify"])

    def test_previous_result_delete_failure_does_not_stop_verification(self):
        first_result = SimpleNamespace(id=201, delete=AsyncMock(side_effect=RuntimeError("already gone")))
        second_result = SimpleNamespace(id=202, delete=AsyncMock())
        callback_bot = CallbackBot()
        callback_bot.send_message = AsyncMock(side_effect=[
            SimpleNamespace(id=203, delete=AsyncMock()), first_result,
            SimpleNamespace(id=204, delete=AsyncMock()), second_result,
        ])
        callback_bot, callback = self.get_callback(callback_bot)
        order = pending_order()
        verification = AsyncMock(return_value={"status": "pending", "credited": False})

        with patch("plugins.deposit.repository.get_current_pending_auto_upi_order", return_value=order), \
                patch("plugins.deposit.verify_pending_order", new=verification):
            asyncio.run(callback(SimpleNamespace(sender_id=7, message=SimpleNamespace(delete=AsyncMock()), answer=AsyncMock())))
            asyncio.run(callback(SimpleNamespace(sender_id=7, message=SimpleNamespace(delete=AsyncMock()), answer=AsyncMock())))

        self.assertEqual(verification.await_count, 2)
        self.assertEqual(callback_bot.send_message.await_count, 4)
        first_result.delete.assert_awaited_once_with()

    def test_already_deleted_result_and_original_payment_message_are_ignored(self):
        first_result = SimpleNamespace(id=301, delete=AsyncMock(side_effect=RuntimeError("message not found")))
        callback_bot = CallbackBot()
        callback_bot.send_message = AsyncMock(side_effect=[
            SimpleNamespace(id=302, delete=AsyncMock()), first_result,
            SimpleNamespace(id=303, delete=AsyncMock()), first_result,
        ])
        callback_bot, callback = self.get_callback(callback_bot)
        order = pending_order()
        original_message = SimpleNamespace(delete=AsyncMock(), edit=AsyncMock())
        verification = AsyncMock(return_value={"status": "pending", "credited": False})

        with patch("plugins.deposit.repository.get_current_pending_auto_upi_order", return_value=order), \
                patch("plugins.deposit.verify_pending_order", new=verification):
            asyncio.run(callback(SimpleNamespace(sender_id=7, message=original_message, answer=AsyncMock())))
            asyncio.run(callback(SimpleNamespace(sender_id=7, message=original_message, answer=AsyncMock())))

        self.assertEqual(verification.await_count, 2)
        original_message.delete.assert_not_awaited()
        original_message.edit.assert_not_awaited()

    def test_callback_sends_new_approved_message_without_editing(self):
        callback_bot, callback = self.get_callback()
        result = {"status": "paid", "credited": True, "result": {"amount": 100, "previous_balance": 25, "balance": 125}}
        message = SimpleNamespace(edit=AsyncMock())
        event = SimpleNamespace(sender_id=7, message=message, answer=AsyncMock())
        with patch("plugins.deposit.repository.get_current_pending_auto_upi_order", return_value=pending_order()), \
                patch("plugins.deposit.verify_pending_order", new=AsyncMock(return_value=result)):
            asyncio.run(callback(event))

        self.assertEqual(callback_bot.send_message.await_count, 2)
        self.assertEqual(callback_bot.send_message.await_args_list[0].args[1], AUTO_UPI_CHECKING_TEXT)
        self.assertEqual(callback_bot.send_message.await_args_list[1].args[1], payment_success_text(100, 25, 125))
        message.edit.assert_not_awaited()

    def test_callback_resolves_explicit_order_id_before_verification(self):
        callback_bot, callback = self.get_callback()
        order = pending_order(amount=10)
        event = SimpleNamespace(
            sender_id=7,
            data=f"auto_upi_check:{order['order_id']}".encode(),
            pattern_match=SimpleNamespace(group=lambda _index: order["order_id"].encode()),
            message=SimpleNamespace(edit=AsyncMock()),
            answer=AsyncMock(),
        )
        with patch("plugins.deposit.repository.get_auto_upi_order", return_value=order) as get_order, \
                patch("plugins.deposit.verify_pending_order", new=AsyncMock(return_value={"status": "pending"})) as verify:
            asyncio.run(callback(event))

        get_order.assert_called_once_with(7, order["order_id"])
        verify.assert_awaited_once_with(order, unittest.mock.ANY, notify=False)
        event.answer.assert_awaited_once_with()

    def test_payment_not_found_text_is_quoted_without_markdown_markers(self):
        text = payment_not_found_text()
        self.assertTrue(text.startswith("<blockquote>"))
        self.assertTrue(text.endswith("</blockquote>"))
        self.assertNotIn("**", text)

    def test_real_check_callback_preserves_balance_and_message_edit_is_unused(self):
        callback_bot, callback = self.get_callback()
        repository = MongoRepository(client=mongomock.MongoClient(), database_name="auto_upi_callback_paid")
        repository.ensure_indexes()
        repository.ensure_user(7)
        order = repository.create_auto_upi_order(7, 10, 10, "ORD20260924CALLBACK2", repository._now() + timedelta(minutes=5))
        message = SimpleNamespace(edit=AsyncMock())
        event = SimpleNamespace(
            sender_id=7,
            data=f"auto_upi_check:{order['order_id']}".encode(),
            pattern_match=SimpleNamespace(group=lambda _index: order["order_id"].encode()),
            message=message,
            answer=AsyncMock(),
        )
        FakeImap.raw_messages = [payment_email(order["order_id"], 10, datetime.now(timezone.utc))]
        with patch.object(auto_upi_verifier, "repository", repository), \
                patch("plugins.deposit.repository", repository), \
                patch("utils.imap_verifier.get_imap_credentials", return_value=("test@example.com", "test-password")), \
                patch("utils.imap_verifier.imaplib.IMAP4_SSL", FakeImap):
            asyncio.run(callback(event))
            first_balance = repository.get_user(7)["balance"]
            asyncio.run(callback(event))

        self.assertEqual(first_balance, 10)
        self.assertEqual(repository.get_user(7)["balance"], 10)
        self.assertEqual(callback_bot.send_message.await_count, 4)
        self.assertTrue(all(
            "𝐀ᴜᴛᴏ 𝐕ᴇʀɪғɪᴇᴅ" in call.args[1]
            for call in callback_bot.send_message.await_args_list[1::2]
        ))
        message.edit.assert_not_awaited()

    def test_background_notification_uses_existing_success_text(self):
        telegram_bot = SimpleNamespace(send_message=AsyncMock())
        result = {"user_id": 7, "amount": 100, "previous_balance": 25, "balance": 125}
        with patch("plugins.deposit.process_referral_bonus", new=AsyncMock()):
            asyncio.run(auto_upi_verifier._notify_paid(pending_order(), result, telegram_bot))
        telegram_bot.send_message.assert_awaited_once_with(7, payment_success_text(100, 25, 125))


if __name__ == "__main__":
    unittest.main()
