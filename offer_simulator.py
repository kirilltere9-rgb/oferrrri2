"""DEMO Telegram Business offer UI.

This module only simulates an offer visually. It never transfers Stars/Gram and
never calls Telegram payment/gift transfer APIs.
"""
import re
from urllib.parse import unquote

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ui import esc, num, pretty

GIFT_RE = re.compile(
    r"(?:https?://)?(?:(?:www\.)?t\.me|(?:www\.)?telegram\.me)/nft/(?P<slug>[^/?#\s]+)-(?P<number>\d+)(?=$|[/?#\s])",
    re.I,
)
AMOUNT_RE = re.compile(r"(?P<amount>\d+(?:[.,]\d+)?)\s*(?P<currency>грам(?:м|s)?|gram(?:s)?|зв(?:езд|ёзд)?|stars?|⭐)", re.I)
LOCALE_RU = 'ru'
LOCALE_EN = 'en'
STATUS_PENDING = 'pending'
STATUS_ACCEPTED = 'accepted'
STATUS_DECLINED = 'declined'
STATUS_EXPIRED = 'expired'

DECLINE_EMOJI_ID = '5778527486270770928'
ACCEPT_EMOJI_ID = '5776375003280838798'
TRANSFER_EMOJI_ID = '5879770735999717115'
GIFT_EMOJI_ID = '6032937473162614352'


def _currency(raw: str):
    x = raw.lower().replace('ё', 'е')
    if x.startswith(('грам', 'gram')):
        return 'gram'
    return 'stars'


def parse_offer_command(text: str):
    text = (text or '').strip()
    m = re.match(r'^\.(?:офер|оффер)(?:\s+(.+))?$', text, re.I)
    if not m:
        return None
    tail = (m.group(1) or '').strip()
    locale = LOCALE_RU
    if re.match(r'^ру(?:с)?\b', tail, re.I):
        tail = re.sub(r'^ру(?:с)?\s*', '', tail, flags=re.I)
    elif re.match(r'^англ(?:ийский)?\b', tail, re.I):
        locale = LOCALE_EN
        tail = re.sub(r'^англ(?:ийский)?\s*', '', tail, flags=re.I)
    gift = GIFT_RE.search(tail)
    if not gift:
        return {'locale': locale, 'error': 'gift_url'}
    gift_name = unquote(gift.group('slug'))
    gift_number = int(gift.group('number'))
    # Normalize the NFT URL so query strings/fragments/trailing slashes do not
    # become part of the URL button. Telegram accepts both t.me and telegram.me
    # links, with or without https/www; keep the canonical https://t.me form.
    gift_url = f'https://t.me/nft/{gift_name}-{gift_number}'
    # Amount may be placed before OR after the NFT URL. This makes copy/paste
    # from common offer formats much less fragile.
    before = tail[:gift.start()].strip()
    after = tail[gift.end():].strip()
    amount = AMOUNT_RE.search(after) or AMOUNT_RE.search(before)
    if not amount:
        return {'locale': locale, 'gift_name': gift_name, 'gift_number': gift_number, 'error': 'amount'}
    value = float(amount.group('amount').replace(',', '.'))
    if value <= 0:
        return {'locale': locale, 'gift_name': gift_name, 'gift_number': gift_number, 'error': 'amount'}
    return {
        'locale': locale,
        'gift_name': gift_name,
        'gift_number': gift_number,
        'amount': value,
        'currency': _currency(amount.group('currency')),
        'gift_url': gift_url
    }


def format_amount(amount, currency, locale='ru'):
    value = f'{amount:g}'
    if locale == LOCALE_EN:
        return f'{value} Gram' if currency == 'gram' else f'{value} Stars'
    return f'{value} Gram' if currency == 'gram' else f'{value} Stars'


def gift_title(name, number):
    return f'{pretty(name)} #{num(number)}'


def offer_help_text(locale=LOCALE_RU):
    if locale == LOCALE_EN:
        return (
            '<b>Usage</b>\n\n'
            '<code>.офер https://t.me/nft/PartySparkler-140864 150 gram</code>\n'
            '<code>.оффер https://t.me/nft/PartySparkler-140864 4500 Stars</code>\n'
            '<code>.офер ру https://t.me/nft/PartySparkler-140864 4500 зв</code>\n'
            '<code>.офер англ https://t.me/nft/PartySparkler-140864 4500 stars</code>'
        )
    return (
        '<b>Использование</b>\n\n'
        '<code>.офер https://t.me/nft/PartySparkler-140864 150 грам</code>\n'
        '<code>.оффер https://t.me/nft/PartySparkler-140864 4500 Stars</code>\n'
        '<code>.офер ру https://t.me/nft/PartySparkler-140864 4500 зв</code>\n'
        '<code>.офер англ https://t.me/nft/PartySparkler-140864 4500 stars</code>'
    )


def keyboard(offer_id: int, locale: str, status: str = STATUS_PENDING, gift_url: str | None = None, gift_title_text: str | None = None):
    if status != STATUS_PENDING:
        return None
    # Proven V23 keyboard: only the action buttons. The NFT preview/link is
    # sent separately so the first offer message stays V58-safe.
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Отклонить', icon_custom_emoji_id=DECLINE_EMOJI_ID, callback_data=f'of:decline:{offer_id}'),
        InlineKeyboardButton(text='Принять', icon_custom_emoji_id=ACCEPT_EMOJI_ID, callback_data=f'of:accept:{offer_id}'),
    ]])


def transfer_keyboard(offer: dict, locale: str):
    # Only the buyer transfer button remains. The URL may be tg://send_gift?to=...
    buyer_url = offer.get('transfer_profile_url') or offer.get('buyer_url')
    rows = []
    if buyer_url:
        rows.append([InlineKeyboardButton(
            text='Передать' if locale == LOCALE_RU else 'Transfer',
            icon_custom_emoji_id=TRANSFER_EMOJI_ID,
            url=buyer_url,
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _linked_title(offer: dict):
    # The NFT name itself is the clickable link, exactly like the working
    # offer UI: Telegram renders the NFT preview above the text, while the
    # gift title in the text opens the same NFT.
    title = gift_title(offer['gift_name'], offer['gift_number'])
    url = (offer.get('gift_url') or '').strip()
    if url:
        return f'<a href="{esc(url)}">{esc(title)}</a>'
    return esc(title)


def _format_remaining(remaining_seconds):
    try:
        total = max(0, int(remaining_seconds))
    except (TypeError, ValueError):
        total = 0
    hours = total // 3600
    minutes = (total % 3600) // 60
    return hours, minutes


def build_offer_text(offer: dict, locale: str, remaining_seconds=None, **_ignored):
    """Build the single-message offer shown under the Telegram NFT preview."""
    title = _linked_title(offer)
    amount = format_amount(offer['amount'], offer['currency'], locale)
    sender = (offer.get('seller_username') or '').strip()
    sender_html = f'Пользователь <b>{esc(sender)}</b>' if sender else '<b>Пользователь</b>'

    if remaining_seconds is None:
        remaining_seconds = 6 * 3600 - 1
    hours, minutes = _format_remaining(remaining_seconds)
    if locale == LOCALE_EN:
        return (
            f'{sender_html} offers you <b>{esc(amount)}</b> for the gift {title}.\n\n'
            f'This offer is active for <b>{hours}h {minutes}m</b>.'
        ), None
    return (
        f'{sender_html} предлагает Вам <b>{esc(amount)}</b> за подарок {title}.\n\n'
        f'Это предложение активно ещё <b>{hours}ч {minutes}м</b>.'
    ), None

def accepted_alert_text(offer: dict, locale: str):
    title = gift_title(offer['gift_name'], offer['gift_number'])
    amount = format_amount(offer['amount'], offer['currency'], locale)
    if locale == LOCALE_EN:
        return f'Offer to sell {title} was successfully accepted.\n\nTransfer the gift to the buyer to receive {amount} on your balance.'
    return f'Предложение о продаже {title} успешно принято.\n\nПередайте подарок покупателю для зачисления {amount} на ваш баланс.'


def final_offer_text(offer: dict, locale: str):
    title = _linked_title(offer)
    amount = format_amount(offer['amount'], offer['currency'], locale)
    gift_emoji = f'<tg-emoji emoji-id="{GIFT_EMOJI_ID}">🎁</tg-emoji>'
    if locale == LOCALE_EN:
        return (
            f'{gift_emoji} Gift: {title} successfully sold!\n\n'
            f'<b>• Transfer the gift to the buyer to receive {esc(amount)} on your balance.</b>'
        )
    return (
        f'{gift_emoji} Gift: {title} успешно продан!\n\n'
        f'<b>• Передайте подарок покупателю для зачисления {esc(amount)} на ваш баланс.</b>'
    )


def declined_text(offer: dict, locale: str):
    if locale == LOCALE_EN:
        return f'<tg-emoji emoji-id="{DECLINE_EMOJI_ID}">❌</tg-emoji> <b>Offer declined.</b>'
    return f'<tg-emoji emoji-id="{DECLINE_EMOJI_ID}">❌</tg-emoji> <b>Предложение отклонено.</b>'
