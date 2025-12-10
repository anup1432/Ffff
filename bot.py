"""
Complete single-file bot.py
- Webhook-ready (preferred)
- Telethon userbot for ownership verification
- MongoDB for users/withdraws/settings
- Features: Profile, Balance, Price (admin), Withdraw (admin approve), Support, Verify (ownership)
- Uses env vars: BOT_TOKEN, API_ID, API_HASH, ADMIN_ID, MONGO_URL, USERBOT_SESSION, CHANNEL_ID, WEBHOOK_URL (opt), PORT (opt)
"""

import os
import asyncio
import datetime
import logging
from typing import Optional

from aiohttp import web
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.types import ChannelParticipantsAdmins
from telethon.errors import RPCError

from pymongo import MongoClient
from bson.objectid import ObjectId

# -------------------------
# Logging & load .env
# -------------------------
logging.basicConfig(level=logging.INFO)
load_dotenv()

# -------------------------
# ENV VARS (as you provided)
# -------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH")
ADMIN_ID = int(os.getenv("ADMIN_ID") or 0)
RAW_MONGO_URL = os.getenv("MONGO_URL", "")
USERBOT_SESSION = os.getenv("USERBOT_SESSION")
CHANNEL_ID = int(os.getenv("CHANNEL_ID") or 0)

# Optional for webhook mode
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. https://your-app.onrender.com/webhook
PORT = int(os.getenv("PORT", 0))        # e.g. 10000

# -------------------------
# Basic validation
# -------------------------
missing = []
for name, v in [
    ("BOT_TOKEN", BOT_TOKEN),
    ("API_ID", API_ID),
    ("API_HASH", API_HASH),
    ("ADMIN_ID", ADMIN_ID),
    ("RAW_MONGO_URL", RAW_MONGO_URL),
    ("USERBOT_SESSION", USERBOT_SESSION),
]:
    if not v:
        missing.append(name)
if missing:
    raise RuntimeError(f"Missing required ENV vars: {', '.join(missing)}. Fill them and redeploy.")

# Fix MONGO URL if user provided "srv://..." or skipped scheme
MONGO_URL = RAW_MONGO_URL.strip()
if MONGO_URL.startswith("srv://"):
    MONGO_URL = "mongodb+" + MONGO_URL  # e.g. mongodb+srv://...
elif not MONGO_URL.startswith("mongodb://") and not MONGO_URL.startswith("mongodb+srv://"):
    # try to be helpful: if they pasted without scheme
    MONGO_URL = "mongodb+srv://" + MONGO_URL.lstrip("/")

logging.info("Using Mongo URL: %s", MONGO_URL if "@" in MONGO_URL else "mongodb+srv://<hidden>")

# -------------------------
# MongoDB setup
# -------------------------
mongo = MongoClient(MONGO_URL)
db = mongo.get_database("botydb")
users_col = db.get_collection("users")
withdraws_col = db.get_collection("withdraws")
settings_col = db.get_collection("settings")

# Ensure default price exists (if not set)
settings_col.update_one({"key": "old_price"}, {"$setOnInsert": {"value": 100}}, upsert=True)

# -------------------------
# Telethon userbot setup
# -------------------------
userbot = TelegramClient(StringSession(USERBOT_SESSION), API_ID, API_HASH)

# -------------------------
# Aiogram Bot + Dispatcher
# -------------------------
bot = Bot(BOT_TOKEN)
dp = Dispatcher(bot=bot)

# -------------------------
# Helpers
# -------------------------
def ensure_user(user_id: int):
    users_col.update_one(
        {"user_id": user_id},
        {"$setOnInsert": {"user_id": user_id, "balance": 0, "verified_groups": []}},
        upsert=True
    )

def get_price() -> int:
    doc = settings_col.find_one({"key": "old_price"})
    try:
        return int(doc["value"])
    except Exception:
        return 0

def set_price(value: int):
    settings_col.update_one({"key": "old_price"}, {"$set": {"value": int(value)}}, upsert=True)

# Build main menu keyboard (inline)
def main_menu():
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton("👤 Profile", callback_data="prof")],
        [types.InlineKeyboardButton("💰 Balance", callback_data="bal")],
        [types.InlineKeyboardButton("🏷 Price", callback_data="price")],
        [types.InlineKeyboardButton("📤 Withdraw", callback_data="wd")],
        [types.InlineKeyboardButton("🆘 Support", callback_data="sup")],
    ])

def back_button():
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton("🔙 Back", callback_data="back")]
    ])

# -------------------------
# Start / Menu
# -------------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message, **kwargs):
    ensure_user(message.from_user.id)
    await message.answer("Welcome! Choose an option:", reply_markup=main_menu())

# -------------------------
# Profile callback
# -------------------------
@dp.callback_query(lambda c: c.data == "prof")
async def cb_profile(q: types.CallbackQuery, **kwargs):
    ensure_user(q.from_user.id)
    user = users_col.find_one({"user_id": q.from_user.id}) or {}
    balance = user.get("balance", 0)
    verified = len(user.get("verified_groups", []))
    text = f"👤 Profile\n\nBalance: `{balance}`\nVerified groups: {verified}"
    await q.message.edit_text(text, reply_markup=main_menu())

# -------------------------
# Balance callback
# -------------------------
@dp.callback_query(lambda c: c.data == "bal")
async def cb_balance(q: types.CallbackQuery, **kwargs):
    ensure_user(q.from_user.id)
    user = users_col.find_one({"user_id": q.from_user.id}) or {}
    balance = user.get("balance", 0)
    await q.message.edit_text(f"💰 Balance: `{balance}`", reply_markup=main_menu())

# -------------------------
# Price view callback
# -------------------------
@dp.callback_query(lambda c: c.data == "price")
async def cb_price(q: types.CallbackQuery, **kwargs):
    price = get_price()
    await q.message.edit_text(f"🏷 Current price for OLD group (2016-2024): `{price}`", reply_markup=main_menu())

# -------------------------
# Admin: set price
# -------------------------
@dp.message(Command(commands=["price"]))
async def cmd_price(message: types.Message, **kwargs):
    if message.from_user.id != ADMIN_ID:
        return await message.reply("Unauthorized")
    parts = message.text.split()
    if len(parts) < 2:
        return await message.reply("Usage: /price <amount>")
    try:
        val = int(parts[1])
    except:
        return await message.reply("Amount must be a number.")
    set_price(val)
    await message.reply(f"Price set to {val}")

# -------------------------
# Withdraw flow
# -------------------------
@dp.callback_query(lambda c: c.data == "wd")
async def cb_withdraw(q: types.CallbackQuery, **kwargs):
    await q.message.edit_text("Send your crypto address (0x...):", reply_markup=back_button())

@dp.message(lambda m: isinstance(m.text, str) and m.text.strip().lower().startswith("0x"))
async def handle_withdraw_address(message: types.Message, **kwargs):
    ensure_user(message.from_user.id)
    user = users_col.find_one({"user_id": message.from_user.id}) or {"balance": 0}
    balance = int(user.get("balance", 0))
    if balance <= 0:
        return await message.reply("Your balance is zero — nothing to withdraw.")

    addr = message.text.strip()
    wd_doc = {
        "user_id": message.from_user.id,
        "address": addr,
        "amount": balance,
        "status": "pending",
        "created_at": datetime.datetime.utcnow()
    }
    res = withdraws_col.insert_one(wd_doc)
    wid = str(res.inserted_id)

    # notify admin with inline approve/decline
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton("✅ Approve", callback_data=f"approve:{wid}"),
            types.InlineKeyboardButton("❌ Decline", callback_data=f"decline:{wid}")
        ]
    ])
    admin_text = f"Withdraw request:\nID: {wid}\nUser: {message.from_user.id}\nAmount: {balance}\nAddress: {addr}"
    try:
        await bot.send_message(ADMIN_ID, admin_text, reply_markup=kb)
    except Exception as e:
        logging.exception("Failed to notify admin: %s", e)

    await message.reply("✅ Withdraw request submitted. Admin will review it.")

# Admin view pending withdraws
@dp.message(Command("wdlist"))
async def cmd_wdlist(message: types.Message, **kwargs):
    if message.from_user.id != ADMIN_ID:
        return await message.reply("Unauthorized")
    docs = list(withdraws_col.find({"status": "pending"}))
    if not docs:
        return await message.reply("No pending withdraws.")
    lines = []
    for d in docs:
        lines.append(f"ID: {str(d['_id'])} — User: {d['user_id']} — Amount: {d.get('amount',0)} — Addr: {d['address']}")
    await message.reply("\n".join(lines))

# Approve / Decline callbacks
@dp.callback_query(lambda c: c.data and (c.data.startswith("approve:") or c.data.startswith("decline:")))
async def cb_approve_decline(q: types.CallbackQuery, **kwargs):
    if q.from_user.id != ADMIN_ID:
        return await q.answer("Unauthorized", show_alert=True)

    data = q.data
    if data.startswith("approve:"):
        wid = data.split(":",1)[1]
        try:
            wd = withdraws_col.find_one({"_id": ObjectId(wid)})
        except Exception:
            wd = None
        if not wd or wd.get("status") != "pending":
            return await q.answer("Request not found or already processed.", show_alert=True)

        # deduct balance and mark approved
        users_col.update_one({"user_id": wd["user_id"]}, {"$inc": {"balance": -int(wd.get("amount",0))}})
        withdraws_col.update_one({"_id": ObjectId(wid)}, {"$set": {"status": "approved", "processed_at": datetime.datetime.utcnow(), "processed_by": q.from_user.id}})

        # update admin message
        try:
            await q.message.edit_text(q.message.text + f"\n\n✅ Approved by admin {q.from_user.id}")
        except Exception:
            pass

        # notify user
        try:
            await bot.send_message(wd["user_id"], f"✅ Your withdraw (ID {wid}) approved. Address: {wd['address']}")
        except Exception:
            pass

        # notify channel
        if CHANNEL_ID:
            try:
                await bot.send_message(int(CHANNEL_ID), f"Withdraw APPROVED — user {wd['user_id']} — amount {wd.get('amount',0)} — addr {wd['address']}")
            except Exception:
                pass

        return await q.answer("Approved")

    else:  # decline
        wid = data.split(":",1)[1]
        try:
            wd = withdraws_col.find_one({"_id": ObjectId(wid)})
        except Exception:
            wd = None
        if not wd or wd.get("status") != "pending":
            return await q.answer("Request not found or already processed.", show_alert=True)

        withdraws_col.update_one({"_id": ObjectId(wid)}, {"$set": {"status": "declined", "processed_at": datetime.datetime.utcnow(), "processed_by": q.from_user.id}})
        try:
            await q.message.edit_text(q.message.text + f"\n\n❌ Declined by admin {q.from_user.id}")
        except Exception:
            pass
        try:
            await bot.send_message(wd["user_id"], f"❌ Your withdraw (ID {wid}) has been declined by admin.")
        except Exception:
            pass
        return await q.answer("Declined")

# Support
@dp.callback_query(lambda c: c.data == "sup")
async def cb_support(q: types.CallbackQuery, **kwargs):
    # notify admin
    try:
        await bot.send_message(ADMIN_ID, f"⚠ Support request from user {q.from_user.id}")
    except Exception:
        pass
    await q.message.edit_text("Support request sent to admin.", reply_markup=main_menu())

@dp.callback_query(lambda c: c.data == "back")
async def cb_back(q: types.CallbackQuery, **kwargs):
    await q.message.edit_text("Back to menu:", reply_markup=main_menu())

# Admin extra commands
@dp.message(Command("users"))
async def cmd_users(message: types.Message, **kwargs):
    if message.from_user.id != ADMIN_ID:
        return await message.reply("Unauthorized")
    total = users_col.count_documents({})
    return await message.reply(f"Total users: {total}")

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, **kwargs):
    if message.from_user.id != ADMIN_ID:
        return await message.reply("Unauthorized")
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Usage: /broadcast <text>")
    text = parts[1]
    # naive broadcast; be careful with rate limits
    users = users_col.find({})
    count = 0
    for u in users:
        try:
            await bot.send_message(int(u["user_id"]), text)
            count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await message.reply(f"Broadcast sent to ~{count} users.")

# ---------- Verification Flow ----------
async def is_userbot_admin_in(entity):
    try:
        participants = await userbot.get_participants(entity, filter=ChannelParticipantsAdmins)
        me = await userbot.get_me()
        for p in participants:
            if getattr(p, "id", None) == getattr(me, "id", None):
                return True
    except Exception:
        pass
    return False

async def verify_group_process(caller_user_id: int, group_link: str, timeout: int = 120):
    # join
    try:
        await userbot(JoinChannelRequest(group_link))
    except RPCError:
        pass
    except Exception:
        try:
            await userbot.send_message(group_link, "A")
        except Exception:
            await bot.send_message(caller_user_id, "❌ Could not join or message the provided group. Check link.")
            return

    # get entity
    try:
        entity = await userbot.get_entity(group_link)
    except Exception:
        await bot.send_message(caller_user_id, "❌ Invalid group link or entity not found.")
        return

    # check creation date if available
    created = getattr(entity, "date", None)
    if created:
        year = created.year
        if not (2016 <= year <= 2024):
            await bot.send_message(caller_user_id, "❌ Only OLD groups allowed (2016–2024).")
            return

    # send A message
    try:
        await userbot.send_message(entity, "A")
    except Exception:
        pass

    # wait for owner to promote userbot
    await bot.send_message(caller_user_id, "🔎 Waiting up to 2 minutes for owner to promote the account. Once promoted, verification will complete.")
    waited = 0
    interval = 5
    while waited < timeout:
        admin_now = await is_userbot_admin_in(entity)
        if admin_now:
            price = get_price()
            users_col.update_one({"user_id": caller_user_id}, {"$inc": {"balance": price}, "$push": {"verified_groups": group_link}}, upsert=True)
            await bot.send_message(caller_user_id, f"✅ Ownership verified! `{price}` added to your balance.")
            if CHANNEL_ID:
                try:
                    await bot.send_message(int(CHANNEL_ID), f"Ownership verified: user {caller_user_id} for group {group_link}. Added {price}.")
                except Exception:
                    pass
            return
        await asyncio.sleep(interval)
        waited += interval
    await bot.send_message(caller_user_id, "⏱ Verification timed out. Owner didn't promote the account within the time limit.")

# /verify command
@dp.message(Command("verify"))
async def cmd_verify(message: types.Message, **kwargs):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Usage: /verify <group_link_or_username>\nExample: /verify t.me/examplegroup")
    link = parts[1].strip()
    ensure_user(message.from_user.id)
    await message.reply("Starting verification — the userbot will join and send 'A'. Owner must promote the account.")
    asyncio.create_task(verify_group_process(message.from_user.id, link))

# -------------------------
# Webhook receiver (if using webhook)
# -------------------------
async def telegram_webhook(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="no json")
    update = types.Update(**data)
    await dp.feed_update(bot, update)
    return web.Response(text="ok")

# -------------------------
# Startup & shutdown
# -------------------------
async def on_startup(app: web.Application):
    # start userbot
    try:
        await userbot.start()
        logging.info("Userbot started")
    except Exception as e:
        logging.exception("Userbot start failed: %s", e)
    # set webhook if provided
    if WEBHOOK_URL:
        try:
            await bot.set_webhook(WEBHOOK_URL)
            logging.info("Webhook set to %s", WEBHOOK_URL)
        except Exception as e:
            logging.exception("Failed to set webhook: %s", e)

async def on_shutdown(app: web.Application):
    try:
        await bot.delete_webhook()
    except Exception:
        pass
    try:
        await userbot.disconnect()
    except Exception:
        pass

# -------------------------
# App factory & run
# -------------------------
def create_app():
    app = web.Application()
    # register webhook receiver at /webhook
    app.router.add_post("/webhook", telegram_webhook)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app

if __name__ == "__main__":
    # If WEBHOOK_URL provided, run as web service (Render recommended)
    app = create_app()
    port = PORT if PORT else (10000)
    web.run_app(app, host="0.0.0.0", port=port)
