import asyncio, json, os
from datetime import datetime,timezone
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import text
import redis.asyncio as redis
engine=create_async_engine(os.environ['DATABASE_URL'],pool_pre_ping=True)
Session=async_sessionmaker(engine)
r=redis.from_url(os.environ.get('REDIS_URL','redis://redis:6379/0'),decode_responses=True)
bot=Bot(os.environ['TELEGRAM_BOT_TOKEN']);dp=Dispatcher()
@dp.message(CommandStart())
async def start(m:Message):
    code=(m.text or '').split(maxsplit=1)
    if len(code)==2:
        async with Session.begin() as s:
            row=(await s.execute(text('SELECT user_id FROM link_codes WHERE code=:c AND expires > now() FOR UPDATE'),{'c':code[1]})).first()
            if row:
                await s.execute(text('UPDATE users SET telegram_id=NULL WHERE telegram_id=:tg'),{'tg':m.from_user.id})
                await s.execute(text('UPDATE users SET telegram_id=:tg WHERE id=:id'),{'tg':m.from_user.id,'id':row.user_id})
                await s.execute(text('DELETE FROM link_codes WHERE code=:c'),{'c':code[1]})
                await m.answer('✅ Telegram привязан к ЗвероЛок! Отправьте геопозицию.')
            else:await m.answer('Ссылка истекла. Получите новую в профиле сайта.')
    else:await m.answer('Откройте профиль ЗвероЛок и привяжите Telegram через кнопку.')
    await m.answer('📍 Отправьте свою геопозицию:',reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='📍 Отправить геопозицию',request_location=True)]],resize_keyboard=True))
@dp.message(F.location)
async def location(m:Message):
    async with Session.begin() as s:
        res=await s.execute(text('UPDATE users SET lat=:lat,lon=:lon WHERE telegram_id=:tg RETURNING id'),{'lat':m.location.latitude,'lon':m.location.longitude,'tg':m.from_user.id})
        ok=res.first()
    await m.answer('✅ Геопозиция сохранена. Радиус и виды животных синхронизированы с сайтом.' if ok else 'Сначала привяжите Telegram в профиле сайта.')
@dp.message(Command('radius'))
async def radius(m:Message):
    try:radius=int(m.text.split()[1]);assert 1<=radius<=100
    except (ValueError,IndexError,AssertionError):return await m.answer('Используйте /radius 5 (от 1 до 100 км)')
    async with Session.begin() as s:
        result=await s.execute(text('UPDATE users SET radius_km=:radius WHERE telegram_id=:tg RETURNING id'),{'radius':radius,'tg':m.from_user.id})
        ok=result.first()
    await m.answer(f'Радиус: {radius} км. Настройка обновлена на сайте.' if ok else 'Сначала привяжите Telegram.')
@dp.message(Command('animals'))
async def animals(m:Message):
    kinds=(m.text or '').split()[1:]
    if not kinds or not set(kinds)<=set(('cat','dog','other')):return await m.answer('Пример: /animals cat dog')
    async with Session.begin() as s:
        result=await s.execute(text('UPDATE users SET kinds=:k WHERE telegram_id=:tg RETURNING id'),{'k':','.join(kinds),'tg':m.from_user.id})
        ok=result.first()
    await m.answer('Виды животных синхронизированы с сайтом.' if ok else 'Сначала привяжите Telegram.')
async def alerts():
    pub=r.pubsub();await pub.subscribe('telegram-alerts')
    try:
        async for event in pub.listen():
            if event['type']=='message':
                item=json.loads(event['data'])
                try:await bot.send_message(item['chat_id'],item['text'])
                except Exception:pass
    finally:await pub.aclose()
async def main():
    task=asyncio.create_task(alerts())
    try:await dp.start_polling(bot)
    finally:task.cancel();await r.aclose();await engine.dispose()
if __name__=='__main__':asyncio.run(main())
