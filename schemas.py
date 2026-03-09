"""
schemas.py  –  Pydantic request / response models.
"""
from __future__ import annotations
from datetime import datetime
from typing import Optional, List
from uuid import UUID
from pydantic import BaseModel, EmailStr, field_validator, model_validator
import re


# ── Helpers ──────────────────────────────────────────────────────────────────

def _require_non_empty(v: str, field: str) -> str:
    if not v or not v.strip():
        raise ValueError(f"{field} must not be empty")
    return v.strip()


# ── Auth / Login ─────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str

    @field_validator("username")
    @classmethod
    def validate_username(cls, v):
        return _require_non_empty(v, "username")


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int          # seconds until access_token expiry


class RefreshTokenRequest(BaseModel):
    refresh_token: str


# ── Sign-Up ───────────────────────────────────────────────────────────────────

class SignupRequest(BaseModel):
    # Identity
    username: str
    email: EmailStr
    phone_number: str
    password: str
    confirm_password: str

    # Account validation (verify identity against existing bank record)
    ssn_last4: str           # last 4 digits of SSN
    date_of_birth: str       # YYYY-MM-DD
    primer_account_number: str
    debit_card_cvv: str

    # Legal
    terms_accepted: bool
    terms_version: str = "1.0"

    @field_validator("username")
    @classmethod
    def validate_username(cls, v):
        v = _require_non_empty(v, "username")
        if not re.match(r"^[a-zA-Z0-9_.-]{3,32}$", v):
            raise ValueError(
                "Username must be 3–32 chars and contain only letters, digits, _, -, ."
            )
        return v

    @field_validator("phone_number")
    @classmethod
    def validate_phone(cls, v):
        digits = re.sub(r"\D", "", v)
        if len(digits) < 10 or len(digits) > 15:
            raise ValueError("Phone number must be 10–15 digits")
        return digits

    @field_validator("ssn_last4")
    @classmethod
    def validate_ssn(cls, v):
        if not re.match(r"^\d{4}$", v):
            raise ValueError("ssn_last4 must be exactly 4 digits")
        return v

    @field_validator("date_of_birth")
    @classmethod
    def validate_dob(cls, v):
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("date_of_birth must be YYYY-MM-DD")
        return v

    @field_validator("primer_account_number")
    @classmethod
    def validate_account_number(cls, v):
        if not re.match(r"^\d{8,17}$", v):
            raise ValueError("Account number must be 8–17 digits")
        return v

    @field_validator("debit_card_cvv")
    @classmethod
    def validate_cvv(cls, v):
        if not re.match(r"^\d{3,4}$", v):
            raise ValueError("CVV must be 3 or 4 digits")
        return v

    @field_validator("terms_accepted")
    @classmethod
    def must_accept_terms(cls, v):
        if not v:
            raise ValueError("You must accept the Terms and Conditions to register")
        return v

    @model_validator(mode="after")
    def passwords_match(self):
        if self.password != self.confirm_password:
            raise ValueError("Passwords do not match")
        return self


class SignupResponse(BaseModel):
    message: str
    user_id: UUID


# ── User ─────────────────────────────────────────────────────────────────────

class UserProfile(BaseModel):
    id: UUID
    username: str
    email: str
    phone_number: Optional[str] = None
    is_verified: bool
    created_at: datetime
    last_login_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ── Account ───────────────────────────────────────────────────────────────────

class AccountSummary(BaseModel):
    id: UUID
    account_type: str
    status: str
    account_number_last4: str
    card_number_last4: Optional[str] = None
    nickname: Optional[str] = None
    currency: str
    # Decrypted at response-build time
    balance: Optional[str] = None
    available_balance: Optional[str] = None
    opened_at: datetime

    model_config = {"from_attributes": True}


class TransactionItem(BaseModel):
    id: UUID
    transaction_type: str
    amount: Optional[str] = None
    description: Optional[str] = None
    merchant_name: Optional[str] = None
    reference_id: str
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class AccountDetailResponse(BaseModel):
    profile: UserProfile
    accounts: List[AccountSummary]


class TransactionListResponse(BaseModel):
    account_id: UUID
    transactions: List[TransactionItem]
    total: int


# ── OAuth ─────────────────────────────────────────────────────────────────────

class OAuthCallbackRequest(BaseModel):
    code: str
    state: str


class OAuthLoginURLResponse(BaseModel):
    authorization_url: str
    state: str


# ── Password ──────────────────────────────────────────────────────────────────

class PasswordStrengthResponse(BaseModel):
    is_valid: bool
    score: int          # 0–4
    errors: List[str]
    suggestions: List[str]
