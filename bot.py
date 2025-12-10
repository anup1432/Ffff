# -----------------------------------------
# FULL WEBHOOK TELEGRAM BOT (Render Ready)
# Ownership Verify + Balance + Withdraw
# MongoDB + Userbot Session + Admin Panel
# -----------------------------------------

import os
import asyncio
import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties
from aiogram.webhook.aiohttp_server import (
    SimpleRequestHandler,
    setup_application,
)
from aiogram import BaseMiddleware
from aiogram.enums import ParseMode
from aiogram.client.bot import Update

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest

from pymongo import MongoClient
from dotenv import load_dotenv

# -----------------------------
# LOAD ENV
# -----------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
MONGO_URL = os.getenv("MONGO_URL")
USERBOT_SESSION = os.getenv("USERBOT_SESSION")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", 0))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 10000))

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# -----------------------------
# MONGO DB
# -----------------------------
mongo = MongoClient(MONGO_URL)
db = mongo["botydb"]
users = db["users"]
withdraws = db["withdraws"]
settings = db["settings"]

# -----------------------------
# USERBOT CLIENT
# -----------------------------
userbot = TelegramClient(StringSession(USERBOT_SESSION), API_ID, API_HASH)


# -----------------------------
# KEYBOARDS
# -----------------------------
def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Profile", callback_data="prof")],
        [InlineKeyboardButton(text="💰 Balance", callback_data="bal")],
        [InlineKeyboardButton(text="🏷 Price", callback_data="price")],
        [InlineKeyboardButton(text="📤 Withdraw", callback_data="wd")],
        [InlineKeyboardButton(text="🆘 Support", callback_data="sup")],
    ])


def back_button():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Back", callback_data="back")]
    ])


# -----------------------------
# BOT COMMANDS
# -----------------------------
@dp.message(F.text == "/start")
async def start_cmd(msg: types.Message):
    users.update_one(
        {"user_id": msg.from_user.id},
        {"$setOnInsert": {"balance": 0}},
        upsert=True
    )
    await msg.answer("Welcome! Select an option:", reply_markup=main_menu())


@dp.callback_query(F.data == "prof")
async def profile(q: types.CallbackQuery):
    user = users.find_one({"user_id": q.from_user.id}) or {"balance": 0}
    await q.message.edit_text(
        f"👤 <b>Your Profile</b>\n\n💳 Balance: <code>{user['balance']}</code>",
        reply_markup=main_menu()
    )


@dp.callback_query(F.data == "bal")
async def balance(q: types.CallbackQuery):
    user = users.find_one({"user_id": q.from_user.id}) or {"balance": 0}
    await q.message.edit_text(
        f"💰 <b>Your Balance:</b> <code>{user['balance']}</code>",
        reply_markup=main_menu()
    )


@dp.callback_query(F.data == "price")
async def price(q: types.CallbackQuery):
    price_data = settings.find_one({"key": "old_price"}) or {"value": 0}
    await q.message.edit_text(
        f"🏷 <b>Old Group Price:</b> <code>{price_data['value']}</code>",
        reply_markup=main_menu()
    )


# -----------------------------
# ADMIN COMMAND – SET PRICE
# -----------------------------
@dp.message(F.text.startswith("/price"))
async def set_price(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return await msg.answer("Access Denied")

    try:
        new_price = int(msg.text.split()[1])
    except:
        return await msg.answer("Send like: /price 2000")

    settings.update_one({"key": "old_price"}, {"$set": {"value": new_price}}, upsert=True)
    await msg.answer(f"Price Updated → {new_price}")


# -----------------------------
# WITHDRAW
# -----------------------------
@dp.callback_query(F.data == "wd")
async def wd(q: types.CallbackQuery):
    await q.message.edit_text("Send your crypto address (0x...):", reply_markup=back_button())


@dp.message()
async def handle_withdraw(msg: types.Message):
    if msg.text.startswith("0x"):
        withdraws.insert_one({
            "user_id": msg.from_user.id,
            "address": msg.text,
            "status": "pending",
            "date": datetime.datetime.utcnow()
        })
        await msg.answer("Withdraw submitted ✔️ Admin will approve soon.")


@dp.message(F.text == "/wdlist")
async def wdlist(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return await msg.answer("Unauthorized")

    data = withdraws.find({"status": "pending"})
    text = "<b>Pending Withdraws:</b>\n\n"
    for x in data:
        text += f"📌 <b>ID:</b> {x['_id']} — <b>User:</b> {x['user_id']} — <b>{x['address']}</b>\n"

    await msg.answer(text)


# -----------------------------
# SUPPORT
# -----------------------------
@dp.callback_query(F.data == "sup")
async def support(q: types.CallbackQuery):
    await q.message.edit_text("Support request sent to Admin.", reply_markup=main_menu())
    await bot.send_message(ADMIN_ID, f"📞 Support Request From: {q.from_user.id}")


# -----------------------------
# OWNERSHIP VERIFICATION
# -----------------------------
async def verify_group(user_id, link):
    try:
        await userbot(JoinChannelRequest(link))
    except:
        pass

    entity = await userbot.get_entity(link)
    created = entity.date.year

    if 2016 <= created <= 2024:
        await userbot.send_message(link, "A")

        price_data = settings.find_one({"key": "old_price"}) or {"value": 0}
        users.update_one({"user_id": user_id}, {"$inc": {"balance": price_data['value']}})

        await bot.send_message(
            user_id,
            f"✅ Ownership Verified!\nAdded: <code>{price_data['value']}</code> balance."
        )
    else:
        await bot.send_message(user_id, "❌ Group must be between 2016–2024.")


# -----------------------------
# WEBHOOK ENDPOINT
# -----------------------------
async def webhook(request: web.Request):
    data = await request.json()
    update = Update.model_validate(data)
    await dp.feed_update(bot, update)
    return web.json_response({"ok": True})


# -----------------------------
# MAIN APP (WEBHOOK SERVER)
# -----------------------------
async def main():
    print("Starting userbot...")
    await userbot.start()

    print("Bot started with webhook")

    app = web.Application()
    app.router.add_post("/webhook", webhook)

    setup_application(app, dp, bot=bot)

    # set webhook
    await bot.set_webhook(WEBHOOK_URL)

    return app


if __name__ == "__main__":
    web.run_app(main(), port=PORT)
