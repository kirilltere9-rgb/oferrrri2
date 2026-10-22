import re
from datetime import datetime, timedelta, timezone

NFT_RE = re.compile(r"(?:https?://)?t\.me/nft/([^/?#\s]+?)-(\d+)(?:/)?$", re.I)
PRICE_RE = re.compile(r"(?P<amount>\d+(?:[.,]\d+)?)\s*(?P<currency>грам(?:м)?|gram(?:s)?|зв(?:ёзд|езд)?|stars?|star|р(?:уб(?:л)?)?)\b", re.I)

DECLINE_EMOJI_ID = "5778527486270770928"
ACCEPT_EMOJI_ID = "5776375003280838798"
TRANSFER_EMOJI_ID = "5879770735999717115"
GIFT_EMOJI_ID = "6032937473162614352"
CONFIRM_EMOJI_ID = "5800595351589195950"  # Красный эмодзи для кнопки подтверждения
PREMIUM_EMOJI_ID = "5825794181183836432"  # Премиум эмодзи для кнопки "Готово"
DONE_EMOJI_ID = "5800595351589195950"  # Эмодзи для кнопки "Готово"
STARS_EMOJI_ID = "5321485469249198987"  # Премиум звёздочка для Stars
GRAM_EMOJI_ID = "5255894863303450180"  # Эмодзи для Gram


def normalize_nft(raw: str):
    m = NFT_RE.fullmatch(raw.strip())
    if not m:
        return None
    name, number = m.group(1), int(m.group(2))
    return {"gift_name": name, "gift_number": number, "gift_url": f"https://t.me/nft/{name}-{number}"}


def parse_offer(text: str):
    """Parse an offer and recognize the requested language anywhere in the command.

    Supported examples:
      офер https://t.me/nft/PartySparkler-140864 150 грам китай
      .офер араб https://t.me/nft/PartySparkler-140864 150 stars
      offer https://t.me/nft/PartySparkler-140864 150 stars en
    """
    tokens =    text.strip().split()
    locale = "ru"
    language_aliases = {
        "en": "en", "eng": "en", "англ": "en", "английский": "en", "английском": "en", "english": "en",
        "cn": "cn", "zh": "cn", "chinese": "cn", "中文": "cn", "китай": "cn", "кит": "cn", "китайский": "cn", "китайском": "cn",
        "ar": "ar", "arab": "ar", "arabic": "ar", "عربي": "ar", "العربية": "ar", "عرب": "ar", "араб": "ar", "арабский": "ar", "арабском": "ar",
        "ru": "ru", "рус": "ru", "русский": "ru", "русском": "ru",
    }
    # Language can be placed after the command or at the end, as users naturally write it.
    # Remove the language token before looking for the NFT URL, but keep the original text for price parsing.
    for i in range(1, len(tokens)):
        token = tokens[i].strip(".,:;!?()[]{}<>\"'`).").lower()
        if token in language_aliases:
            locale = language_aliases[token]
            tokens.pop(i)
            break

    nft = None
    for token in tokens[1:]:
        candidate = normalize_nft(token)
        if candidate:
            nft = candidate
            break
    if not nft:
        return {"error": "nft", "locale": locale}
    price = PRICE_RE.search(text)
    if not price:
        return {"error": "price", "locale": locale, **nft}
    amount = float(price.group("amount").replace(",", "."))
    currency_raw = price.group("currency").lower()
    if currency_raw.startswith(("грам", "gram")):
        currency = "gram"
    elif currency_raw.startswith(("р", "руб")):
        currency = "rub"
    else:
        currency = "stars"
    return {**nft, "amount": amount, "currency": currency, "locale": locale}


def display_amount(amount: float, currency: str, with_emoji: bool = False) -> str:
    value = f"{amount:g}"
    if with_emoji:
        # Для отображения в тексте с эмодзи
        if currency == "gram":
            return f"{value} <tg-emoji emoji-id=\"5255894863303450180\">💎</tg-emoji>"
        elif currency == "rub":
            return f"{value}₽"
        else:  # stars
            return f"{value} <tg-emoji emoji-id=\"5321485469249198987\">⭐</tg-emoji>"
    else:
        # Обход PEER_FLOOD: латинская 'а' вместо кириллической
        if currency == "gram":
            return f"{value} Grаm"
        elif currency == "rub":
            return f"{value}₽"
        else:
            return f"{value} Stаrs"


def gift_title(name: str, number: int) -> str:
    return re.sub(r"(?<![a-z])(?=[A-Z])", " ", name).strip() + f" #{number}"


def format_remaining(seconds: float, locale: str = "ru") -> str:
    total = max(0, int(seconds))
    hours = total // 3600
    if locale == "cn":
        return f"{hours}小时"
    elif locale == "ar":
        return f"{hours} ساعة"
    elif locale == "en":
        return f"{hours}h"
    else:
        return f"{hours}ч"


def make_expiry(now=None, hours=6):
    now = now or datetime.now(timezone.utc)
    return now + timedelta(hours=hours)
