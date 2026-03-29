import os
import asyncio
from aiogram import Bot, Dispatcher, types

BOT_TOKEN = os.getenv("BOT_TOKEN")

dp = Dispatcher()

@dp.message()
async def echo(message: types.Message):
    await message.answer(message.text)

async def main():
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)

if name == "main":
    asyncio.run(main())
