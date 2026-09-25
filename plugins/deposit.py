import os
import re
import html
import urllib.parse
import io
import uuid
import asyncio
from datetime import datetime, timedelta, timezone
from telethon import events, Button
from telethon.errors import MessageNotModifiedError
from database import cur, db, get_usdt_rate, update_balance, approve_deposit, to_usd, get_log_channels_db, is_admin, repository
from config import AUTO_CANCEL_SECONDS, AUTO_UPI_ID, PE_GIFT, PE_LIGHTNING, P_MONEY, P_CARD, P_UPI, P_CW, P_NO, P_YES, P_WARN, P_INR, P_USDT, P_KEY, PE_CHECK, P_ACC, P_ID, LOG_CHANNEL_ID, LOG_CHANNELS, ADMIN_ID, SUPER_ADMINS, CWALLET_QR, CWALLET_ID, UPI_ID, bot, logger
from utils.keyboards import style_btn
from utils.states import deposit_input, waiting_proof, admin_dep_state, custom_dep_amt, get_user_lock
from utils.banners import send_bannered_message
from utils.auto_upi_verifier import (
    payment_expired_text,
    payment_not_found_text,
    payment_success_text,
    verify_pending_order,
)

AUTO_UPI_VERIFICATION_TIMEOUT_SECONDS = 45
AUTO_UPI_CHECKING_TEXT = "⏳ 𝐂ʜᴇᴄᴋɪɴɢ ʏᴏᴜʀ 𝐏ᴀʏᴍᴇɴᴛ..."
_AUTO_UPI_STATUS_MESSAGES = {}


def _auto_upi_status_key(user_id, order_id):
    return int(user_id), str(order_id) if order_id is not None else None


async def _delete_previous_auto_upi_status_message(status_key):
    previous_message = _AUTO_UPI_STATUS_MESSAGES.pop(status_key, None)
    if previous_message is None:
        return

    logger.info(
        "[AUTO_UPI_CHECK] deleting previous result message message_id=%s",
        getattr(previous_message, "id", None),
    )
    try:
        await previous_message.delete()
    except Exception:
        logger.info("[AUTO_UPI_CHECK] previous result message unavailable, continuing")
    else:
        logger.info("[AUTO_UPI_CHECK] previous result message deleted")


async def _delete_auto_upi_message(message, message_type):
    if message is None:
        return
    logger.info(
        "[AUTO_UPI_CHECK] deleting %s message message_id=%s",
        message_type,
        getattr(message, "id", None),
    )
    try:
        await message.delete()
    except Exception:
        logger.info(
            "[AUTO_UPI_CHECK] %s message unavailable, continuing",
            message_type,
        )


async def _send_auto_upi_checking_message(telegram_bot, user_id):
    logger.info("[AUTO_UPI_CHECK] sending immediate checking message")
    try:
        message = await telegram_bot.send_message(user_id, AUTO_UPI_CHECKING_TEXT)
    except Exception:
        logger.exception("[AUTO_UPI_CHECK] checking message send failed")
        return None
    logger.info(
        "[AUTO_UPI_CHECK] checking message sent message_id=%s",
        getattr(message, "id", None),
    )
    return message


async def _send_auto_upi_result_message(telegram_bot, user_id, status_key, text, buttons=None):
    logger.info("[AUTO_UPI_CHECK] sending final result message")
    try:
        message = await telegram_bot.send_message(user_id, text, buttons=buttons)
    except Exception:
        logger.exception("[AUTO_UPI_CHECK] final result message send failed")
        return None
    _AUTO_UPI_STATUS_MESSAGES[status_key] = message
    logger.info(
        "[AUTO_UPI_CHECK] final result message sent message_id=%s",
        getattr(message, "id", None),
    )
    return message

async def deposit_menu(event):
    btns = [
        [style_btn("⚡ 𝐀ᴜᴛᴏ 𝐔𝐏𝐈 (𝐈ɴsᴛᴀɴᴛ 𝐐𝐑 & 𝐔𝐓𝐑)", "dep_choose_AutoUPI", "success", icon=5409271925014801629)],
        [style_btn("✍️ 𝐌ᴀɴᴜᴀʟ 𝐔𝐏𝐈 (𝐒ᴄʀᴇᴇɴsʜᴏᴛ 𝐏ʀᴏᴏғ)", "dep_choose_ManualUPI", "primary", icon=5409098988156629257)],
        [style_btn("💎 𝐂ᴡᴀʟʟᴇᴛ (5% 𝐁𝐎𝐍𝐔𝐒)", "dep_choose_Cwallet", "primary", icon=5440627033111557670)]
    ]
    
    customs = cur.execute("SELECT name FROM custom_payments").fetchall()
    for c in customs:
        btns.append([style_btn(f"{c[0]}", f"dep_choose_{c[0]}", "primary", icon=5408832111773757273)])
        
    btns.append([style_btn("🔙 𝐁ᴀᴄᴋ ᴛᴏ 𝐃ᴀsʜʙᴏᴀʀᴅ", b"dashboard_main", "danger", icon=6129812419028982717)])
    
    msg = (f"<blockquote>💳 <b>𝐑ᴇᴄʜᴀʀɢᴇ / 𝐀ᴅᴅ 𝐅ᴜɴᴅs</b>\n\n"
           f"⚡ <b>𝐀ᴜᴛᴏ 𝐔𝐏𝐈:</b> 𝐈ɴsᴛᴀɴᴛ ᴀᴜᴛᴏ-ᴄʀᴇᴅɪᴛ ᴠɪᴀ 12-ᴅɪɢɪᴛ 𝐔𝐓𝐑.\n"
           f"✍️ <b>𝐌ᴀɴᴜᴀʟ 𝐔𝐏𝐈:</b> 𝐔ᴘʟᴏᴀᴅ ᴘᴀʏᴍᴇɴᴛ sᴄʀᴇᴇɴsʜᴏᴛ ғᴏʀ ᴀᴅᴍɪɴ ᴀᴘᴘʀᴏᴠᴀʟ.\n"
           f"💎 <b>𝐂ᴡᴀʟʟᴇᴛ:</b> 𝐂ʀʏᴘᴛᴏ ᴘᴀʏᴍᴇɴᴛ ᴡɪᴛʜ 5% ᴇxᴛʀᴀ ʙᴏɴᴜs.\n\n"
           f"<i>👇 𝐏ʟᴇᴀsᴇ sᴇʟᴇᴄᴛ ʏᴏᴜʀ ᴘʀᴇғᴇʀʀᴇᴅ ᴍᴇᴛʜᴏᴅ:</i></blockquote>")
           
    if await send_bannered_message(bot, event, "deposit", msg, btns):
        return
    if isinstance(event, events.CallbackQuery.Event):
        try: await event.edit(msg, buttons=btns)
        except MessageNotModifiedError: pass
    else: await event.respond(msg, buttons=btns)

async def manual_deposit_init(event, method):
    if method in ("AutoUPI", "ManualUPI"):
        uid = event.sender_id
        deposit_input[uid] = {'step': 'keypad', 'method': method, 'amount': ''}
        return await event.edit(
            f"{P_MONEY} <b>𝐄ɴᴛᴇʀ 𝐀ᴍᴏᴜɴᴛ</b>\n\n"
            f"<blockquote>𝐄ɴᴛᴇʀ ʏᴏᴜʀ 𝐑ᴇᴄʜᴀʀɢᴇ 𝐀ᴍᴏᴜɴᴛ ᴜsɪɴɢ ᴛʜᴇ ᴋᴇʏᴘᴀᴅ.</blockquote>",
            buttons=get_keypad(),
        )
    uid = event.sender_id
    deposit_input[uid] = {'step': 'wait_amt', 'method': method}
    await event.edit(f"{P_MONEY} <b>𝐄ɴᴛᴇʀ 𝐃ᴇᴘᴏsɪᴛ 𝐀ᴍᴏᴜɴᴛ (ɪɴ {P_INR}):</b>\n\n<i>𝐌ɪɴɪᴍᴜᴍ ᴅᴇᴘᴏsɪᴛ ɪs {P_INR}10.</i>", buttons=[[Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", "cancel_action")]])

async def process_referral_bonus(user_id, amt):
    try:
        row = cur.execute("SELECT referred_by FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row or not row[0]: return
        ref_id = row[0]
        
        pct_row = cur.execute("SELECT value FROM settings WHERE key='ref_percent'").fetchone()
        pct = int(pct_row[0]) if pct_row else 3
        
        bonus = int(amt * (pct / 100))
        if bonus <= 0: return
        
        async with get_user_lock(ref_id):
            update_balance(ref_id, bonus)
            db.commit()
            
        try: await bot.send_message(int(ref_id), f"{PE_GIFT} <b>Referral Bonus!</b>\nYour friend deposited {P_INR}{amt}. You received <b>{P_INR}{bonus}</b> ({pct}%) in your balance!")
        except Exception as exc:
            logger.warning("Referral bonus notification failed: error_type=%s", type(exc).__name__)
    except Exception as e: logger.error(f"Ref bonus error: {e}")

def get_admin_custom_keypad(dep_id):
    return [
        [style_btn("1", f"dkp|{dep_id}|1", "primary", icon=5375125990118793401), style_btn("2", f"dkp|{dep_id}|2", "primary", icon=5409098988156629257), style_btn("3", f"dkp|{dep_id}|3", "primary", icon=6154249597532248059)],
        [style_btn("4", f"dkp|{dep_id}|4", "primary", icon=5796170975699544141), style_btn("5", f"dkp|{dep_id}|5", "primary", icon=5409320020058584473), style_btn("6", f"dkp|{dep_id}|6", "primary", icon=5409098988156629257)],
        [style_btn("7", f"dkp|{dep_id}|7", "primary", icon=6129779562529168023), style_btn("8", f"dkp|{dep_id}|8", "primary", icon=5355292788923593967), style_btn("9", f"dkp|{dep_id}|9", "primary", icon=5408832111773757273)],
        [style_btn("Del", f"dkp|{dep_id}|del", "danger", icon=6129732880529628243), style_btn("0", f"dkp|{dep_id}|0", "primary", icon=6154249597532248059), style_btn("Confirm", f"dkp|{dep_id}|conf", "success", 5409098988156629257, icon=5409320020058584473)],
        [style_btn("𝐂ᴀɴᴄᴇʟ", f"dkp|{dep_id}|cancel", "danger", icon=6129888444245089008)]
    ]

def get_manual_deposit(deposit_id):
    return repository.get_deposit(deposit_id)


# We will skip the automated UPI part in this script to save space if needed, 
# or I can port it directly. The user had a keypad logic for UPI amounts.
def get_keypad():
    return [
        [style_btn("1", b"kp_1", style_type="primary", icon=5408832111773757273), style_btn("2", b"kp_2", style_type="primary", icon=5408832111773757273), style_btn("3", b"kp_3", style_type="primary", icon=6129888444245089008)],
        [style_btn("4", b"kp_4", style_type="primary", icon=6064275556008989746), style_btn("5", b"kp_5", style_type="primary", icon=6129627894349045589), style_btn("6", b"kp_6", style_type="primary", icon=5409320020058584473)],
        [style_btn("7", b"kp_7", style_type="primary", icon=5375125990118793401), style_btn("8", b"kp_8", style_type="primary", icon=6129731974291527294), style_btn("9", b"kp_9", style_type="primary", icon=6170048080679801421)],
        [style_btn("Del", b"kp_del", style_type="danger", icon=6203982793379154737), style_btn("0", b"kp_0", style_type="primary", icon=5408832111773757273), style_btn("Confirm", b"kp_done", style_type="success", icon=6064310143380625195)],
        [style_btn("𝐂ᴀɴᴄᴇʟ", b"cancel_action", style_type="danger", icon=5796170975699544141)]
    ]

def _keypad_message(amount):
    shown_amount = amount or "0"
    return f"{P_MONEY} <b>𝐄ɴᴛᴇʀ 𝐀ᴍᴏᴜɴᴛ</b>\n\n<blockquote>{P_INR}<code>{shown_amount}</code></blockquote>"

def _build_auto_upi_uri(active_upi, order):
    purpose = order["order_id"]
    params = f"pa={active_upi}&" + urllib.parse.urlencode({
        "pn": "Numbott",
        "am": f"{order['payable_amount']:.2f}",
        "cu": "INR",
        "tn": purpose,
        "tr": purpose,
    })
    return f"upi://pay?{params}"


def generate_auto_upi_order_id(created_at=None, suffix=None):
    created_at = created_at or datetime.now(timezone.utc)
    suffix = (suffix or uuid.uuid4().hex[:8]).upper()
    return f"ORD{created_at:%Y%m%d}{suffix}"


async def create_auto_upi_payment(event, amount):
    uid = event.sender_id
    amount = int(amount)
    purpose = generate_auto_upi_order_id()
    expires_at = repository._now() + timedelta(seconds=AUTO_CANCEL_SECONDS)
    order = repository.create_auto_upi_order(uid, amount, amount, purpose, expires_at)

    upi_url = _build_auto_upi_uri(AUTO_UPI_ID, order)
    logger.info("AUTO_UPI: generated payment URI order_id=%s", order["order_id"])
    try:
        import qrcode
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(upi_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        qr_file = io.BytesIO()
        qr_file.name = "upi_qr.png"
        img.save(qr_file, "PNG")
        qr_file.seek(0)
        await bot.send_file(uid, qr_file)
        payment_message = (
            "🏦 𝐀ᴜᴛᴏᴍᴀᴛɪᴄ 𝐏ᴀʏᴍᴇɴᴛ (𝐔𝐏𝐈)\n\n"
            f"💰 𝐀ᴍᴏᴜɴᴛ: ₹{order['payable_amount']}\n"
            f"🆔 𝐎ʀᴅᴇʀ 𝐈ᴅ: {order['order_id']}\n\n"
            "👇 𝐒ᴄᴀɴ ᴛʜᴇ 𝐐ʀ ᴀʙᴏᴠᴇ.\n"
            "𝐂ʟɪᴄᴋ ✅ 𝐂ʜᴇᴄᴋ 𝐏ᴀʏᴍᴇɴᴛ 𝐒ᴛᴀᴛᴜs ᴀғᴛᴇʀ ᴘᴀʏɪɴɢ."
        )
        await bot.send_message(
            uid,
            payment_message,
            buttons=[
                [Button.inline(
                    "✅ 𝐂ʜᴇᴄᴋ 𝐏ᴀʏᴍᴇɴᴛ 𝐒ᴛᴀᴛᴜs",
                    f"auto_upi_check:{order['order_id']}".encode(),
                )],
                [Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", b"auto_upi_cancel")],
            ],
        )
    except Exception as exc:
        logger.error("Failed to send Auto UPI QR: %s", exc)
        await bot.send_message(uid, "❌ Unable to generate the payment QR right now. Please try again.")

def register_deposit(bot):
    _AUTO_UPI_STATUS_MESSAGES.clear()

    @bot.on(events.NewMessage(pattern=r"(?i)^(💳 𝐃ᴇᴘᴏsɪᴛ|💳 Deposit)$"))
    async def msg_deposit(e):
        await deposit_menu(e)

    @bot.on(events.CallbackQuery(pattern=r"^(open_deposit_menu|deposit_menu|depm_menu_main|depm_upi)$"))
    async def cb_deposit_menu_main(e):
        await deposit_menu(e)

    @bot.on(events.CallbackQuery(pattern=rb"^auto_upi_check(?::([A-Za-z0-9_-]+))?$"))
    async def cb_auto_upi_check(e):
        raw_callback_data = getattr(e, "data", b"auto_upi_check")
        callback_data = raw_callback_data.decode() if isinstance(raw_callback_data, bytes) else str(raw_callback_data)
        pattern_match = getattr(e, "pattern_match", None)
        order_id = pattern_match.group(1) if pattern_match else None
        if isinstance(order_id, bytes):
            order_id = order_id.decode()
        if not order_id and callback_data.startswith("auto_upi_check:"):
            order_id = callback_data.partition(":")[2] or None

        logger.info("[AUTO_UPI_CHECK] callback received user_id=%s", e.sender_id)
        logger.info("[AUTO_UPI_CHECK] callback_data=%s", callback_data)
        logger.info("[AUTO_UPI_CHECK] order_id=%s", order_id)
        try:
            await e.answer()
        except Exception:
            logger.exception("[AUTO_UPI_CHECK] callback answer failed")
        else:
            logger.info("[AUTO_UPI_CHECK] callback answered")

        order = repository.get_auto_upi_order(e.sender_id, order_id) if order_id else repository.get_current_pending_auto_upi_order(e.sender_id)
        pending_order = order if order and order.get("status") == "pending" else None
        resolved_order_id = order.get("order_id") if order else order_id
        status_key = _auto_upi_status_key(e.sender_id, resolved_order_id)
        logger.info("[AUTO_UPI_CHECK] order found=%s order_id=%s", bool(order), resolved_order_id)
        logger.info("[AUTO_UPI_CHECK] order status=%s", order.get("status") if order else None)
        await _delete_previous_auto_upi_status_message(status_key)
        checking_message = await _send_auto_upi_checking_message(bot, e.sender_id)

        async def send_final_result(text, buttons=None):
            await _delete_auto_upi_message(checking_message, "checking")
            return await _send_auto_upi_result_message(
                bot,
                e.sender_id,
                status_key,
                text,
                buttons=buttons,
            )

        if not pending_order:
            if order and order.get("status") == "expired":
                logger.info("[AUTO_UPI_CHECK] setting final result=expired")
                final_text = payment_expired_text()
            elif order and order.get("status") == "paid":
                success_text = payment_success_text(
                    order.get("payable_amount", order.get("amount")),
                    order.get("previous_balance"),
                    order.get("balance"),
                )
                logger.info("[AUTO_UPI_CHECK] setting final result=already_completed")
                final_text = success_text
            else:
                logger.info("[AUTO_UPI_CHECK] setting final result=not_found")
                final_text = payment_not_found_text()
            await send_final_result(final_text)
            logger.info("[AUTO_UPI_CHECK] callback completed")
            return

        logger.info("[AUTO_UPI_CHECK] verification started order_id=%s", resolved_order_id)
        try:
            verification = verify_pending_order(pending_order, bot, notify=False)
            result = await asyncio.wait_for(
                verification,
                timeout=AUTO_UPI_VERIFICATION_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error("[AUTO_UPI_CHECK] verification timeout order_id=%s", resolved_order_id)
            final_text = (
                "⚠️ <b>Payment verification timed out.</b>\nPlease try again shortly."
            )
            await send_final_result(final_text)
            logger.info("[AUTO_UPI_CHECK] callback completed")
            return
        except Exception:
            logger.exception("[AUTO_UPI_CHECK] verification exception order_id=%s", resolved_order_id)
            final_text = (
                "⚠️ <b>Payment verification is temporarily unavailable.</b>\nPlease try again shortly."
            )
            await send_final_result(final_text)
            logger.info("[AUTO_UPI_CHECK] callback completed")
            return

        status = result.get("status")
        logger.info("[AUTO_UPI_CHECK] verification result=%s", status)
        final_buttons = None
        if status == "paid":
            payment = result.get("result") or {}
            final_text = payment_success_text(
                payment.get("amount", pending_order.get("payable_amount")),
                payment.get("previous_balance"),
                payment.get("balance"),
            )
            logger.info("[AUTO_UPI_CHECK] setting final result=paid")
        elif status == "expired":
            logger.info("[AUTO_UPI_CHECK] setting final result=expired")
            final_text = payment_expired_text()
        else:
            not_found_buttons = [
                [Button.inline(
                    "✅ 𝐂ʜᴇᴄᴋ 𝐏ᴀʏᴍᴇɴᴛ 𝐒ᴛᴀᴛᴜs",
                    f"auto_upi_check:{resolved_order_id}".encode(),
                )],
                [Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", b"auto_upi_cancel")],
            ]
            logger.info("[AUTO_UPI_CHECK] setting final result=not_found")
            final_buttons = not_found_buttons
            final_text = payment_not_found_text()
        await send_final_result(final_text, buttons=final_buttons)
        logger.info("[AUTO_UPI_CHECK] callback completed")

    @bot.on(events.CallbackQuery(pattern=b"^auto_upi_cancel$"))
    async def cb_auto_upi_cancel(e):
        order = repository.get_current_pending_auto_upi_order(e.sender_id)
        if order:
            repository.cancel_auto_upi_order(e.sender_id, order["_id"])
        await e.answer("Payment cancelled.", alert=False)
        await deposit_menu(e)

    @bot.on(events.CallbackQuery(pattern=r"^dep_choose_(.+)$"))
    async def cb_choose_dep_method(e):
        method = e.pattern_match.group(1).decode()
        await manual_deposit_init(e, method)

    @bot.on(events.CallbackQuery(pattern=r"^kp_(\d|del|done)$"))
    async def cb_auto_upi_keypad(e):
        uid = e.sender_id
        state = deposit_input.get(uid)
        if not state or state.get('step') != 'keypad' or state.get('method') not in ("AutoUPI", "ManualUPI"):
            return await e.answer("This payment session has expired.", alert=True)

        key = e.pattern_match.group(1).decode()
        amount = state.get('amount', '')
        if key == 'del':
            state['amount'] = amount[:-1]
        elif key == 'done':
            try:
                parsed_amount = int(amount)
            except ValueError:
                parsed_amount = 0
            if parsed_amount < 10:
                return await e.answer("Minimum recharge is ₹10.", alert=True)
            if parsed_amount > 50000:
                return await e.answer("Maximum recharge is ₹50,000.", alert=True)
            if state.get('method') == "ManualUPI":
                state['step'] = 'wait_amt'
                try:
                    await msg_wait_amt(e, amount_override=parsed_amount)
                except events.StopPropagation:
                    pass
                return
            deposit_input.pop(uid, None)
            await e.answer("Payment created", alert=False)
            return await create_auto_upi_payment(e, parsed_amount)
        elif len(amount) < 7:
            state['amount'] = amount + key
        try:
            await e.edit(_keypad_message(state.get('amount', '')), buttons=get_keypad())
        except MessageNotModifiedError:
            pass

    @bot.on(events.NewMessage(func=lambda e: e.sender_id in deposit_input and deposit_input[e.sender_id]['step'] == 'wait_amt'))
    async def msg_wait_amt(e, amount_override=None):
        uid = e.sender_id
        text = str(amount_override) if amount_override is not None else (e.text or "").strip()
        
        # Check if user sent photo/document/link/letters instead of pure numbers
        if amount_override is None and (e.photo or e.document or e.media or not text.isdigit()):
            return await e.reply(f"<blockquote>{P_NO} <b>❌ 𝐈ɴᴠᴀʟɪᴅ 𝐀ᴍᴏᴜɴᴛ!</b>\n\n"
                                 f"𝐏ʟᴇᴀsᴇ ᴇɴᴛᴇʀ a valid <b>numeric amount</b> (digits only, e.g. <code>50</code>, <code>100</code>, <code>500</code>).\n"
                                 f"<i>𝐋ɪɴᴋs, sᴄʀᴇᴇɴsʜᴏᴛs, ʟᴇᴛᴛᴇʀs ᴏʀ sᴘᴇᴄɪᴀʟ ᴄʜᴀʀᴀᴄᴛᴇʀs ᴀʀᴇ ɴᴏᴛ ᴀʟʟᴏᴡᴇᴅ.</i></blockquote>",
                                 buttons=[[Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", "cancel_action")]])
        try:
            amt = int(text)
            if amt < 10: 
                return await e.reply(f"<blockquote>{P_WARN} <b>Minimum Deposit is {P_INR}10.</b>\n𝐏ʟᴇᴀsᴇ ᴇɴᴛᴇʀ {P_INR}10 ᴏʀ ᴍᴏʀᴇ:</blockquote>",
                                     buttons=[[Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", "cancel_action")]])
            if amt > 50000:
                return await e.reply(f"<blockquote>{P_WARN} <b>Maximum Deposit is {P_INR}50,000.</b>\n𝐏ʟᴇᴀsᴇ ᴇɴᴛᴇʀ a smaller amount:</blockquote>",
                                     buttons=[[Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", "cancel_action")]])
                
            method = deposit_input[uid]['method']
            waiting_proof[uid] = {'amount': amt, 'method': method}
            deposit_input.pop(uid)
            
            rate = get_usdt_rate()
            usdt_amt = round(amt / rate, 2)
            rate_text = f"<blockquote>{P_MONEY} <b>𝐀ᴍᴏᴜɴᴛ ᴛᴏ 𝐏ᴀʏ:</b> {P_INR}{amt} (~{P_USDT}{usdt_amt} USDT)\n💱 <i>𝐄xᴄʜᴀɴɢᴇ 𝐑ᴀᴛᴇ: {P_INR}{rate} = $1</i></blockquote>"
            
            if method == "Cwallet":
                msg = (f"<blockquote>{P_CARD} <b>𝐌ᴇᴛʜᴏᴅ:</b> {method}\n\n🚀 <b>𝐀ᴅᴅʀᴇss / 𝐈𝐃:</b>\n<code>{CWALLET_ID}</code></blockquote>\n"
                       f"{rate_text}\n"
                       f"<blockquote>👉 <b>𝐒ᴇɴᴅ 𝐏ʀᴏᴏғ:</b>\n𝐏ʟᴇᴀsᴇ sᴇɴᴅ ᴛʜᴇ 𝐓ʀᴀɴsᴀᴄᴛɪᴏɴ 𝐇ᴀsʜ (𝐋ɪɴᴋ) ᴏʀ ᴀ 𝐒ᴄʀᴇᴇɴsʜᴏᴛ ᴏғ ᴛʜᴇ ᴘᴀʏᴍᴇɴᴛ ɴᴏᴡ.</blockquote>")
                try: await bot.send_file(uid, CWALLET_QR, caption=msg, buttons=[[Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", "cancel_action")]])
                except Exception: await bot.send_message(uid, msg + f"\n\n🔗 QR Link: {CWALLET_QR}", buttons=[[Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", "cancel_action")]])
            elif method in ("AutoUPI", "UPI"):
                upi_res = cur.execute("SELECT value FROM settings WHERE key='upi_id'").fetchone()
                active_upi = upi_res[0] if upi_res and upi_res[0] else UPI_ID
                
                upi_url = f"upi://pay?pa={active_upi}&am={amt}&cu=INR"
                instruction = "👉 <b>𝐀ғᴛᴇʀ 𝐏ᴀʏɪɴɢ:</b>\n𝐏ʟᴇᴀsᴇ ᴇɴᴛᴇʀ ʏᴏᴜʀ <b>12-ᴅɪɢɪᴛ 𝐔𝐓𝐑 / 𝐑ᴇғ 𝐍ᴏ.</b> (ᴏʀ 𝐓ʀᴀɴsᴀᴄᴛɪᴏɴ 𝐈𝐃) ʙᴇʟᴏᴡ:"
                    
                msg = (f"<blockquote>⚡ <b>𝐀ᴜᴛᴏ 𝐔𝐏𝐈 𝐃ᴇᴘᴏsɪᴛ (𝐈ɴsᴛᴀɴᴛ)</b>\n\n🆔 <b>UPI ID:</b>\n<code>{active_upi}</code></blockquote>\n"
                       f"{rate_text}\n"
                       f"<blockquote>{instruction}</blockquote>")
                try: 
                    import qrcode
                    qr = qrcode.QRCode(version=1, box_size=10, border=4)
                    qr.add_data(upi_url)
                    qr.make(fit=True)
                    img = qr.make_image(fill_color="black", back_color="white")
                    
                    qr_file = io.BytesIO()
                    qr_file.name = "upi_qr.png"
                    img.save(qr_file, "PNG")
                    qr_file.seek(0)
                    
                    await bot.send_file(uid, qr_file, caption=msg, buttons=[[Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", "cancel_action")]])
                except Exception as e: 
                    logger.error(f"Failed to send UPI QR: {e}")
                    await bot.send_message(uid, msg, buttons=[[Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", "cancel_action")]])
            elif method == "ManualUPI":
                upi_res = cur.execute("SELECT value FROM settings WHERE key='upi_id'").fetchone()
                active_upi = upi_res[0] if upi_res and upi_res[0] else UPI_ID
                
                upi_url = f"upi://pay?pa={active_upi}&am={amt}&cu=INR"
                instruction = "👉 <b>𝐒ᴇɴᴅ 𝐏ʀᴏᴏғ:</b>\n𝐏ʟᴇᴀsᴇ sᴇɴᴅ ᴀ ᴄʟᴇᴀʀ <b>𝐒ᴄʀᴇᴇɴsʜᴏᴛ</b> ᴏғ ᴛʜᴇ ᴘᴀʏᴍᴇɴᴛ ɴᴏᴡ. 𝐎ᴜʀ ᴀᴅᴍɪɴ ᴡɪʟʟ ᴠᴇʀɪғʏ ᴀɴᴅ ᴀᴘᴘʀᴏᴠᴇ ɪɴsᴛᴀɴᴛʟʏ."
                    
                msg = (f"<blockquote>✍️ <b>𝐌ᴀɴᴜᴀʟ 𝐔𝐏𝐈 𝐃ᴇᴘᴏsɪᴛ</b>\n\n🆔 <b>UPI ID:</b>\n<code>{active_upi}</code></blockquote>\n"
                       f"{rate_text}\n"
                       f"<blockquote>{instruction}</blockquote>")
                try: 
                    import qrcode
                    qr = qrcode.QRCode(version=1, box_size=10, border=4)
                    qr.add_data(upi_url)
                    qr.make(fit=True)
                    img = qr.make_image(fill_color="black", back_color="white")
                    
                    qr_file = io.BytesIO()
                    qr_file.name = "upi_qr.png"
                    img.save(qr_file, "PNG")
                    qr_file.seek(0)
                    
                    await bot.send_file(uid, qr_file, caption=msg, buttons=[[Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", "cancel_action")]])
                except Exception as e: 
                    logger.error(f"Failed to send UPI QR: {e}")
                    await bot.send_message(uid, msg, buttons=[[Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", "cancel_action")]])
            else:
                row = cur.execute("SELECT caption, qr_file_id FROM custom_payments WHERE name=?", (method,)).fetchone()
                if row:
                    cap = f"<blockquote>{row[0]}</blockquote>\n{rate_text}\n<blockquote>👇 <b>𝐀ғᴛᴇʀ ᴘᴀʏɪɴɢ, sᴇɴᴅ ᴀ ᴄʟᴇᴀʀ 𝐒ᴄʀᴇᴇɴsʜᴏᴛ ʜᴇʀᴇ:</b></blockquote>"
                    btns = [[Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", "cancel_action")]]
                    if row[1] and os.path.exists(row[1]): 
                        try: await bot.send_file(e.chat_id, row[1], caption=cap, buttons=btns)
                        except: await e.reply(cap, buttons=btns)
                    else: await e.reply(cap, buttons=btns)
                else: await e.reply(f"{P_CARD} <b>{method} Deposit</b>{rate_text}\n\n👇 Send Screenshot here:", buttons=[[Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", "cancel_action")]])
            raise events.StopPropagation
        except ValueError: 
            await e.respond(f"{P_NO} Please enter a valid number in {P_INR} (INR).")
            raise events.StopPropagation
        except events.StopPropagation:
            raise
        except Exception as e_amt:
            logger.error(f"Error in msg_wait_amt: {e_amt}")

    @bot.on(events.NewMessage(func=lambda e: e.sender_id in waiting_proof and (e.photo or e.document or e.media or (e.text and not e.text.startswith('/')))))
    async def msg_wait_proof(e):
        uid = e.sender_id
        info = waiting_proof.get(uid)
        if not info:
            logger.warning("Manual deposit proof received without pending state: user_id=%s message_id=%s", uid, e.id)
            return await e.reply("⚠️ Your deposit session has expired. Please start Manual Deposit again.")
        final_amt = info['amount']
        if info['method'] == "Cwallet": final_amt = int(final_amt * 1.05)
        
        # 1. AUTO-UPI / IMAP UTR FLOW
        if info['method'] in ("AutoUPI", "UPI") and e.text and not (e.photo or e.document or e.media):
            utr_input = re.sub(r'[^0-9A-Za-z]', '', e.text.strip())
            if len(utr_input) < 6:
                waiting_proof[uid] = info
                return await e.reply(f"<blockquote>{P_WARN} <b>Invalid UTR!</b>\n\n𝐏ʟᴇᴀsᴇ ᴇɴᴛᴇʀ ʏᴏᴜʀ valid <b>12-ᴅɪɢɪᴛ 𝐔𝐓𝐑 / 𝐑ᴇғᴇʀᴇɴᴄᴇ 𝐍ᴏ.</b> (ᴏʀ 𝐓ʀᴀɴsᴀᴄᴛɪᴏɴ 𝐈𝐃).</blockquote>")
            
            from database import is_payment_redeemed_db, record_redeemed_payment_db

            # Anti-Duplicate UTR / TxnID Pre-check
            if is_payment_redeemed_db(utr=utr_input, txn_id=utr_input):
                waiting_proof[uid] = info  # keep active
                return await e.reply(f"<blockquote>{P_NO} <b>❌ 𝐓ʀᴀɴsᴀᴄᴛɪᴏɴ 𝐀ʟʀᴇᴀᴅʏ 𝐔sᴇᴅ!</b>\n\n𝐓ʜɪs 𝐔𝐓𝐑 / 𝐓ʀᴀɴsᴀᴄᴛɪᴏɴ 𝐈𝐃 (<code>{utr_input}</code>) ʜᴀs ᴀʟʀᴇᴀᴅʏ ʙᴇᴇɴ ʀᴇᴅᴇᴇᴍᴇᴅ!\n<i>𝐃ᴜᴘʟɪᴄᴀᴛᴇ ᴏʀ ʀᴇᴜsᴇᴅ ᴘᴀʏᴍᴇɴᴛs ᴀʀᴇ sᴛʀɪᴄᴛʟʏ ᴘʀᴏʜɪʙɪᴛᴇᴅ.</i></blockquote>")
            
            dep_mode_res = cur.execute("SELECT value FROM settings WHERE key='deposit_mode'").fetchone()
            dep_mode = dep_mode_res[0] if dep_mode_res and dep_mode_res[0] else "auto"
            
            if dep_mode in ("auto", "hybrid"):
                status_msg = await e.reply(f"<blockquote>⏳ <b>𝐕ᴇʀɪғʏɪɴɢ ʏᴏᴜʀ ᴘᴀʏᴍᴇɴᴛ...</b>\n𝐂ʜᴇᴄᴋɪɴɢ 𝐔𝐓𝐑 <code>{utr_input}</code> ᴡɪᴛʜ ʙᴀɴᴋ sᴇʀᴠᴇʀs. 𝐏ʟᴇᴀsᴇ ᴡᴀɪᴛ...</blockquote>")
                from utils.imap_verifier import verify_payment_utr
                ok, v_res = await verify_payment_utr(utr_input)
                
                if ok:
                    # Multi-Identifier Check (Email Message-ID, UTR, TxnID)
                    msg_id = v_res.get('email_msg_id')
                    det_utr = v_res.get('detected_utr')
                    det_txn = v_res.get('detected_txnid')
                    
                    if is_payment_redeemed_db(email_msg_id=msg_id, utr=det_utr, txn_id=det_txn):
                        waiting_proof[uid] = info
                        fail_text = (f"<blockquote>{P_NO} <b>❌ 𝐏ᴀʏᴍᴇɴᴛ 𝐀ʟʀᴇᴀᴅʏ 𝐑ᴇᴅᴇᴇᴍᴇᴅ!</b>\n\n"
                                     f"𝐓ʜɪs ᴘᴀʏᴍᴇɴᴛ ʜᴀs ᴀʟʀᴇᴀᴅʏ ʙᴇᴇɴ ᴄʀᴇᴅɪᴛᴇᴅ ᴘʀᴇᴠɪᴏᴜsʟʏ (ᴜsɪɴɢ 𝐔𝐓𝐑/𝐓ʀᴀɴsᴀᴄᴛɪᴏɴ 𝐈𝐃).\n"
                                     f"<i>𝐄ᴀᴄʜ ᴘᴀʏᴍᴇɴᴛ ᴄᴀɴ ᴏɴʟʏ ʙᴇ ʀᴇᴅᴇᴇᴍᴇᴅ ᴏɴᴄᴇ.</i></blockquote>")
                        try: await status_msg.edit(fail_text, buttons=[[Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", "cancel_action")]])
                        except: await e.reply(fail_text, buttons=[[Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", "cancel_action")]])
                        return

                    credited_amt = v_res.get('amount') or final_amt
                    async with get_user_lock(uid):
                        prev_row = cur.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
                        prev_bal = prev_row[0] if prev_row else 0
                        update_balance(uid, credited_amt)
                        cur.execute("INSERT INTO deposits (user_id, amount, method_name, status, utr) VALUES (?, ?, 'UPI (Auto)', 'approved', ?)",
                                    (uid, credited_amt, utr_input))
                        cur.execute("UPDATE users SET total_deposited = total_deposited + ? WHERE user_id=?", (credited_amt, uid))
                        
                        # Lock all identifiers so neither UTR, TxnID, nor Email can ever be used again!
                        record_redeemed_payment_db(msg_id, det_utr, det_txn, credited_amt, uid)
                        db.commit()
                        
                    await process_referral_bonus(uid, credited_amt)
                    
                    new_bal = prev_bal + credited_amt
                    success_text = (f"<blockquote>{PE_CHECK} <b>🎉 𝐏ᴀʏᴍᴇɴᴛ 𝐕ᴇʀɪғɪᴇᴅ & 𝐂ʀᴇᴅɪᴛᴇᴅ!</b>\n\n"
                                    f"{P_MONEY} <b>𝐀ᴍᴏᴜɴᴛ 𝐀ᴅᴅᴇᴅ:</b> <b>{P_INR}{credited_amt}</b> (${to_usd(credited_amt):.2f})\n"
                                    f"🔑 <b>𝐔𝐓𝐑:</b> <code>{utr_input}</code>\n"
                                    f"👤 <b>𝐒ᴇɴᴅᴇʀ:</b> <code>{v_res.get('sender', 'User')}</code>\n"
                                    f"📈 <b>𝐍ᴇᴡ 𝐁ᴀʟᴀɴᴄᴇ:</b> <b>{P_INR}{new_bal}</b> (${to_usd(new_bal):.2f})</blockquote>")
                    btns = [
                        [style_btn("📲 𝐁ᴜʏ 𝐀ᴄᴄᴏᴜɴᴛ", "open_buy_categories", "success", icon=5440627033111557670)],
                        [style_btn("🔙 𝐁ᴀᴄᴋ ᴛᴏ 𝐃ᴀsʜʙᴏᴀʀᴅ", "dashboard_main", "danger", icon=6129812419028982717)]
                    ]
                    try: await status_msg.edit(success_text, buttons=btns)
                    except Exception as exc:
                        logger.warning("Auto deposit success message edit failed; using reply fallback: error_type=%s", type(exc).__name__)
                        await e.reply(success_text, buttons=btns)
                    
                    for log_ch in get_log_channels_db():
                        try:
                            await bot.send_message(log_ch, f"<blockquote>⚡ <b>✅ 𝐀𝐔𝐓𝐎-𝐔𝐏𝐈 𝐃𝐄𝐏𝐎𝐒𝐈𝐓 𝐕𝐄𝐑𝐈𝐅𝐈𝐄𝐃</b>\n\n👤 <b>User:</b> <code>{uid}</code>\n💰 <b>Amount:</b> <b>{P_INR}{credited_amt}</b>\n🔑 <b>UTR:</b> <code>{utr_input}</code>\n💳 <b>Sender:</b> {v_res.get('sender')}</blockquote>")
                        except Exception as log_ex:
                            logger.error(f"Failed to log auto deposit to {log_ch}: {log_ex}")
                    return
                else:
                    waiting_proof[uid] = info
                    fail_text = (f"<blockquote>{P_NO} <b>❌ 𝐏ᴀʏᴍᴇɴᴛ 𝐍ᴏᴛ 𝐅ᴏᴜɴᴅ!</b>\n\n"
                                 f"🔑 <b>𝐔𝐓𝐑:</b> <code>{utr_input}</code>\n\n"
                                 f"𝐍ᴏ ʀᴇᴄᴇɴᴛ ᴘᴀʏᴍᴇɴᴛ ᴡᴀs ғᴏᴜɴᴅ ғᴏʀ ᴛʜɪs 𝐔𝐓𝐑.\n"
                                 f"• 𝐈ғ ʏᴏᴜ ᴊᴜsᴛ ᴘᴀɪᴅ, ᴘʟᴇᴀsᴇ ᴡᴀɪᴛ <b>1-2 ᴍɪɴᴜᴛᴇs</b> ᴀɴᴅ ᴛʀʏ ᴀɢᴀɪɴ.\n"
                                 f"• 𝐎ʀ ᴜsᴇ <b>✍️ 𝐌ᴀɴᴜᴀʟ 𝐔𝐏𝐈</b> ᴛᴏ ᴜᴘʟᴏᴀᴅ ʏᴏᴜʀ ᴘᴀʏᴍᴇɴᴛ sᴄʀᴇᴇɴsʜᴏᴛ.</blockquote>")
                    try: await status_msg.edit(fail_text, buttons=[[Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", "cancel_action")]])
                    except Exception as exc:
                        logger.warning("Auto deposit failure message edit failed; using reply fallback: error_type=%s", type(exc).__name__)
                        await e.reply(fail_text, buttons=[[Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", "cancel_action")]])
                    return

        # 2. MANUAL SCREENSHOT / PROOF FLOW
        screenshot_file_id = getattr(getattr(e, "file", None), "id", None)
        try:
            deposit, created = repository.create_manual_deposit(
                uid, final_amt, info['method'], screenshot_file_id,
                e.chat_id or uid, e.id,
            )
        except Exception:
            waiting_proof[uid] = info
            logger.exception("Manual deposit MongoDB write failed: user_id=%s message_id=%s", uid, e.id)
            return await e.reply("❌ We could not submit your deposit right now. Please try again.")

        waiting_proof.pop(uid, None)
        dep_id = deposit["_id"]
        if not created:
            logger.info("Duplicate manual deposit proof ignored: user_id=%s message_id=%s deposit_id=%s", uid, e.id, dep_id)
            return await e.reply("⏳ This deposit screenshot has already been submitted for review.")
        
        await e.reply(f"<blockquote>{PE_GIFT} <b>𝐃ᴇᴘᴏsɪᴛ ʀᴇǫᴜᴇsᴛ sᴜʙᴍɪᴛᴛᴇᴅ!</b>\n\n⏳ 𝐏ʟᴇᴀsᴇ ᴡᴀɪᴛ ᴡʜɪʟᴇ ᴀɴ ᴀᴅᴍɪɴ ᴠᴇʀɪғɪᴇs ʏᴏᴜʀ ᴘᴀʏᴍᴇɴᴛ. 𝐘ᴏᴜʀ ʙᴀʟᴀɴᴄᴇ ᴡɪʟʟ ʙᴇ ᴄʀᴇᴅɪᴛᴇᴅ ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ!</blockquote>")
        
        rate = get_usdt_rate()
        usdt_val = round(final_amt / rate, 2)
        proof_text = f"\n📝 <b>Details / Note:</b> <code>{html.escape(e.text[:200])}</code>" if (e.text and not e.photo and not e.document) else ""
        cap = (f"<blockquote>{PE_LIGHTNING} <b>𝐍ᴇᴡ 𝐃ᴇᴘᴏsɪᴛ 𝐑ᴇǫᴜᴇsᴛ</b>\n\n"
               f"{P_ACC} <b>𝐔sᴇʀ:</b> <code>{uid}</code>\n"
               f"{P_MONEY} <b>𝐀ᴍᴏᴜɴᴛ:</b> <b>{P_INR}{final_amt}</b> (~${usdt_val})\n"
               f"{P_CARD} <b>𝐌ᴇᴛʜᴏᴅ:</b> <code>{info['method']}</code>\n"
               f"{P_ID} <b>𝐃ᴇᴘᴏsɪᴛ 𝐈𝐃:</b> <code>#{dep_id}</code>{proof_text}</blockquote>")
        
        btns = [
            [style_btn(f"✅ 𝐀ᴄᴄᴇᴘᴛ (₹{final_amt})", f"dep_acc|{dep_id}|{uid}|{info['method']}|exact|{final_amt}", "success", icon=5409098988156629257), 
             style_btn("❌ 𝐑ᴇᴊᴇᴄᴛ", f"dep_rej|{dep_id}|{uid}", "danger", icon=5409119256107297715)],
            [style_btn("✏️ 𝐂ᴜsᴛᴏᴍ 𝐀ᴍᴏᴜɴᴛ", f"dep_acc|{dep_id}|{uid}|{info['method']}|custom|0", "primary", icon=5409098988156629257)]
        ]
        
        # Deliver to Primary Channel -> Fallback Channel -> Admin DM
        delivered = False
        target_channels = get_log_channels_db()
        
        # 1. Try Log Channels in priority order
        for log_ch in target_channels:
            try:
                if e.media:
                    await bot.send_file(log_ch, e.media, caption=cap, buttons=btns)
                else:
                    await bot.send_message(log_ch, cap, buttons=btns)
                delivered = True
                break  # Successfully delivered to primary channel!
            except Exception as ex:
                logger.error(f"Failed to send deposit to channel {log_ch}: {ex}, trying fallback...")
        
        # 2. If all channels failed or no channels configured -> Fallback to Admin PM
        if not delivered:
            try:
                admin_rows = cur.execute("SELECT user_id FROM admins").fetchall()
                admin_ids = [r[0] for r in admin_rows]
                for sa in SUPER_ADMINS:
                    if sa and sa not in admin_ids:
                        admin_ids.append(sa)
                
                for a_id in admin_ids:
                    try:
                        if e.media:
                            await bot.send_file(a_id, e.media, caption=f"🔔 <b>[FALLBACK PAYMENT APPROVAL]</b>\n{cap}", buttons=btns)
                        else:
                            await bot.send_message(a_id, f"🔔 <b>[FALLBACK PAYMENT APPROVAL]</b>\n{cap}", buttons=btns)
                        delivered = True
                        break  # Delivered to admin PM
                    except Exception as exc:
                        logger.warning("Payment approval fallback delivery failed: attempt=%s error_type=%s", admin_ids.index(a_id) + 1, type(exc).__name__)
            except Exception as e_adm:
                logger.error(f"Error sending fallback deposit to admin DM: {e_adm}")
            if not delivered:
                logger.error("Manual deposit admin notification failed: deposit_id=%s user_id=%s", dep_id, uid)

    @bot.on(events.CallbackQuery(pattern=r"^dep_acc\|"))
    async def cb_dep_acc(e):
        admin_uid = e.sender_id
        if not is_admin(admin_uid):
            return await e.answer("🚫 Access Denied! Only Bot Admins can approve deposits.", alert=True)
            
        p = e.data.decode().split("|")
        dep_id, t_uid, method, a_type = p[1], int(p[2]), p[3], p[4]
        
        deposit = get_manual_deposit(dep_id)
        if not deposit or deposit.get("status") != "pending":
            return await e.answer("⚠️ This deposit request has already been processed!", alert=True)
        deposit_uid = int(deposit["user_id"])
        logger.info("Manual deposit approval: deposit_id=%s user_id=%s amount=%s", dep_id, deposit_uid, deposit["amount"])
        
        if a_type == "exact":
            amt = int(deposit["amount"])
            async with get_user_lock(deposit_uid):
                try:
                    approval = approve_deposit(dep_id, amt)
                except Exception as exc:
                    logger.exception("Manual deposit approval failed: deposit_id=%s error_type=%s", dep_id, type(exc).__name__)
                    return await e.answer("❌ Deposit approval failed. No balance was credited.", alert=True)
            if approval.get("already_processed"):
                return await e.answer("⚠️ This deposit request has already been processed!", alert=True)
            credited_uid = approval["user_id"]
            prev_bal = approval["previous_balance"]
            new_bal = approval["balance"]
            logger.info(
                "Manual deposit approved: deposit_id=%s user_id=%s balance=%s status=%s",
                dep_id, credited_uid, new_bal, approval["status"],
            )
            
            await process_referral_bonus(credited_uid, amt)
            
            user_msg = (f"<blockquote>{PE_CHECK} <b>🎉 𝐃ᴇᴘᴏsɪᴛ 𝐀ᴘᴘʀᴏᴠᴇᴅ!</b>\n\n"
                        f"{P_MONEY} <b>𝐀ᴍᴏᴜɴᴛ 𝐀ᴅᴅᴇᴅ:</b> <b>{P_INR}{amt}</b> (${to_usd(amt):.2f})\n"
                        f"📉 <b>𝐏ʀᴇᴠɪᴏᴜs 𝐁ᴀʟᴀɴᴄᴇ:</b> {P_INR}{prev_bal}\n"
                        f"📈 <b>𝐍ᴇᴡ 𝐁ᴀʟᴀɴᴄᴇ:</b> <b>{P_INR}{new_bal}</b> (${to_usd(new_bal):.2f})</blockquote>")
            try: await bot.send_message(int(credited_uid), user_msg)
            except Exception as exc:
                logger.warning("Deposit approval user notification failed: deposit_id=%s error_type=%s", dep_id, type(exc).__name__)
            
            approved_text = (f"<blockquote>{PE_CHECK} <b>✅ 𝐃ᴇᴘᴏsɪᴛ 𝐀ᴘᴘʀᴏᴠᴇᴅ!</b>\n\n"
                             f"{P_ACC} <b>𝐔sᴇʀ:</b> <code>{credited_uid}</code>\n"
                             f"{P_MONEY} <b>𝐀ᴍᴏᴜɴᴛ 𝐂ʀᴇᴅɪᴛᴇᴅ:</b> <b>{P_INR}{amt}</b>\n"
                             f"{P_CARD} <b>𝐌ᴇᴛʜᴏᴅ:</b> <code>{method}</code>\n"
                             f"👨‍💻 <b>𝐀ᴘᴘʀᴏᴠᴇᴅ 𝐁ʏ:</b> <code>{admin_uid}</code></blockquote>")
            try: await e.edit(approved_text)
            except MessageNotModifiedError: pass
            await e.answer(f"✅ Approved! ₹{amt} credited to user {credited_uid}.", alert=True)
            
        elif a_type == "custom":
            custom_dep_amt[int(dep_id)] = "0"
            await e.edit(f"<blockquote>{P_KEY} <b>Enter 𝐂ᴜsᴛᴏᴍ 𝐀ᴍᴏᴜɴᴛ for User <code>{t_uid}</code>:</b>\n\n{P_MONEY} <b>{P_INR}0</b></blockquote>", buttons=get_admin_custom_keypad(int(dep_id)))
            
    @bot.on(events.CallbackQuery(pattern=r"^dep_rej\|"))
    async def cb_dep_rej(e):
        admin_uid = e.sender_id
        if not is_admin(admin_uid):
            return await e.answer("🚫 Access Denied! Only Bot Admins can reject deposits.", alert=True)
            
        p = e.data.decode().split("|")
        dep_id, t_uid = p[1], int(p[2])
        
        deposit = get_manual_deposit(dep_id)
        if not deposit or deposit.get("status") != "pending":
            return await e.answer("⚠️ This deposit request has already been processed!", alert=True)

        try:
            rejection = repository.reject_deposit(dep_id)
        except Exception:
            return await e.answer("❌ Deposit rejection failed. Please try again.", alert=True)
        if rejection.get("already_processed"):
            return await e.answer("⚠️ This deposit request has already been processed!", alert=True)
        deposit = get_manual_deposit(dep_id)
        
        try:
            await bot.send_message(int(deposit["user_id"]), f"<blockquote>{P_NO} <b>❌ 𝐃ᴇᴘᴏsɪᴛ 𝐑ᴇᴊᴇᴄᴛᴇᴅ!</b>\n\n𝐘ᴏᴜʀ ᴅᴇᴘᴏsɪᴛ ʀᴇǫᴜᴇsᴛ ᴏғ <b>{P_INR}{deposit['amount']}</b> ᴡᴀs ʀᴇᴊᴇᴄᴛᴇᴅ ʙʏ ᴀᴅᴍɪɴ.\n𝐈ғ ʏᴏᴜ ᴀʟʀᴇᴀᴅʏ ᴘᴀɪᴅ, ᴘʟᴇᴀsᴇ ᴄᴏɴᴛᴀᴄᴛ <b>𝐒ᴜᴘᴘᴏʀᴛ</b> ᴡɪᴛʜ ʏᴏᴜʀ ᴘᴀʏᴍᴇɴᴛ ᴘʀᴏᴏғ.</blockquote>")
        except Exception as exc:
            logger.warning("Deposit rejection user notification failed: deposit_id=%s error_type=%s", dep_id, type(exc).__name__)
        
        rej_text = (f"<blockquote>{P_NO} <b>❌ 𝐃ᴇᴘᴏsɪᴛ 𝐑ᴇᴊᴇᴄᴛᴇᴅ!</b>\n\n"
                    f"{P_ACC} <b>𝐔sᴇʀ:</b> <code>{deposit['user_id']}</code>\n"
                    f"{P_MONEY} <b>𝐀ᴍᴏᴜɴᴛ:</b> {P_INR}{deposit['amount']}\n"
                    f"👨‍💻 <b>𝐑ᴇᴊᴇᴄᴛᴇᴅ 𝐁ʏ:</b> <code>{admin_uid}</code></blockquote>")
        try: await e.edit(rej_text)
        except MessageNotModifiedError: pass
        await e.answer(f"❌ Deposit #{dep_id} rejected.", alert=True)

    @bot.on(events.CallbackQuery(pattern=r"^dkp\|"))
    async def cb_dkp(e):
        uid = e.sender_id
        if not is_admin(uid):
            return await e.answer("🚫 Access Denied! Only Bot Admins can set deposit amounts.", alert=True)
            
        _, dep_id, action = e.data.decode().split("|")
        dep_id = int(dep_id)
        deposit = get_manual_deposit(dep_id)
        if not deposit or deposit.get("status") != "pending":
            return await e.answer("⚠️ Already processed.", alert=True)
        t_uid = deposit["user_id"]
        method = deposit.get("method_name") or deposit.get("payment_method")
        orig_amt = deposit["amount"]
        
        curr = custom_dep_amt.get(dep_id, "0")
        
        if action.isdigit():
            if curr == "0": curr = action
            else: curr += action
            if len(curr) > 7: curr = curr[:7]
        elif action == "del": 
            curr = curr[:-1] or "0"
        elif action == "cancel":
            btns = [
                [style_btn(f"✅ 𝐀ᴄᴄᴇᴘᴛ (₹{orig_amt})", f"dep_acc|{dep_id}|{t_uid}|{method}|exact|{orig_amt}", "success", icon=5409098988156629257), 
                 style_btn("❌ 𝐑ᴇᴊᴇᴄᴛ", f"dep_rej|{dep_id}|{t_uid}", "danger", icon=5409119256107297715)],
                [style_btn("✏️ 𝐂ᴜsᴛᴏᴍ 𝐀ᴍᴏᴜɴᴛ", f"dep_acc|{dep_id}|{t_uid}|{method}|custom|0", "primary", icon=5409098988156629257)]
            ]
            return await e.edit(f"<blockquote>{PE_LIGHTNING} <b>𝐍ᴇᴡ 𝐃ᴇᴘᴏsɪᴛ 𝐑ᴇǫᴜᴇsᴛ</b>\n\n{P_ACC} 𝐔sᴇʀ: <code>{t_uid}</code>\n{P_MONEY} 𝐑ᴇǫᴜᴇsᴛ: <b>{P_INR}{orig_amt}</b>\n{P_CARD} 𝐌ᴇᴛʜᴏᴅ: <code>{method}</code>\n{P_ID} 𝐑ᴇғ: <code>#{dep_id}</code></blockquote>", buttons=btns)
        elif action == "conf":
            amt = int(curr)
            if amt <= 0: return await e.answer("Amount must be > 0", alert=True)
            
            async with get_user_lock(t_uid):
                try:
                    approval = approve_deposit(dep_id, amt)
                except Exception as exc:
                    logger.exception("Custom deposit approval failed: deposit_id=%s error_type=%s", dep_id, type(exc).__name__)
                    return await e.answer("❌ Deposit approval failed. No balance was credited.", alert=True)
            if approval.get("already_processed"):
                return await e.answer("⚠️ This deposit request has already been processed!", alert=True)
            credited_uid = approval["user_id"]
            prev_bal = approval["previous_balance"]
            new_bal = approval["balance"]
            logger.info(
                "Manual deposit approved: deposit_id=%s user_id=%s balance=%s status=%s",
                dep_id, credited_uid, new_bal, approval["status"],
            )
                
            await process_referral_bonus(credited_uid, amt)
            conf_text = (f"<blockquote>{PE_CHECK} <b>✅ 𝐃ᴇᴘᴏsɪᴛ 𝐀ᴘᴘʀᴏᴠᴇᴅ (𝐂ᴜsᴛᴏᴍ)!</b>\n\n"
                         f"{P_ACC} <b>𝐔sᴇʀ:</b> <code>{credited_uid}</code>\n"
                         f"{P_MONEY} <b>𝐀ᴍᴏᴜɴᴛ 𝐂ʀᴇᴅɪᴛᴇᴅ:</b> <b>{P_INR}{amt}</b>\n"
                         f"👨‍💻 <b>𝐀ᴘᴘʀᴏᴠᴇᴅ 𝐁ʏ:</b> <code>{uid}</code></blockquote>")
            await e.edit(conf_text)
            try:
                await bot.send_message(int(credited_uid), f"<blockquote>{PE_CHECK} <b>🎉 𝐃ᴇᴘᴏsɪᴛ 𝐀ᴘᴘʀᴏᴠᴇᴅ!</b>\n\n{P_MONEY} <b>𝐀ᴍᴏᴜɴᴛ 𝐀ᴅᴅᴇᴅ:</b> <b>{P_INR}{amt}</b>\n📉 <b>𝐎ʟᴅ:</b> {P_INR}{prev_bal} | 📈 <b>𝐍ᴇᴡ:</b> <b>{new_bal}</b></blockquote>")
            except Exception as exc:
                logger.warning("Custom deposit approval user notification failed: deposit_id=%s error_type=%s", dep_id, type(exc).__name__)
            await e.answer(f"✅ Approved ₹{amt} for user {credited_uid}.", alert=True)
            return

        custom_dep_amt[dep_id] = curr
        await e.edit(f"<blockquote>{P_KEY} <b>Enter 𝐂ᴜsᴛᴏᴍ 𝐀ᴍᴏᴜɴᴛ for User <code>{t_uid}</code>:</b>\n\n{P_MONEY} <b>{P_INR}{curr}</b></blockquote>", buttons=get_admin_custom_keypad(dep_id))
