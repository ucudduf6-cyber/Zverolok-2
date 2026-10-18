import asyncio, json, os, secrets
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, Query, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy import String, Integer, Float, DateTime, ForeignKey, Text, select, func, text, delete
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from geoalchemy2 import Geography
from geoalchemy2.functions import ST_DWithin, ST_SetSRID, ST_MakePoint
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError
import jwt
from pywebpush import webpush, WebPushException
import redis.asyncio as redis
import httpx
import logging

DB = os.environ['DATABASE_URL']
SECRET = os.environ['JWT_SECRET']
ORIGIN = os.environ.get('PUBLIC_ORIGIN', 'http://localhost:5173')
DB = DB.replace("postgresql://", "postgresql+asyncpg://", 1)
DB = DB.replace("postgres://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DB, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)
ph = PasswordHasher()
r = redis.from_url(os.environ.get('REDIS_URL','redis://localhost:6379/0'), decode_responses=True)
class Base(DeclarativeBase): pass
class User(Base):
    __tablename__='users'
    id: Mapped[int]=mapped_column(primary_key=True)
    email: Mapped[str]=mapped_column(String(255), unique=True, nullable=True)
    password_hash: Mapped[str]=mapped_column(Text, nullable=True)
    telegram_id: Mapped[int]=mapped_column(unique=True, nullable=True)
    lat: Mapped[float]=mapped_column(Float, nullable=True)
    lon: Mapped[float]=mapped_column(Float, nullable=True)
    radius_km: Mapped[int]=mapped_column(Integer, default=5)
    kinds: Mapped[str]=mapped_column(String(100), default='cat,dog,other')
    role: Mapped[str]=mapped_column(String(20), default='user')
class Pet(Base):
    __tablename__='pets'
    id: Mapped[int]=mapped_column(primary_key=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id',ondelete='CASCADE'))
    name: Mapped[str]=mapped_column(String(100))
    kind: Mapped[str]=mapped_column(String(20))
    status: Mapped[str]=mapped_column(String(10))
    description: Mapped[str]=mapped_column(String(1000),default='')
    lat: Mapped[float]=mapped_column(Float)
    lon: Mapped[float]=mapped_column(Float)
    location: Mapped[object]=mapped_column(Geography('POINT',srid=4326,spatial_index=True))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
class PushSub(Base):
    __tablename__='push_subscriptions'
    id: Mapped[int]=mapped_column(primary_key=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id',ondelete='CASCADE'))
    endpoint: Mapped[str]=mapped_column(Text,unique=True)
    p256dh: Mapped[str]=mapped_column(Text)
    auth: Mapped[str]=mapped_column(Text)
class LinkCode(Base):
    __tablename__='link_codes'
    code: Mapped[str]=mapped_column(String(64),primary_key=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id',ondelete='CASCADE'))
    expires: Mapped[datetime]=mapped_column(DateTime(timezone=True))

@asynccontextmanager
async def lifespan(app):
    async with engine.begin() as conn:
        await conn.execute(text('CREATE EXTENSION IF NOT EXISTS postgis'))
        await conn.run_sync(Base.metadata.create_all)
    await configure_telegram_webhook()
    yield
    await r.aclose()
app=FastAPI(title='ЗвероЛок API',lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=[ORIGIN],allow_credentials=True,allow_methods=['*'],allow_headers=['*'])
security=HTTPBearer()
async def db():
    async with Session() as s: yield s

def token(user): return jwt.encode({'sub':str(user.id),'exp':datetime.now(timezone.utc)+timedelta(days=7)},SECRET,algorithm='HS256')
async def current(c:HTTPAuthorizationCredentials=Depends(security),s:AsyncSession=Depends(db)):
    try: uid=int(jwt.decode(c.credentials,SECRET,algorithms=['HS256'])['sub'])
    except Exception: raise HTTPException(401,'Invalid token')
    u=await s.get(User,uid)
    if not u: raise HTTPException(401,'Unknown user')
    return u
class Credentials(BaseModel):
    email:str=Field(min_length=5,max_length=255)
    password:str=Field(min_length=12,max_length=256)
class Settings(BaseModel):
    lat:float|None=None
    lon:float|None=None
    radius_km:int=Field(default=5,ge=1,le=100)
    kinds:list[str]=Field(default=['cat','dog','other'])
class PetInput(BaseModel):
    name:str=Field(min_length=1,max_length=100)
    kind:str
    status:str
    description:str=Field(default='',max_length=1000)
    lat:float=Field(ge=-90,le=90)
    lon:float=Field(ge=-180,le=180)
class Subscription(BaseModel):
    endpoint:str
    keys:dict[str,str]
def user_dict(u):return {'id':u.id,'email':u.email,'telegram_linked':bool(u.telegram_id),'lat':u.lat,'lon':u.lon,'radius_km':u.radius_km,'kinds':u.kinds.split(',')}
def pet_dict(p):return {'id':p.id,'user_id':p.user_id,'name':p.name,'kind':p.kind,'status':p.status,'description':p.description,'lat':p.lat,'lon':p.lon,'created_at':p.created_at.isoformat()}
@app.post('/api/register')
async def register(body:Credentials,s:AsyncSession=Depends(db)):
    email=body.email.strip().lower()
    if await s.scalar(select(User).where(User.email==email)):raise HTTPException(409,'Email already registered')
    u=User(email=email,password_hash=ph.hash(body.password));s.add(u)
    await s.commit();await s.refresh(u)
    return {'token':token(u),'user':user_dict(u)}
@app.post('/api/login')
async def login(body:Credentials,s:AsyncSession=Depends(db)):
    u=await s.scalar(select(User).where(User.email==body.email.strip().lower()))
    try:
        if not u or not u.password_hash or not ph.verify(u.password_hash,body.password):raise HTTPException(401,'Invalid credentials')
    except (VerifyMismatchError,VerificationError):raise HTTPException(401,'Invalid credentials')
    return {'token':token(u),'user':user_dict(u)}
@app.get('/api/me')
async def me(u:User=Depends(current)):return user_dict(u)
@app.put('/api/settings')
async def settings(body:Settings,u:User=Depends(current),s:AsyncSession=Depends(db)):
    if (body.lat is None)!=(body.lon is None):raise HTTPException(422,'Coordinates must be paired')
    if body.lat is not None and not (-90<=body.lat<=90 and -180<=body.lon<=180):raise HTTPException(422,'Invalid coordinates')
    if not set(body.kinds).issubset({'cat','dog','other'}):raise HTTPException(422,'Invalid kinds')
    u.lat=body.lat;u.lon=body.lon;u.radius_km=body.radius_km;u.kinds=','.join(body.kinds)
    await s.commit();return user_dict(u)
@app.get('/api/pets')
async def pets(lat:float=Query(ge=-90,le=90),lon:float=Query(ge=-180,le=180),radius_km:int=Query(10,ge=1,le=100),s:AsyncSession=Depends(db)):
    point=ST_SetSRID(ST_MakePoint(lon,lat),4326)
    rows=(await s.scalars(select(Pet).where(ST_DWithin(Pet.location,point,radius_km*1000)).order_by(Pet.created_at.desc()).limit(500))).all()
    return [pet_dict(x) for x in rows]
async def publish(payload):await r.publish('pet-events',json.dumps(payload))
async def notify(p:Pet):
    point=ST_SetSRID(ST_MakePoint(p.lon,p.lat),4326)
    async with Session() as s:
        users=(await s.scalars(select(User).where(User.lat.is_not(None),User.lon.is_not(None),User.id!=p.user_id))).all()
        subs=(await s.scalars(select(PushSub))).all()
    # Use PostGIS for pet retrieval; this notification path uses a compact haversine calculation for user preferences.
    import math
    def dist(a,b,c,d):
        x=math.sin(math.radians(c-a)/2)**2+math.cos(math.radians(a))*math.cos(math.radians(c))*math.sin(math.radians(d-b)/2)**2
        return 12742*math.asin(min(1,math.sqrt(x)))
    targets={u.id:u for u in users if p.kind in u.kinds.split(',') and dist(u.lat,u.lon,p.lat,p.lon)<=u.radius_km}
    title='ЗвероЛок'
    body='ЗвероЛок: Рядом с вами замечено потерянное животное!' if p.status=='lost' else 'ЗвероЛок: Рядом найдено животное!'
    def send(sub):
        return webpush(subscription_info={'endpoint':sub.endpoint,'keys':{'p256dh':sub.p256dh,'auth':sub.auth}},data=json.dumps({'title':title,'body':body,'url':'/'}),vapid_private_key=os.environ['VAPID_PRIVATE_KEY'],vapid_claims={'sub':os.environ['VAPID_SUBJECT']})
    for sub in subs:
        if sub.user_id in targets and os.getenv('VAPID_PRIVATE_KEY'):
            try:await asyncio.to_thread(send,sub)
            except WebPushException as e:
                if getattr(e.response,'status_code',None) in (404,410):
                    async with Session() as s:
                        await s.execute(delete(PushSub).where(PushSub.id==sub.id));await s.commit()
            except Exception:pass
    for u in targets.values():
        if u.telegram_id:
            await telegram_send(u.telegram_id, '⚠️ Внимание! В вашем районе потерялась собака.' if p.kind=='dog' and p.status=='lost' else body)
@app.post('/api/pets',status_code=201)
async def create_pet(body:PetInput,u:User=Depends(current),s:AsyncSession=Depends(db)):
    if body.kind not in ('cat','dog','other') or body.status not in ('lost','found'):raise HTTPException(422,'Invalid kind/status')
    count=await s.scalar(select(func.count()).select_from(Pet).where(Pet.user_id==u.id))
    if count>=10:raise HTTPException(403,'Maximum 10 listings')
    p=Pet(user_id=u.id,**body.model_dump(),location=f'SRID=4326;POINT({body.lon} {body.lat})')
    s.add(p);await s.commit();await s.refresh(p)
    await publish({'type':'created','pet':pet_dict(p)})
    asyncio.create_task(notify(p))
    return pet_dict(p)
@app.delete('/api/pets/{pet_id}')
async def remove_pet(pet_id:int,u:User=Depends(current),s:AsyncSession=Depends(db)):
    p=await s.get(Pet,pet_id)
    if not p:raise HTTPException(404,'Not found')
    if p.user_id!=u.id and u.role!='admin':raise HTTPException(403,'Forbidden')
    await s.delete(p);await s.commit();await publish({'type':'deleted','id':pet_id});return {'ok':True}
@app.post('/api/push/subscribe')
async def subscribe(body:Subscription,u:User=Depends(current),s:AsyncSession=Depends(db)):
    if not body.endpoint.startswith('https://') or not all(body.keys.get(k) for k in ('p256dh','auth')):raise HTTPException(422,'Invalid subscription')
    sub=await s.scalar(select(PushSub).where(PushSub.endpoint==body.endpoint))
    if sub:sub.user_id=u.id;sub.p256dh=body.keys['p256dh'];sub.auth=body.keys['auth']
    else:s.add(PushSub(user_id=u.id,endpoint=body.endpoint,p256dh=body.keys['p256dh'],auth=body.keys['auth']))
    await s.commit();return {'ok':True}
@app.post('/api/telegram/link')
async def link(u:User=Depends(current),s:AsyncSession=Depends(db)):
    code=secrets.token_urlsafe(24)
    s.add(LinkCode(code=code,user_id=u.id,expires=datetime.now(timezone.utc)+timedelta(minutes=10)))
    await s.commit();return {'url':f"https://t.me/{os.environ.get('BOT_USERNAME','YourBotUsername')}?start={code}"}
@app.websocket('/ws')
async def websocket(ws:WebSocket):
    await ws.accept();pub=r.pubsub();await pub.subscribe('pet-events')
    try:
        async for event in pub.listen():
            if event['type']=='message':await ws.send_text(event['data'])
    except (WebSocketDisconnect,RuntimeError):pass
    finally:await pub.aclose()

# Telegram webhook works inside this FastAPI service: no background worker or Redis
# subscriber is needed to answer commands and deliver Telegram notifications.
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_WEBHOOK_SECRET = os.getenv('TELEGRAM_WEBHOOK_SECRET', '')
TELEGRAM_API = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}'
log = logging.getLogger('zverolok.telegram')

async def telegram_call(method: str, payload: dict):
    if not TELEGRAM_TOKEN:
        log.warning('TELEGRAM_BOT_TOKEN is missing')
        return None
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.post(f'{TELEGRAM_API}/{method}', json=payload)
            response.raise_for_status()
            result = response.json()
            if not result.get('ok'):
                log.error('Telegram %s failed: %s', method, result.get('description'))
            return result
    except (httpx.HTTPError, ValueError) as exc:
        log.error('Telegram %s request failed: %s', method, type(exc).__name__)
        return None

async def telegram_send(chat_id: int, message: str, location_button: bool = False):
    data = {'chat_id': chat_id, 'text': message}
    if location_button:
        data['reply_markup'] = {'keyboard': [[{'text': '📍 Отправить геопозицию', 'request_location': True}]], 'resize_keyboard': True}
    await telegram_call('sendMessage', data)

async def configure_telegram_webhook():
    if not TELEGRAM_TOKEN or not TELEGRAM_WEBHOOK_SECRET:
        log.warning('Telegram webhook disabled: set TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET')
        return
    public_url = os.getenv('PUBLIC_API_URL', 'https://zverolok-api.onrender.com').rstrip('/')
    result = await telegram_call('setWebhook', {
        'url': public_url + '/telegram/webhook',
        'secret_token': TELEGRAM_WEBHOOK_SECRET,
        'allowed_updates': ['message'],
        'drop_pending_updates': False,
    })
    if result and result.get('ok'):
        log.info('Telegram webhook configured')

@app.post('/telegram/webhook', include_in_schema=False)
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    if not TELEGRAM_TOKEN or not TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(503, 'Telegram is not configured')
    if not secrets.compare_digest(x_telegram_bot_api_secret_token or '', TELEGRAM_WEBHOOK_SECRET):
        raise HTTPException(403, 'Forbidden')
    update = await request.json()
    message = update.get('message') or {}
    chat = message.get('chat') or {}
    sender = message.get('from') or {}
    chat_id, telegram_id = chat.get('id'), sender.get('id')
    if not chat_id or not telegram_id or chat.get('type') != 'private':
        return {'ok': True}
    command_text = (message.get('text') or '').strip()
    command = command_text.split(maxsplit=1)[0].split('@')[0].lower() if command_text else ''
    if command == '/start':
        parts = command_text.split(maxsplit=1)
        if len(parts) == 2:
            async with Session.begin() as session:
                row = (await session.execute(text('SELECT user_id FROM link_codes WHERE code=:code AND expires > now() FOR UPDATE'), {'code': parts[1]})).first()
                if row:
                    await session.execute(text('UPDATE users SET telegram_id=NULL WHERE telegram_id=:tg'), {'tg': telegram_id})
                    await session.execute(text('UPDATE users SET telegram_id=:tg WHERE id=:uid'), {'tg': telegram_id, 'uid': row.user_id})
                    await session.execute(text('DELETE FROM link_codes WHERE code=:code'), {'code': parts[1]})
            await telegram_send(chat_id, '✅ Telegram привязан к ЗвероЛок!' if row else 'Ссылка истекла. Получите новую в профиле сайта.')
        else:
            await telegram_send(chat_id, '🐾 Привет! Это ЗвероЛок. Чтобы получать оповещения, привяжите Telegram в профиле сайта.\nКоманды: /start, /radius 5, /animals cat dog')
        await telegram_send(chat_id, '📍 Отправьте свою геопозицию:', location_button=True)
    elif 'location' in message:
        loc = message['location']
        async with Session.begin() as session:
            result = await session.execute(text('UPDATE users SET lat=:lat, lon=:lon WHERE telegram_id=:tg RETURNING id'), {'lat': loc['latitude'], 'lon': loc['longitude'], 'tg': telegram_id})
            linked = result.first() is not None
        await telegram_send(chat_id, '✅ Геопозиция сохранена.' if linked else 'Сначала привяжите Telegram в профиле сайта.')
    elif command == '/radius':
        try:
            radius = int(command_text.split()[1])
            if not 1 <= radius <= 100:
                raise ValueError()
        except (ValueError, IndexError):
            await telegram_send(chat_id, 'Используйте /radius 5 (от 1 до 100 км)')
            return {'ok': True}
        async with Session.begin() as session:
            result = await session.execute(text('UPDATE users SET radius_km=:radius WHERE telegram_id=:tg RETURNING id'), {'radius': radius, 'tg': telegram_id})
            linked = result.first() is not None
        await telegram_send(chat_id, f'✅ Радиус: {radius} км.' if linked else 'Сначала привяжите Telegram.')
    elif command == '/animals':
        kinds = command_text.split()[1:]
        if not kinds or not set(kinds) <= {'cat', 'dog', 'other'}:
            await telegram_send(chat_id, 'Пример: /animals cat dog')
            return {'ok': True}
        async with Session.begin() as session:
            result = await session.execute(text('UPDATE users SET kinds=:kinds WHERE telegram_id=:tg RETURNING id'), {'kinds': ','.join(kinds), 'tg': telegram_id})
            linked = result.first() is not None
        await telegram_send(chat_id, '✅ Виды животных обновлены.' if linked else 'Сначала привяжите Telegram.')
    elif command:
        await telegram_send(chat_id, '🐾 Команды: /start, /radius 5, /animals cat dog')
    return {'ok': True}
