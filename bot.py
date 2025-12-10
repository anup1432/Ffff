# bot.py
# Single-file Webhook bot + Telethon userbot + MongoDB
# Features: ownership verify, balance, withdraw (admin approve), price admin, support
# Requirements: aiogram, aiohttp, telethon, pymongo, python-dotenv

import os
import asyncio
import datetime
from typing import Optional

from aiohttp import web
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.webhook.aiohttp_server import SimpleRequestHandler

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.types import ChannelParticipantsAdmins
from telethon.errors import RPCError

from pymongo import MongoClient
from bson.objectid import ObjectId

load_dotenv()

# --------- ENV ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. https://your-service.onrender.com/webhook
PORT = int(os.getenv("PORT", 10000))

API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH")
ADMIN_ID = int(os.getenv("ADMIN_ID") or 0)
MONGO_URL = os.getenv("MONGO_URL")
USERBOT_SESSION = os.getenv("USERBOT_SESSION")  # StringSession string
CHANNEL_ID = int(os.getenv("CHANNEL_ID") or 0)  # optional: channel for notifications

# Minimal checks
if not BOT_TOKEN or not WEBHOOK_URL or not API_ID or not API_HASH or not MONGO_URL or not USERBOT_SESSION:
    raise RuntimeError("Missing required ENV vars. Set BOT_TOKEN, WEBHOOK_URL, API_ID, API_HASH, MONGO_URL, USERBOT_SESSION")

# --------- DB ----------
mongo = MongoClient(MONGO_URL)
db = mongo.get_database("botydb")
users_col = db.get_collection("users")
withdraws_col = db.get_collection("withdraws")
settings_col = db.get_collection("settings")

# Ensure a default price exists
settings_col.update_one({"key": "old_price"}, {"$setOnInsert": {"value": 100}}, upsert=True)

# --------- Bot & Dispatcher (Aiogram Webhook) ----------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot=bot)

# --------- Telethon userbot ----------
userbot = TelegramClient(StringSession(USERBOT_SESSION), API_ID, API_HASH)

# --------- Keyboards ----------
def main_menu():
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton("👤 Profile", callback_data="prof")],
            [types.InlineKeyboardButton("💰 Balance", callback_data="bal")],
            [types.InlineKeyboardButton("🏷 Price", callback_data="price")],
            [types.InlineKeyboardButton("📤 Withdraw", callback_data="wd")],
            [types.InlineKeyboardButton("🆘 Support", callback_data="sup")]
        ]
    )

def back_button():
    return types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton("🔙 Back", callback_data="back")]]
    )

# ---------- Helpers ----------
def ensure_user(user_id: int):
    users_col.update_one(
        {"user_id": user_id},
        {"$setOnInsert": {"user_id": user_id, "balance": 0}},
        upsert=True
    )

def get_price() -> int:
    doc = settings_col.find_one({"key": "old_price"})
    return int(doc["value"]) if doc and "value" in doc else 0

# ---------- Handlers ----------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, **kwargs):
    ensure_user(message.from_user.id)
    await message.answer("Welcome! Use the menu:", reply_markup=main_menu())

# Callback: Profile
@dp.callback_query(lambda c: c.data == "prof")
async def prof_cb(q: types.CallbackQuery, **kwargs):
    user = users_col.find_one({"user_id": q.from_user.id}) or {}
    balance = user.get("balance", 0)
    await q.message.edit_text(f"👤 Profile\n\nBalance: `{balance}`", reply_markup=main_menu())

# Balance
@dp.callback_query(lambda c: c.data == "bal")
async def bal_cb(q: types.CallbackQuery, **kwargs):
    user = users_col.find_one({"user_id": q.from_user.id}) or {}
    balance = user.get("balance", 0)
    await q.message.edit_text(f"💰 Balance: `{balance}`", reply_markup=main_menu())

# Price view
@dp.callback_query(lambda c: c.data == "price")
async def price_cb(q: types.CallbackQuery, **kwargs):
    price = get_price()
    await q.message.edit_text(f"🏷 Current price for OLD group: `{price}`", reply_markup=main_menu())

# Admin: set price
@dp.message(Command(commands=["price"]))
async def admin_price_cmd(message: types.Message, **kwargs):
    if message.from_user.id != ADMIN_ID:
        return await message.reply("Unauthorized")
    parts = message.text.split()
    if len(parts) < 2:
        return await message.reply("Usage: /price <amount>")
    try:
        val = int(parts[1])
    except:
        return await message.reply("Amount must be a number")
    settings_col.update_one({"key": "old_price"}, {"$set": {"value": val}}, upsert=True)
    await message.reply(f"Price set to {val}")

# Withdraw button pressed -> ask for address
@dp.callback_query(lambda c: c.data == "wd")
async def wd_cb(q: types.CallbackQuery, **kwargs):
    await q.message.edit_text("Send your crypto address (0x...):", reply_markup=back_button())

# Receive address (messages starting with 0x) -> create withdraw request
@dp.message(lambda m: m.text and m.text.strip().lower().startswith("0x"))
async def address_msg(message: types.Message, **kwargs):
    ensure_user(message.from_user.id)
    user = users_col.find_one({"user_id": message.from_user.id}) or {"balance": 0}
    amount_available = user.get("balance", 0)
    if amount_available <= 0:
        return await message.reply("Your balance is zero. Nothing to withdraw.")

    wd_doc = {
        "user_id": message.from_user.id,
        "address": message.text.strip(),
        "status": "pending",
        "amount": amount_available,
        "created_at": datetime.datetime.utcnow()
    }
    res = withdraws_col.insert_one(wd_doc)
    wid = str(res.inserted_id)

    # notify admin with approve/decline buttons
    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton("✅ Approve", callback_data=f"approve:{wid}"),
             types.InlineKeyboardButton("❌ Decline", callback_data=f"decline:{wid}")]
        ]
    )
    admin_text = f"Withdraw request:\nID: {wid}\nUser: {message.from_user.id}\nAmount: {amount_available}\nAddress: {wd_doc['address']}"
    try:
        await bot.send_message(ADMIN_ID, admin_text, reply_markup=kb)
    except Exception:
        pass

    await message.reply("Withdraw request submitted. Admin will review it.")

# Admin view pending withdraws
@dp.message(Command("wdlist"))
async def wdlist_cmd(message: types.Message, **kwargs):
    if message.from_user.id != ADMIN_ID:
        return await message.reply("Unauthorized")
    docs = list(withdraws_col.find({"status": "pending"}))
    if not docs:
        return await message.reply("No pending withdraws.")
    text_lines = []
    for d in docs:
        text_lines.append(f"ID: {str(d['_id'])} — User: {d['user_id']} — Amount: {d.get('amount',0)} — Addr: {d['address']}")
    await message.reply("\n".join(text_lines))

# Admin approve/decline callback
@dp.callback_query(lambda c: c.data and (c.data.startswith("approve:") or c.data.startswith("decline:")))
async def approve_decline_cb(q: types.CallbackQuery, **kwargs):
    if q.from_user.id != ADMIN_ID:
        return await q.answer("Unauthorized", show_alert=True)

    data = q.data
    if data.startswith("approve:"):
        wid = data.split(":",1)[1]
        wd = withdraws_col.find_one({"_id": ObjectId(wid)})
        if not wd or wd.get("status") != "pending":
            return await q.answer("Request not found or already processed.", show_alert=True)
        # Deduct balance (all amount) and mark approved
        users_col.update_one({"user_id": wd["user_id"]}, {"$inc": {"balance": -int(wd.get("amount",0))}})
        withdraws_col.update_one({"_id": ObjectId(wid)}, {"$set": {"status": "approved", "processed_at": datetime.datetime.utcnow(), "processed_by": q.from_user.id}})
        await q.message.edit_text(q.message.text + f"\n\n✅ Approved by admin {q.from_user.id}")
        # notify user & channel
        try:
            await bot.send_message(wd["user_id"], f"✅ Your withdraw (ID {wid}) has been approved by admin. Address: {wd['address']}.")
        except Exception:
            pass
        if CHANNEL_ID:
            try:
                await bot.send_message(CHANNEL_ID, f"Withdraw APPROVED: user {wd['user_id']} — amount {wd.get('amount',0)} — addr {wd['address']}")
            except Exception:
                pass
        return await q.answer("Approved")

    else:  # decline
        wid = data.split(":",1)[1]
        wd = withdraws_col.find_one({"_id": ObjectId(wid)})
        if not wd or wd.get("status") != "pending":
            return await q.answer("Request not found or already processed.", show_alert=True)
        withdraws_col.update_one({"_id": ObjectId(wid)}, {"$set": {"status": "declined", "processed_at": datetime.datetime.utcnow(), "processed_by": q.from_user.id}})
        await q.message.edit_text(q.message.text + f"\n\n❌ Declined by admin {q.from_user.id}")
        try:
            await bot.send_message(wd["user_id"], f"❌ Your withdraw (ID {wid}) has been declined by admin.")
        except Exception:
            pass
        return await q.answer("Declined")

# Support button
@dp.callback_query(lambda c: c.data == "sup")
async def support_cb(q: types.CallbackQuery, **kwargs):
    await q.message.edit_text("Support request sent to admin.", reply_markup=main_menu())
    try:
        await bot.send_message(ADMIN_ID, f"Support request from user {q.from_user.id}")
    except Exception:
        pass

# Back button: return to main menu
@dp.callback_query(lambda c: c.data == "back")
async def back_cb(q: types.CallbackQuery, **kwargs):
    await q.message.edit_text("Back to menu:", reply_markup=main_menu())

# ---------- Verification flow ----------
async def is_userbot_admin_in(entity):
    """
    Return True if userbot account is in admin list for the entity (channel/group).
    """
    try:
        participants = await userbot.get_participants(entity, filter=ChannelParticipantsAdmins)
        me = await userbot.get_me()
        for p in participants:
            # p is a User; compare id
            if getattr(p, "id", None) == getattr(me, "id", None):
                return True
    except Exception:
        pass
    return False

async def verify_group_process(caller_user_id: int, group_link: str):
    """
    Full verify flow:
    - join group
    - check creation year
    - send "A"
    - poll for promotion of userbot account (max timeout)
    - on success, add balance to caller user
    """
    # normalize group_link: remove telegram prefixes if provided
    link = group_link.strip()
    # try to join
    try:
        await userbot(JoinChannelRequest(link))
    except RPCError:
        # ignore join errors
        pass
    except Exception:
        # try sending message directly (some links may be usernames)
        try:
            await userbot.send_message(link, "A")
        except Exception:
            await bot.send_message(caller_user_id, "❌ Could not join or message the group. Check the link.")
            return

    # fetch entity
    try:
        entity = await userbot.get_entity(link)
    except Exception:
        await bot.send_message(caller_user_id, "❌ Invalid group link or entity not found.")
        return

    # check creation date
    created = getattr(entity, "date", None)
    if created:
        year = created.year
        if not (2016 <= year <= 2024):
            await bot.send_message(caller_user_id, "❌ Only OLD groups (2016–2024) are allowed.")
            return

    # send signal message "A"
    try:
        await userbot.send_message(entity, "A")
    except Exception:
        # ignore if blocked
        pass

    # now poll for promotion: check if userbot is admin
    max_wait = 120  # seconds
    interval = 5
    waited = 0
    await bot.send_message(caller_user_id, "🔎 Waiting up to 2 minutes for the owner to promote the account. Once promoted, verification will complete.")
    while waited < max_wait:
        admin_now = await is_userbot_admin_in(entity)
        if admin_now:
            # grant balance
            price = get_price()
            ensure_user_doc = users_col.update_one  # alias for speed
            users_col.update_one({"user_id": caller_user_id}, {"$inc": {"balance": price}}, upsert=True)
            await bot.send_message(caller_user_id, f"✅ Ownership verified! `{price}` added to your balance.")
            # optional notify admin/channel
            if CHANNEL_ID:
                try:
                    await bot.send_message(CHANNEL_ID, f"Ownership verified for user {caller_user_id} for group {link}. Added {price}.")
                except Exception:
                    pass
            return
        await asyncio.sleep(interval)
        waited += interval

    # timed out
    await bot.send_message(caller_user_id, "⏱ Verification timed out. Owner did not promote the account within 2 minutes. Try again or ask owner to promote quickly.")

# helper wrapper to match naming earlier
def ensure_user_doc(user_id: int):
    users_col.update_one({"user_id": user_id}, {"$setOnInsert": {"user_id": user_id, "balance": 0}}, upsert=True)

# /verify command
@dp.message(Command("verify"))
async def verify_cmd(message: types.Message, **kwargs):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Usage: /verify <group_link_or_username>\nExample: /verify t.me/examplegroup")
    link = parts[1].strip()
    ensure_user_doc(message.from_user.id)
    await message.reply("Starting verification. The userbot will join and send a signal message 'A'. Then owner should promote the account.")
    asyncio.create_task(verify_group_process(message.from_user.id, link))

# ---------- Webhook receiver ----------
async def telegram_webhook(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="no json")
    update = types.Update(**data)
    await dp.feed_update(bot, update)
    return web.Response(text="ok")

# ---------- Startup & Shutdown ----------
async def on_startup(app: web.Application):
    # start userbot
    try:
        await userbot.start()
    except Exception as e:
        print("Userbot start error:", e)
        # do not raise - bot can still function except verify flows
    # set webhook
    await bot.set_webhook(WEBHOOK_URL)
    print("Webhook set to", WEBHOOK_URL)

async def on_shutdown(app: web.Application):
    try:
        await bot.delete_webhook()
    except Exception:
        pass
    try:
        await userbot.disconnect()
    except Exception:
        pass

# ---------- Create aiohttp app ----------
def create_app():
    app = web.Application()
    # register aiogram webhook handler at /webhook
    app.router.add_post("/webhook", telegram_webhook)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app

# ---------- Run ----------
if __name__ == "__main__":
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=PORT)
