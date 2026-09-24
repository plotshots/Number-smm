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
        "❌ **❌ 𝐏ᴀʏᴍᴇɴᴛ 𝐍ᴏᴛ 𝐅ᴏᴜɴᴅ!**\n"
        "🔑 𝐍ᴏ ʀᴇᴄᴇɴᴛ 𝐩ᴀʏᴍᴇɴᴛ ᴡᴀs ғᴏᴜɴᴅ.\n"
        "• 𝐈ғ ʏᴏᴜ ᴊᴜsᴛ ᴘᴀɪᴅ, ᴘʟᴇᴀsᴇ ᴡᴀɪᴛ **1-2 ᴍɪɴᴜᴛᴇs** ᴀɴᴅ ᴛʀʏ 𝐚ɢᴀɪɴ.\n"
        "• 𝐎ʀ ᴜsᴇ **✍️ 𝐌ᴀɴᴜᴀʟ 𝐔𝐏𝐈** ᴛᴏ ᴜᴘʟᴏᴀᴅ ʏᴏᴜʀ 𝐩ᴀʏᴍᴇɴᴛ sᴄʀᴇᴇɴsʜᴏᴛ."
    )


def payment_success_text(amount, previous_balance, new_balance):
    return (
        "✅ 🎉 𝐃ᴇᴘᴏsɪᴛ 𝐀ᴘᴘʀᴏᴠᴇᴅ!\n\n"
        "🤖 𝐀ᴜᴛᴏ 𝐕ᴇʀɪғɪᴇᴅ\n\n"
        f"💰 𝐀ᴍᴏᴜɴᴛ 𝐀ᴅᴅᴇᴅ: ₹{amount}\n"
        f"📉 𝐏ʀᴇᴠɪᴏᴜs 𝐁ᴀʟᴀɴᴄᴇ: ₹{previous_balance}\n"
        f"📈 𝐍ᴇᴡ 𝐁ᴀʟᴀɴᴄᴇ: ₹{new_balance}"
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
        logger.info(
            "AUTO_UPI: already completed / duplicate prevented order_id=%s",
            order.get("order_id") if order else None,
        )
        return {"status": order.get("status") if order else None, "credited": False}

    logger.info("AUTO_UPI: verification started order_id=%s", order.get("order_id"))
    logger.info("AUTO_UPI: order identified order_id=%s", order.get("order_id"))

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
        logger.info("AUTO_UPI: payment completed order_id=%s", order.get("order_id"))
        if notify:
            await _notify_paid(order, result, telegram_bot)
        return {"status": "paid", "credited": True, "result": result}
    logger.info(
        "AUTO_UPI: already completed / duplicate prevented order_id=%s",
        order.get("order_id"),
    )
    return {
        "status": result.get("status"),
        "credited": False,
        "already_processed": True,
        "result": result,
    }


async def verify_current_user_order(user_id, telegram_bot=bot, notify=True):
    order = repository.get_current_pending_auto_upi_order(user_id)
    if not order:
        order = repository.get_latest_auto_upi_order(user_id)
        if order and order.get("status") == "paid":
            return {
                "status": "paid",
                "credited": False,
                "already_processed": True,
                "result": {
                    "amount": order.get("payable_amount", order.get("amount")),
                    "previous_balance": order.get("previous_balance"),
                    "balance": order.get("balance"),
                    "user_id": order.get("user_id"),
                    "order_id": order.get("order_id"),
                },
                "order": order,
            }
        if order and order.get("status") == "expired":
            return {"status": "expired", "credited": False, "order": order}
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
