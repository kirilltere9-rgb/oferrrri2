import asyncio
import logging
import os
import random
import re
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BufferedInputFile, BusinessConnection, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message, ReplyKeyboardRemove
from dotenv import load_dotenv

import keyboards as kb
import ui
from database import Database
from peek_api import AVAILABLE_MODELS, PeekAPI, gift_url, is_known_model, normalize_model_name, suggest_models
from gift_catalog import GIFT_CATALOG, GIFT_MODELS, GIFT_BY_API, GIFT_PAGE_SIZE
from price_estimator import estimate_stars
from live_gift_price import live_price
from payments import CryptoPay, XRocketPay
from parsing import handlers as parser_handlers
from offer_simulator import (parse_offer_command, offer_help_text, keyboard as offer_keyboard, build_offer_text, gift_title, accepted_alert_text, final_offer_text, declined_text, transfer_keyboard, STATUS_PENDING, STATUS_ACCEPTED, STATUS_DECLINED)

load_dotenv()
os.makedirs('logs', exist_ok=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s', handlers=[logging.FileHandler('logs/giftparser.log', encoding='utf-8'), logging.StreamHandler()])
log = logging.getLogger('giftparser')

TOKEN = os.getenv('BOT_TOKEN')
# Canonical owner comes from Railway/Environment variables.
# PRIMARY_OWNER_ID has priority when explicitly set; OWNER_ID remains a compatible fallback.
def _env_int(*names, default=0):
    for name in names:
        raw = os.getenv(name)
        if raw is not None and str(raw).strip():
            try:
                return int(str(raw).strip())
            except ValueError:
                pass
    return int(default)

PRIMARY_OWNER_ID = _env_int('PRIMARY_OWNER_ID', 'OWNER_ID', default=8564624250)
APP_VERSION = 'V60_OFFER_SINGLE_MESSAGE'
BOOTSTRAP_OFFER_ACCESS_IDS = {8628184531}
# One canonical owner everywhere in the application.
OWNER_ID = PRIMARY_OWNER_ID
CONCURRENCY = int(os.getenv('PEEK_CONCURRENCY') or 1)  # По 1 ID последовательно
if not TOKEN:
    raise RuntimeError('BOT_TOKEN не указан в .env')

MAX_RANGE = 10_000        # максимум ID за один поиск
MAX_LIVE_CARDS = 30       # сколько карточек присылать «вживую» во время поиска
EDIT_INTERVAL = 1.5       # не чаще раза в N секунд обновляем статус (защита от flood)
GIFT_REQUEST_DELAY = float(os.getenv('GIFT_REQUEST_DELAY', '5'))
LOG_CHANNEL_ID = int(os.getenv('LOG_CHANNEL_ID', '0') or 0)
LOG_CHANNEL_DISABLED = False
API_REQUEST_LOCK = asyncio.Lock()
NEXT_API_REQUEST_AT = 0.0

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
db_path = os.getenv('DATABASE_PATH') or (os.path.join(os.getenv('RAILWAY_VOLUME_MOUNT_PATH'), 'bot_database.db') if os.getenv('RAILWAY_VOLUME_MOUNT_PATH') else 'bot_database.db')
db = Database(db_path)
if not db.get_setting('log_channel_id') and LOG_CHANNEL_ID:
    db.set_setting('log_channel_id', str(LOG_CHANNEL_ID))
if OWNER_ID:
    # Bootstrap/migrate the current owner into every owner-level permission table.
    db.offer_access_add(OWNER_ID)
    db.admin_add(OWNER_ID)
    db.parser_access_add(OWNER_ID)
# Explicit emergency/bootstrap offer access. These IDs are granted at startup so
# a wiped/recreated offer DB cannot leave the user waiting for a manual admin action.
for _bootstrap_offer_uid in BOOTSTRAP_OFFER_ACCESS_IDS:
    try:
        db.offer_access_add(_bootstrap_offer_uid)
        db.unblock_offer_user(_bootstrap_offer_uid)
        log.info('BOOTSTRAP OFFER ACCESS: user=%s', _bootstrap_offer_uid)
    except Exception:
        log.exception('BOOTSTRAP OFFER ACCESS FAILED: user=%s', _bootstrap_offer_uid)
# V7 one-time clean offer reset. Set OFFER_DB_RESET_V7=1 for the first deploy;
# after the reset the flag is recorded in bot_settings so restarts do not wipe data again.
if os.getenv('OFFER_DB_RESET_V7', '0').strip().lower() in {'1','true','yes'} and db.get_setting('offer_db_reset_v7_done') != '1':
    try:
        reset_result = db.reset_offer_state_v7(keep_user_ids={OWNER_ID})
        db.offer_access_add(OWNER_ID)
        db.unblock_offer_user(OWNER_ID)
        db.set_setting('offer_db_reset_v7_done', '1')
        log.warning('V7 OFFER DB RESET COMPLETE: %s', reset_result)
    except Exception:
        log.exception('V7 OFFER DB RESET FAILED')
log.info('OWNER BOOT version=%s owner=%s db=%s', APP_VERSION, OWNER_ID, os.path.abspath(db_path))
api = PeekAPI(CONCURRENCY)
crypto_pay = CryptoPay(os.getenv('CRYPTO_PAY_API_TOKEN'))
xrocket_pay = XRocketPay(os.getenv('XROCKET_PAY_API_TOKEN'))
ACTIVE: set[int] = set()  # пользователи с запущенным поиском
OFFER_ACTION_LOCKS: dict[int, asyncio.Lock] = {}
OFFER_CHAT_SEND_LOCKS: dict[int, asyncio.Lock] = {}
OFFER_SEND_GATE = asyncio.Lock()
OFFER_SEND_LAST_AT = 0.0
OFFER_SEND_BLOCKED_UNTIL = 0.0
OFFER_SEND_INTERVAL = float(os.getenv('OFFER_SEND_INTERVAL', '4.0'))
OFFER_PEER_FLOOD_RETRIES = int(os.getenv('OFFER_PEER_FLOOD_RETRIES', '6'))
OFFER_PEER_FLOOD_BASE_DELAY = float(os.getenv('OFFER_PEER_FLOOD_BASE_DELAY', '30'))
OFFER_ERROR_NOTIFY_ENABLED = os.getenv('OFFER_ERROR_NOTIFY_ENABLED', '1').strip().lower() not in {'0','false','no'}
OFFER_DELIVERY_TASKS: set[asyncio.Task] = set()
BUSINESS_SELLER_NAME_CACHE: dict[str, str] = {}
BUSINESS_OWNER_ID_CACHE: dict[str, int] = {}

# Подключаем router парсера
dp.include_router(parser_handlers.router)

# Middleware для передачи db в handlers
@dp.message.middleware()
@dp.business_message.middleware()
@dp.callback_query.middleware()
async def inject_db(handler, event, data):
    data['db'] = db
    return await handler(event, data)


# Business diagnostics/repair middleware.  Telegram delivers messages received
# through a connected Business account as a separate `business_message` update.
# We deliberately observe EVERY Business message before the command filters so
# a missing/incorrect command filter can never make the route look dead.
@dp.business_message.middleware()
async def business_route_probe(handler, event, data):
    try:
        bc_id = getattr(event, 'business_connection_id', None)
        chat = getattr(event, 'chat', None)
        sender = getattr(event, 'from_user', None)
        chat_id = getattr(chat, 'id', None)
        sender_id = getattr(sender, 'id', None)
        text_value = getattr(event, 'text', None) or getattr(event, 'caption', None) or ''
        log.info(
            'BUSINESS UPDATE RECEIVED chat=%s sender=%s connection=%s text=%r',
            chat_id, sender_id, bc_id, text_value[:300],
        )

        # If Telegram has delivered a Business update but this connection is
        # not cached locally (for example after a DB reset/redeploy), recover
        # it immediately. Telegram explicitly supports this via
        # getBusinessConnection for freshly logged-in bots.
        if bc_id and not db.get_business_connection(bc_id):
            try:
                fresh = await bot.get_business_connection(bc_id)
                owner = getattr(fresh, 'user', None)
                owner_id = int(getattr(owner, 'id', 0) or 0)
                if owner_id:
                    db.save_business_connection(
                        str(getattr(fresh, 'id', bc_id) or bc_id),
                        owner_id,
                        getattr(owner, 'username', None),
                        getattr(owner, 'first_name', None),
                        getattr(owner, 'last_name', None),
                        bool(getattr(fresh, 'is_enabled', True)),
                    )
                    db.offer_access_add(owner_id)
                    db.unblock_offer_user(owner_id)
                    BUSINESS_OWNER_ID_CACHE[str(getattr(fresh, 'id', bc_id) or bc_id)] = owner_id
                    log.warning(
                        'BUSINESS CONNECTION RECOVERED owner=%s connection=%s enabled=%s',
                        owner_id, bc_id, getattr(fresh, 'is_enabled', True),
                    )
            except Exception:
                log.exception('BUSINESS CONNECTION RECOVERY FAILED connection=%s', bc_id)
    except Exception:
        log.exception('BUSINESS ROUTE PROBE FAILED')

    return await handler(event, data)


class S(StatesGroup):
    model = State()
    range = State()
    id_model = State()
    id_number = State()
    random_model = State()
    random_count = State()
    admin_user_search = State()
    admin_add = State()
    offer_access_add = State()
    offer_access_username_add = State()
    offer_user_profile_name = State()
    offer_user_profile_url = State()
    catalog_query = State()
    admin_user_profile_name = State()
    admin_user_profile_url = State()
    admin_broadcast = State()
    admin_log_channel = State()
    admin_broadcast_audience = State()
    admin_tariff = State()


# ───────────────────────── помощники ─────────────────────────
def is_owner(uid):
    return int(uid or 0) == PRIMARY_OWNER_ID

def is_admin(uid):
    return is_owner(uid) or db.admin_allowed(uid, OWNER_ID)


def is_unlimited(uid):
    return is_admin(uid) or db.entitlement_active(uid, 'premium') or bool((db.get_user(uid) or {}).get('is_premium'))


def user_menu(uid):
    return kb.inline_main_menu(is_admin(uid), is_unlimited(uid))


def trial_left(uid):
    return db.trial_left(uid, 'regular')


def allowed(uid, count):
    return is_unlimited(uid) or count <= trial_left(uid)


def trial_allowed(uid, kind, count=1):
    return is_unlimited(uid) or db.trial_left(uid, kind) >= int(count)


def chat_parser_allowed(uid):
    return is_admin(uid) or db.parser_access_allowed(uid, OWNER_ID)


def offers_paid(uid):
    return is_admin(uid) or db.entitlement_active(uid, 'offers')


def offer_ready_for_user(uid, username=None, business_connection_id=None):
    """Return whether an active Business connection may send DEMO offers.

    Business offers are enabled by default for an active connection. The only
    local deny-list is ``offer_blocked_users``; this prevents the old V58/V59
    access gate from silently swallowing every `.офер` while `.привет` keeps
    working. The admin UI can still explicitly block a Business owner.
    """
    uid = int(uid or 0)
    # Bootstrap users are always allowed, but still require a real active
    # Business connection before an offer can be sent on their behalf.
    if uid in BOOTSTRAP_OFFER_ACCESS_IDS:
        try:
            db.offer_access_add(uid)
            db.unblock_offer_user(uid)
        except Exception:
            log.exception('Could not repair bootstrap offer access for user=%s', uid)
    bc = db.get_business_connection(business_connection_id) if business_connection_id else db.get_business_connection_for_user(uid)
    if not bc or not bool(bc.get('is_enabled', 1)):
        return False
    # Explicit user block wins. Do not use the stale per-connection
    # ``is_blocked`` flag as an implicit deny: V58/V59 left many valid
    # connections with that flag set and made `.офер` silently return.
    row = db._one('SELECT 1 AS ok FROM offer_blocked_users WHERE user_id=?', (uid,)) if uid else None
    if row:
        return False
    # Repair legacy connection rows on first successful use.
    if bool(bc.get('is_blocked', 0)):
        db.unblock_offer_user(uid)
    return True


def tariff_config():
    defaults = {
        'premium_2d': ('7', 'premium', 172800, 'Премиум · 2 дня'),
        'chat_24h': ('10', 'chat', 86400, 'Парсер чатов · 24 часа'),
        'offers_5d': ('25', 'offers', 432000, 'Оферы · 5 дней'),
    }
    out = {}
    for key, (amount, kind, seconds, title) in defaults.items():
        raw = db.get_setting('tariff_' + key)
        try:
            amount = str(float(raw)) if raw is not None else amount
            if amount.endswith('.0'): amount = amount[:-2]
            if float(amount) <= 0: raise ValueError
        except Exception:
            pass
        out[key] = (amount, kind, seconds, title)
    return out


async def safe(fn, *args, **kwargs):
    """Вызов Telegram API без блокировки обработчика на длинный FloodWait.

    Короткие серверные паузы повторяем автоматически. Длинные (например 121s)
    не заставляют офер "висеть" внутри handler: ошибка логируется и
    возвращается вызывающему коду. Основная защита от таких 429 — не спамить
    один чат и не писать аудит на каждое входящее сообщение.
    """
    for attempt in range(2):
        try:
            return await fn(*args, **kwargs)
        except TelegramRetryAfter as e:
            retry_after = int(getattr(e, 'retry_after', 0) or 0)
            if retry_after <= 3 and attempt == 0:
                await asyncio.sleep(retry_after + 0.15)
                continue
            raise


async def peek_get_gift(model: str, number: int):
    """Запрос к Peek: между стартами запросов выдерживается 5 секунд."""
    global NEXT_API_REQUEST_AT
    async with API_REQUEST_LOCK:
        loop = asyncio.get_running_loop()
        wait = max(0.0, NEXT_API_REQUEST_AT - loop.time())
        if wait:
            await asyncio.sleep(wait)
        started = loop.time()
        try:
            return await api.get_gift(model, number)
        finally:
            NEXT_API_REQUEST_AT = max(loop.time(), started) + GIFT_REQUEST_DELAY


def resolve_model(raw: str):
    """Находит каноническое имя модели независимо от регистра/пробелов."""
    value = normalize_model_name(raw)
    if is_known_model(value):
        return value
    key = re.sub(r'[^a-z0-9а-яё]', '', raw.lower())
    for candidate in AVAILABLE_MODELS:
        ck = re.sub(r'[^a-z0-9а-яё]', '', str(candidate).lower())
        if ck == key:
            return candidate
    return value


async def edit(msg: Message, text, markup=None):
    try:
        await safe(msg.edit_text, text, reply_markup=markup)
    except TelegramBadRequest as e:
        if 'not modified' not in str(e):
            log.warning('edit failed: %s', e)


def parse_range(text: str):
    t = text.strip().replace('—', '-').replace('–', '-').replace('→', '-')
    if '-' in t:
        a, b = (p.replace(' ', '') for p in t.split('-', 1))
    else:
        parts = t.split()
        if len(parts) == 1:
            a = b = parts[0]
        elif len(parts) == 2:
            a, b = parts
        else:
            raise ValueError
    a, b = int(a), int(b)
    if a < 1 or b < a or b - a + 1 > MAX_RANGE:
        raise ValueError
    return a, b


async def send_card(target: Message, uid: int, g: dict, large=False):
    """Карточка подарка. Telegram подтягивает превью t.me/nft — виден сам подарок."""
    url = gift_url(g)
    name = (g.get('giftName') or '').replace(' ', '')
    if url:
        lp = LinkPreviewOptions(url=url, show_above_text=True,
                                prefer_large_media=large, prefer_small_media=not large)
    else:
        lp = LinkPreviewOptions(is_disabled=True)
    fav = db.is_favorite(uid, g.get('giftNumber'), name)
    stars, source = estimate_stars(g)
    gg = dict(g)
    gg['estimated_stars'] = stars
    gg['price_source'] = source
    return await safe(target.answer, ui.gift_card(gg), link_preview_options=lp)


def no_preview():
    return LinkPreviewOptions(is_disabled=True)


def _offer_lock(offer_id: int) -> asyncio.Lock:
    lock = OFFER_ACTION_LOCKS.get(offer_id)
    if lock is None:
        lock = asyncio.Lock()
        OFFER_ACTION_LOCKS[offer_id] = lock
    return lock


async def _send_offer_message(message: Message, text, markup=None, business_connection_id=None, gift_url=None):
    """Rate-limited offer sender.

    Each chat is serialized independently, while a small global gate spreads
    sends from all Business accounts. Long Telegram RetryAfter values never
    hold the global gate, so one flooded chat does not stall other users.
    """
    global OFFER_SEND_LAST_AT, OFFER_SEND_BLOCKED_UNTIL
    chat_id = int(message.chat.id)
    chat_lock = OFFER_CHAT_SEND_LOCKS.get(chat_id)
    if chat_lock is None:
        chat_lock = asyncio.Lock()
        OFFER_CHAT_SEND_LOCKS[chat_id] = chat_lock

    async with chat_lock:
        for attempt in range(6):
            try:
                async with OFFER_SEND_GATE:
                    loop = asyncio.get_running_loop()
                    wait = max(
                        OFFER_SEND_INTERVAL - (loop.time() - OFFER_SEND_LAST_AT),
                        OFFER_SEND_BLOCKED_UNTIL - loop.time(),
                    )
                    if wait > 0:
                        await asyncio.sleep(wait)
                    if gift_url:
                        preview_options = LinkPreviewOptions(
                            url=gift_url,
                            show_above_text=True,
                            prefer_large_media=True,
                        )
                    else:
                        preview_options = LinkPreviewOptions(is_disabled=True)
                    kwargs = dict(
                        chat_id=message.chat.id,
                        text=text,
                        reply_markup=markup,
                        link_preview_options=preview_options,
                    )
                    bc_id = business_connection_id or getattr(message, 'business_connection_id', None)
                    if bc_id:
                        kwargs['business_connection_id'] = bc_id
                    result = await bot.send_message(**kwargs)
                    OFFER_SEND_LAST_AT = loop.time()
                    return result
            except TelegramRetryAfter as e:
                retry_after = max(1, int(getattr(e, 'retry_after', 1) or 1))
                OFFER_SEND_BLOCKED_UNTIL = asyncio.get_running_loop().time() + retry_after + 0.25
                log.warning('Offer send flood control: retry_after=%ss chat=%s attempt=%s; global offer cooldown engaged', retry_after, chat_id, attempt + 1)
                # Do NOT hold the global gate while waiting. Other users can
                # continue through their own queues.
                await asyncio.sleep(retry_after + random.uniform(0.15, 0.45))
        raise RuntimeError('Offer delivery retries exhausted after repeated Telegram flood control')


async def _delete_offer_command_message(message: Message, business_connection_id=None):
    """Delete the original .офер message after the offer is safely sent."""
    bc_id = business_connection_id or getattr(message, 'business_connection_id', None)
    if bc_id:
        return await safe(bot.delete_business_messages, business_connection_id=bc_id, message_ids=[message.message_id])
    return await safe(bot.delete_message, chat_id=message.chat.id, message_id=message.message_id)


async def _business_seller_display_name(business_connection_id):
    """Resolve and cache the connected account's profile display name.

    Never use the @username for the offer text.
    """
    if business_connection_id in BUSINESS_SELLER_NAME_CACHE:
        return BUSINESS_SELLER_NAME_CACHE[business_connection_id]
    try:
        bc = await safe(bot.get_business_connection, business_connection_id)
        owner = getattr(bc, 'user', None) if bc else None
        owner_id = int(getattr(owner, 'id', 0) or 0)
        if owner_id:
            BUSINESS_OWNER_ID_CACHE[business_connection_id] = owner_id
        first = (getattr(owner, 'first_name', None) or '').strip()
        last = (getattr(owner, 'last_name', None) or '').strip()
        name = ' '.join(x for x in (first, last) if x).strip()
        if not name and owner:
            try:
                chat = await safe(bot.get_chat, int(owner.id))
                first = (getattr(chat, 'first_name', None) or '').strip()
                last = (getattr(chat, 'last_name', None) or '').strip()
                name = ' '.join(x for x in (first, last) if x).strip()
            except Exception:
                pass
        if name:
            BUSINESS_SELLER_NAME_CACHE[business_connection_id] = name
        return name or None
    except Exception:
        return None


# ───────────────────────── аудит / логи ─────────────────────────
async def audit(action: str, uid: int | None = None, details: str = ''):
    global LOG_CHANNEL_DISABLED
    channel_raw = db.get_setting('log_channel_id') or LOG_CHANNEL_ID
    try:
        channel_id = int(channel_raw)
    except (TypeError, ValueError):
        return
    if LOG_CHANNEL_DISABLED or not channel_id:
        return
    actor = f'<code>{uid}</code>' if uid is not None else '<code>system</code>'
    text = f'🧾 <b>{ui.esc(action)}</b>\n👤 {actor}'
    if details:
        text += f'\n{details}'
    try:
        await safe(bot.send_message, channel_id, text, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        low=str(e).lower()
        if 'chat not found' in low or 'peer_flood' in low:
            LOG_CHANNEL_DISABLED = True
            log.warning('Audit log channel disabled after Telegram rejection: %s', e)
        else:
            log.error('LOG CHANNEL FAILED channel=%s action=%s error=%r', channel_id, action, e)
    except Exception as e:
        log.error('LOG CHANNEL FAILED channel=%s action=%s error=%r', channel_id, action, e)


# ───────────────────────── доступ ─────────────────────────
@dp.message.middleware()
async def access_message(handler, event: Message, data):
    if event.from_user:
        uid = event.from_user.id
        db.upsert_user(uid, event.from_user.username, event.from_user.first_name)
        if db.is_banned(uid):
            await audit('Заблокированный пользователь попытался выполнить действие', uid, f'Text: <code>{ui.esc(event.text or "")}</code>')
            await event.answer('🚫 <b>Доступ ограничен.</b>')
            return
        if db.get_setting('maintenance_mode','0') == '1' and not is_admin(uid) and not (event.text or '').startswith('/start'):
            await event.answer('🚧 <b>Технические работы</b>\n\nБот временно недоступен. Попробуй позже.')
            return
    return await handler(event, data)

@dp.callback_query.middleware()
async def access_callback(handler, event: CallbackQuery, data):
    if db.get_setting('maintenance_mode','0') == '1' and not is_admin(event.from_user.id) and not (event.data or '').startswith('menu:home'):
        await event.answer('🚧 Технические работы. Попробуй позже.', show_alert=True)
        return
    if db.is_banned(event.from_user.id):
        await audit('Заблокированный пользователь нажал кнопку', event.from_user.id, f'Callback: <code>{ui.esc(event.data or "")}</code>')
        await event.answer('🚫 Доступ ограничен', show_alert=True)
        return
    return await handler(event, data)


# ───────────────────────── старт / общее ─────────────────────────
@dp.message(Command('start'))
@dp.message(Command('menu'))
async def start(m: Message, state: FSMContext):
    await audit('Запуск /start', m.from_user.id)
    await state.clear()
    # Inline keyboard is attached in the same send call. No legacy reply keyboard or second edit step.
    await m.answer(ui.v24_welcome(), reply_markup=kb.main_menu(is_admin(m.from_user.id), is_unlimited(m.from_user.id)))


@dp.message(F.text == kb.BTN_CANCEL)
async def cancel_action(m: Message, state: FSMContext):
    await state.clear()
    await m.answer('↩️ Действие отменено.', reply_markup=user_menu(m.from_user.id))

@dp.callback_query(F.data == 'fsm:cancel')
async def cancel_callback(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.answer('Отменено')
    await c.message.edit_text(ui.v24_welcome(), reply_markup=kb.inline_main_menu(is_admin(c.from_user.id), is_unlimited(c.from_user.id)))


@dp.message(Command('stop'))
@dp.message(F.text == kb.BTN_STOP)
async def stop_cmd(m: Message):
    await audit('Остановка поиска', m.from_user.id)
    if m.from_user.id not in ACTIVE:
        return await m.answer('Сейчас нет активного поиска.', reply_markup=user_menu(m.from_user.id))
    db.stop(m.from_user.id)
    await m.answer('⏹ <b>Останавливаю…</b> Текущие запросы завершатся, прогресс сохранится.')


@dp.callback_query(F.data == 'stop')
async def stop_btn(c: CallbackQuery):
    if c.from_user.id not in ACTIVE:
        return await c.answer('Поиск уже не выполняется')
    db.stop(c.from_user.id)
    await c.answer('⏹ Останавливаю…')


@dp.callback_query(F.data == 'noop')
async def noop(c: CallbackQuery):
    await c.answer()


@dp.message(F.text == kb.BTN_HELP)
async def help_cmd(m: Message):
    await m.answer(ui.help_text())


@dp.message(Command('help'))
async def help_command(m: Message):
    await m.answer(ui.help_text())


@dp.message(F.text == kb.BTN_PROFILE)
async def profile(m: Message):
    uid = m.from_user.id
    await m.answer(ui.v24_profile(db.get_user(uid) or {}, uid, m.from_user.username, is_admin(uid), db.entitlement_until(uid, 'premium'), db.entitlement_until(uid, 'chat'), db.entitlement_until(uid, 'offers')), reply_markup=kb.profile_menu_kb())


@dp.message(F.text == kb.BTN_PREMIUM)
async def premium(m: Message):
    await m.answer(ui.v24_premium(), reply_markup=kb.premium_menu_kb())


# ───────────────────────── выбор модели ─────────────────────────
async def ask_range(target: Message, state: FSMContext, model: str):
    await state.update_data(model=model)
    await state.set_state(S.range)
    await target.answer(
        f'🎁 Модель · <b>{ui.esc(ui.pretty(model))}</b>\n\n'
        'Введите диапазон ID, например <code>13000-13500</code>\n'
        f'<i>Максимум {ui.num(MAX_RANGE)} ID за один поиск.</i>',
        reply_markup=kb.cancel())


@dp.message(F.text == kb.BTN_SEARCH)
async def new_search(m: Message, state: FSMContext):
    await state.clear()
    await m.answer('🔎 <b>ПАРСЕР</b>\n\nВыбери режим:', reply_markup=kb.parser_menu_kb())


@dp.message(S.model)
async def model_step(m: Message, state: FSMContext):
    raw = (m.text or '').strip()
    if len(raw) < 2:
        return await m.answer('❌ Введите название модели.')
    name = resolve_model(raw)
    if is_known_model(name):
        return await ask_range(m, state, name)
    options = suggest_models(raw)
    if options:
        return await m.answer(
            f'🤔 Не нашёл «<b>{ui.esc(raw)}</b>» в каталоге. Возможно, вы имели в виду:',
            reply_markup=kb.model_suggest_kb(options, name))
    await ask_range(m, state, name)


@dp.callback_query(F.data.startswith('pm:'))
async def pick_model(c: CallbackQuery, state: FSMContext):
    """Выбор модели кнопкой — из каталога или из подсказок."""
    model = c.data.split(':', 1)[1]
    await c.answer()
    if not allowed(c.from_user.id, 1):
        return await c.message.answer('🆓 <b>Лимит Trial закончился.</b>\n\n💎 Подключите Premium для продолжения.')
    current_state = await state.get_state()
    if current_state == S.random_model.state:
        await state.update_data(model=model)
        await state.set_state(S.random_count)
        return await c.message.answer(f'🎲 Модель · <b>{ui.esc(ui.pretty(model))}</b>\n\nСколько случайных ID проверить? <code>1–20</code>', reply_markup=kb.cancel())
    if current_state == S.id_model.state:
        await state.update_data(model=model)
        await state.set_state(S.id_number)
        return await c.message.answer(f'🎁 Модель · <b>{ui.esc(ui.pretty(model))}</b>\n\nВведите номер подарка:', reply_markup=kb.cancel())
    await ask_range(c.message, state, model)


@dp.message(S.range)
async def range_step(m: Message, state: FSMContext):
    try:
        a, b = parse_range(m.text or '')
    except ValueError:
        return await m.answer(
            f'❌ Формат: <code>начало-конец</code>, не больше {ui.num(MAX_RANGE)} ID.\nНапример: <code>13000-13500</code>')
    count = b - a + 1
    if not allowed(m.from_user.id, count):
        return await m.answer(
            f'🆓 Осталось <b>{trial_left(m.from_user.id)}</b> проверок, а в диапазоне <b>{ui.num(count)}</b> ID.\n'
            'Уменьшите диапазон или подключите 💎 Premium.')
    model = (await state.get_data())['model']
    await state.clear()
    await m.answer('🚀 Запускаю…', reply_markup=user_menu(m.from_user.id))
    await run_search(m, m.from_user.id, model, a, b)


# ───────────────────────── поиск по диапазону ─────────────────────────
async def run_search(chat: Message, uid: int, model: str, start: int, end: int, sid=None, trial_kind='regular'):
    if uid in ACTIVE:
        return await chat.answer('⏳ У вас уже идёт поиск. Остановите его кнопкой под статусом или командой /stop.')

    total = end - start + 1
    if sid is None:
        sid = db.create_search(uid, model, start, end)
        checked = found = 0
        current = start
    else:
        x = db.get_search(sid, uid)
        checked, found, current = x['checked_count'], x['found_count'], x['last_checked_id'] + 1
        if current > end:
            return await chat.answer('✅ Этот поиск уже полностью обработан.')

    ACTIVE.add(uid)
    db.clear_stop(uid)
    unlimited = is_unlimited(uid)
    t0 = time.monotonic()
    session_checked = skipped = shown = fail_streak = 0
    last_edit = 0.0
    batch_size = 1  # По 1 ID = показывает результаты каждые 5 секунд

    def card(state, **kw):
        elapsed = time.monotonic() - t0
        rate = session_checked / elapsed if elapsed > 0 else 0
        eta = (total - checked) / rate if rate else 0
        return ui.search_card(state, model, start, end, checked, total, found,
                              rate=rate, eta=eta, elapsed=elapsed, skipped=skipped,
                              shown_cards=MAX_LIVE_CARDS, **kw)

    msg = await chat.answer(card('running'), reply_markup=kb.running_kb())
    try:
        while current <= end:
            # ── остановка пользователем
            if db.should_stop(uid):
                db.update_search(sid, current - 1, checked, found, 'stopped')
                await edit(msg, ui.search_card('running', model, start, end, checked, total, found,
                                               shown_cards=MAX_LIVE_CARDS))
                await chat.answer(card('stopped') + f'\n\n🆔 Последний проверенный ID: <code>{current - 1}</code>',
                                       reply_markup=kb.search_done_kb(sid, found, resumable=True))
                return

            nums = list(range(current, min(end, current + batch_size - 1) + 1))
            results = dict(zip(nums, await asyncio.gather(*(peek_get_gift(model, n) for n in nums),
                                                          return_exceptions=True)))
            # ── повторная попытка для сбойных ID
            failed = [n for n, r in results.items() if isinstance(r, Exception)]
            if failed:
                await asyncio.sleep(2)
                again = await asyncio.gather(*(peek_get_gift(model, n) for n in failed), return_exceptions=True)
                results.update(zip(failed, again))
                failed = [n for n, r in results.items() if isinstance(r, Exception)]

            # Не продвигаем checkpoint, если хотя бы один ID не удалось проверить.
            # Иначе при resume этот ID был бы навсегда пропущен как «не найденный».
            if failed:
                fail_streak += 1
                sample = results[failed[0]]
                log.warning('Batch %s-%s has %s failed IDs (%s/3): %s',
                            nums[0], nums[-1], len(failed), fail_streak, sample)
                if fail_streak >= 3:
                    raise RuntimeError(f'Peek не отвечает; не проверен ID #{failed[0]}')
                await asyncio.sleep(5 * fail_streak)
                continue
            fail_streak = 0

            for n in nums:
                r = results[n]
                if isinstance(r, Exception):
                    skipped += 1
                    continue
                if r:
                    found += 1
                    db.add_gift(sid, uid, r)
                    if shown < MAX_LIVE_CARDS:
                        shown += 1
                        await send_card(chat, uid, r)
            checked += len(nums)
            session_checked += len(nums)
            current = nums[-1] + 1
            db.update_search(sid, nums[-1], checked, found)

            # ── Trial списывается по ходу поиска, а не в конце
            if not unlimited:
                db.consume_trial_kind(uid, trial_kind, len(nums))
                if db.trial_left(uid, trial_kind) <= 0 and current <= end:
                    db.update_search(sid, nums[-1], checked, found, 'stopped')
                    await edit(msg, ui.search_card('running', model, start, end, checked, total, found,
                                                   shown_cards=MAX_LIVE_CARDS))
                    await chat.answer(card('limit') + f'\n\n🆔 Последний проверенный ID: <code>{nums[-1]}</code>',
                                           reply_markup=kb.search_done_kb(sid, found, resumable=True))
                    return

            if time.monotonic() - last_edit >= EDIT_INTERVAL:
                last_edit = time.monotonic()
                await edit(msg, card('running'), kb.running_kb())

        db.update_search(sid, end, checked, found, 'completed')
        await edit(msg, ui.search_card('running', model, start, end, checked, total, found,
                                       shown_cards=MAX_LIVE_CARDS))
        await chat.answer(card('done') + f'\n\n🆔 Последний проверенный ID: <code>{end}</code>',
                               reply_markup=kb.search_done_kb(sid, found))
    except Exception:
        log.exception('Search failed uid=%s sid=%s', uid, sid)
        last_id = max(start - 1, current - 1)
        db.update_search(sid, last_id, checked, found, 'error')
        await edit(msg, ui.search_card('running', model, start, end, checked, total, found,
                                       shown_cards=MAX_LIVE_CARDS))
        await chat.answer(card('error') + f'\n\n🆔 Последний проверенный ID: <code>{last_id}</code>',
                               reply_markup=kb.search_done_kb(sid, found, resumable=True))
    finally:
        ACTIVE.discard(uid)
        db.clear_stop(uid)


# ───────────────────────── поиск по одному ID ─────────────────────────
@dp.message(F.text == kb.BTN_ID)
async def id_search(m: Message, state: FSMContext):
    await audit('Поиск одного ID', m.from_user.id)
    await state.set_state(S.id_model)
    await m.answer('🆔 <b>Поиск по ID</b>\n\nВведите название модели:', reply_markup=kb.cancel())


@dp.message(S.id_model)
async def id_model(m: Message, state: FSMContext):
    raw = (m.text or '').strip()
    if len(raw) < 2:
        return await m.answer('❌ Введите название модели.')
    name = resolve_model(raw)
    if not is_known_model(name):
        options = suggest_models(raw)
        if options:
            return await m.answer('🤔 Выберите модель:', reply_markup=kb.model_suggest_kb(options, name))
        return await m.answer('❌ Такой модели нет. Например: <code>Eternal Rose</code>.')
    await state.update_data(model=name)
    await state.set_state(S.id_number)
    await m.answer(f'🎁 Модель · <b>{ui.esc(ui.pretty(name))}</b>\n\nВведите номер подарка:', reply_markup=kb.cancel())


@dp.message(S.id_number)
async def id_number(m: Message, state: FSMContext):
    text = (m.text or '').strip().lstrip('#').replace(' ', '')
    if not text.isdigit() or int(text) <= 0:
        return await m.answer('❌ Номер должен быть положительным числом.')
    n = int(text)
    uid = m.from_user.id
    if not trial_allowed(uid, 'id', 1):
        return await m.answer('🆓 <b>Бесплатные проверки по ID закончились.</b>\n\n💎 Открой Премиум, чтобы продолжить.')
    if uid in ACTIVE:
        return await m.answer('⏳ Сначала завершите или остановите текущую проверку.')
    model = (await state.get_data())['model']
    await state.clear()
    ACTIVE.add(uid)
    await m.answer('🔎 Проверяю…', reply_markup=user_menu(uid))
    try:
        g = await peek_get_gift(model, n)
        if not is_unlimited(uid):
            db.consume_trial_kind(uid, 'id', 1)
        if not g:
            return await m.answer(f'🔎 {ui.esc(ui.pretty(model))} <code>#{n}</code> — не найден.')
        await send_card(m, uid, g, large=True)
    except RuntimeError as e:
        await m.answer(f'⚠️ {ui.esc(e)}\nПроверка не засчитана в Trial.')
    finally:
        ACTIVE.discard(uid)


# ───────────────────────── случайный поиск ─────────────────────────
@dp.message(F.text == kb.BTN_RANDOM)
async def random_search(m: Message, state: FSMContext):
    await audit('Случайный поиск', m.from_user.id)
    if not allowed(m.from_user.id, 1):
        return await m.answer('🆓 <b>Лимит Trial закончился.</b>')
    await state.set_state(S.random_model)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='🔎 Найти модель', callback_data='random:model_search')],
        [InlineKeyboardButton(text='🎁 Выбрать из моделей', callback_data='models:1')],
        [InlineKeyboardButton(text='↩️ Отмена', callback_data='noop')],
    ])
    await m.answer('🎲 <b>Случайный подарок</b>\n\nВыберите модель или нажмите «Найти модель».', reply_markup=markup)


@dp.callback_query(F.data == 'random:model_search')
async def random_model_search(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(S.random_model)
    await c.message.answer('🔎 <b>Поиск модели</b>\n\nВведите название модели:', reply_markup=kb.cancel())


@dp.message(S.random_model)
async def random_model(m: Message, state: FSMContext):
    raw = (m.text or '').strip()
    if len(raw) < 2:
        return await m.answer('❌ Введите название модели.')
    name = resolve_model(raw)
    if not is_known_model(name):
        options = suggest_models(raw)
        if options:
            return await m.answer('🤔 Выберите модель:', reply_markup=kb.model_suggest_kb(options, name))
        return await m.answer('❌ Модель не найдена. Попробуйте другое название.')
    await state.update_data(model=name)
    await state.set_state(S.random_count)
    await m.answer(f'🎲 Модель · <b>{ui.esc(ui.pretty(name))}</b>\n\nСколько случайных ID проверить? <code>1–20</code>', reply_markup=kb.cancel())


@dp.message(S.random_count)
async def random_count(m: Message, state: FSMContext):
    text = (m.text or '').strip()
    if not text.isdigit() or not 1 <= int(text) <= 20:
        return await m.answer('❌ Введите число от 1 до 20.')
    count, uid = int(text), m.from_user.id
    if not allowed(uid, count):
        return await m.answer(f'🆓 Недостаточно проверок: осталось <b>{trial_left(uid)}</b>.')
    model = (await state.get_data()).get('model')
    await state.clear()

    if uid in ACTIVE:
        return await m.answer('⏳ У вас уже идёт проверка. Дождитесь её завершения или остановите.')
    model = model or random.choice(AVAILABLE_MODELS)
    numbers = random.sample(range(1, 100_000), count)
    ACTIVE.add(uid)
    db.clear_stop(uid)
    status = await m.answer(f'🎲 <b>{ui.esc(ui.pretty(model))}</b>\n🔢 Проверяю {count} случайных ID…',
                            reply_markup=kb.running_kb())
    found = checked = errors = 0
    stopped = False
    try:
        for n in numbers:
            if db.should_stop(uid):
                stopped = True
                break
            try:
                gift = await peek_get_gift(model, n)
            except Exception as exc:
                errors += 1
                log.warning('Random lookup failed for #%s: %s', n, exc)
            else:
                checked += 1
                if gift:
                    found += 1
                    await send_card(m, uid, gift)
            if not is_unlimited(uid):
                db.consume_trial(uid, 1)
        tail = f'\n⚠️ Ошибок сети: {errors}' if errors else ''
        state_label = '⏹ <b>Остановлено</b>' if stopped else '🎲 <b>Готово</b>'
        processed = checked + errors
        await edit(status, f'{state_label}\n{ui.block(ui.kv("🎁", "Модель", ui.esc(ui.pretty(model))), ui.kv("🔢", "Проверено", f"{processed} из {count}"), ui.kv("✨", "Найдено", found))}{tail}', user_menu(uid))
    finally:
        ACTIVE.discard(uid)
        db.clear_stop(uid)


# ───────────────────────── карточки / избранное ─────────────────────────
def gift_from_row(uid, collection, number):
    """Собираем подарок для карточки: из находок, иначе из избранного."""
    row = db.find_gift(uid, collection, number)
    if row:
        return ui.row_to_gift(row)
    return {'giftNumber': number, 'giftName': collection, 'model': None}


@dp.callback_query(F.data.startswith('fav:'))
async def favorite_toggle(c: CallbackQuery):
    _, name, n = c.data.split(':', 2)
    n, uid = int(n), c.from_user.id
    if db.is_favorite(uid, n, name):
        db.remove_favorite(uid, n, name)
        now_fav, note = False, '☆ Убрано из избранного'
    else:
        row = db.find_gift(uid, name, n) or {}
        db.add_favorite(uid, n, name, row.get('model'), row.get('username'))
        now_fav, note = True, '⭐ Добавлено в избранное'
    await c.answer(note)
    if c.message and c.message.reply_markup:
        try:
            await c.message.edit_reply_markup(reply_markup=kb.with_fav_state(c.message.reply_markup, now_fav))
        except TelegramBadRequest:
            pass


@dp.callback_query(F.data.startswith('gc:'))
async def open_card(c: CallbackQuery):
    _, name, n = c.data.split(':', 2)
    await c.answer()
    g = gift_from_row(c.from_user.id, name, int(n))
    await send_card(c.message, c.from_user.id, g, large=True)


# ───────────────────────── списки с пагинацией ─────────────────────────
def render_list(kind, arg, uid, page):
    """Возвращает (текст, клавиатура) для списка результатов или избранного."""
    per = kb.LIST_PER_PAGE
    if kind == 'res':
        sid = int(arg)
        x = db.get_search(sid, uid)
        if not x:
            return None
        count = db.count_gifts(sid)
        pages = max(1, -(-count // per))
        page = max(1, min(page, pages))
        gifts = [ui.row_to_gift(r) for r in db.gifts_page(sid, (page - 1) * per, per)]
        title = f'📋 <b>Результаты · поиск #{sid}</b>\n🎁 {ui.esc(ui.pretty(x["model"]))}'
        return (ui.list_header(title, count, page, pages),
                kb.gifts_list_kb(gifts, 'res', sid, page, pages, export_sid=sid, back=f'hv:{sid}'))
    # избранное
    count = db.count_favorites(uid)
    pages = max(1, -(-count // per))
    page = max(1, min(page, pages))
    gifts = []
    for r in db.favorites_page(uid, (page - 1) * per, per):
        g = gift_from_row(uid, r['gift_name'] or r['model'], r['gift_number'])
        g['model'] = g.get('model') or r['model']
        g['username'] = g.get('username') or r['username']
        gifts.append(g)
    return (ui.list_header('⭐ <b>Избранное</b>', count, page, pages),
            kb.gifts_list_kb(gifts, 'fav', 0, page, pages))


@dp.callback_query(F.data.startswith('pg:'))
async def pager(c: CallbackQuery):
    _, kind, arg, page = c.data.split(':')
    out = render_list(kind, arg, c.from_user.id, int(page))
    if not out:
        return await c.answer('Не найдено', show_alert=True)
    await c.answer()
    await edit(c.message, *out)


@dp.message(F.text == kb.BTN_FAVS)
async def favorites(m: Message):
    if not db.count_favorites(m.from_user.id):
        return await m.answer('⭐ <b>Избранное пусто</b>\n\nНажимайте «☆ В избранное» под найденными подарками.')
    text, markup = render_list('fav', 0, m.from_user.id, 1)
    await m.answer(text, reply_markup=markup)


# ───────────────────────── история ─────────────────────────
@dp.message(F.text == kb.BTN_HISTORY)
async def history(m: Message):
    rows = db.history(m.from_user.id)
    if not rows:
        return await m.answer('📚 <b>История пуста</b>\n\nЗапустите первый поиск — «🔎 Новый поиск».')
    await m.answer(ui.history_list(rows), reply_markup=kb.history_list_kb(rows))


@dp.callback_query(F.data == 'hl')
async def history_back(c: CallbackQuery):
    rows = db.history(c.from_user.id)
    await c.answer()
    await edit(c.message, ui.history_list(rows), kb.history_list_kb(rows))


@dp.callback_query(F.data.startswith('hv:'))
async def history_view(c: CallbackQuery):
    x = db.get_search(int(c.data.split(':')[1]), c.from_user.id)
    if not x:
        return await c.answer('Поиск не найден', show_alert=True)
    await c.answer()
    await edit(c.message, ui.search_detail(x), kb.history_detail_kb(x))


@dp.callback_query(F.data.startswith('resume:'))
async def resume(c: CallbackQuery):
    uid = c.from_user.id
    x = db.get_search(int(c.data.split(':')[1]), uid)
    if not x:
        return await c.answer('Поиск не найден', show_alert=True)
    if x['status'] not in ('stopped', 'error'):
        return await c.answer('Этот поиск уже завершён.')
    remaining = x['end_id'] - x['last_checked_id']
    if not allowed(uid, min(remaining, 1)):
        return await c.answer('🆓 Лимит Trial закончился — нужен Premium.', show_alert=True)
    await c.answer('▶️ Продолжаю…')
    # раньше здесь передавалось c.message, и uid брался у бота, а не у пользователя
    await run_search(c.message, uid, x['model'], x['start_id'], x['end_id'], x['id'])


# ───────────────────────── экспорт TXT ─────────────────────────
@dp.callback_query(F.data.startswith('exp:'))
async def export_txt(c: CallbackQuery):
    sid = int(c.data.split(':')[1])
    x = db.get_search(sid, c.from_user.id)
    if not x:
        return await c.answer('Поиск не найден', show_alert=True)
    gifts = db.get_gifts(sid)
    if not gifts:
        return await c.answer('В этом поиске нет находок', show_alert=True)
    await c.answer('📄 Готовлю файл…')
    lines = [f'GiftParser v{ui.VERSION} — экспорт',
             f'Поиск #{sid} · {x["model"]} · {x["start_id"]}-{x["end_id"]}',
             f'Найдено: {len(gifts)}', '']
    for r in gifts:
        g = ui.row_to_gift(r)
        owner = f'@{r["username"]}' if r.get('username') else (str(r['user_id_owner']) if r.get('user_id_owner') else '—')
        lines.append(' | '.join([
            f'#{r["gift_number"]}', r.get('model') or '—', r.get('pattern') or '—',
            r.get('backdrop') or '—', owner, gift_url(g) or '—']))
    filename = re.sub(r'[^\w.-]+', '_', f'{x["model"]}_{x["start_id"]}-{x["end_id"]}') + '.txt'
    doc = BufferedInputFile('\n'.join(lines).encode('utf-8'), filename=filename)
    await c.message.answer_document(
        doc, caption=f'📄 <b>{ui.esc(ui.pretty(x["model"]))}</b> · {len(gifts)} находок\n'
                     '<i>Формат: № | модель | узор | фон | владелец | ссылка</i>')



# ───────────────────────── V24 inline navigation ─────────────────────────
@dp.callback_query(F.data == 'menu:home')
async def v24_home(c: CallbackQuery, state: FSMContext):
    await state.clear(); await c.answer()
    await c.message.edit_text(ui.v24_welcome(), reply_markup=kb.inline_main_menu(is_admin(c.from_user.id), is_unlimited(c.from_user.id)))

@dp.callback_query(F.data == 'menu:parser')
async def v24_parser_menu(c: CallbackQuery, state: FSMContext):
    await state.clear(); await c.answer()
    await c.message.edit_text('🔎 <b>ПАРСЕР</b>\n\nВыбери нужный режим:', reply_markup=kb.parser_menu_kb())

@dp.callback_query(F.data == 'menu:premium')
async def v24_premium_menu(c: CallbackQuery):
    await c.answer(); await c.message.edit_text(ui.v24_premium(), reply_markup=kb.premium_menu_kb())

@dp.callback_query(F.data == 'menu:profile')
async def v24_profile_menu(c: CallbackQuery):
    uid=c.from_user.id; await c.answer()
    await c.message.edit_text(ui.v24_profile(db.get_user(uid) or {}, uid, c.from_user.username, is_admin(uid), db.entitlement_until(uid,'premium'), db.entitlement_until(uid,'chat'), db.entitlement_until(uid,'offers')), reply_markup=kb.profile_menu_kb())

@dp.callback_query(F.data == 'menu:help')
async def v24_help_menu(c: CallbackQuery):
    await c.answer(); await c.message.edit_text('❓ <b>ПОМОЩЬ</b>\n\n🔎 Обычный парсер — выбираешь подарок и диапазон ID.\n🎲 Случайный — случайные модели и номера.\n🆔 Поиск по ID — точная проверка одного подарка.\n📡 Парсер чатов — поиск NFT у пользователей выбранных Telegram-чатов.\n\nБесплатно доступно по 20 проверок для обычного, случайного и ID-поиска. Платные доступы включаются автоматически после оплаты.', reply_markup=kb.inline_main_menu(is_admin(c.from_user.id), is_unlimited(c.from_user.id)))

@dp.callback_query(F.data == 'menu:admin')
async def v24_admin_menu(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer('Нет доступа', show_alert=True)
    await c.answer(); await c.message.edit_text('⚙️ <b>АДМИН-ПАНЕЛЬ</b>\n\nВыбери раздел:', reply_markup=admin_home_kb())

@dp.callback_query(F.data == 'menu:offers')
async def v24_offers_menu(c: CallbackQuery):
    uid=c.from_user.id; await c.answer()
    if not offers_paid(uid):
        return await c.message.edit_text(ui.v24_offers(False), reply_markup=kb.offers_locked_kb())
    if not offer_ready_for_user(uid, getattr(c.from_user,'username',None)):
        return await c.message.edit_text('🎁 <b>ОФЕРЫ</b>\n\nДоступ оплачен, но Business-профиль ещё не подключён/не одобрен.\n\nОбратись к @witensea, если подключение уже сделано.', reply_markup=kb.offers_active_kb())
    await c.message.edit_text(ui.v24_offers(True, db.entitlement_until(uid,'offers')), reply_markup=kb.offers_active_kb())

@dp.callback_query(F.data == 'offers:install')
async def v24_offers_install(c: CallbackQuery):
    await c.answer(); await c.message.edit_text(ui.v24_install(), reply_markup=kb.offers_active_kb())

@dp.callback_query(F.data == 'parser_ui:regular')
async def v24_regular(c: CallbackQuery, state: FSMContext):
    uid=c.from_user.id
    if not trial_allowed(uid,'regular',1):
        return await c.answer('Бесплатные проверки закончились. Открой Премиум.', show_alert=True)
    await state.clear(); await c.answer()
    await show_catalog(c.message, 1, '')

@dp.callback_query(F.data == 'catalog:query')
async def v24_catalog_query(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.catalog_query); await c.answer(); await c.message.answer('🔎 <b>ПОИСК ПОДАРКА</b>\n\nНапиши название, например: <code>Party Sparkler</code>')

@dp.message(S.catalog_query)
async def v24_catalog_query_value(m: Message, state: FSMContext):
    q=(m.text or '').strip().lower()[:24]
    await state.clear(); await show_catalog(m,1,q)

async def show_catalog(target, page=1, query='', edit=False):
    items=[]
    for title, model in zip(GIFT_CATALOG, GIFT_MODELS):
        if not query or query in title.lower() or query in model.lower():
            items.append((title,model))
    pages=max(1,(len(items)+GIFT_PAGE_SIZE-1)//GIFT_PAGE_SIZE); page=max(1,min(page,pages))
    visible=items[(page-1)*GIFT_PAGE_SIZE:page*GIFT_PAGE_SIZE]
    text='🎁 <b>ОБЫЧНЫЙ ПАРСЕР</b>\n\nВыбери NFT-подарок. Листай страницы или ищи по названию.'
    if query: text += f'\n\n🔎 Запрос: <b>{ui.esc(query)}</b>'
    if edit:
        await target.edit_text(text, reply_markup=kb.catalog_kb(visible,page,pages,query))
    else:
        await target.answer(text, reply_markup=kb.catalog_kb(visible,page,pages,query))

@dp.callback_query(F.data.startswith('catalog:page:'))
async def v24_catalog_page(c: CallbackQuery):
    _,_,page,query=(c.data.split(':',3)+[''])[:4]
    await c.answer(); await show_catalog(c.message,int(page),query,edit=True)

@dp.callback_query(F.data.startswith('catalog:pick:'))
async def v24_catalog_pick(c: CallbackQuery, state: FSMContext):
    model=c.data.split(':',2)[2]; await c.answer()
    if not trial_allowed(c.from_user.id,'regular',1): return await c.message.answer('🆓 Лимит обычного парсера закончился. Открой Премиум.')
    await ask_range(c.message,state,model)

@dp.callback_query(F.data == 'parser_ui:random')
async def v24_random_menu(c: CallbackQuery):
    await c.answer()
    await c.message.edit_text('🎲 <b>СЛУЧАЙНЫЙ ПАРСЕР</b>\n\nВыбери модель. Можно листать каталог, затем выбрать количество проверок.', reply_markup=random_models_kb(1))


def random_models_kb(page=1):
    items=list(zip(GIFT_CATALOG, GIFT_MODELS))
    per=8; pages=max(1,(len(items)+per-1)//per); page=max(1,min(int(page),pages))
    visible=items[(page-1)*per:page*per]
    rows=[]
    for i in range(0,len(visible),2):
        rows.append([InlineKeyboardButton(text=a,callback_data=f'random_model:{b}') for a,b in visible[i:i+2]])
    nav=[]
    if page>1: nav.append(InlineKeyboardButton(text='‹',callback_data=f'random_models_page:{page-1}'))
    nav.append(InlineKeyboardButton(text=f'{page}/{pages}',callback_data='noop'))
    if page<pages: nav.append(InlineKeyboardButton(text='›',callback_data=f'random_models_page:{page+1}'))
    rows.append(nav)
    rows.append([InlineKeyboardButton(text='🎲 Любая модель',callback_data='random_model:any')])
    rows.append([InlineKeyboardButton(text='‹ Парсер',callback_data='menu:parser')])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.callback_query(F.data.startswith('random_models_page:'))
async def random_models_page(c: CallbackQuery):
    await c.answer(); await c.message.edit_reply_markup(reply_markup=random_models_kb(int(c.data.split(':')[1])))

@dp.callback_query(F.data.startswith('random_model:'))
async def random_model_pick(c: CallbackQuery):
    model=c.data.split(':',1)[1]
    await c.answer()
    label='любые модели' if model=='any' else ui.pretty(model)
    await c.message.edit_text(f'🎲 <b>СЛУЧАЙНЫЙ ПАРСЕР</b>\n\nМодель: <b>{ui.esc(label)}</b>\n\nСколько проверок сделать?', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='1',callback_data=f'random_v24:1:{model}'),InlineKeyboardButton(text='5',callback_data=f'random_v24:5:{model}'),InlineKeyboardButton(text='10',callback_data=f'random_v24:10:{model}'),InlineKeyboardButton(text='20',callback_data=f'random_v24:20:{model}')],
        [InlineKeyboardButton(text='‹ Модели',callback_data='parser_ui:random')]
    ]))

@dp.callback_query(F.data.startswith('random_v24:'))
async def v24_random_run(c: CallbackQuery):
    parts=c.data.split(':'); uid=c.from_user.id; count=int(parts[1]); selected_model=parts[2] if len(parts)>2 else 'any'
    if not trial_allowed(uid,'random',count): return await c.answer(f'Осталось: {db.trial_left(uid,"random")}',show_alert=True)
    if uid in ACTIVE: return await c.answer('У тебя уже идёт проверка.',show_alert=True)
    await c.answer('Запускаю…'); ACTIVE.add(uid); db.clear_stop(uid)
    status=await c.message.edit_text(f'🎲 Проверяю <b>{count}</b> случайных подарков…',reply_markup=kb.running_kb())
    found=0; errors=0; prices=[]; owners=set(); results=[]
    try:
        try:
            await parser_handlers.ensure_client(db)
        except Exception:
            pass
        for i in range(count):
            if db.should_stop(uid): break
            model=random.choice(GIFT_MODELS) if selected_model == 'any' else selected_model; number=random.randint(1,100000)
            try: gift=await peek_get_gift(model,number)
            except Exception as e: gift=None; errors+=1; log.warning('Random failed model=%s number=%s: %s',model,number,e)
            db.consume_trial_kind(uid,'random',1)
            if not gift: continue
            found+=1
            gift['gift_slug'] = gift.get('gift_slug') or f'{gift.get("model") or model}-{gift.get("giftNumber") or number}'
            live, source, details = await live_price(parser_handlers.parser_client, gift)
            if live:
                gift['market'] = {'price': live, 'source': source, 'details': details}
                prices.append(live)
            owner = gift.get('username') or gift.get('userId') or gift.get('owner')
            if owner: owners.add(str(owner))
            results.append(gift)
            await send_card(c.message,uid,gift)
        total=sum(prices) if prices else 0
        avg=round(total/len(prices)) if prices else 0
        owner_text=', '.join(f'@{x}' if not x.isdigit() else x for x in list(owners)[:15]) or 'не определены'
        summary=(f'🎲 <b>СЛУЧАЙНЫЙ ПАРСЕР ЗАВЕРШЁН</b>\n\n'
                 f'🔎 Проверено: <b>{count}</b>\n'
                 f'🎁 Найдено подарков: <b>{found}</b>\n'
                 f'👤 Владельцев: <b>{len(owners)}</b>\n'
                 f'👥 Владельцы: <b>{ui.esc(owner_text)}</b>\n'
                 f'⭐ Оценено по Telegram: <b>{len(prices)}/{found}</b>\n'
                 f'💰 Суммарная оценка: <b>~{total:,} Stars</b>\n'
                 f'📊 Средняя цена: <b>~{avg:,} Stars</b>' if prices else
                 f'🎲 <b>СЛУЧАЙНЫЙ ПАРСЕР ЗАВЕРШЁН</b>\n\nПроверено: <b>{count}</b>\nНайдено подарков: <b>{found}</b>\nВладельцев: <b>{len(owners)}</b>\n⚠️ Актуальную цену Telegram не удалось получить для найденных подарков.')
        await edit(status,summary,reply_markup=kb.inline_main_menu(is_admin(uid),is_unlimited(uid)))
    finally:
        ACTIVE.discard(uid); db.clear_stop(uid)

@dp.callback_query(F.data == 'parser_ui:id')
async def v24_id(c: CallbackQuery,state:FSMContext):
    if not trial_allowed(c.from_user.id,'id',1): return await c.answer('Бесплатные проверки по ID закончились. Открой Премиум.',show_alert=True)
    await c.answer(); await state.set_state(S.id_model); await c.message.answer('🆔 <b>ПОИСК ПО ID</b>\n\nНапиши название NFT-подарка:',reply_markup=kb.cancel())

@dp.callback_query(F.data == 'parser_ui:chats')
async def v24_chats(c: CallbackQuery,state:FSMContext):
    uid=c.from_user.id
    if not chat_parser_allowed(uid): return await c.answer('🔒 Доступ к парсеру чатов не активен. Купи пакет 10 TON на 24 часа.',show_alert=True)
    await c.answer()
    from parsing import keyboards as parser_kb
    session=db.get_parser_session()
    await c.message.edit_text(parser_handlers.format_parser_menu(session),reply_markup=parser_kb.parser_main_menu(bool(session and session.get('is_active'))))

@dp.callback_query(F.data.startswith('buy:'))
async def buy_plan(c: CallbackQuery):
    uid = int(c.from_user.id)
    plan = c.data.split(':', 1)[1]
    plans = tariff_config()
    if plan not in plans:
        return await c.answer('Тариф не найден', show_alert=True)
    amount, kind, seconds, title = plans[plan]
    await c.answer()
    payload = f'{uid}:{plan}:{int(time.time())}'
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='💳 CryptoBot', callback_data=f'pay:crypto:{plan}')],
        [InlineKeyboardButton(text='🚀 xRocket', callback_data=f'pay:xrocket:{plan}')],
        [InlineKeyboardButton(text='‹ Премиум', callback_data='menu:premium')],
    ])
    await c.message.edit_text(f'💳 <b>{ui.esc(title)}</b>\n\nСтоимость: <b>{amount} TON</b>\n\nВыбери способ оплаты:', reply_markup=markup)

async def _create_crypto_invoice(c: CallbackQuery, plan: str):
    plans = tariff_config()
    amount, kind, seconds, title = plans[plan]; uid=int(c.from_user.id)
    if not crypto_pay.configured:
        return await c.answer('CryptoBot не настроен.', show_alert=True)
    payload=f'{uid}:{plan}:{int(time.time())}'
    try:
        inv=await crypto_pay.create_invoice(amount, f'GiftParser — {title}', payload)
        db.save_payment_invoice(inv['invoice_id'], uid, plan, amount, payload)
        url=inv.get('bot_invoice_url') or inv.get('mini_app_invoice_url') or inv.get('web_app_invoice_url')
        if not url: raise RuntimeError('CryptoBot не вернул ссылку оплаты')
        await c.message.edit_text(f'💳 <b>CryptoBot</b>\n\nТариф: <b>{ui.esc(title)}</b>\nСумма: <b>{amount} TON</b>\n\nПосле оплаты доступ активируется автоматически.', reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='💳 Оплатить CryptoBot', url=url)],[InlineKeyboardButton(text='‹ Назад', callback_data=f'buy:{plan}')]]))
    except Exception as e:
        log.exception('CryptoBot invoice failed uid=%s plan=%s',uid,plan); await c.answer(f'Не удалось создать счёт: {str(e)[:180]}',show_alert=True)

@dp.callback_query(F.data.startswith('pay:crypto:'))
async def pay_crypto(c: CallbackQuery):
    return await _create_crypto_invoice(c, c.data.split(':',2)[2])

@dp.callback_query(F.data.startswith('pay:xrocket:'))
async def pay_xrocket(c: CallbackQuery):
    plan=c.data.split(':',2)[2]
    plans={'premium_2d':('7','premium',172800,'Премиум · 2 дня'),'chat_24h':('10','chat',86400,'Парсер чатов · 24 часа'),'offers_5d':('25','offers',432000,'Оферы · 5 дней')}
    if plan not in plans: return await c.answer('Тариф не найден',show_alert=True)
    amount, kind, seconds, title=plans[plan]; uid=int(c.from_user.id)
    if not xrocket_pay.configured: return await c.answer('xRocket не настроен. Добавь XROCKET_PAY_API_TOKEN.',show_alert=True)
    client_id=f'gp_{uid}_{plan}_{int(time.time()*1000)}'
    try:
        inv=await xrocket_pay.create_invoice(amount, title, client_id, uid, c.from_user.username)
        db.save_xrocket_invoice(client_id, str(inv.get('id') or client_id), uid, plan, amount, 'TON', 'active')
        url=((inv.get('links') or {}).get('telegramBotLink') or (inv.get('links') or {}).get('webLink'))
        if not url: raise RuntimeError('xRocket не вернул ссылку оплаты')
        await c.answer()
        await c.message.edit_text(f'🚀 <b>xRocket</b>\n\nТариф: <b>{ui.esc(title)}</b>\nСумма: <b>{amount} TON</b>\n\nПосле оплаты доступ активируется автоматически.', reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🚀 Оплатить через xRocket', url=url)],[InlineKeyboardButton(text='‹ Назад', callback_data=f'buy:{plan}')]]))
    except Exception as e:
        log.exception('xRocket invoice failed uid=%s plan=%s',uid,plan); await c.answer(f'Не удалось создать xRocket-счёт: {str(e)[:180]}',show_alert=True)

@dp.callback_query(F.data == 'profile:payments')
async def v24_profile_payments(c: CallbackQuery):
    rows=db.payment_history(c.from_user.id,20)
    await c.answer()
    if not rows:
        return await c.message.edit_text('💳 <b>ИСТОРИЯ ПОКУПОК</b>\n\nПокупок пока нет.', reply_markup=kb.profile_menu_kb())
    labels={'premium_2d':'Premium · 2 дня','chat_24h':'Парсер чатов · 24 часа','offers_5d':'Оферы · 5 дней'}
    lines=[]
    for r in rows:
        status='✅ оплачено' if r['status']=='paid' else ('⌛ ожидает' if r['status']=='active' else f'• {r["status"]}')
        lines.append(f'• <b>{ui.esc(labels.get(r["plan"],r["plan"]))}</b> · {ui.esc(str(r["amount"]))} TON · {status}')
    await c.message.edit_text('💳 <b>ИСТОРИЯ ПОКУПОК</b>\n\n'+'\n'.join(lines), reply_markup=kb.profile_menu_kb())


@dp.callback_query(F.data == 'profile:favorites')
async def v24_profile_favorites(c: CallbackQuery):
    await c.answer(); await favorites(c.message)

# ───────────────────────── каталог моделей ─────────────────────────
async def show_models(target: Message, page: int, edit_mode=False):
    markup, pages = kb.models_kb(page)
    text = ui.models_text(page, pages)
    if edit_mode:
        await edit(target, text, markup)
    else:
        await target.answer(text, reply_markup=markup)


@dp.message(F.text == kb.BTN_MODELS)
async def models(m: Message):
    await show_models(m, 1)


@dp.callback_query(F.data.startswith('models:'))
async def models_page(c: CallbackQuery):
    await c.answer()
    await show_models(c.message, int(c.data.split(':')[1]), edit_mode=True)


def personal_offer_profile_text(uid):
    profile = db.offer_user_profile_get(uid)
    if profile:
        return (
            '🎁 <b>Доступ к оферам</b>\n\n'
            '🟢 Доступ разрешён.\n\n'
            f'👤 Профиль передачи: <b>{ui.esc(profile["name"])}</b>\n'
            f'🔗 <code>{ui.esc(profile["url"])}</code>\n\n'
            'Этот профиль используется только для твоих оферов.'
        )
    return ('🎁 <b>Доступ к оферам</b>\n\n'
            '🟢 Доступ разрешён.\n\n'
            '⚠️ Профиль передачи ещё не настроен.\n'
            'Добавь свой профиль — он будет использоваться только в твоих оферах.')


def personal_offer_profile_kb(uid):
    profile = db.offer_user_profile_get(uid)
    buttons=[]
    buttons.append([InlineKeyboardButton(text='✏️ Настроить профиль передачи', callback_data='offer:self:profile')])
    if profile:
        buttons.append([InlineKeyboardButton(text='🗑 Удалить мой профиль', callback_data='offer:self:profile:remove')])
    buttons.append([InlineKeyboardButton(text='⬅️ Назад', callback_data='offer:self:back')])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(F.text == kb.BTN_OFFERS)
async def offers_tab(m: Message):
    uid = int(m.from_user.id)
    if not offers_paid(uid):
        return await m.answer(ui.v24_offers(False), reply_markup=kb.offers_locked_kb())
    if not offer_ready_for_user(uid, getattr(m.from_user, 'username', None)):
        return await m.answer('🎁 <b>ОФЕРЫ</b>\n\nДоступ куплен, но Business-профиль ещё не подключён или не одобрен владельцем.\n\n@witensea', reply_markup=kb.offers_active_kb())
    await m.answer(ui.v24_offers(True, db.entitlement_until(uid, 'offers')), reply_markup=kb.offers_active_kb())


@dp.callback_query(F.data == 'offer:self:profile')
async def offer_self_profile_start(c: CallbackQuery, state: FSMContext):
    uid = int(c.from_user.id)
    if not offer_ready_for_user(uid, getattr(c.from_user, 'username', None)):
        return await c.answer('Сначала подключите Business и получите разрешение на оферы.', show_alert=True)
    await c.answer()
    await state.set_state(S.offer_user_profile_name)
    await c.message.answer('✏️ <b>Профиль передачи</b>\n\nОтправь название профиля, например: <code>Мой профиль</code>')


@dp.message(S.offer_user_profile_name)
async def offer_self_profile_name(m: Message, state: FSMContext):
    uid = int(m.from_user.id)
    if not offer_ready_for_user(uid, getattr(m.from_user, 'username', None)):
        await state.clear(); return
    name = (m.text or '').strip()
    if not name or len(name) > 64:
        return await m.answer('❌ Название должно быть от 1 до 64 символов.')
    await state.update_data(personal_profile_name=name)
    await state.set_state(S.offer_user_profile_url)
    await m.answer(
        '🔗 <b>Куда передавать подарок?</b>\n\n'
        'Отправь username <b>без @</b>. Например: <code>witensea</code>\n'
        'Бот автоматически сделает ссылку: <code>tg://send_gift?to=witensea</code>\n\n'
        'Также можно отправить готовую ссылку <code>tg://send_gift?to=username</code>.'
    )


@dp.message(S.offer_user_profile_url)
async def offer_self_profile_url(m: Message, state: FSMContext):
    uid = int(m.from_user.id)
    if not offer_ready_for_user(uid, getattr(m.from_user, 'username', None)):
        await state.clear(); return
    raw = (m.text or '').strip()
    # New transfer format: plain Telegram username -> tg://send_gift?to=username.
    # Keep compatibility with already saved/full links as well.
    username = raw.lstrip('@').strip()
    if re.fullmatch(r'[A-Za-z0-9_]{5,32}', username):
        url = f'tg://send_gift?to={username}'
    elif re.fullmatch(r'tg://send_gift\?to=[A-Za-z0-9_]{5,32}', raw, re.I):
        url = raw
    elif re.fullmatch(r'tg://user\?id=-?\d+', raw, re.I):
        url = raw
    elif re.fullmatch(r'https?://t\.me/[A-Za-z0-9_]{5,32}/?', raw, re.I):
        username = raw.rstrip('/').rsplit('/', 1)[-1]
        url = f'tg://send_gift?to={username}'
    else:
        return await m.answer('❌ Отправь username без @, например <code>witensea</code>.')
    data = await state.get_data(); name = data.get('personal_profile_name') or 'Мой профиль'
    db.offer_user_profile_set(uid, name, url)
    await state.clear()
    await m.answer('✅ <b>Твой профиль передачи сохранён.</b>\n\nОн используется только для твоих новых оферов.', reply_markup=personal_offer_profile_kb(uid))


@dp.callback_query(F.data == 'offer:self:profile:remove')
async def offer_self_profile_remove(c: CallbackQuery):
    uid = int(c.from_user.id)
    if not offer_ready_for_user(uid, getattr(c.from_user, 'username', None)):
        return await c.answer('Доступ к оферам закрыт.', show_alert=True)
    db.offer_user_profile_remove(uid)
    await c.answer('Профиль удалён')
    await c.message.edit_text(personal_offer_profile_text(uid), reply_markup=personal_offer_profile_kb(uid))


@dp.callback_query(F.data == 'offer:self:back')
async def offer_self_back(c: CallbackQuery):
    await c.answer()
    try:
        await c.message.delete()
    except Exception:
        pass


# ───────────────────────── Рассылка ─────────────────────────
@dp.callback_query(F.data == 'adm:broadcast')
async def adm_broadcast_start(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer('Нет доступа', show_alert=True)
    await c.answer()
    await state.set_state(S.admin_broadcast)
    await c.message.edit_text(
        '📣 <b>РАССЫЛКА</b>\n\n'
        'Отправь следующим сообщением то, что нужно разослать: <b>текст, фото, GIF, видео, документ, аудио, голосовое или стикер</b>.\n\n'
        'Поддерживается одно сообщение за рассылку. После получения я покажу подтверждение.',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='‹ Назад', callback_data='adm:home')]])
    )

@dp.callback_query(F.data == 'adm:broadcast_cancel')
async def adm_broadcast_cancel(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer('Нет доступа', show_alert=True)
    await state.clear()
    await c.answer('Отменено')
    await c.message.edit_text('⚙️ <b>АДМИН-ПАНЕЛЬ</b>', reply_markup=admin_home_kb())

@dp.message(S.admin_broadcast)
async def adm_broadcast_preview(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.clear(); return
    await state.update_data(broadcast_chat_id=m.chat.id, broadcast_message_id=m.message_id, broadcast_audience='all')
    count = len(db.broadcast_user_ids('all'))
    await m.answer(
        f'📣 <b>ПРЕДПРОСМОТР РАССЫЛКИ</b>\n\nАудитория: <b>Все</b> · Получателей: <b>{count}</b>\n\nВыбери аудиторию или запускай.',
        reply_markup=broadcast_audience_kb('all')
    )

def broadcast_audience_kb(selected='all'):
    labels=[('all','🌐 Все'),('premium','💎 Premium'),('chat','📡 Парсер чатов'),('offers','🎁 Оферы'),('active','🟢 Активные 7 дней')]
    rows=[]
    for key,label in labels:
        count=len(db.broadcast_user_ids(key))
        rows.append([InlineKeyboardButton(text=f'{label} · {count}' + (' ✅' if key==selected else ''), callback_data=f'adm:broadcast_audience:{key}')])
    rows.append([InlineKeyboardButton(text='🚀 Начать рассылку', callback_data='adm:broadcast_send')])
    rows.append([InlineKeyboardButton(text='✖️ Отмена', callback_data='adm:broadcast_cancel')])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.callback_query(F.data.startswith('adm:broadcast_audience:'))
async def adm_broadcast_audience(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return await c.answer('Нет доступа', show_alert=True)
    audience=c.data.split(':')[-1]
    data=await state.get_data()
    if not data.get('broadcast_message_id'): return await c.answer('Сначала отправь сообщение для рассылки.', show_alert=True)
    await state.update_data(broadcast_audience=audience)
    await c.answer('Аудитория изменена')
    await c.message.edit_text(f'📣 <b>РАССЫЛКА</b>\n\nАудитория: <b>{ui.esc(audience)}</b>\nПолучателей: <b>{len(db.broadcast_user_ids(audience))}</b>', reply_markup=broadcast_audience_kb(audience))

@dp.callback_query(F.data == 'adm:broadcast_send')
async def adm_broadcast_send(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer('Нет доступа', show_alert=True)
    data = await state.get_data()
    source_chat = data.get('broadcast_chat_id'); source_message = data.get('broadcast_message_id')
    if not source_chat or not source_message:
        await state.clear(); return await c.answer('Сообщение рассылки не найдено.', show_alert=True)
    await c.answer('Рассылка запущена')
    await c.message.edit_text('📣 <b>РАССЫЛКА ЗАПУЩЕНА</b>\n\nБот отправляет сообщения безопасной очередью.')
    await state.clear()
    asyncio.create_task(run_broadcast(int(source_chat), int(source_message), int(c.from_user.id), data.get('broadcast_audience') or 'all'))

async def run_broadcast(source_chat: int, source_message: int, admin_id: int, audience='all'):
    rows = db.broadcast_user_ids(audience)
    sent = failed = 0
    for uid in rows:
        try:
            await bot.copy_message(chat_id=int(uid), from_chat_id=source_chat, message_id=source_message)
            sent += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(min(int(getattr(e, 'retry_after', 1) or 1), 30) + 0.2)
            try:
                await bot.copy_message(chat_id=int(uid), from_chat_id=source_chat, message_id=source_message)
                sent += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(float(os.getenv('BROADCAST_DELAY', '0.08')))
    try:
        await bot.send_message(admin_id, f'✅ <b>РАССЫЛКА ЗАВЕРШЕНА</b>\n\nОтправлено: <b>{sent}</b>\nОшибок: <b>{failed}</b>')
    except Exception:
        pass
    log.info('Broadcast finished admin=%s audience=%s sent=%s failed=%s', admin_id, audience, sent, failed)

# ───────────────────────── новая админка V25 ─────────────────────────
def admin_home_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='📊 Статистика', callback_data='adm:stats')],
        [InlineKeyboardButton(text='👥 Управление пользователями', callback_data='adm:users')],
        [InlineKeyboardButton(text='🔎 Доступ к парсеру', callback_data='adm:parser_access')],
        [InlineKeyboardButton(text='🎁 Доступ к оферам', callback_data='adm:offer_access')],
        [InlineKeyboardButton(text='💳 Тарифы', callback_data='adm:tariffs')],
        [InlineKeyboardButton(text='📣 Рассылка', callback_data='adm:broadcast')],
        [InlineKeyboardButton(text='📜 Логи', callback_data='adm:logs')],
        [InlineKeyboardButton(text='🛠 Система и канал логов', callback_data='adm:system')],
        [InlineKeyboardButton(text='🚧 Технические работы', callback_data='adm:maintenance')],
        [InlineKeyboardButton(text='🔄 Обновить', callback_data='adm:home')],
    ])


def admin_back(data='adm:home'):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='‹ Назад', callback_data=data)]])


def admin_users_kb(rows):
    buttons=[]
    for u in rows:
        uid=int(u['user_id'])
        name=(' @'+u['username']) if u.get('username') else (u.get('first_name') or str(uid))
        tag='🚫' if u.get('is_banned') else ('💎' if u.get('is_premium') else '👤')
        buttons.append([InlineKeyboardButton(text=f'{tag} {str(name).strip()[:28]}', callback_data=f'adm:user:{uid}')])
    buttons += [
        [InlineKeyboardButton(text='🔎 Найти пользователя', callback_data='adm:userfind')],
        [InlineKeyboardButton(text='‹ Назад', callback_data='adm:home')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_user_text(uid):
    u=db.get_user(uid) or {}
    parser_direct=bool(db._one('SELECT 1 AS ok FROM parser_access_users WHERE user_id=?',(uid,)))
    offer_until=db.entitlement_until(uid,'offers')
    chat_until=db.entitlement_until(uid,'chat')
    premium_until=db.entitlement_until(uid,'premium')
    role='👑 Владелец' if is_owner(uid) else ('🛡 Администратор' if is_admin(uid) else '👤 Пользователь')
    lines=[
        f'👤 <b>ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ</b>', '',
        f'🆔 <code>{uid}</code>',
        f'👤 {ui.esc("@"+u["username"] if u.get("username") else u.get("first_name") or "Без имени")}',
        f'🏷 {role}',
        f'🚫 Бан: <b>{"да" if u.get("is_banned") else "нет"}</b>',
        '',
        f'💎 Премиум: <b>{"активен" if db.entitlement_active(uid,"premium") else "нет"}</b>' + (f' до <code>{ui.esc(str(premium_until)[:19].replace("T"," "))}</code>' if premium_until else ''),
        f'📡 Парсер чатов: <b>{"доступен" if db.entitlement_active(uid,"chat") else ("выдан вручную" if parser_direct else "нет")}</b>' + (f' до <code>{ui.esc(str(chat_until)[:19].replace("T"," "))}</code>' if chat_until else ''),
        f'🎁 Оферы: <b>{"активны" if db.entitlement_active(uid,"offers") else "нет"}</b>' + (f' до <code>{ui.esc(str(offer_until)[:19].replace("T"," "))}</code>' if offer_until else ''),
        '',
        f'🔎 Поисков: <b>{u.get("total_searches",0)}</b> · 🎁 Находок: <b>{u.get("total_found",0)}</b>',
    ]
    return '\n'.join(lines)


def admin_user_kb(uid):
    admin_target=is_admin(uid) and not is_owner(uid)
    buttons=[
        [InlineKeyboardButton(text='💎 Выдать 2 дня Premium', callback_data=f'adm:grant:premium:{uid}:172800')],
        [InlineKeyboardButton(text='📡 Выдать 24ч парсера чатов', callback_data=f'adm:grant:chat:{uid}:86400')],
        [InlineKeyboardButton(text='🎁 Выдать 5 дней оферов', callback_data=f'adm:grant:offers:{uid}:432000')],
        [InlineKeyboardButton(text='♻️ Сбросить Business-сессии', callback_data=f'adm:business_reset:{uid}')],
        [InlineKeyboardButton(text='🔎 Выдать доступ к парсеру', callback_data=f'adm:parser_user:add:{uid}')],
        [InlineKeyboardButton(text='🚫 Снять ручной доступ к парсеру', callback_data=f'adm:parser_user:remove:{uid}')],
        [InlineKeyboardButton(text=('🛡 Снять администратора' if admin_target else '🛡 Сделать администратором'), callback_data=f'adm:admin_toggle:{uid}')],
        [InlineKeyboardButton(text=('✅ Разбанить' if db.is_banned(uid) else '🚫 Заблокировать'), callback_data=f'adm:ban:{uid}')],
        [InlineKeyboardButton(text='🔄 Сбросить бесплатные лимиты', callback_data=f'adm:trial_reset:{uid}')],
        [InlineKeyboardButton(text='‹ Пользователи', callback_data='adm:users')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def parser_access_text():
    mode=db.get_parser_setting('access_mode','owner') or 'owner'
    labels={'owner':'👑 Только владелец + ручные разрешения','premium':'💎 Владелец + Premium + ручные разрешения','developers':'🛡 Владелец + разработчики + ручные разрешения','all':'🌐 Все пользователи'}
    return '🔎 <b>ДОСТУП К ПАРСЕРУ ЧАТОВ</b>\n\nТекущий режим: <b>'+labels.get(mode,labels['owner'])+'</b>\n\nВыбери правило доступа ниже.'


def parser_access_kb():
    mode=db.get_parser_setting('access_mode','owner') or 'owner'
    names=[('owner','👑 Для владельца'),('premium','💎 Для Premium'),('developers','🛡 Для разработчиков'),('all','🌐 Для всех')]
    rows=[]
    for key,label in names:
        rows.append([InlineKeyboardButton(text=label + (' ✅' if key==mode else ''), callback_data=f'adm:parser_mode:{key}')])
    rows.append([InlineKeyboardButton(text='‹ Назад', callback_data='adm:home')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def offers_admin_text():
    active=db._one("SELECT COUNT(*) n FROM users WHERE offer_until IS NOT NULL AND offer_until > CURRENT_TIMESTAMP")
    connections=len(db.business_connections_list())
    return ('🎁 <b>ДОСТУП К ОФЕРАМ</b>\n\n'
            f'🟢 Активных доступов: <b>{int((active or {}).get("n",0))}</b>\n'
            f'🔗 Business-подключений: <b>{connections}</b>\n\n'
            'Покупка: <b>25 TON · 5 дней</b>.\n'
            'Владелец и администраторы имеют служебный доступ.')


def offers_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='🔗 Business-профили', callback_data='adm:offer_business_connections')],
        [InlineKeyboardButton(text='👤 Пользователи', callback_data='adm:users')],
        [InlineKeyboardButton(text='🧹 Сбросить всех пользователей оферов', callback_data='adm:offer_reset_all')],
        [InlineKeyboardButton(text='‹ Назад', callback_data='adm:home')],
    ])

def offer_reset_all_confirm_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='⚠️ Да, сбросить всех', callback_data='adm:offer_reset_all:confirm')],
        [InlineKeyboardButton(text='‹ Отмена', callback_data='adm:offer_access')],
    ])


def tariffs_text():
    plans = tariff_config()
    lines=['💳 <b>ТАРИФЫ</b>','']
    labels={'premium_2d':'⚡ Premium · 2 дня','chat_24h':'📡 Парсер чатов · 24 часа','offers_5d':'🎁 Оферы · 5 дней'}
    for key in ('premium_2d','chat_24h','offers_5d'):
        lines.append(f'{labels[key]}: <b>{plans[key][0]} TON</b>')
    lines += ['', 'Выбери тариф, чтобы изменить цену. Изменение сохраняется в SQLite.']
    return '\n'.join(lines)

def tariffs_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f'⚡ Premium: {tariff_config()["premium_2d"][0]} TON', callback_data='adm:tariff:premium_2d')],
        [InlineKeyboardButton(text=f'📡 Чаты: {tariff_config()["chat_24h"][0]} TON', callback_data='adm:tariff:chat_24h')],
        [InlineKeyboardButton(text=f'🎁 Оферы: {tariff_config()["offers_5d"][0]} TON', callback_data='adm:tariff:offers_5d')],
        [InlineKeyboardButton(text='‹ Назад', callback_data='adm:home')],
    ])


@dp.message(F.text == kb.BTN_ADMIN)
async def admin(m: Message):
    if not is_admin(m.from_user.id): return
    await m.answer('⚙️ <b>АДМИН-ПАНЕЛЬ</b>\n\nВыбери раздел.', reply_markup=admin_home_kb())


@dp.callback_query(F.data == 'adm:home')
async def adm_home(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer('Нет доступа', show_alert=True)
    await c.answer(); await c.message.edit_text('⚙️ <b>АДМИН-ПАНЕЛЬ</b>\n\nВыбери раздел.', reply_markup=admin_home_kb())


@dp.callback_query(F.data == 'adm:stats')
async def adm_stats(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer('Нет доступа', show_alert=True)
    s=db.stats(); active_pay=len(db.active_payment_invoices(1000))
    text=('📊 <b>СТАТИСТИКА</b>\n\n'
          f'👥 Пользователей: <b>{s["users"]}</b>\n'
          f'💎 Premium: <b>{s["premium"]}</b>\n'
          f'🚫 Банов: <b>{s["banned"]}</b>\n'
          f'🔎 Поисков: <b>{s["searches"]}</b>\n'
          f'🎁 Находок: <b>{s["found"]}</b>\n'
          f'⚡ Активных поисков: <b>{len(ACTIVE)}</b>\n'
          f'💳 Ожидающих платежей: <b>{active_pay}</b>\n'
          f'🎁 Business-подключений: <b>{len(db.business_connections_list())}</b>')
    await c.answer(); await c.message.edit_text(text, reply_markup=admin_back())


@dp.callback_query(F.data == 'adm:users')
async def adm_users(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer('Нет доступа', show_alert=True)
    await c.answer(); await c.message.edit_text('👥 <b>УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ</b>\n\nВыбери пользователя или найди его по ID/@username.', reply_markup=admin_users_kb(db.recent_users(15)))


@dp.callback_query(F.data == 'adm:userfind')
async def adm_userfind(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return await c.answer('Нет доступа', show_alert=True)
    await c.answer(); await state.set_state(S.admin_user_search)
    await c.message.edit_text('🔎 <b>НАЙТИ ПОЛЬЗОВАТЕЛЯ</b>\n\nОтправь Telegram ID или @username.', reply_markup=admin_back('adm:users'))


@dp.message(S.admin_user_search)
async def adm_userfind_value(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): await state.clear(); return
    q=(m.text or '').strip()
    rows=db.search_users(q,10)
    await state.clear()
    if not rows: return await m.answer('❌ Пользователь не найден.', reply_markup=admin_users_kb(db.recent_users(10)))
    await m.answer('🔎 <b>РЕЗУЛЬТАТ ПОИСКА</b>', reply_markup=admin_users_kb(rows))


@dp.callback_query(F.data.startswith('adm:user:'))
async def adm_user(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer('Нет доступа', show_alert=True)
    uid=int(c.data.split(':')[2]); u=db.get_user(uid)
    if not u: return await c.answer('Пользователь не найден', show_alert=True)
    await c.answer(); await c.message.edit_text(admin_user_text(uid), reply_markup=admin_user_kb(uid))


@dp.callback_query(F.data == 'adm:parser_access')
async def adm_parser_access(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer('Нет доступа', show_alert=True)
    await c.answer(); await c.message.edit_text(parser_access_text(), reply_markup=parser_access_kb())


@dp.callback_query(F.data.regexp(r'^adm:parser_mode:(owner|premium|developers|all)$'))
async def adm_parser_mode(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer('Нет доступа', show_alert=True)
    mode=c.data.rsplit(':',1)[1]
    db.set_parser_setting('access_mode',mode)
    await audit('Изменён режим доступа к парсеру',c.from_user.id,f'Режим: <code>{mode}</code>')
    await c.answer('Режим сохранён'); await c.message.edit_text(parser_access_text(),reply_markup=parser_access_kb())


@dp.callback_query(F.data.regexp(r'^adm:parser_user:(add|remove):-?\d+$'))
async def adm_parser_user_access(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer('Нет доступа', show_alert=True)
    action,uid_s=c.data.split(':')[2:4]; uid=int(uid_s)
    if is_owner(uid) or (is_admin(uid) and uid != c.from_user.id):
        if action=='remove' and is_owner(uid): return await c.answer('Владелец всегда имеет доступ.',show_alert=True)
    if action=='add': db.parser_access_add(uid); msg='🔎 Доступ к парсеру выдан.'
    else: db.parser_access_remove(uid); msg='🔒 Ручной доступ к парсеру снят.'
    await c.answer(msg,show_alert=True); await c.message.edit_text(admin_user_text(uid),reply_markup=admin_user_kb(uid))


@dp.callback_query(F.data.startswith('adm:admin_toggle:'))
async def adm_admin_toggle(c: CallbackQuery):
    if not is_owner(c.from_user.id): return await c.answer('Только владелец может управлять администраторами.',show_alert=True)
    uid=int(c.data.split(':')[2])
    if is_owner(uid): return await c.answer('Владелец уже имеет максимальные права.',show_alert=True)
    if db.admin_allowed(uid,OWNER_ID): db.admin_remove(uid); msg='🛡 Администратор снят.'
    else: db.admin_add(uid); msg='🛡 Администратор добавлен.'
    await c.answer(msg,show_alert=True); await c.message.edit_text(admin_user_text(uid),reply_markup=admin_user_kb(uid))


@dp.callback_query(F.data.regexp(r'^adm:grant:(premium|chat|offers):-?\d+:\d+$'))
async def adm_grant(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer('Нет доступа',show_alert=True)
    _,_,kind,uid_s,seconds_s=c.data.split(':'); uid=int(uid_s); seconds=int(seconds_s)
    db.grant_entitlement(uid,kind,seconds)
    if kind=='premium': db.set_premium(uid,True)
    if kind=='offers':
        # One admin action must be sufficient for DEMO offers. Keep the
        # entitlement and Business approval in sync for this exact Telegram ID.
        db.offer_access_add(uid)
        db.unblock_offer_user(uid)
        log.info('OFFER ACCESS GRANTED via admin user=%s seconds=%s', uid, seconds)
    await c.answer('Доступ выдан' + (' — оферы разрешены для Business-профиля.' if kind=='offers' else ''),show_alert=True); await c.message.edit_text(admin_user_text(uid),reply_markup=admin_user_kb(uid))


@dp.callback_query(F.data.startswith('adm:trial_reset:'))
async def adm_trial_reset(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer('Нет доступа',show_alert=True)
    uid=int(c.data.split(':')[2])
    with db._db() as conn:
        conn.execute('UPDATE users SET trial_regular_left=20, trial_random_left=20, trial_id_left=20 WHERE user_id=?',(uid,))
    await c.answer('Лимиты сброшены'); await c.message.edit_text(admin_user_text(uid),reply_markup=admin_user_kb(uid))


@dp.callback_query(F.data.startswith('adm:ban:'))
async def adm_ban(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer('Нет доступа',show_alert=True)
    uid=int(c.data.split(':')[2])
    if is_owner(uid): return await c.answer('Владельца заблокировать нельзя.',show_alert=True)
    new=not db.is_banned(uid); db.set_ban(uid,new)
    await c.answer('Пользователь заблокирован' if new else 'Пользователь разблокирован',show_alert=True)
    await c.message.edit_text(admin_user_text(uid),reply_markup=admin_user_kb(uid))


@dp.callback_query(F.data == 'adm:offer_access')
async def adm_offer_access(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer('Нет доступа',show_alert=True)
    await c.answer(); await c.message.edit_text(offers_admin_text(),reply_markup=offers_admin_kb())


@dp.callback_query(F.data == 'adm:tariffs')
async def adm_tariffs(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer('Нет доступа',show_alert=True)
    await c.answer(); await c.message.edit_text(tariffs_text(),reply_markup=tariffs_kb())


@dp.callback_query(F.data.regexp(r'^adm:tariff:(premium_2d|chat_24h|offers_5d)$'))
async def adm_tariff_start(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return await c.answer('Нет доступа', show_alert=True)
    key=c.data.split(':')[-1]
    await state.set_state(S.admin_tariff); await state.update_data(tariff_key=key)
    current=tariff_config()[key][0]
    await c.answer(); await c.message.edit_text(f'💳 <b>Изменение тарифа</b>\n\nТекущая цена: <b>{current} TON</b>\n\nОтправь новую цену числом, например <code>12.5</code>.', reply_markup=admin_back('adm:tariffs'))


@dp.message(S.admin_tariff)
async def adm_tariff_value(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): await state.clear(); return
    try:
        value=float((m.text or '').replace(',','.').strip())
        if value <= 0 or value > 100000: raise ValueError
    except Exception:
        return await m.answer('❌ Введи положительную цену до 100000 TON.')
    data=await state.get_data(); key=data.get('tariff_key')
    if key not in tariff_config(): await state.clear(); return await m.answer('❌ Тариф не найден.')
    db.set_setting('tariff_'+key, str(value).rstrip('0').rstrip('.') if '.' in str(value) else str(int(value)))
    await state.clear()
    await m.answer('✅ Цена сохранена.', reply_markup=tariffs_kb())


@dp.callback_query(F.data == 'adm:logs')
async def adm_logs(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer('Нет доступа',show_alert=True)
    try:
        text=''.join(Path('logs/giftparser.log').read_text(encoding='utf-8').splitlines(True)[-35:])[-3800:]
    except Exception as e: text=f'Лог недоступен: {e}'
    await c.answer(); await c.message.edit_text('<b>📜 ЛОГИ</b>\n\n<pre>'+ui.esc(text)+'</pre>',reply_markup=admin_back())


def maintenance_kb():
    enabled=db.get_setting('maintenance_mode','0') == '1'
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=('🔴 Выключить' if enabled else '🟢 Включить'), callback_data='adm:maintenance:toggle')],
        [InlineKeyboardButton(text='‹ Назад', callback_data='adm:home')],
    ])

@dp.callback_query(F.data == 'adm:maintenance')
async def adm_maintenance(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer('Нет доступа', show_alert=True)
    enabled=db.get_setting('maintenance_mode','0') == '1'
    await c.answer(); await c.message.edit_text('🚧 <b>ТЕХНИЧЕСКИЕ РАБОТЫ</b>\n\nСтатус: <b>'+('включены' if enabled else 'выключены')+'</b>\n\nПри включении обычные пользователи не смогут пользоваться ботом, админы продолжат работу.', reply_markup=maintenance_kb())

@dp.callback_query(F.data == 'adm:maintenance:toggle')
async def adm_maintenance_toggle(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer('Нет доступа', show_alert=True)
    new='0' if db.get_setting('maintenance_mode','0') == '1' else '1'
    db.set_setting('maintenance_mode', new)
    await c.answer('Статус изменён', show_alert=True)
    enabled=new=='1'
    await c.message.edit_text('🚧 <b>ТЕХНИЧЕСКИЕ РАБОТЫ</b>\n\nСтатус: <b>'+('включены' if enabled else 'выключены')+'</b>', reply_markup=maintenance_kb())


def system_text():
    log_channel=db.get_setting('log_channel_id') or 'не задан'
    session=db.get_parser_session()
    return ('🛠 <b>СИСТЕМА</b>\n\n'
            f'🤖 Bot: <b>online</b>\n'
            f'💾 SQLite: <b>WAL</b>\n'
            f'🔎 Peek API: <b>{"настроен"}</b>\n'
            f'📡 Telethon: <b>{"подключён" if session and session.get("is_active") else "не подключён"}</b>\n'
            f'💳 CryptoBot: <b>{"готов" if crypto_pay.configured else "не настроен"}</b>\n'
            f'🚀 xRocket: <b>{"готов" if xrocket_pay.configured else "не настроен"}</b>\n'
            f'📣 Рассылка: <b>очередь</b>\n'
            f'📜 Канал логов: <code>{ui.esc(log_channel)}</code>')

def system_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='📜 Изменить канал логов', callback_data='adm:log_channel')],
        [InlineKeyboardButton(text='🔄 Обновить', callback_data='adm:system')],
        [InlineKeyboardButton(text='‹ Назад', callback_data='adm:home')],
    ])

@dp.callback_query(F.data == 'adm:system')
async def adm_system(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer('Нет доступа', show_alert=True)
    await c.answer(); await c.message.edit_text(system_text(), reply_markup=system_kb())

@dp.callback_query(F.data == 'adm:log_channel')
async def adm_log_channel_start(c: CallbackQuery, state: FSMContext):
    if not is_owner(c.from_user.id): return await c.answer('Только владелец.', show_alert=True)
    await state.set_state(S.admin_log_channel); await c.answer()
    await c.message.edit_text('📜 <b>КАНАЛ ЛОГОВ</b>\n\nПришли ID канала вида <code>-1001234567890</code>.\nБот должен быть администратором этого канала.', reply_markup=admin_back('adm:system'))

@dp.message(S.admin_log_channel)
async def adm_log_channel_value(m: Message, state: FSMContext):
    if not is_owner(m.from_user.id): await state.clear(); return
    raw=(m.text or '').strip()
    try: cid=int(raw)
    except ValueError: return await m.answer('❌ Нужен числовой ID канала, например <code>-1001234567890</code>.')
    db.set_setting('log_channel_id', str(cid))
    global LOG_CHANNEL_DISABLED
    LOG_CHANNEL_DISABLED=False
    await state.clear(); await audit('Канал логов изменён', m.from_user.id, f'Новый канал: <code>{cid}</code>')
    await m.answer(f'✅ Канал логов сохранён: <code>{cid}</code>', reply_markup=system_kb())


# Admin: Business-профили оферов.
def admin_business_connections_kb():
    rows = db.business_connections_list()
    buttons = []
    seen = set()
    for row in rows[:100]:
        uid = int(row['user_id'])
        if uid in seen:
            continue
        seen.add(uid)
        name = (' @' + row['username']) if row.get('username') else (row.get('first_name') or str(uid))
        has_access = db.offer_access_allowed(uid, row.get('username'), OWNER_ID)
        if has_access:
            buttons.append([InlineKeyboardButton(text=f'🔴 Забрать доступ · {str(name).strip()[:20]}', callback_data=f'adm:offer_conn:revoke:{uid}')])
        else:
            buttons.append([InlineKeyboardButton(text=f'🟢 Выдать доступ · {str(name).strip()[:20]}', callback_data=f'adm:offer_conn:grant:{uid}')])
    buttons.append([InlineKeyboardButton(text='‹ Назад', callback_data='adm:offer_access')])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@dp.callback_query(F.data.regexp(r'^adm:business_reset:-?\d+$'))
async def adm_business_reset(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer('Нет доступа', show_alert=True)
    uid = int(c.data.split(':')[2])
    u = db.get_user(uid)
    if not u:
        return await c.answer('Пользователь не найден', show_alert=True)

    removed = db.reset_business_sessions(uid)
    BUSINESS_OWNER_ID_CACHE_KEYS = [k for k, v in BUSINESS_OWNER_ID_CACHE.items() if int(v or 0) == uid]
    for key in BUSINESS_OWNER_ID_CACHE_KEYS:
        BUSINESS_OWNER_ID_CACHE.pop(key, None)
        BUSINESS_SELLER_NAME_CACHE.pop(key, None)

    # Keep the user's offer permission/paid entitlement. The reset is only a
    # local session cleanup; the next Business update will create one fresh row.
    db.offer_access_add(uid)
    await audit('Сброшены Business-сессии офера', c.from_user.id,
                f'Пользователь: <code>{uid}</code>; удалено локальных подключений: <b>{removed}</b>')
    await c.answer(f'Сброшено подключений: {removed}', show_alert=True)
    await c.message.edit_text(admin_user_text(uid), reply_markup=admin_user_kb(uid))


@dp.callback_query(F.data == 'adm:offer_reset_all')
async def adm_offer_reset_all_confirm(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer('Нет доступа', show_alert=True)
    await c.answer()
    await c.message.edit_text(
        '🧹 <b>СБРОС ВСЕХ ПОЛЬЗОВАТЕЛЕЙ ОФЕРОВ</b>\n\n'
        'Будут реально удалены из локальной базы: ручные доступы, Business-подключения, блокировки и профили передачи.\n\n'
        'История уже созданных оферов и остальные функции бота не удаляются.\n\n'
        '<b>Продолжить?</b>',
        reply_markup=offer_reset_all_confirm_kb(),
    )


@dp.callback_query(F.data == 'adm:offer_reset_all:confirm')
async def adm_offer_reset_all_execute(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer('Нет доступа', show_alert=True)
    # Keep the canonical owner alive; all other offer users/connections are
    # genuinely removed from the local offer-access state.
    removed = db.reset_all_offer_users(keep_user_ids={OWNER_ID})
    BUSINESS_OWNER_ID_CACHE.clear()
    BUSINESS_SELLER_NAME_CACHE.clear()
    # Re-apply the explicit bootstrap grant requested for 8570516857 only after
    # the reset operation, so that the button still means a full user reset
    # while the configured bootstrap account remains intentionally permitted.
    for _offer_uid in BOOTSTRAP_OFFER_ACCESS_IDS:
        if _offer_uid != OWNER_ID:
            db.offer_access_add(_offer_uid)
            db.unblock_offer_user(_offer_uid)
    await audit(
        'Полностью сброшены пользователи оферов', c.from_user.id,
        'Удалено: ' + ', '.join(f'{k}=<b>{v}</b>' for k, v in removed.items())
    )
    await c.answer('Все локальные пользователи оферов сброшены.', show_alert=True)
    await c.message.edit_text(offers_admin_text(), reply_markup=offers_admin_kb())


@dp.callback_query(F.data == 'adm:offer_business_connections')
async def adm_offer_business_connections(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer('Нет доступа',show_alert=True)
    await c.answer(); await c.message.edit_text('🔗 <b>BUSINESS-ПРОФИЛИ</b>\n\nНовые подключения по умолчанию закрыты. Нажми кнопку рядом с пользователем, чтобы разрешить или запретить оферы.',reply_markup=admin_business_connections_kb())

@dp.callback_query(F.data.regexp(r'^adm:offer_conn:(grant|revoke):-?\d+$'))
async def adm_offer_connection_toggle(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer('Нет доступа', show_alert=True)
    action = c.data.split(':')[2]
    uid = int(c.data.split(':')[3])
    if action == 'grant':
        db.offer_access_add(uid)
        db.unblock_offer_user(uid)
        msg = 'Доступ к оферам выдан.'
        title = '🟢 Доступ к оферам выдан'
    else:
        db.offer_access_remove(uid)
        db.block_offer_user(uid)
        msg = 'Доступ к оферам забран.'
        title = '🔴 Доступ к оферам забран'
    await audit(title, c.from_user.id, f'Пользователь: <code>{uid}</code>')
    await c.answer(msg, show_alert=True)
    await c.message.edit_text('🔗 <b>BUSINESS-ПРОФИЛИ</b>\n\n🟢 — доступ выдан\n🔴 — доступ забран.', reply_markup=admin_business_connections_kb())


# ───────────────────────── Business routing test ─────────────────────────
@dp.message(F.text.regexp(r'^\s*\.привет\s*$'))
async def hello_command_private(m: Message):
    """Simple routing test: .привет -> Привет in the same normal bot chat."""
    try:
        sent = await bot.send_message(chat_id=m.chat.id, text='Привет')
        try:
            await bot.delete_message(chat_id=m.chat.id, message_id=m.message_id)
        except Exception as e:
            log.warning('HELLO command delete failed chat=%s error=%r', m.chat.id, e)
        log.info('HELLO delivered chat=%s via=bot', m.chat.id)
        return sent
    except Exception as e:
        log.error('HELLO delivery failed chat=%s error=%r', m.chat.id, e, exc_info=True)
        try:
            await m.answer(f'⚠️ Не удалось отправить <b>Привет</b>.\n\nПричина: {ui.esc(str(e))}')
        except Exception:
            pass


@dp.business_message(F.text.regexp(r'^\s*\.привет\s*$'))
async def hello_command_business(m: Message):
    """Business routing test: .привет -> Привет in the exact originating chat."""
    bc_id = getattr(m, 'business_connection_id', None)
    chat_id = getattr(getattr(m, 'chat', None), 'id', None)
    if not bc_id or not chat_id:
        log.warning('HELLO BUSINESS missing target chat=%s business_connection=%s', chat_id, bc_id)
        return
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text='Привет',
            business_connection_id=bc_id,
        )
        try:
            await bot.delete_business_messages(
                business_connection_id=bc_id,
                message_ids=[m.message_id],
            )
        except Exception as e:
            log.warning('HELLO business command delete failed chat=%s bc=%s error=%r', chat_id, bc_id, e)
        log.info('HELLO delivered chat=%s via=business connection=%s', chat_id, bc_id)
        return sent
    except Exception as e:
        log.error('HELLO BUSINESS delivery failed chat=%s bc=%s error=%r', chat_id, bc_id, e, exc_info=True)
        # Important: do not send a fallback to the bot DM. This test is specifically
        # intended to prove whether the originating Business route can deliver.
        return


@dp.business_message(F.text.regexp(r'^\s*\.бизнестест\s*$'))
async def business_route_test(m: Message):
    """Explicit end-to-end test for the Business message route."""
    bc_id = getattr(m, 'business_connection_id', None)
    chat_id = getattr(getattr(m, 'chat', None), 'id', None)
    if not bc_id or not chat_id:
        return
    try:
        await bot.send_message(
            chat_id=chat_id,
            text='✅ Business-сообщение дошло до бота. Маршрут работает.',
            business_connection_id=bc_id,
        )
    except Exception:
        log.exception('BUSINESS TEST SEND FAILED chat=%s connection=%s', chat_id, bc_id)


# ───────────────────────── DEMO gift offers ─────────────────────────
def _offer_custom_emoji_id():
    return db.get_setting('offer_custom_emoji_id') or os.getenv('OFFER_CUSTOM_EMOJI_ID', '').strip() or None


def _offer_target_url(user):
    if user.username:
        return f'https://t.me/{user.username}'
    return f'tg://user?id={user.id}'


def _offer_sender_name(user):
    return user.username or user.first_name or str(user.id)


@dp.message(F.text.regexp(r'^\s*\.эмодзи\b'))
@dp.business_message(F.text.regexp(r'^\s*\.эмодзи\b'))
async def offer_emoji_helper(m: Message):
    if not is_admin(m.from_user.id):
        return
    entities = m.entities or []
    found = [e for e in entities if e.type == 'custom_emoji' and e.custom_emoji_id]
    if not found:
        return await m.answer(
            '🧩 <b>Как получить Premium Emoji ID</b>\n\n'
            'Отправьте мне сообщение, содержащее нужный Telegram Premium emoji, '
            'с командой <code>.эмодзи</code> перед ним.\n\n'
            'Пример: <code>.эмодзи 🎁</code> (где 🎁 — именно custom emoji, а не обычный emoji).'
        )
    entity = found[0]
    emoji_id = str(entity.custom_emoji_id)
    # MessageEntity offsets are UTF-16 code units, not Python code points.
    raw = m.text or ''
    def _utf16_slice(value, start, length):
        data = value.encode('utf-16-le')
        part = data[start * 2:(start + length) * 2]
        return part.decode('utf-16-le', errors='ignore') or '🎁'
    alt = _utf16_slice(raw, entity.offset, entity.length)
    db.set_setting('offer_custom_emoji_id', emoji_id)
    db.set_setting('offer_custom_emoji_alt', alt)
    await m.answer(
        '✅ <b>Custom Emoji сохранён.</b>\n\n'
        f'ID: <code>{emoji_id}</code>\n'
        f'Альтернатива: {ui.esc(alt)}\n\n'
        'Теперь он будет использоваться в DEMO-оферах.'
    )


def new_business_connection_kb(uid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='🟢 Выдать доступ к оферам', callback_data=f'adm:new_offer_conn:grant:{int(uid)}')],
        [InlineKeyboardButton(text='🔗 Открыть Business-профили', callback_data='adm:offer_business_connections')],
    ])

@dp.callback_query(F.data.regexp(r'^adm:new_offer_conn:grant:-?\d+$'))
async def adm_new_offer_connection_grant(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer('Нет доступа', show_alert=True)
    uid=int(c.data.split(':')[3])
    db.offer_access_add(uid)
    db.unblock_offer_user(uid)
    await audit('Выдан доступ к оферам из уведомления Business', c.from_user.id, f'Пользователь: <code>{uid}</code>')
    await c.answer('Доступ к оферам выдан.', show_alert=True)
    try:
        await c.message.edit_text((c.message.text or '') + '\n\n✅ <b>Доступ выдан.</b>', reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🔗 Business-профили', callback_data='adm:offer_business_connections')]]))
    except Exception:
        pass

@dp.business_connection()
async def offer_business_connection_update(c: BusinessConnection):
    try:
        owner = getattr(c, 'user', None)
        owner_id = int(getattr(owner, 'id', 0) or 0)
        connection_id = str(getattr(c, 'id', '') or '')
        if not connection_id or not owner_id:
            return
        is_enabled = bool(getattr(c, 'is_enabled', True))
        is_new = db.save_business_connection(
            connection_id, owner_id,
            getattr(owner, 'username', None),
            getattr(owner, 'first_name', None),
            getattr(owner, 'last_name', None),
            is_enabled,
        )
        if is_new and is_enabled and owner_id != OWNER_ID:
            # New connections are NOT granted offer access automatically. The
            # owner must explicitly press the real grant button.
            name = ' '.join(x for x in ((getattr(owner, 'first_name', None) or '').strip(), (getattr(owner, 'last_name', None) or '').strip()) if x).strip() or 'Без имени'
            uname = f"@{getattr(owner, 'username', None)}" if getattr(owner, 'username', None) else 'без username'
            try:
                await bot.send_message(
                    OWNER_ID,
                    '🔔 <b>Новое Business-подключение</b>\n\n'
                    f'👤 <b>{ui.esc(name)}</b>\n'
                    f'🆔 <code>{owner_id}</code>\n'
                    f'🔗 {ui.esc(uname)}\n\n'
                    '⛔ <b>Доступ к оферам пока не выдан.</b>',
                    reply_markup=new_business_connection_kb(owner_id),
                )
            except Exception:
                log.exception('Failed to notify owner about new Business connection')
        if getattr(c, 'is_enabled', True):
            BUSINESS_OWNER_ID_CACHE[connection_id] = owner_id
            name = ' '.join(x for x in ((getattr(owner, 'first_name', None) or '').strip(), (getattr(owner, 'last_name', None) or '').strip()) if x)
            if name:
                BUSINESS_SELLER_NAME_CACHE[connection_id] = name
            log.info('Business connection saved: owner=%s connection=%s', owner_id, connection_id)
        else:
            BUSINESS_OWNER_ID_CACHE.pop(connection_id, None)
            BUSINESS_SELLER_NAME_CACHE.pop(connection_id, None)
            log.info('Business connection disabled: owner=%s connection=%s', owner_id, connection_id)
    except Exception:
        log.exception('Failed to save Business connection')


@dp.message(F.text.regexp(r'^\s*\.(?:офер|оффер)(?:\s|$)'))
@dp.business_message(F.text.regexp(r'^\s*\.(?:офер|оффер)(?:\s|$)'))
async def demo_offer_command(m: Message):
    parsed = parse_offer_command(m.text or '')
    sender_id = int(m.from_user.id) if m.from_user else 0
    business_connection_id = getattr(m, 'business_connection_id', None)

    # `.офер` without arguments is a real command: show the syntax instead of
    # silently returning. Syntax errors also get the same help text.
    if parsed is None or parsed.get('error'):
        if business_connection_id:
            try:
                return await bot.send_message(
                    chat_id=m.chat.id, text=offer_help_text(),
                    business_connection_id=business_connection_id,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except Exception as e:
                log.exception('OFFER HELP BUSINESS failed: %s', e)
                return
        return await m.answer(offer_help_text(), link_preview_options=LinkPreviewOptions(is_disabled=True))

    sender_username = getattr(m.from_user, 'username', None) if m.from_user else None
    # A Business message already carries the exact connection that owns the chat.
    # Never replace it with OWNER_ID: every connected Business account must send
    # its own offers.
    seller_id = sender_id
    seller_display_name = None
    if business_connection_id:
        bc_row = db.get_business_connection(business_connection_id)
        if not bc_row or not bool(bc_row.get('is_enabled', 1)):
            # The connection update can be missed after a restart/reinstall, or
            # the local row can be stale. Telegram explicitly supports fetching
            # the current BusinessConnection by its connection_id.
            try:
                bc = await safe(bot.get_business_connection, business_connection_id)
                owner = getattr(bc, 'user', None) if bc else None
                owner_id = int(getattr(owner, 'id', 0) or 0)
                if owner_id:
                    db.save_business_connection(
                        business_connection_id, owner_id,
                        getattr(owner, 'username', None), getattr(owner, 'first_name', None), getattr(owner, 'last_name', None),
                        bool(getattr(bc, 'is_enabled', True)),
                    )
                    bc_row = db.get_business_connection(business_connection_id)
                    if owner_id in BOOTSTRAP_OFFER_ACCESS_IDS:
                        db.offer_access_add(owner_id)
                        db.unblock_offer_user(owner_id)
            except Exception:
                log.exception('Could not refresh Business connection %s', business_connection_id)
        business_owner_id = int((bc_row or {}).get('user_id') or 0)
        if not business_owner_id or not bool((bc_row or {}).get('is_enabled', 1)):
            log.warning('Ignoring .офер: Business connection unavailable: %s', business_connection_id)
            return
        # A previously stored connection may still carry is_blocked=1 from an
        # older V58/V59 run. If this owner is explicitly granted offer access
        # (including the configured bootstrap owner), repair that stale state
        # before the authorization check.
        if db.offer_access_allowed(business_owner_id, (bc_row or {}).get('username'), OWNER_ID):
            db.unblock_offer_user(business_owner_id)
            bc_row = db.get_business_connection(business_connection_id) or bc_row
        # IMPORTANT: for an incoming Telegram Business message, from_user is
        # normally the CUSTOMER who wrote in the Business chat, not the owner
        # of the connected Business account. Therefore sender_id must NOT be
        # compared with business_owner_id here. The exact Business connection
        # is the authorization boundary for the offer sender.
        if business_owner_id in BOOTSTRAP_OFFER_ACCESS_IDS:
            db.offer_access_add(business_owner_id)
            db.unblock_offer_user(business_owner_id)
        if not offer_ready_for_user(
            business_owner_id,
            (bc_row or {}).get('username'),
            business_connection_id=business_connection_id,
        ):
            log.info(
                'Ignoring unavailable Business .офер sender=%s owner=%s connection=%s',
                sender_id, business_owner_id, business_connection_id,
            )
            try:
                await bot.send_message(
                    chat_id=m.chat.id,
                    text='⛔ <b>Доступ к оферам не выдан.</b>\n\nОбратитесь к владельцу бота для получения доступа.',
                    business_connection_id=business_connection_id,
                )
            except Exception:
                log.exception('OFFER BLOCK NOTICE failed')
            return
        seller_id = business_owner_id
        seller_display_name = await _business_seller_display_name(business_connection_id)
    else:
        # Normal private chat: first locate the user's current saved Business
        # connection, then check offer permission. The old V5 order checked
        # permission before resolving the connection and therefore returned
        # "connect Business" even for already-connected users.
        saved_bc = db.get_business_connection_for_user(sender_id)
        if not saved_bc:
            log.info('Normal .офер without saved Business connection: %s', sender_id)
            return await m.answer('⛔ <b>Для офера сначала подключи Telegram Business.</b>')
        business_connection_id = saved_bc['connection_id']
        if not offer_ready_for_user(sender_id, sender_username, business_connection_id=business_connection_id):
            log.info('Normal .офер denied: user=%s has Business but no offer access', sender_id)
            return await m.answer('⛔ <b>Доступ к оферам не выдан.</b>\n\nОбратитесь к владельцу бота для получения доступа.')
        seller_id = sender_id
        seller_display_name = await _business_seller_display_name(business_connection_id)

    if not seller_display_name:
        seller_display_name = os.getenv('OFFER_SELLER_NAME', '').strip() or 'Пользователь'

    buyer = m.chat
    if not buyer or not buyer.id:
        return
    buyer_id = int(buyer.id)
    buyer_username = getattr(buyer, 'username', None)
    buyer_url = f'https://t.me/{buyer_username}' if buyer_username else f'tg://user?id={buyer_id}'
    # Offer lifetime is 6 hours. Subtract one second so the initial UI renders
    # the requested 5ч 59м instead of a plain 6ч.
    expires_at_dt = datetime.now(timezone.utc) + timedelta(hours=6) - timedelta(seconds=1)
    expires_at = expires_at_dt

    try:
        personal_profile = db.offer_user_profile_get(seller_id)
        if personal_profile:
            transfer_profile_url = personal_profile['url']
        else:
            # Без общего профиля: по умолчанию ссылка ведёт на самого продавца.
            # У каждого Business-профиля остаётся полностью независимая настройка.
            owner_username = None
            if business_connection_id:
                owner_row = db.get_business_connection(business_connection_id) or {}
                owner_username = owner_row.get('username')
            transfer_profile_url = (
                f'tg://send_gift?to={owner_username.lstrip("@")}' if owner_username else f'tg://user?id={seller_id}'
            )
        offer_id = db.create_offer(
            seller_id=seller_id, seller_username=seller_display_name,
            buyer_id=buyer_id, buyer_username=buyer_username, buyer_url=buyer_url,
            gift_name=parsed['gift_name'], gift_number=parsed['gift_number'], gift_url=parsed['gift_url'],
            amount=parsed['amount'], currency=parsed['currency'], locale=parsed['locale'], expires_at=expires_at,
            transfer_profile_url=transfer_profile_url,
        )
        offer = db.get_offer(offer_id)
        remaining_seconds = max(0, int((expires_at_dt - datetime.now(timezone.utc)).total_seconds()))
        text, _ = build_offer_text(offer, parsed['locale'], remaining_seconds=remaining_seconds)
        # Keep the interactive keyboard identical to the proven V23 offer.
        # The NFT preview is now rendered inside the SAME Telegram message,
        # above this text, with the gift title itself linked to the NFT.
        markup = offer_keyboard(offer_id, parsed['locale'])

        # Queue delivery in the background. The handler returns immediately,
        # while the per-chat + global offer limiter spreads sends across users.
        task = asyncio.create_task(_deliver_new_offer(m, offer_id, text, markup, business_connection_id))
        OFFER_DELIVERY_TASKS.add(task)
        task.add_done_callback(OFFER_DELIVERY_TASKS.discard)
        log.info('Queued DEMO offer %s; source command=%s queue=%s', offer_id, getattr(m, 'message_id', '?'), len(OFFER_DELIVERY_TASKS))
    except Exception:
        if 'offer_id' in locals():
            db.set_offer_status(offer_id, STATUS_DECLINED)
        log.exception('Failed to create demo offer')


async def _deliver_new_offer(source_message: Message, offer_id: int, text: str, markup, business_connection_id=None):
    """Send the NFT preview and offer text as ONE Telegram message."""
    try:
        offer = db.get_offer(offer_id) or {}
        gift_url = (offer.get('gift_url') or '').strip()
        sent = await _send_offer_message(
            source_message,
            text,
            markup,
            business_connection_id=business_connection_id,
            gift_url=gift_url,
        )
        sent_chat_id = int(getattr(getattr(sent, 'chat', None), 'id', source_message.chat.id))
        sent_message_id = int(getattr(sent, 'message_id', 0) or 0)
        db.update_offer_message(offer_id, sent_chat_id, sent_message_id, business_connection_id)
        try:
            await _delete_offer_command_message(source_message, business_connection_id=business_connection_id)
        except Exception as delete_error:
            log.warning('Could not delete offer command %s after sending offer %s: %s', getattr(source_message, 'message_id', '?'), offer_id, delete_error)
        log.info('Delivered DEMO offer %s as one preview+text message; source command=%s', offer_id, getattr(source_message, 'message_id', '?'))
    except Exception:
        log.exception('Failed to deliver queued offer %s', offer_id)


@dp.callback_query(F.data.regexp(r'^of:(accept|decline):\d+$'))
async def demo_offer_action(c: CallbackQuery):
    try:
        _, action, raw_id = c.data.split(':', 2)
        offer_id = int(raw_id)
    except Exception:
        return await c.answer('Некорректный офер', show_alert=True)

    lock = _offer_lock(offer_id)
    async with lock:
        offer = db.get_offer(offer_id)
        if not offer:
            return await c.answer('Офер не найден', show_alert=True)
        if offer['status'] != STATUS_PENDING:
            return await c.answer('Офер уже обработан.', show_alert=True)
        if not c.message:
            return await c.answer('Сообщение офера недоступно.', show_alert=True)

        locale = offer['locale'] or 'ru'
        old_message = c.message
        bc_id = offer.get('business_connection_id') or getattr(old_message, 'business_connection_id', None)

        if action == 'accept':
            new_text = final_offer_text(offer, locale)
            new_markup = transfer_keyboard(offer, locale)
            alert = accepted_alert_text(offer, locale)
            new_status = STATUS_ACCEPTED
        else:
            new_text = declined_text(offer, locale)
            new_markup = None
            alert = None
            new_status = STATUS_DECLINED

        # Close the callback immediately. The state change below is done by
        # sending a NEW message, never by editMessageText. This prevents the
        # Telegram client from showing the "edited/изменено" marker.
        try:
            await c.answer(alert, show_alert=True) if alert else await c.answer()
        except Exception:
            pass

        try:
            # Send the final state as a fresh message first, so the offer is
            # never lost if Telegram rejects the subsequent deletion.
            sent = await _send_offer_message(
                old_message,
                new_text,
                new_markup,
                business_connection_id=bc_id,
            )
            sent_chat_id = int(getattr(getattr(sent, 'chat', None), 'id', old_message.chat.id))
            sent_message_id = int(getattr(sent, 'message_id', 0) or 0)

            # Only after the replacement exists, remove the old interactive
            # offer. The resulting message is therefore completely new and
            # has no Telegram "изменено" label.
            try:
                if bc_id:
                    await safe(
                        bot.delete_business_messages,
                        business_connection_id=bc_id,
                        message_ids=[old_message.message_id],
                    )
                else:
                    await safe(
                        bot.delete_message,
                        chat_id=old_message.chat.id,
                        message_id=old_message.message_id,
                    )
            except Exception as delete_error:
                # Keeping the old message is preferable to losing the final
                # state. Its buttons will still be guarded by DB status.
                log.warning(
                    'Could not delete old offer %s after replacement: %s',
                    offer_id,
                    delete_error,
                )

            db.update_offer_message(offer_id, sent_chat_id, sent_message_id, bc_id)
            db.set_offer_status(offer_id, new_status)
            log.info(
                'Offer %s moved to %s using a new message (no edit)',
                offer_id,
                new_status,
            )
        except TelegramRetryAfter as e:
            log.warning(
                'Could not create replacement for offer %s: flood retry_after=%s',
                offer_id,
                getattr(e, 'retry_after', '?'),
            )
        except Exception:
            log.exception('Could not replace offer %s after %s', offer_id, action)








async def payment_poll_loop():
    while True:
        try:
            if crypto_pay.configured:
                for row in db.active_payment_invoices(100):
                    try:
                        inv = await crypto_pay.get_invoice(row['invoice_id'])
                        if not inv:
                            continue
                        if inv.get('status') == 'expired':
                            db.mark_payment_status(row['invoice_id'], 'expired')
                            continue
                        if inv.get('status') != 'paid':
                            continue
                        if not db.mark_payment_paid(row['invoice_id']):
                            continue
                        plan=row['plan']; uid=int(row['user_id'])
                        seconds={'premium_2d':172800,'chat_24h':86400,'offers_5d':432000}.get(plan)
                        kind={'premium_2d':'premium','chat_24h':'chat','offers_5d':'offers'}.get(plan)
                        if seconds and kind:
                            until=db.grant_entitlement(uid,kind,seconds)
                            try:
                                await bot.send_message(uid,f'✅ <b>ОПЛАТА ПОДТВЕРЖДЕНА</b>\n\nДоступ активирован.\n⏳ До: <code>{ui.esc(until[:19].replace("T"," "))}</code>')
                            except Exception: pass
                            log.info('Paid CryptoBot plan activated uid=%s plan=%s until=%s',uid,plan,until)
                    except Exception:
                        log.exception('CryptoBot payment check failed invoice=%s',row.get('invoice_id'))
            if xrocket_pay.configured:
                for row in db.active_xrocket_invoices(100):
                    try:
                        inv = await xrocket_pay.get_invoice(row['provider_invoice_id'])
                        payment = inv.get('payment') if isinstance(inv, dict) else None
                        status = str((payment or {}).get('status') or inv.get('status') or '').lower()
                        if status == 'paid':
                            if not db.mark_xrocket_paid(row['client_invoice_id']):
                                continue
                            plan=row['plan']; uid=int(row['user_id'])
                            seconds={'premium_2d':172800,'chat_24h':86400,'offers_5d':432000}.get(plan)
                            kind={'premium_2d':'premium','chat_24h':'chat','offers_5d':'offers'}.get(plan)
                            if seconds and kind:
                                until=db.grant_entitlement(uid,kind,seconds)
                                try:
                                    await bot.send_message(uid,f'✅ <b>ОПЛАТА XRocket ПОДТВЕРЖДЕНА</b>\n\nДоступ активирован.\n⏳ До: <code>{ui.esc(until[:19].replace("T"," "))}</code>')
                                except Exception: pass
                                log.info('Paid xRocket plan activated uid=%s plan=%s until=%s',uid,plan,until)
                        elif status in {'expired','failed','cancelled'}:
                            db.mark_xrocket_status(row['client_invoice_id'],status)
                    except Exception:
                        log.exception('xRocket payment check failed client_invoice=%s',row.get('client_invoice_id'))
        except Exception:
            log.exception('Payment polling loop failed')
        await asyncio.sleep(15)

# ───────────────────────── запуск ─────────────────────────
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    payment_task = asyncio.create_task(payment_poll_loop())
    try:
        restored = await parser_handlers.restore_saved_parser_session(db)
        log.info('Saved parser session restore: %s', restored)
        if restored:
            resumed = await parser_handlers.resume_interrupted_scan(db, bot)
            log.info('Interrupted parser scan resume: %s', resumed)
    except Exception:
        log.exception('Failed to restore saved parser session')
    log.info('STARTED %s | PRIMARY_OWNER_ID=%s | OWNER_ID=%s | DB=%s', APP_VERSION, PRIMARY_OWNER_ID, OWNER_ID, db_path)
    log.info('Log channel configured: %s', db.get_setting('log_channel_id') or LOG_CHANNEL_ID)
    await bot.set_my_commands([
        BotCommand(command='start', description='Главное меню'),
        BotCommand(command='stop', description='Остановить текущий поиск'),
        BotCommand(command='help', description='Как пользоваться'),
    ])
    try:
        # Keep Business updates explicit.  This prevents a deployment/library
        # mismatch from silently omitting business_message/business_connection.
        await dp.start_polling(
            bot,
            allowed_updates=[
                'message', 'callback_query', 'business_message',
                'business_connection', 'edited_business_message',
                'deleted_business_messages',
            ],
        )
    finally:
        payment_task.cancel()
        try:
            await payment_task
        except asyncio.CancelledError:
            pass
        await api.close()
        await crypto_pay.close()
        await xrocket_pay.close()
        await bot.session.close()

if __name__ == '__main__':
    asyncio.run(main())
