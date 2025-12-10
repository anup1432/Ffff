# ------------------------------
# SINGLE FILE TELEGRAM BOT (FIXED)
# Ownership Verify + Balance + Withdraw + Admin Panel
# MongoDB + 1 Userbot Account
# Render-ready (aiogram 3 compatible)
# ------------------------------

import os
import asyncio
import datetime
from typing import Optional

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.errors import RPCError

from pymongo import MongoClient

# --------------------
# LOAD ENV
# --------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH")
ADMIN_ID = int(os.getenv("ADMIN_ID") or 0)
MONGO_URL = os.getenv("MONGO_URL")
USERBOT_SESSION = os.getenv("USERBOT_SESSION")  # userbot session string
CHANNEL_ID = int(os.getenv("CHANNEL_ID") or 0)    # optional: notification channel id

# minimal validation
if not BOT_TOKEN or not API_ID or not API_HASH or not MONGO_URL or not USERBOT_SESSION:
    raise RuntimeError("Missing required env vars. Set BOT_TOKEN, API_ID, API_HASH, MONGO_URL, USERBOT_SESSION")

# --------------------
# MONGO DB SETUP
# --------------------
mongo = MongoClient(MONGO_URL)
db = mongo.get_database("botydb")
users = db.get_collection("users")
withdraws = db.get_collection("withdraws")
settings = db.get_collection("settings")

# --------------------
# TELEGRAM BOT (Aiogram)
# --------------------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot=bot)

# --------------------
# USERBOT CLIENT (Telethon)
# --------------------
userbot = TelegramClient(StringSession(USERBOT_SESSION), API_ID, API_HASH)

# --------------------
# KEYBOARDS
# --------------------
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("👤 Profile", callback_data="prof")],
        [InlineKeyboardButton("💰 Balance", callback_data="bal")],
        [InlineKeyboardButton("🏷 Price", callback_data="price")],
        [InlineKeyboardButton("📤 Withdraw", callback_data="wd")],
        [InlineKeyboardButton("🆘 Support", callback_data="sup")]
    ])

def back_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("🔙 Back", callback_data="back")]
    ])


# --------------------
# HELPERS
# --------------------
def get_old_price() -> int:
    doc = settings.find_one({"key": "old_price"})
    return int(doc["value"]) if doc and "value" in doc else 0

def ensure_user_doc(user_id: int):
    users.update_one({"user_id": user_id}, {"$setOnInsert": {"balance": 0, "user_id": user_id}}, upsert=True)


# --------------------
# COMMANDS / HANDLERS
# --------------------
@dp.message(F.text == "/start")
async def start_cmd(message: types.Message, **kwargs):
    ensure_user_doc(message.from_user.id)
    await message.answer("Welcome! Select an option:", reply_markup=main_menu())


@dp.callback_query(F.data == "prof")
async def profile(q: types.CallbackQuery, **kwargs):
    user = users.find_one({"user_id": q.from_user.id}) or {}
    balance = user.get("balance", 0)
    await q.message.edit_text(f"👤 Your Profile\n\n💳 Balance: `{balance}`", reply_markup=main_menu())


@dp.callback_query(F.data == "bal")
async def balance(q: types.CallbackQuery, **kwargs):
    user = users.find_one({"user_id": q.from_user.id}) or {}
    balance = user.get("balance", 0)
    await q.message.edit_text(f"💰 Your Balance: `{balance}`", reply_markup=main_menu())


@dp.callback_query(F.data == "price")
async def price(q: types.CallbackQuery, **kwargs):
    price = get_old_price()
    await q.message.edit_text(f"🏷 Current Price for OLD Group: `{price}`", reply_markup=main_menu())


@dp.message(F.text.startswith("/price"))
async def admin_price(message: types.Message, **kwargs):
    if message.from_user.id != ADMIN_ID:
        return await message.reply("Access denied")
    parts = message.text.split()
    if len(parts) < 2:
        return await message.reply("Usage: /price <amount>")
    try:
        new_price = int(parts[1])
    except:
        return await message.reply("Price must be a number.")
    settings.update_one({"key": "old_price"}, {"$set": {"value": new_price}}, upsert=True)
    await message.reply(f"Price updated to {new_price}")


@dp.callback_query(F.data == "wd")
async def wd(q: types.CallbackQuery, **kwargs):
    await q.message.edit_text("Send your crypto address (0x...):", reply_markup=back_button())


@dp.message(F.text.startswith("0x"))
async def handle_address(message: types.Message, **kwargs):
    ensure_user_doc(message.from_user.id)
    withdraws.insert_one({
        "user_id": message.from_user.id,
        "address": message.text.strip(),
        "status": "pending",
        "date": datetime.datetime.utcnow()
    })
    await message.reply("Withdraw request submitted. Admin will approve soon.")


@dp.message(F.text == "/wdlist")
async def wdlist(message: types.Message, **kwargs):
    if message.from_user.id != ADMIN_ID:
        return await message.reply("Unauthorized")
    data = list(withdraws.find({"status": "pending"}))
    if not data:
        return await message.reply("No pending withdraws.")
    text_lines = ["Pending withdraws:\n"]
    for x in data:
        text_lines.append(f"ID: {x['_id']} — User: {x['user_id']} — {x['address']}")
    await message.reply("\n".join(text_lines))


@dp.callback_query(F.data == "sup")
async def support(q: types.CallbackQuery, **kwargs):
    await q.message.edit_text("Support request sent to admin.", reply_markup=main_menu())
    try:
        await bot.send_message(ADMIN_ID, f"⚠ Support request from {q.from_user.id}")
    except Exception:
        pass


# --------------------
# VERIFY FLOW
# --------------------
async def verify_group(user_id: int, group_link: str) -> None:
    """
    Accepts a t.me link or invite link or username.
    Joins group via userbot, checks creation year 2016-2024,
    sends "A" message and (simplified) adds balance.
    """
    # try to join the group (safe)
    try:
        # Telethon can accept a username like "t.me/xyz" or "@xyz" or "xyz"
        await userbot(JoinChannelRequest(group_link))
    except RPCError:
        # ignore join errors (could already be a member)
        pass
    except Exception:
        # if JoinChannelRequest fails for this form, try send message to username directly
        try:
            await userbot.send_message(group_link, "A")
        except Exception:
            await bot.send_message(user_id, "❌ Could not join or message the provided group. Check link.")
            return

    # fetch entity
    try:
        entity = await userbot.get_entity(group_link)
    except Exception:
        await bot.send_message(user_id, "❌ Invalid group link or entity not found.")
        return

    # check creation year if available
    created: Optional[datetime.datetime] = getattr(entity, "date", None)
    if created:
        year = created.year
        if not (2016 <= year <= 2024):
            await bot.send_message(user_id, "❌ Only OLD groups (2016–2024) are allowed.")
            return

    # signal to group (send "A")
    try:
        await userbot.send_message(entity, "A")
    except Exception:
        # ignore if cannot send
        pass

    # simplified verification: here we immediately grant price
    price = get_old_price()
    users.update_one({"user_id": user_id}, {"$inc": {"balance": price}}, upsert=True)
    await bot.send_message(user_id, f"✅ Ownership verified (simplified). `{price}` added to your balance.")


@dp.message(Command("verify"))
async def verify_cmd(message: types.Message, **kwargs):
    """
    Usage: /verify <group_link_or_username>
    Example: /verify t.me/example_group
    """
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("Usage: /verify <group_link_or_username>")

    group_link = parts[1].strip()
    await message.reply("Starting verification — joining & checking group...")
    # start verification in background so handler returns quickly
    asyncio.create_task(verify_group(message.from_user.id, group_link))


# --------------------
# STARTUP / MAIN
# --------------------
async def main():
    print("Starting userbot (Telethon)...")
    try:
        await userbot.start()
        print("Userbot started.")
    except Exception as e:
        print("Userbot start error:", e)
        raise

    print("Starting aiogram bot polling...")
    try:
        await dp.start_polling(bot)
    except Exception as e:
        print("Aiogram polling error:", e)
        raise

if __name__ == "__main__":
    asyncio.run(main())
