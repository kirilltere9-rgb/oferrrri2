import os
from dataclasses import dataclass
from typing import Tuple


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    database_path: str
    offer_help_url: str
    business_debug: bool
    offer_access_mode: str
    offer_always_allowed_ids: Tuple[int, ...]
    require_subscription: bool = True
    channel_id: str = ""
    channel_url: str = ""
    support_url: str = "https://t.me/witensea"
    users_chat_url: str = "https://t.me/"
    offer_expire_hours: float = 24.0
    ton_wallet: str = ""
    payment_amount: float = 10.0
    tonkeeper_deeplink: str = "https://app.tonkeeper.com/transfer/{address}?amount={amount}&text={memo}"
    cryptopay_api_token: str = ""
    bot_username: str = ""

    @property
    def database_url(self) -> str:
        # Compatibility with bot.py versions that still call Database(cfg.database_url).
        return self.database_path


def load_config() -> Config:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    token = os.getenv("BOT_TOKEN", "").strip()
    owner = os.getenv("OWNER_ID", "").strip()
    database_path = os.getenv("DATABASE_PATH", "./data/offers.sqlite3").strip()

    if not token:
        raise RuntimeError("BOT_TOKEN is not set")
    if not owner.isdigit():
        raise RuntimeError("OWNER_ID must be an integer")
    if not database_path:
        database_path = "./data/offers.sqlite3"

    ids = []
    for item in os.getenv("OFFER_ALWAYS_ALLOWED_IDS", "").split(","):
        item = item.strip()
        if item and item.lstrip("-").isdigit():
            ids.append(int(item))

    mode = os.getenv("OFFER_ACCESS_MODE", "owner").strip().lower()
    if mode not in {"owner", "all"}:
        mode = "owner"

    return Config(
        bot_token=token,
        owner_id=int(owner),
        database_path=database_path,
        offer_help_url=os.getenv("OFFER_HELP_URL", "").strip(),
        business_debug=_bool(os.getenv("BUSINESS_DEBUG", "1"), True),
        offer_access_mode=mode,
        offer_always_allowed_ids=tuple(ids),
        require_subscription=_bool(os.getenv("REQUIRE_SUBSCRIPTION", "true"), True),
        channel_id=os.getenv("CHANNEL_ID", "").strip(),
        channel_url=os.getenv("CHANNEL_URL", "").strip(),
        support_url=os.getenv("SUPPORT_URL", "https://t.me/witensea").strip(),
        users_chat_url=os.getenv("USERS_CHAT_URL", "https://t.me/").strip(),
        offer_expire_hours=float(os.getenv("OFFER_EXPIRE_HOURS", "24")),
        ton_wallet=os.getenv("TON_WALLET", "").strip(),
        payment_amount=float(os.getenv("PAYMENT_AMOUNT", "10")),
        cryptopay_api_token=os.getenv("CRYPTOPAY_API_TOKEN", "").strip(),
        bot_username=os.getenv("BOT_USERNAME", "").strip().lstrip("@"),
    )
