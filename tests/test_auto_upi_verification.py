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
    AUTO_UPI_VERIFICATION_TIMEOUT_SECONDS,
    _build_auto_upi_uri,
    _edit_auto_upi_status_message,
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


def pending_order(purpose="ORD20260924TEST0001", amount=100, created_at=None, expires_at=None):
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

    def test_legacy_hyphenated_order_matches_normalized_fampay_purpose(self):
        order = pending_order(purpose="ORD-20260924-CD937E82")
        result = self.verify_email(
            order,
            payment_email("ORD20260924CD937E82", order["payable_amount"], datetime.now(timezone.utc)),
        )
        self.assertEqual(result[0], True)
        self.assertEqual(result[1]["purpose"], order["purpose"])

    def test_new_order_requires_exact_canonical_purpose(self):
        order = pending_order(purpose="ORD20260924CD937E82")
        result = self.verify_email(
            order,
            payment_email("ORD-20260924-CD937E82", order["payable_amount"], datetime.now(timezone.utc)),
        )
        self.assertEqual(result[0], False)

    def test_rejects_different_order_id_after_normalization(self):
        order = pending_order(purpose="ORD-20260924-CD937E82")
        result = self.verify_email(
            order,
            payment_email("ORD-20260924-CD937E83", order["payable_amount"], datetime.now(timezone.utc)),
        )
        self.assertEqual(result[0], False)

    def test_qr_uri_preserves_order_id_in_tn_and_tr(self):
        order_id = "ORD20260924CD937E82"
        uri = _build_auto_upi_uri("merchant@upi", {
            "order_id": order_id,
            "payable_amount": 100,
        })
        decoded = urllib.parse.parse_qs(urllib.parse.urlsplit(uri).query)
        self.assertEqual(decoded["tn"], [order_id])
        self.assertEqual(decoded["tr"], [order_id])

    def test_new_order_id_format_is_canonical(self):
        order_id = generate_auto_upi_order_id(
            datetime(2026, 9, 24, tzinfo=timezone.utc),
            "CD937E82",
        )
        self.assertEqual(order_id, "ORD20260924CD937E82")
        self.assertRegex(order_id, r"^ORD\d{8}[A-Z0-9]{8}$")
        self.assertNotIn("-", order_id)

    def test_new_order_stores_canonical_id_in_mongo(self):
        repository = MongoRepository(client=mongomock.MongoClient(), database_name="auto_upi_id_format_test")
        order_id = generate_auto_upi_order_id(
            datetime(2026, 9, 24, tzinfo=timezone.utc),
            "CD937E82",
        )
        order = repository.create_auto_upi_order(
            7, 100, 100, order_id, repository._now() + timedelta(minutes=5),
        )

        stored = repository.db.upi_orders.find_one({"_id": order_id})
        self.assertEqual(order["order_id"], "ORD20260924CD937E82")
        self.assertEqual(stored["order_id"], "ORD20260924CD937E82")
        self.assertEqual(stored["purpose"], "ORD20260924CD937E82")
        self.assertNotIn("-", stored["order_id"])

    def test_rejects_wrong_amount_and_wrong_purpose(self):
        order = pending_order()
        now = datetime.now(timezone.utc)
        wrong_amount = self.verify_email(order, payment_email(order["purpose"], 99, now))
        wrong_purpose = self.verify_email(order, payment_email("ORD-OTHER-999", 100, now))
        self.assertEqual(wrong_amount[0], False)
        self.assertEqual(wrong_purpose[0], False)

    def test_rejects_old_email_and_expired_order(self):
        order = pending_order()
        old_email = payment_email(order["purpose"], 100, order["created_at"] - timedelta(seconds=10))
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
            7, 100, 100, "ORD20260924CONCUR01", repository._now() + timedelta(minutes=5),
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
        callback_message = SimpleNamespace(message=payment_not_found_text(), buttons=None, photo=None)

        async def edit_message(text, buttons=None):
            callback_message.message = text
            callback_message.buttons = buttons

        callback_message.edit = AsyncMock(side_effect=edit_message)
        event = SimpleNamespace(
            sender_id=7,
            message=callback_message,
            answer=AsyncMock(),
        )
        pending_result = {"status": "pending", "credited": False}
        with patch("plugins.deposit.repository.get_current_pending_auto_upi_order", return_value=pending_order()), \
            patch("plugins.deposit.verify_pending_order", new=AsyncMock(return_value=pending_result)) as verify:
            asyncio.run(callback(event))
            asyncio.run(callback(event))

        self.assertEqual(verify.await_count, 2)
        self.assertEqual(callback_message.edit.await_count, 4)
        self.assertEqual(
            [call.args[0] for call in callback_message.edit.await_args_list],
            [AUTO_UPI_CHECKING_TEXT, payment_not_found_text(), AUTO_UPI_CHECKING_TEXT, payment_not_found_text()],
        )
        self.assertEqual(event.answer.await_count, 2)

    def test_check_status_edits_checking_then_auto_verified_success(self):
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
        success_result = {
            "status": "paid",
            "credited": True,
            "result": {"amount": 100, "previous_balance": 25, "balance": 125},
        }
        callback_message = SimpleNamespace(message="payment status", buttons=None, photo=None, id=1, edit=AsyncMock())
        event = SimpleNamespace(sender_id=7, message=callback_message, answer=AsyncMock())
        with patch("plugins.deposit.repository.get_current_pending_auto_upi_order", return_value=pending_order()), \
            patch("plugins.deposit.verify_pending_order", new=AsyncMock(return_value=success_result)) as verify:
            asyncio.run(callback(event))

        verify.assert_awaited_once()
        success_text = payment_success_text(100, 25, 125)
        self.assertEqual(callback_message.edit.await_args_list[0].args, (AUTO_UPI_CHECKING_TEXT,))
        self.assertEqual(callback_message.edit.await_args_list[1].args, (success_text,))
        self.assertIn("🤖 𝐀ᴜᴛᴏ 𝐕ᴇʀɪғɪᴇᴅ", success_text)

    def test_check_status_resolves_callback_order_before_verification(self):
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
        order = pending_order(amount=10)
        callback_message = SimpleNamespace(message="payment status", buttons=None, photo=None, id=1)
        callback_message.edit = AsyncMock()
        event = SimpleNamespace(
            sender_id=7,
            data=f"auto_upi_check:{order['order_id']}".encode(),
            pattern_match=SimpleNamespace(group=lambda _index: order["order_id"].encode()),
            message=callback_message,
            answer=AsyncMock(),
        )
        pending_result = {"status": "pending", "credited": False}
        with patch("plugins.deposit.repository.get_auto_upi_order", return_value=order) as get_order, \
                patch("plugins.deposit.verify_pending_order", new=AsyncMock(return_value=pending_result)) as verify:
            asyncio.run(callback(event))

        get_order.assert_called_once_with(7, order["order_id"])
        verify.assert_awaited_once_with(order, unittest.mock.ANY, notify=False)
        self.assertEqual(callback_message.edit.await_args_list[0].args, ("⏳ 𝐂ʜᴇᴄᴋɪɴɢ ʏᴏᴜʀ 𝐏ᴀʏᴍᴇɴᴛ...",))
        event.answer.assert_awaited_once_with()

    def test_check_status_exception_replaces_checking_state(self):
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
        callback_message = SimpleNamespace(message="payment status", buttons=None, photo=None, id=1, edit=AsyncMock())
        event = SimpleNamespace(sender_id=7, message=callback_message, answer=AsyncMock())
        with patch("plugins.deposit.repository.get_current_pending_auto_upi_order", return_value=pending_order()), \
            patch("plugins.deposit.verify_pending_order", new=AsyncMock(side_effect=RuntimeError("imap unavailable"))):
            asyncio.run(callback(event))

        self.assertEqual(callback_message.edit.await_args_list[0].args, (AUTO_UPI_CHECKING_TEXT,))
        self.assertIn("temporarily unavailable", callback_message.edit.await_args_list[1].args[0])

    def test_check_status_timeout_replaces_checking_state(self):
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
        callback_message = SimpleNamespace(message="payment status", buttons=None, photo=None, id=1, edit=AsyncMock())
        event = SimpleNamespace(sender_id=7, message=callback_message, answer=AsyncMock())

        async def slow_verification(*_args, **_kwargs):
            await asyncio.sleep(1)

        with patch("plugins.deposit.repository.get_current_pending_auto_upi_order", return_value=pending_order()), \
                patch("plugins.deposit.verify_pending_order", new=slow_verification), \
                patch("plugins.deposit.AUTO_UPI_VERIFICATION_TIMEOUT_SECONDS", 0.01):
            asyncio.run(callback(event))

        event.answer.assert_awaited_once_with()
        self.assertEqual(callback_message.edit.await_args_list[0].args, (AUTO_UPI_CHECKING_TEXT,))
        self.assertIn("timed out", callback_message.edit.await_args_list[1].args[0])

    def test_status_edit_uses_photo_caption_target_when_callback_message_is_media(self):
        callback_message = SimpleNamespace(
            id=10,
            message="QR caption",
            buttons=None,
            photo=object(),
            edit=AsyncMock(),
        )
        callback = SimpleNamespace(sender_id=7, message=callback_message, answer=AsyncMock())

        asyncio.run(_edit_auto_upi_status_message(callback, "Updated caption"))

        callback_message.edit.assert_awaited_once_with("Updated caption", buttons=None)

    def test_status_edit_handles_deleted_message_without_affecting_verification(self):
        callback_message = SimpleNamespace(
            id=11,
            message="Payment status",
            buttons=None,
            photo=None,
            edit=AsyncMock(side_effect=RuntimeError("message to edit not found")),
        )
        callback = SimpleNamespace(sender_id=7, message=callback_message, answer=AsyncMock())

        with patch("plugins.deposit.logger.exception") as log_exception:
            result = asyncio.run(_edit_auto_upi_status_message(callback, "Updated status"))

        self.assertFalse(result)
        callback.answer.assert_not_awaited()
        log_exception.assert_called()

    def test_paid_order_status_lookup_does_not_credit_again(self):
        paid_order = pending_order()
        paid_order.update({
            "status": "paid",
            "payable_amount": 100,
            "previous_balance": 25,
            "balance": 125,
            "verification_status": "AUTO VERIFIED",
        })

        async def exercise():
            with patch.object(auto_upi_verifier, "repository") as repository_mock:
                repository_mock.get_current_pending_auto_upi_order.return_value = None
                repository_mock.get_latest_auto_upi_order.return_value = paid_order
                return await auto_upi_verifier.verify_pending_order(paid_order, notify=False)

        result = asyncio.run(exercise())
        self.assertEqual(result["status"], "paid")
        self.assertFalse(result["credited"])
        self.assertTrue(result["already_processed"])
        self.assertEqual(result["result"]["balance"], 125)

    def test_background_notification_uses_auto_verified_success_text(self):
        telegram_bot = SimpleNamespace(send_message=AsyncMock())
        order = pending_order()
        result = {
            "user_id": 7,
            "amount": 100,
            "previous_balance": 25,
            "balance": 125,
        }
        with patch("plugins.deposit.process_referral_bonus", new=AsyncMock()):
            asyncio.run(auto_upi_verifier._notify_paid(order, result, telegram_bot))

        telegram_bot.send_message.assert_awaited_once_with(
            7, payment_success_text(100, 25, 125),
        )
        self.assertIn("🤖 𝐀ᴜᴛᴏ 𝐕ᴇʀɪғɪᴇᴅ", telegram_bot.send_message.await_args.args[1])

    def test_real_check_callback_reaches_payment_not_found(self):
        callback_bot = SimpleNamespace(handlers=[])
        callback_bot.on = lambda _pattern: lambda handler: callback_bot.handlers.append(handler) or handler
        register_deposit(callback_bot)
        callback = next(handler for handler in callback_bot.handlers if handler.__name__ == "cb_auto_upi_check")
        repository = MongoRepository(client=mongomock.MongoClient(), database_name="auto_upi_callback_pending")
        repository.ensure_indexes()
        repository.ensure_user(7)
        order = repository.create_auto_upi_order(7, 10, 10, "ORD20260924CALLBACK1", repository._now() + timedelta(minutes=5))
        message = SimpleNamespace(message="payment", buttons=None, photo=None, edit=AsyncMock())
        event = SimpleNamespace(
            sender_id=7,
            data=f"auto_upi_check:{order['order_id']}".encode(),
            pattern_match=SimpleNamespace(group=lambda _index: order["order_id"].encode()),
            message=message,
            answer=AsyncMock(),
        )
        FakeImap.raw_messages = []
        with patch.object(auto_upi_verifier, "repository", repository), \
                patch("plugins.deposit.repository", repository), \
                patch("utils.imap_verifier.get_imap_credentials", return_value=("test@example.com", "test-password")), \
                patch("utils.imap_verifier.imaplib.IMAP4_SSL", FakeImap):
            asyncio.run(callback(event))
        self.assertEqual(message.edit.await_args_list[0].args, (AUTO_UPI_CHECKING_TEXT,))
        self.assertIn("𝐏ᴀʏᴍᴇɴᴛ 𝐍ᴏᴛ 𝐅ᴏᴜɴᴅ", message.edit.await_args_list[-1].args[0])
        self.assertEqual(repository.get_user(7)["balance"], 0)

    def test_real_check_callback_credits_matching_payment_once(self):
        callback_bot = SimpleNamespace(handlers=[])
        callback_bot.on = lambda _pattern: lambda handler: callback_bot.handlers.append(handler) or handler
        register_deposit(callback_bot)
        callback = next(handler for handler in callback_bot.handlers if handler.__name__ == "cb_auto_upi_check")
        repository = MongoRepository(client=mongomock.MongoClient(), database_name="auto_upi_callback_paid")
        repository.ensure_indexes()
        repository.ensure_user(7)
        order = repository.create_auto_upi_order(7, 10, 10, "ORD20260924CALLBACK2", repository._now() + timedelta(minutes=5))
        raw_message = payment_email(order["order_id"], 10, datetime.now(timezone.utc))
        message = SimpleNamespace(message="payment", buttons=None, photo=None, edit=AsyncMock())
        event = SimpleNamespace(
            sender_id=7,
            data=f"auto_upi_check:{order['order_id']}".encode(),
            pattern_match=SimpleNamespace(group=lambda _index: order["order_id"].encode()),
            message=message,
            answer=AsyncMock(),
        )
        FakeImap.raw_messages = [raw_message]
        with patch.object(auto_upi_verifier, "repository", repository), \
                patch("plugins.deposit.repository", repository), \
                patch("utils.imap_verifier.get_imap_credentials", return_value=("test@example.com", "test-password")), \
                patch("utils.imap_verifier.imaplib.IMAP4_SSL", FakeImap):
            asyncio.run(callback(event))
            first_balance = repository.get_user(7)["balance"]
            asyncio.run(callback(event))
        self.assertEqual(first_balance, 10)
        self.assertEqual(repository.get_user(7)["balance"], 10)
        self.assertIn("𝐀ᴜᴛᴏ 𝐕ᴇʀɪғɪᴇᴅ", message.edit.await_args_list[1].args[0])


if __name__ == "__main__":
    unittest.main()
