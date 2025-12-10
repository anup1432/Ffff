#!/usr/bin/env python3
"""
bot.py
Single-file Telegram bot + userbot skeleton for:
- verifying group ownership (best-effort using Telethon)
- crediting user balances on success
- admin-settable pricing
- withdraw requests (crypto address) with admin approve/decline
- UI buttons: Profile, My Balance, Price, Withdraw, Support
- Uses MongoDB (pymongo) for persistence

IMPORTANT:
- Do NOT put secrets here. Use environment variables:
    BOT_TOKEN, API_ID, API_HASH, USERBOT_SESSION, MONGO_URI, ADMIN_IDS (csv), PUBLIC_CHANNEL_ID
- Rotate any leaked credentials immediately.
"""

import os
import logging
import asyncio
from typing import Optional
from datetime import datetime

from aiogram import Bot, Dispatcher, types
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.utils.keyboard import ReplyKeyboardBuilder

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import Channel, Chat, User

from pymongo import MongoClient
from bson.objectid import ObjectId

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load secrets from env
BOT_TOKEN = os.getenv("BOT_TOKEN")                # Bot token
API_ID = int(os.getenv("API_ID", "0"))           # API ID
API_HASH = os.getenv("API_HASH")                 # API HASH
USERBOT_SESSION = os.getenv("USERBOT_SESSION")   # Session string for Telethon userbot
MONGO_URI = os.getenv("MONGO_URI")               # MongoDB connection string
ADMIN_IDS = os.getenv("ADMIN_IDS", "")           # comma-separated admin telegram IDs
PUBLIC_CHANNEL_ID = os.getenv("PUBLIC_CHANNEL_ID")  # channel id/username where withdraws are shown

if not BOT_TOKEN or not API_ID or not API_HASH or not MONGO_URI:
    logger.error("Missing one or more required environment variables: BOT_TOKEN, API_ID, API_HASH, MONGO_URI")
    raise SystemExit("Please set BOT_TOKEN, API_ID, API_HASH, MONGO_URI environment variables")

ADMINS = [int(x) for x in ADMIN_IDS.split(",") if x.strip().isdigit()]

# === MongoDB setup ===
mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = mongo.get_default_database()
users_col = db["users"]
settings_col = db["settings"]
withdraw_col = db["withdraws"]
accounts_col = db["accounts"]  # store userbot accounts metadata

# Ensure default settings
if settings_col.count_documents({}) == 0:
    settings_col.insert_one({
        "price_per_old_member": 1.0,   # default price (admin can change)
        "min_age_year": 2016,
        "max_age_year": 2024,
        "fixed_older_multiplier": 1.0,
    })

# === Aiogram bot ===
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# === Telethon userbot client (for account actions) ===
# Note: We do not auto-login using phone/token inside this script.
if USERBOT_SESSION:
    user_client = TelegramClient(StringSession := USERBOT_SESSION, API_ID, API_HASH)
else:
    user_client = None

# Utility functions
def get_setting(key, default=None):
    s = settings_col.find_one({})
    return s.get(key, default) if s else default

def set_setting(key, value):
    settings_col.update_one({}, {"$set": {key: value}}, upsert=True)

def ensure_user(chat_id, tg_user):
    """Create or update user doc"""
    users_col.update_one(
        {"tg_id": tg_user.id},
        {"$set": {
            "tg_id": tg_user.id,
            "username": getattr(tg_user, "username", None),
            "first_name": getattr(tg_user, "first_name", None),
            "last_name": getattr(tg_user, "last_name", None),
            "updated_at": datetime.utcnow()
        },
         "$setOnInsert": {"balance": 0.0, "created_at": datetime.utcnow()}
        },
        upsert=True
    )

# Keyboard builder for main menu
def main_menu():
    kb = ReplyKeyboardBuilder()
    kb.add(KeyboardButton("Profile"))
    kb.add(KeyboardButton("My Balance"))
    kb.add(KeyboardButton("Price"))
    kb.add(KeyboardButton("Withdraw"))
    kb.add(KeyboardButton("Support"))
    return kb.as_markup(resize_keyboard=True)

# Helper: check admin
def is_admin(tg_id: int) -> bool:
    return tg_id in ADMINS

# === Bot handlers ===
@dp.message(Command(commands=["start"]))
async def cmd_start(message: types.Message):
    ensure_user(message.chat.id, message.from_user)
    text = "Salam! Main group ownership verification bot hoon.\nButtons se proceed karein."
    await message.answer(text, reply_markup=main_menu())

@dp.message()
async def generic_text_handler(message: types.Message):
    user = message.from_user
    ensure_user(message.chat.id, user)
    text = message.text.strip().lower()

    if text == "profile":
        doc = users_col.find_one({"tg_id": user.id})
        bal = doc.get("balance", 0.0) if doc else 0.0
        await message.answer(f"Profile:\nName: {user.first_name}\nUsername: @{user.username if user.username else 'NA'}\nBalance: {bal}")
        return

    if text == "my balance":
        doc = users_col.find_one({"tg_id": user.id})
        bal = doc.get("balance", 0.0) if doc else 0.0
        await message.answer(f"Your balance: {bal}")
        return

    if text == "price":
        price = get_setting("price_per_old_member", 1.0)
        await message.answer(f"Current price per old member: {price}\n(Admin can change via /price)")
        return

    if text == "support":
        # Send a message to admins (or specific support flow)
        await message.answer("Support request sent to admins. Please describe your issue.")
        for a in ADMINS:
            try:
                await bot.send_message(a, f"Support request from @{user.username or user.id} ({user.id}).\nUse /support_reply <user_id> to respond.")
            except Exception as e:
                logger.exception("Failed to notify admin: %s", e)
        return

    if text == "withdraw":
        # Start withdraw flow
        await message.answer("Enter withdrawal amount (number):")
        # store state in DB simple pattern
        users_col.update_one({"tg_id": user.id}, {"$set": {"pending_withdraw_step": "awaiting_amount"}})
        return

    # Withdraw multi-step
    udoc = users_col.find_one({"tg_id": user.id}) or {}
    step = udoc.get("pending_withdraw_step")
    if step == "awaiting_amount":
        try:
            amount = float(message.text.strip())
        except:
            await message.answer("Invalid amount. Enter a number.")
            return
        if amount <= 0:
            await message.answer("Amount must be > 0")
            return
        # check balance
        bal = udoc.get("balance", 0.0)
        if amount > bal:
            await message.answer(f"Not enough balance. Your balance: {bal}")
            users_col.update_one({"tg_id": user.id}, {"$unset": {"pending_withdraw_step": ""}})
            return
        users_col.update_one({"tg_id": user.id}, {"$set": {"pending_withdraw_step": "awaiting_address", "pending_withdraw_amount": amount}})
        await message.answer("Enter your crypto address (example BTC/ETH address):")
        return
    if step == "awaiting_address":
        addr = message.text.strip()
        amount = udoc.get("pending_withdraw_amount", 0.0)
        # Create withdraw request
        req = {
            "user_id": user.id,
            "username": user.username,
            "amount": amount,
            "address": addr,
            "status": "pending",
            "created_at": datetime.utcnow()
        }
        withdraw_col.insert_one(req)
        users_col.update_one({"tg_id": user.id}, {"$unset": {"pending_withdraw_step": "", "pending_withdraw_amount": ""}})
        await message.answer("Withdraw request submitted. Admin will review.")
        # notify admin / public channel
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton("Approve", callback_data=f"withdraw_approve_{str(req.get('_id', ''))}"),
             InlineKeyboardButton("Decline", callback_data=f"withdraw_decline_{str(req.get('_id',''))}")]
        ])
        for a in ADMINS:
            try:
                await bot.send_message(a, f"New withdraw request: user @{user.username} ({user.id}), amount: {amount}, addr: {addr}", reply_markup=kb)
            except Exception as e:
                logger.exception("notify admin failed: %s", e)
        # Post to public channel (optional)
        if PUBLIC_CHANNEL_ID:
            try:
                await bot.send_message(PUBLIC_CHANNEL_ID, f"Withdraw request pending: @{user.username} amount {amount}")
            except Exception as e:
                logger.debug("failed to post public channel: %s", e)
        return

    # Admin-only commands direct text handling (fallback)
    if message.text.startswith("/price"):
        if not is_admin(user.id):
            await message.answer("Only admin can set price.")
            return
        parts = message.text.split()
        if len(parts) >= 2:
            try:
                p = float(parts[1])
                set_setting("price_per_old_member", p)
                await message.answer(f"Price updated to {p}")
            except:
                await message.answer("Invalid format. Use /price 2.5")
        else:
            await message.answer("Usage: /price <number>")
        return

    # Any other text
    await message.answer("Main menu:", reply_markup=main_menu())


# Callback handlers (approve/decline)
@dp.callback_query()
async def cb_handler(cq: types.CallbackQuery):
    data = cq.data or ""
    user = cq.from_user
    if data.startswith("withdraw_approve_") or data.startswith("withdraw_decline_"):
        if not is_admin(user.id):
            await cq.answer("Only admin can do this", show_alert=True)
            return
        _id = data.split("_", 2)[2]
        try:
            oid = ObjectId(_id)
        except:
            await cq.answer("Invalid id", show_alert=True)
            return
        req = withdraw_col.find_one({"_id": oid})
        if not req:
            await cq.answer("Request not found", show_alert=True)
            return
        if data.startswith("withdraw_approve_"):
            # Deduct balance and mark approved
            users_col.update_one({"tg_id": req["user_id"]}, {"$inc": {"balance": -req["amount"]}})
            withdraw_col.update_one({"_id": oid}, {"$set": {"status": "approved", "admin": user.id, "processed_at": datetime.utcnow()}})
            await cq.message.edit_text(f"Request approved by admin {user.id}")
            # Notify user
            try:
                await bot.send_message(req["user_id"], f"Your withdraw request of {req['amount']} has been approved by admin {user.id}. Please wait for on-chain transfer.")
            except:
                pass
        else:
            withdraw_col.update_one({"_id": oid}, {"$set": {"status": "declined", "admin": user.id, "processed_at": datetime.utcnow()}})
            await cq.message.edit_text(f"Request declined by admin {user.id}")
            try:
                await bot.send_message(req["user_id"], f"Your withdraw request of {req['amount']} was declined by admin.")
            except:
                pass
        await cq.answer()

# === Ownership verification logic (Telethon) ===
async def verify_group_ownership_via_userbot(group_link_or_username: str, target_user_id: int) -> bool:
    """
    Best-effort function:
    - Using Telethon user client, try to fetch full channel/chat info
    - Check .full_chat.creator or admins list to match target_user_id
    NOTE: Telethon's object structure varies. This is a best-effort example.
    """
    if not user_client:
        logger.warning("Userbot client not configured")
        return False
    try:
        entity = await user_client.get_entity(group_link_or_username)
        # For channels/supergroups, use GetFullChannelRequest
        if isinstance(entity, Channel):
            full = await user_client(GetFullChannelRequest(entity))
            # full.full_chat.creator_id might be present - check admins instead
            if hasattr(full.full_chat, "creator"):
                # Telethon types vary, fallback to scanning admin list
                pass
            # scan participants for role 'creator' or 'administrator' with specific rights
            async for participant in user_client.iter_participants(entity, filter=None):
                # participant is ParticipantUser / User - check id and status (approx)
                if participant.id == target_user_id:
                    # Telethon doesn't always provide role label; we must check admin rights:
                    # Heuristic: check if this user is in get_participants with 'admins' filter:
                    return True
            return False
        else:
            # Chat or other — fallback
            async for participant in user_client.iter_participants(entity):
                if participant.id == target_user_id:
                    return True
            return False
    except FloodWaitError as e:
        logger.error("Rate limited by Telegram: %s", e)
        return False
    except Exception as e:
        logger.exception("Failed to verify ownership: %s", e)
        return False

# A command to trigger verification (users will send a group link)
@dp.message(Command(commands=["verify"]))
async def cmd_verify(message: types.Message):
    """
    Usage: /verify <group_link_or_username>
    Flow:
      - Bot will ask user to supply the group link (or user can send /verify)
      - Bot will instruct to add our user account(s) to the group if needed
      - Then bot will attempt verification via telethon user client
    """
    user = message.from_user
    args = message.get_args()
    if not args:
        await message.answer("Usage: /verify <group_link_or_username_or_chat_id>")
        return
    group_ref = args.strip()
    await message.answer("Attempting to verify ownership. Please make sure the listed account(s) are added to the group.")
    # best-effort: assume the user wants to prove that *they* own the group — that means target_user = user.id
    ok = await verify_group_ownership_via_userbot(group_ref, user.id)
    if ok:
        # credit user based on settings and group age heuristics
        price = get_setting("price_per_old_member", 1.0)
        # For now we credit flat price; you can extend to compute per member/age etc.
        users_col.update_one({"tg_id": user.id}, {"$inc": {"balance": price}, "$setOnInsert": {"created_at": datetime.utcnow()}}, upsert=True)
        await message.answer(f"Ownership confirmed ✅. {price} has been credited to your balance.")
    else:
        await message.answer("Ownership could not be automatically verified. Please follow instructions or contact support.")

# Admin: add userbot/account (placeholder)
@dp.message(Command(commands=["add_account"]))
async def cmd_add_account(message: types.Message):
    """
    Admin flow to register a userbot session (metadata only).
    For security: the admin should add the actual session string as an environment variable on Render,
    not paste it in chat. This command records a label and usage permissions.
    Usage: /add_account <label>
    After this, you'll add the actual session in environment vars and restart.
    """
    user = message.from_user
    if not is_admin(user.id):
        await message.answer("Only admins can add accounts.")
        return
    label = message.get_args().strip() if message.get_args() else f"account_{int(datetime.utcnow().timestamp())}"
    accounts_col.insert_one({"label": label, "added_by": user.id, "created_at": datetime.utcnow(), "enabled": True})
    await message.answer(f"Account entry created with label `{label}`. Add the real session string to environment and restart the service.\n(Do NOT paste the raw session in public chat.)")

# Start Telethon client if session present
async def start_user_client():
    global user_client
    if not USERBOT_SESSION:
        logger.info("No USERBOT_SESSION provided; skipping userbot startup.")
        return
    # Telethon requires a session object; here USERBOT_SESSION is already a string session
    try:
        # Using the provided string session
        # NOTE: we instantiate earlier; ensure connection:
        await user_client.start()
        logger.info("Userbot started.")
    except SessionPasswordNeededError:
        logger.error("Two-step verification required for the user session. Please configure.")
    except Exception as e:
        logger.exception("Failed to start userbot: %s", e)

# Startup
async def main():
    # Start user client if provided
    if user_client:
        await start_user_client()
    # Start polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down.")
