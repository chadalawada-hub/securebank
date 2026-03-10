"""
routers/accounts.py  –  Protected account & transaction endpoints.

All routes require a valid JWT Bearer token.

GET  /accounts/me            – user profile + all accounts
GET  /accounts/{account_id}  – single account detail
GET  /accounts/{account_id}/transactions  – paginated transaction list

──────────────────────────────────────────────────────────────────────────────
ALTERNATE AUTHENTICATION METHODS  (documented here for engineering reference)
──────────────────────────────────────────────────────────────────────────────

The primary method is Bearer JWT in the Authorization header.  Below are the
production-grade alternatives supported or readily extensible:

1. mTLS (Mutual TLS / Client Certificate Auth)
   ─────────────────────────────────────────────
   • Client presents a TLS certificate issued by your PKI CA.
   • FastAPI sits behind an Nginx/AWS ALB reverse proxy that terminates mTLS
     and forwards the verified DN in X-Client-Cert-DN header.
   • Ideal for: machine-to-machine (M2M) calls, open-banking partner APIs.
   • Pros: phishing-resistant, certificate revocation (CRL/OCSP) possible.
   • Cons: certificate lifecycle management overhead.

2. OAuth2 Client Credentials (M2M)
   ──────────────────────────────────
   • Third-party apps or internal micro-services exchange client_id +
     client_secret for a short-lived access token (RFC 6749 §4.4).
   • No user context; scoped to specific API permissions.
   • Pros: standard, auditable, revocable.

3. FIDO2 / WebAuthn Passkeys
   ───────────────────────────
   • User registers a passkey (Touch ID, Face ID, hardware key).
   • Authentication is a cryptographic challenge-response — no password sent.
   • On success, backend issues a JWT exactly as today.
   • Pros: phishing-proof, highest assurance level (AAL3).
   • Libraries: `py_webauthn` on the server, WebAuthn API on the client.

4. TOTP / HOTP (MFA layer on top of JWT)
   ──────────────────────────────────────
   • After password login, user must supply a 6-digit TOTP (Google Authenticator).
   • Implemented as a second factor: login returns a short-lived "mfa_pending"
     token; the /auth/mfa/verify endpoint upgrades it to a full access token.
   • Pros: widely understood, battle-tested.

5. Step-Up Authentication (SCA)
   ────────────────────────────
   • Sensitive operations (wire transfers, PII update) require re-authentication
     mid-session via biometric or OTP, even with a valid JWT.
   • Implemented by checking an `sca_verified_at` claim in the JWT and
     requiring it to be < 5 minutes old for high-risk routes.

6. API Key (Internal / Mobile SDK)
   ──────────────────────────────────
   • A long-lived, rotatable API key scoped per mobile app instance.
   • Sent in the X-API-Key header alongside the JWT for defence-in-depth.
   • Stored as SHA-256 hash in the database.

7. Session Cookie (Web Application)
   ───────────────────────────────────
   • For the web app: the access JWT is stored in an HttpOnly, Secure,
     SameSite=Strict cookie rather than localStorage.
   • Prevents XSS token theft entirely.
   • The FastAPI dependency can accept both Bearer header AND cookie
     (dual-mode, see `get_current_user` below).
"""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status, Cookie
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from jose import JWTError

from database import get_db
from models import User, BankAccount, Transaction
from schemas import AccountDetailResponse, AccountSummary, UserProfile, TransactionListResponse, TransactionItem
from security import decode_access_token
from backend.kms_service import kms_decrypt

router  = APIRouter(prefix="/accounts", tags=["Accounts"])
bearer  = HTTPBearer(auto_error=False)


# ── JWT Dependency (supports header + cookie for web/mobile dual use) ─────────

async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    access_token_cookie: Annotated[str | None, Cookie()] = None,
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Extract and validate JWT from:
      1. Authorization: Bearer <token>   (mobile app / SPA)
      2. HttpOnly cookie `access_token`  (server-rendered web app)

    Raises HTTP 401 if token is missing or invalid.
    """
    token = None
    if credentials:
        token = credentials.credentials
    elif access_token_cookie:
        token = access_token_cookie

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = decode_access_token(token)
        user_id: str = payload.get("sub")
        if not user_id:
            raise JWTError("Missing subject")
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    result = await db.execute(select(User).where(User.id == user_id))
    user: User | None = result.scalar_one_or_none()

    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_account_summary(acct: BankAccount) -> AccountSummary:
    return AccountSummary(
        id                   = acct.id,
        account_type         = acct.account_type.value,
        status               = acct.status.value,
        account_number_last4 = acct.account_number_last4,
        card_number_last4    = acct.card_number_last4,
        nickname             = acct.nickname,
        currency             = acct.currency,
        balance              = _safe_decrypt(acct.balance_encrypted),
        available_balance    = _safe_decrypt(acct.available_balance_encrypted),
        opened_at            = acct.opened_at,
    )

def _safe_decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return kms_decrypt(value)
    except Exception:
        return None

def _build_user_profile(user: User) -> UserProfile:
    return UserProfile(
        id            = user.id,
        username      = user.username,
        email         = _safe_decrypt(user.email_encrypted) or "—",
        phone_number  = _safe_decrypt(user.phone_encrypted),
        is_verified   = user.is_verified,
        created_at    = user.created_at,
        last_login_at = user.last_login_at,
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get(
    "/me",
    response_model=AccountDetailResponse,
    summary="Get authenticated user's profile and all bank accounts",
)
async def get_my_accounts(current_user: CurrentUser):
    """
    Returns the authenticated user's decrypted profile and all linked
    bank accounts (balances decrypted via AWS KMS at request time).

    Requires:  Authorization: Bearer <access_token>
    """
    return AccountDetailResponse(
        profile  = _build_user_profile(current_user),
        accounts = [_build_account_summary(a) for a in current_user.accounts],
    )


@router.get(
    "/{account_id}",
    response_model=AccountSummary,
    summary="Get a specific bank account",
)
async def get_account(
    account_id: UUID,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    """Fetch a single account owned by the authenticated user."""
    result = await db.execute(
        select(BankAccount).where(
            BankAccount.id      == account_id,
            BankAccount.user_id == current_user.id,
        )
    )
    acct: BankAccount | None = result.scalar_one_or_none()
    if not acct:
        raise HTTPException(status_code=404, detail="Account not found.")
    return _build_account_summary(acct)


@router.get(
    "/{account_id}/transactions",
    response_model=TransactionListResponse,
    summary="List transactions for an account (paginated)",
)
async def get_transactions(
    account_id: UUID,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    page: int  = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    """
    Returns paginated transactions for *account_id* owned by the current user.
    Amounts and descriptions are decrypted via AWS KMS.
    """
    # Ownership check
    acct_result = await db.execute(
        select(BankAccount).where(
            BankAccount.id == account_id,
            BankAccount.user_id == current_user.id,
        )
    )
    if not acct_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Account not found.")

    offset = (page - 1) * limit
    result = await db.execute(
        select(Transaction)
        .where(Transaction.account_id == account_id)
        .order_by(Transaction.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    txns = result.scalars().all()

    items = [
        TransactionItem(
            id               = t.id,
            transaction_type = t.transaction_type.value,
            amount           = _safe_decrypt(t.amount_encrypted),
            description      = _safe_decrypt(t.description_encrypted),
            merchant_name    = t.merchant_name,
            reference_id     = t.reference_id,
            status           = t.status,
            created_at       = t.created_at,
        )
        for t in txns
    ]

    return TransactionListResponse(
        account_id   = account_id,
        transactions = items,
        total        = len(items),
    )
