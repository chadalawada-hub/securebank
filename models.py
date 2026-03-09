"""
models.py – SQLAlchemy ORM models.

All PII columns store AWS-KMS–encrypted ciphertext (base64).
Encryption/decryption is handled transparently by the KMS service layer,
NOT inside the ORM, so raw SQL queries also benefit from protection.
"""
import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Boolean, DateTime, Text,
    ForeignKey, Enum as SAEnum, Index, func
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship, declarative_base
import enum

Base = declarative_base()


# ─── Enums ───────────────────────────────────────────────────────────────────

class AccountType(str, enum.Enum):
    CHECKING = "CHECKING"
    SAVINGS  = "SAVINGS"
    MONEY_MARKET = "MONEY_MARKET"
    CD       = "CD"

class AccountStatus(str, enum.Enum):
    ACTIVE   = "ACTIVE"
    FROZEN   = "FROZEN"
    CLOSED   = "CLOSED"

class TransactionType(str, enum.Enum):
    DEBIT    = "DEBIT"
    CREDIT   = "CREDIT"
    TRANSFER = "TRANSFER"

class OAuthProvider(str, enum.Enum):
    GOOGLE   = "GOOGLE"
    APPLE    = "APPLE"
    MICROSOFT = "MICROSOFT"


# ─── User ────────────────────────────────────────────────────────────────────

class User(Base):
    """
    Core user entity.

    PII columns (marked # PII-ENCRYPTED) hold AWS-KMS ciphertext.
    Lookup columns (e.g. email_hash, ssn_hash) hold SHA-256 hex digests
    so we can query without decrypting the whole row.
    """
    __tablename__ = "users"

    id               = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username         = Column(String(64), unique=True, nullable=False, index=True)

    # ── Credentials ─────────────────────────────────────────────
    password_hash    = Column(String(256), nullable=True)   # SHA-256 hex; NULL for pure-OAuth users
    is_oauth_only    = Column(Boolean, default=False)

    # ── PII – stored encrypted (AWS KMS AES-256-GCM) ────────────
    email_encrypted  = Column(Text, nullable=False)          # PII-ENCRYPTED
    phone_encrypted  = Column(Text, nullable=True)           # PII-ENCRYPTED
    ssn_encrypted    = Column(Text, nullable=True)           # PII-ENCRYPTED
    dob_encrypted    = Column(Text, nullable=True)           # PII-ENCRYPTED  (YYYY-MM-DD)
    full_name_encrypted = Column(Text, nullable=True)        # PII-ENCRYPTED

    # ── Lookup hashes (SHA-256, hex) ─────────────────────────────
    email_hash       = Column(String(64), unique=True, nullable=False, index=True)
    ssn_hash         = Column(String(64), nullable=True, index=True)
    phone_hash       = Column(String(64), nullable=True, index=True)

    # ── Status ───────────────────────────────────────────────────
    is_active        = Column(Boolean, default=True)
    is_verified      = Column(Boolean, default=False)
    is_locked        = Column(Boolean, default=False)
    failed_login_attempts = Column(String(4), default="0")
    locked_until     = Column(DateTime(timezone=True), nullable=True)

    # ── T&C ──────────────────────────────────────────────────────
    terms_accepted   = Column(Boolean, default=False)
    terms_accepted_at = Column(DateTime(timezone=True), nullable=True)
    terms_version    = Column(String(16), nullable=True)

    # ── Timestamps ───────────────────────────────────────────────
    created_at       = Column(DateTime(timezone=True), server_default=func.now())
    updated_at       = Column(DateTime(timezone=True), onupdate=func.now())
    last_login_at    = Column(DateTime(timezone=True), nullable=True)

    # ── Relationships ─────────────────────────────────────────────
    accounts         = relationship("BankAccount", back_populates="user", lazy="selectin")
    oauth_connections = relationship("OAuthConnection", back_populates="user", lazy="selectin")
    refresh_tokens   = relationship("RefreshToken", back_populates="user")

    __table_args__ = (
        Index("ix_users_email_hash", "email_hash"),
        Index("ix_users_ssn_hash", "ssn_hash"),
    )


# ─── Bank Account ────────────────────────────────────────────────────────────

class BankAccount(Base):
    __tablename__ = "bank_accounts"

    id               = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id          = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    account_type     = Column(SAEnum(AccountType), nullable=False)
    status           = Column(SAEnum(AccountStatus), default=AccountStatus.ACTIVE)

    # PII-ENCRYPTED: account_number, routing_number, balance, card details
    account_number_encrypted = Column(Text, nullable=False)   # PII-ENCRYPTED
    routing_number_encrypted = Column(Text, nullable=False)   # PII-ENCRYPTED
    balance_encrypted        = Column(Text, nullable=False)   # PII-ENCRYPTED  (string repr of Decimal)
    available_balance_encrypted = Column(Text, nullable=True) # PII-ENCRYPTED
    card_number_encrypted    = Column(Text, nullable=True)    # PII-ENCRYPTED  (debit card)
    cvv_hash                 = Column(String(64), nullable=True)  # SHA-256 (for validation only)

    # Masked values for display (not sensitive)
    account_number_last4 = Column(String(4), nullable=False)
    card_number_last4    = Column(String(4), nullable=True)

    # Lookup hash – never store plain account number queryable
    account_number_hash  = Column(String(64), unique=True, nullable=False, index=True)

    nickname         = Column(String(64), nullable=True)
    currency         = Column(String(3), default="USD")

    opened_at        = Column(DateTime(timezone=True), server_default=func.now())
    closed_at        = Column(DateTime(timezone=True), nullable=True)
    updated_at       = Column(DateTime(timezone=True), onupdate=func.now())

    user             = relationship("User", back_populates="accounts")
    transactions     = relationship("Transaction", back_populates="account", lazy="dynamic")


# ─── Transaction ─────────────────────────────────────────────────────────────

class Transaction(Base):
    __tablename__ = "transactions"

    id               = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id       = Column(UUID(as_uuid=True), ForeignKey("bank_accounts.id", ondelete="CASCADE"), nullable=False)

    transaction_type = Column(SAEnum(TransactionType), nullable=False)
    amount_encrypted = Column(Text, nullable=False)          # PII-ENCRYPTED
    description_encrypted = Column(Text, nullable=True)      # PII-ENCRYPTED
    merchant_name    = Column(String(128), nullable=True)    # Non-sensitive display name
    reference_id     = Column(String(64), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))

    balance_after_encrypted = Column(Text, nullable=True)    # PII-ENCRYPTED

    status           = Column(String(20), default="COMPLETED")
    created_at       = Column(DateTime(timezone=True), server_default=func.now())

    account          = relationship("BankAccount", back_populates="transactions")

    __table_args__ = (
        Index("ix_transactions_account_created", "account_id", "created_at"),
    )


# ─── OAuth Connection ────────────────────────────────────────────────────────

class OAuthConnection(Base):
    """Links a User to an OAuth provider identity."""
    __tablename__ = "oauth_connections"

    id               = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id          = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider         = Column(SAEnum(OAuthProvider), nullable=False)
    provider_user_id = Column(String(256), nullable=False)
    access_token_encrypted  = Column(Text, nullable=True)    # PII-ENCRYPTED
    refresh_token_encrypted = Column(Text, nullable=True)    # PII-ENCRYPTED
    scope            = Column(Text, nullable=True)
    created_at       = Column(DateTime(timezone=True), server_default=func.now())
    updated_at       = Column(DateTime(timezone=True), onupdate=func.now())

    user             = relationship("User", back_populates="oauth_connections")

    __table_args__ = (
        Index("ix_oauth_provider_user", "provider", "provider_user_id", unique=True),
    )


# ─── Refresh Token ───────────────────────────────────────────────────────────

class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id               = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id          = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash       = Column(String(64), unique=True, nullable=False, index=True)
    expires_at       = Column(DateTime(timezone=True), nullable=False)
    revoked          = Column(Boolean, default=False)
    created_at       = Column(DateTime(timezone=True), server_default=func.now())

    user             = relationship("User", back_populates="refresh_tokens")
