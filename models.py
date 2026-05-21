from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Boolean,
    DateTime,
    Text,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


# =========================
# USERS
# =========================
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, index=True, nullable=False)

    validemail_key = Column(String, nullable=True)
    goo_api_key = Column(String, nullable=True)
    goo_user_api_key = Column(String, nullable=True)

    goo_user_api_key_aqua = Column(String, nullable=True)
    goo_user_api_key_tsum = Column(String, nullable=True)
    goo_user_api_key_nur = Column(String, nullable=True)

    goo_team_key = Column(String, nullable=True)

    goo_team_api_key = Column(String, nullable=True)
    goo_team_api_key_aqua = Column(String, nullable=True)
    goo_team_api_key_tsum = Column(String, nullable=True)
    goo_team_api_key_nur = Column(String, nullable=True)

    goo_profile_id = Column(String, nullable=True)

    sender_name = Column(String, nullable=True)

    is_banned = Column(Boolean, default=False)
    access_granted = Column(Boolean, default=False)
    is_admin = Column(Boolean, default=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    domains = relationship("Domain", back_populates="user", cascade="all, delete-orphan")
    email_accounts = relationship("EmailAccount", back_populates="user", cascade="all, delete-orphan")
    proxies = relationship("Proxy", back_populates="user", cascade="all, delete-orphan")
    offers = relationship("Offer", back_populates="user", cascade="all, delete-orphan")
    quick_templates = relationship("QuickTemplate", back_populates="user", cascade="all, delete-orphan")
    conversation_links = relationship("ConversationLink", back_populates="user", cascade="all, delete-orphan")
    seller_blacklist = relationship("SellerBlacklist", back_populates="user", cascade="all, delete-orphan")
    lines = relationship("Line", back_populates="user", cascade="all, delete-orphan")
    facebook_accounts = relationship(
        "FacebookAccount", back_populates="user", cascade="all, delete-orphan"
    )


# =========================
# APP SETTINGS
# =========================
class AppSetting(Base):
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True)
    key = Column(String, nullable=False, unique=True, index=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TeamKey(Base):
    __tablename__ = "team_keys"

    id = Column(Integer, primary_key=True)
    team_name = Column(String, nullable=False, unique=True)
    team_api_key = Column(String, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class UserSetting(Base):
    __tablename__ = "user_settings"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_user_settings_user_key"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    key = Column(String, nullable=False)
    value = Column(Text, nullable=True)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    html_nick = Column(Text, nullable=True, default="")
    html_signature = Column(Text, nullable=True, default="")
    sender_name = Column(Text, nullable=True, default="")

    user = relationship("User")


# =========================
# DOMAINS
# =========================
class Domain(Base):
    __tablename__ = "domains"

    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    domain = Column(String, nullable=False)
    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="domains")


# =========================
# EMAIL ACCOUNTS
# =========================
class EmailAccount(Base):
    __tablename__ = "email_accounts"

    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    email = Column(String, nullable=False)
    password = Column(String, nullable=False)

    provider = Column(String, nullable=False, default="gmail")

    proxy_id = Column(ForeignKey("proxies.id", ondelete="SET NULL"), nullable=True, index=True)

    status = Column(String, nullable=True, default="active")
    last_error = Column(Text, nullable=True)

    last_seen_uid = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="email_accounts")
    lines = relationship("Line", back_populates="account", cascade="all, delete-orphan")


# =========================
# PROXIES
# =========================
class Proxy(Base):
    __tablename__ = "proxies"

    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    host = Column(String, nullable=False)
    port = Column(Integer, nullable=False)
    username = Column(String, nullable=True)
    password = Column(String, nullable=True)

    type = Column(String, default="socks5")

    is_active = Column(Boolean, default=True)
    last_error = Column(Text, nullable=True)

    last_seen_uid = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="proxies")


# =========================
# FACEBOOK (Marketplace)
# =========================
class FacebookAccount(Base):
    __tablename__ = "facebook_accounts"

    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    label = Column(String, nullable=True)
    cookies_json = Column(Text, nullable=False)

    is_active = Column(Boolean, default=True)
    last_error = Column(Text, nullable=True)
    last_checked_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="facebook_accounts")


# =========================
# OFFERS
# =========================
class Offer(Base):
    __tablename__ = "offers"

    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    title = Column(Text, nullable=True)
    price = Column(String, nullable=True)
    link = Column(Text, nullable=True)
    photo = Column(Text, nullable=True)

    person_name = Column(String, nullable=True)
    raw_json = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="offers")
    emails = relationship("OfferEmail", back_populates="offer", cascade="all, delete-orphan")


class OfferEmail(Base):
    __tablename__ = "offer_emails"

    id = Column(Integer, primary_key=True)
    offer_id = Column(ForeignKey("offers.id", ondelete="CASCADE"), nullable=False, index=True)

    email = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    offer = relationship("Offer", back_populates="emails")


# =========================
# SENT EMAILS
# =========================
class SentEmail(Base):
    __tablename__ = "sent_emails"
    __table_args__ = (UniqueConstraint("user_id", "email", name="uq_sent_email_user_email"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    email = Column(String, nullable=False, index=True)
    sent_at = Column(DateTime, default=datetime.utcnow)
    sent_count = Column(Integer, default=1)


class MailingSend(Base):
    """Каждое письмо рассылки: offer_id + тема + ящик (для ответов с другого email продавца)."""

    __tablename__ = "mailing_sends"

    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    offer_id = Column(ForeignKey("offers.id", ondelete="CASCADE"), nullable=False, index=True)
    offer_email_id = Column(Integer, nullable=True, index=True)

    inbox_email = Column(String, nullable=False, index=True)
    to_email = Column(String, nullable=False, index=True)
    subject = Column(Text, nullable=True)
    title_snapshot = Column(Text, nullable=True)

    sent_at = Column(DateTime, default=datetime.utcnow, index=True)


class GlobalSentEmail(Base):
    __tablename__ = "global_sent_emails"
    __table_args__ = (UniqueConstraint("email", name="uq_global_sent_email_email"),)

    id = Column(Integer, primary_key=True)
    email = Column(String, nullable=False, index=True)
    first_user_id = Column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    first_sent_at = Column(DateTime, default=datetime.utcnow)


# =========================
# QUICK TEMPLATES
# =========================
class UserJsonBlob(Base):
    """JSON-данные пользователя (пресеты и т.п.) — для Railway Postgres, не теряются при redeploy."""

    __tablename__ = "user_json_blobs"
    __table_args__ = (
        UniqueConstraint("telegram_id", "blob_key", name="uq_user_json_blob_tg_key"),
    )

    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    blob_key = Column(String(64), nullable=False, index=True)
    payload = Column(Text, nullable=False, default="[]")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class QuickTemplate(Base):
    __tablename__ = "quick_templates"

    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    title = Column(String(64), nullable=False, default="Шаблон")
    body = Column(Text, nullable=False, default="")

    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="quick_templates")


# =========================
# CONVERSATION LINKS
# =========================
class ConversationLink(Base):
    __tablename__ = "conversation_links"
    __table_args__ = (
        UniqueConstraint("user_id", "account_email", "from_email", name="uq_convlink_user_acc_from"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    account_email = Column(String, nullable=False, index=True)
    from_email = Column(String, nullable=False, index=True)

    ad_url = Column(Text, nullable=True)
    # Used by "Создать ссылку" flow (e.g. generated GAG/deeplink).
    # Might be absent in existing DBs; added via automigration in database.py.
    generated_link = Column(Text, nullable=True)
    # ✅ ТЗ: чтобы повторные письма от одного продавца крепились к первому сообщению в TG.
    # Храним message_id первого сообщения (pin/anchor).
    tg_message_id = Column(BigInteger, nullable=True)
    # Для продавцов из ЧС: закреплённое объявление по диалогу (не путать разные лоты).
    pinned_offer_id = Column(ForeignKey("offers.id", ondelete="SET NULL"), nullable=True, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="conversation_links")


class SellerBlacklist(Base):
    """Личный ЧС: имя продавца из JSON (void) — повторно не валидировать у этого user."""

    __tablename__ = "seller_blacklist"
    __table_args__ = (
        UniqueConstraint("user_id", "seller_name_key", name="uq_seller_blacklist_user_name"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    seller_name_key = Column(String, nullable=False, index=True)
    seller_name_display = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="seller_blacklist")


# =========================
# INCOMING MAIL
# =========================
class IncomingMail(Base):
    __tablename__ = "incoming_mails"
    __table_args__ = (
        UniqueConstraint("account_id", "imap_uid", name="uq_incoming_account_uid"),
    )

    id = Column(Integer, primary_key=True)

    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id = Column(ForeignKey("email_accounts.id", ondelete="CASCADE"), nullable=False, index=True)

    imap_uid = Column(Integer, nullable=False, index=True)

    account_email = Column(String, nullable=False, index=True)
    from_email = Column(String, nullable=False, index=True)
    from_name = Column(String, nullable=True)
    subject = Column(String, nullable=True)
    date_str = Column(String, nullable=True)
    body = Column(Text, nullable=True)

    ad_url = Column(Text, nullable=True)
    generated_link = Column(Text, nullable=True)

    resolved_offer_id = Column(ForeignKey("offers.id", ondelete="SET NULL"), nullable=True, index=True)
    resolved_offer_email_id = Column(ForeignKey("offer_emails.id", ondelete="SET NULL"), nullable=True, index=True)

    # ID карточки в Telegram — не слать дубликат при повторном IMAP-опросе
    telegram_message_id = Column(Integer, nullable=True, index=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User")
    account = relationship("EmailAccount")


# =========================
# LINES (ТВОЯ НОВАЯ МОДЕЛЬ)
# =========================
class Line(Base):
    __tablename__ = "lines"

    id = Column(Integer, primary_key=True)

    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id = Column(ForeignKey("email_accounts.id", ondelete="CASCADE"), nullable=False, index=True)

    is_active = Column(Boolean, default=True)

    auto_reply_enabled = Column(Boolean, default=False)
    auto_send_enabled = Column(Boolean, default=False)
    auto_link_enabled = Column(Boolean, default=False)

    template_title = Column(String(64), nullable=True)
    html_name = Column(String(128), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="lines")
    account = relationship("EmailAccount", back_populates="lines")
