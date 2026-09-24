import asyncio
from datetime import datetime, timezone

from database import repository
from config import logger, bot
from utils.imap_verifier import verify_auto_upi_order

VERIFICATION_INTERVAL_SECONDS = 20
_verifier_task = None


def _is_expired(order):
    expires_at = order.get("expires_at")
    if not expires_at:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= expires_at


def payment_not_found_text():
    return (
        "⏳ <b>Payment Not Found Yet</b>\n\n"
        "We couldn't detect your payment yet.\n"
        "If you have already paid, please wait a little and try again."
    )


def payment_success_text(amount, previous_balance, new_balance):
    return (
        "✅ <b>🎉 𝐃ᴇᴘᴏsɪᴛ 𝐀ᴘᴘʀᴏᴠᴇᴅ!</b>\n\n"
        f"💰 <b>𝐀ᴍᴏᴜɴᴛ 𝐀ᴅᴅᴇᴅ:</b> <b>₹{amount}</b>\n"
        f"📉 <b>𝐏ʀᴇᴠɪᴏᴜs 𝐁ᴀʟᴀɴᴄᴇ:</b> ₹{previous_balance}\n"
        f"📈 <b>𝐍ᴇᴡ 𝐁ᴀʟᴀɴᴄᴇ:</b> <b>₹{new_balance}</b>"
    )


def payment_expired_text():
    return (
        "❌ <b>PAYMENT EXPIRED</b>\n\n"
        "Payment was not detected within the allowed time."
    )


async def _notify_paid(order, result, telegram_bot):
    try:
        from plugins.deposit import process_referral_bonus
        await process_referral_bonus(result["user_id"], result["amount"])
    except Exception:
        logger.exception("Auto UPI referral bonus failed: order_id=%s", order.get("order_id"))
    try:
        await telegram_bot.send_message(
            int(result["user_id"]),
            payment_success_text(
                result["amount"], result["previous_balance"], result["balance"],
            ),
        )
    except Exception:
        logger.exception("Auto UPI success notification failed: order_id=%s", order.get("order_id"))


async def verify_pending_order(order, telegram_bot=bot, notify=True):
    """Verify and settle one pending order, returning its state transition."""
    if not order or order.get("status") != "pending":
        return {"status": order.get("status") if order else None, "credited": False}

    if _is_expired(order):
        expired = repository.expire_auto_upi_order(order["_id"])
        if expired:
            if notify:
                try:
                    await telegram_bot.send_message(int(order["user_id"]), payment_expired_text())
                except Exception:
                    logger.exception("Auto UPI expiry notification failed: order_id=%s", order.get("order_id"))
            return {"status": "expired", "credited": False}
        return {"status": "already_processed", "credited": False}

    verified, payment = await verify_auto_upi_order(order)
    if not verified:
        if payment == "Payment order has expired.":
            expired = repository.expire_auto_upi_order(order["_id"])
            if expired:
                if notify:
                    try:
                        await telegram_bot.send_message(int(order["user_id"]), payment_expired_text())
                    except Exception:
                        logger.exception("Auto UPI expiry notification failed: order_id=%s", order.get("order_id"))
                return {"status": "expired", "credited": False}
        return {"status": "pending", "credited": False, "reason": payment}

    result = repository.complete_auto_upi_order(order["_id"], payment)
    if result.get("credited"):
        if notify:
            await _notify_paid(order, result, telegram_bot)
        return {"status": "paid", "credited": True, "result": result}
    return {
        "status": result.get("status"),
        "credited": False,
        "already_processed": True,
        "result": result,
    }


async def verify_current_user_order(user_id, telegram_bot=bot, notify=True):
    order = repository.get_current_pending_auto_upi_order(user_id)
    if not order:
        return {"status": None, "credited": False, "reason": "no_pending_order"}
    result = await verify_pending_order(order, telegram_bot, notify=notify)
    result["order"] = order
    return result


async def _verification_loop(telegram_bot):
    while True:
        try:
            for order in repository.get_pending_auto_upi_orders():
                try:
                    await verify_pending_order(order, telegram_bot)
                except Exception:
                    logger.exception("Auto UPI background verification failed: order_id=%s", order.get("order_id"))
            await asyncio.sleep(VERIFICATION_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Auto UPI background verifier loop failed")
            await asyncio.sleep(VERIFICATION_INTERVAL_SECONDS)


def start_auto_upi_verifier(telegram_bot=bot):
    """Start at most one process-local verifier task."""
    global _verifier_task
    if _verifier_task is None or _verifier_task.done():
        _verifier_task = asyncio.create_task(_verification_loop(telegram_bot), name="auto-upi-verifier")
    return _verifier_task
