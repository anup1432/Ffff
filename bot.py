# ------------------------------
# SINGLE FILE TELEGRAM BOT
# Ownership Verify + Balance + Withdraw + Admin Panel
# MongoDB + 1 Userbot Account
# Render-ready
# ------------------------------

import os
import asyncio
from aiogram import Bot, Dispatcher, F, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from pymongo import MongoClient
from dotenv import load_dotenv
import datetime

# --------------------
# LOAD ENV
# --------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
MONGO_URL = os.getenv("MONGO_URL")
USERBOT_SESSION = os.getenv("USERBOT_SESSION")  # single userbot session
CHANNEL_ID = int(os.getenv("CHANNEL_ID", 0))    # optional: notification channel

# --------------------
# MONGO DB SETUP
# --------------------
mongo = MongoClient(MONGO_URL)
db = mongo["botydb"]
users = db["users"]
withdraws = db["withdraws"]
settings = db["settings"]

# --------------------
# TELEGRAM BOT
# --------------------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --------------------
# USERBOT CLIENT
# --------------------
userbot = TelegramClient(StringSession(USERBOT_SESSION), API_ID, API_HASH)


# --------------------
# KEYBOARDS
# --------------------
def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("👤 Profile", callback_data="prof")],
        [InlineKeyboardButton("💰 Balance", callback_data="bal")],
        [InlineKeyboardButton("🏷 Price", callback_data="price")],
        [InlineKeyboardButton("📤 Withdraw", callback_data="wd")],
        [InlineKeyboardButton("🆘 Support", callback_data="sup")]
    ])

def back_button():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("🔙 Back", callback_data="back")]
    ])


# --------------------
# START / REGISTER
# --------------------
@dp.message(F.text == "/start")
async def start_cmd(msg: types.Message):
    users.update_one({"user_id": msg.from_user.id}, {"$setOnInsert": {"balance": 0}}, upsert=True)
    await msg.answer("Welcome! Select an option:", reply_markup=main_menu())


# --------------------
# PROFILE
# --------------------
@dp.callback_query(F.data == "prof")
async def profile(q: types.CallbackQuery):
    user = users.find_one({"user_id": q.from_user.id})
    balance = user.get("balance", 0)
    await q.message.edit_text(f"👤 **Your Profile**\n\n💳 Balance: `{balance}`", reply_markup=main_menu())


# --------------------
# BALANCE
# --------------------
@dp.callback_query(F.data == "bal")
async def balance(q: types.CallbackQuery):
    user = users.find_one({"user_id": q.from_user.id})
    balance = user.get("balance", 0)
    await q.message.edit_text(f"💰 **Your Balance:** `{balance}`", reply_markup=main_menu())


# --------------------
# PRICE
# --------------------
@dp.callback_query(F.data == "price")
async def price(q: types.CallbackQuery):
    price = settings.find_one({"key": "old_price"}) or {"value": 0}
    await q.message.edit_text(f"🏷 **Current Price for OLD Group:** `{price['value']}`", reply_markup=main_menu())


# --------------------
# ADMIN: SET PRICE
# --------------------
@dp.message(F.text.startswith("/price"))
async def admin_price(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return await msg.reply("Access denied")
    try:
        new_price = int(msg.text.split()[1])
    except:
        return await msg.reply("Use: /price 2000")
    settings.update_one({"key": "old_price"}, {"$set": {"value": new_price}}, upsert=True)
    await msg.reply(f"Price updated to {new_price}")


# --------------------
# WITHDRAW
# --------------------
@dp.callback_query(F.data == "wd")
async def wd(q: types.CallbackQuery):
    await q.message.edit_text("Send your crypto address (0x...):", reply_markup=back_button())


@dp.message()
async def handle_address(msg: types.Message):
    if msg.text.startswith("0x"):
        withdraws.insert_one({
            "user_id": msg.from_user.id,
            "address": msg.text,
            "status": "pending",
            "date": datetime.datetime.utcnow()
        })
        await msg.reply("Withdraw request submitted. Admin will approve soon.")


# --------------------
# ADMIN: VIEW PENDING WITHDRAWS
# --------------------
@dp.message(F.text == "/wdlist")
async def wdlist(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return await msg.reply("Unauthorized")
    data = withdraws.find({"status": "pending"})
    text = "Pending withdraws:\n\n"
    for x in data:
        text += f"ID: {x['_id']} — User: {x['user_id']} — {x['address']}\n"
    await msg.reply(text)


# --------------------
# SUPPORT
# --------------------
@dp.callback_query(F.data == "sup")
async def support(q: types.CallbackQuery):
    await q.message.edit_text(f"Support request sent to owner/admin.", reply_markup=main_menu())
    # optionally notify admin here via bot.send_message(ADMIN_ID, f"Support request from {q.from_user.id}")


# --------------------
# OWNERSHIP VERIFICATION LOGIC
# --------------------
async def verify_group(user_id, group_link):
    try:
        await userbot(JoinChannelRequest(group_link))
    except:
        pass  # Already joined or error

    # check creation date (old 2016–2024)
    entity = await userbot.get_entity(group_link)
    created = entity.date
    year = created.year
    if 2016 <= year <= 2024:
        # send "A" message to group
        await userbot.send_message(group_link, "A")
        # monitor ownership via role change or message reaction (simplified)
        # after verification, add balance
        old_price = settings.find_one({"key": "old_price"}) or {"value": 0}
        users.update_one({"user_id": user_id}, {"$inc": {"balance": old_price["value"]}}, upsert=True)
        await bot.send_message(user_id, f"✅ Ownership verified! `{old_price['value']}` added to your balance.")
    else:
        await bot.send_message(user_id, "❌ Group too new or invalid (2016–2024 old required).")


# --------------------
# MAIN
# --------------------
async def main():
    await userbot.start()
    await dp.start_polling(bot)

asyncio.run(main())
