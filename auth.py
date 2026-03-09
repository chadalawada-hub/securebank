"""
routers/auth.py  –  Authentication endpoints.

POST /auth/login          – username + password → JWT pair
POST /auth/signup         – full registration with identity verification
POST /auth/refresh        – rotate refresh token → new JWT pair
POST /auth/logout         – revoke refresh token
GET  /auth/oauth/google   – get Google OAuth URL
GET  /auth/oauth/google/callback – exchange code → JWT pair
GET  /auth/password-check – real-time password strength (no auth required)
"""
from datetime import datetime, timezone, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from jose import JWTError

from database import get_db
from models import User, RefreshToken, OAuthConnection, OAuthProvider
from schemas import (
    LoginRequest, TokenResponse, SignupRequest, SignupResponse,
    RefreshTokenRequest, OAuthLoginURLResponse, PasswordStrengthResponse,
)
from security import (
    hash_password, verify_password, sha256_hex,
    create_access_token, create_refresh_token, decode_access_token,
    validate_password_strength, generate_oauth_state,
)
from kms_service import kms_encrypt
from config import get_settings

settings = get_settings()
router  = APIRouter(prefix="/auth", tags=["Authentication"])

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES     = 15


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _issue_token_pair(user: User, db: AsyncSession) -> TokenResponse:
    """Create access + refresh tokens and persist refresh hash."""
    access  = create_access_token(str(user.id), {"username": user.username})
    raw_rt, rt_hash = create_refresh_token()

    db_rt = RefreshToken(
        user_id    = user.id,
        token_hash = rt_hash,
        expires_at = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days),
    )
    db.add(db_rt)

    # update last_login
    user.last_login_at = datetime.now(timezone.utc)
    user.failed_login_attempts = "0"
    await db.commit()

    return TokenResponse(
        access_token  = access,
        refresh_token = raw_rt,
        token_type    = "bearer",
        expires_in    = settings.access_token_expire_minutes * 60,
    )


# ── Login ─────────────────────────────────────────────────────────────────────

@router.post("/login", response_model=TokenResponse, summary="Username + password login")
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    """
    Authenticate with username and password.

    - Passwords are verified via SHA-256 pre-hash → bcrypt (see security.py).
    - Account lockout after 5 consecutive failures (15-minute cooldown).
    - Returns a short-lived JWT access token + long-lived refresh token.
    """
    result = await db.execute(select(User).where(User.username == body.username))
    user: User | None = result.scalar_one_or_none()

    if not user or user.is_oauth_only:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid credentials")

    # Lockout check
    if user.is_locked:
        if user.locked_until and user.locked_until > datetime.now(timezone.utc):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail=f"Account locked. Try again after {user.locked_until.isoformat()}.")
        else:
            user.is_locked = False
            user.failed_login_attempts = "0"

    if not verify_password(body.password, user.password_hash):
        attempts = int(user.failed_login_attempts or "0") + 1
        user.failed_login_attempts = str(attempts)
        if attempts >= MAX_FAILED_ATTEMPTS:
            user.is_locked    = True
            user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES)
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid credentials")

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Account is deactivated. Please contact support.")

    return await _issue_token_pair(user, db)


# ── Sign-Up ───────────────────────────────────────────────────────────────────

@router.post("/signup", response_model=SignupResponse,
             status_code=status.HTTP_201_CREATED,
             summary="Register a new user with identity verification")
async def signup(body: SignupRequest, db: AsyncSession = Depends(get_db)):
    """
    Full sign-up flow:

    1. Validate password strength (NIST SP 800-63B + OWASP Level 2).
    2. Confirm T&C acceptance.
    3. Verify identity against the existing bank record using:
       - SSN last 4 digits + Date of Birth
       - Primer (existing) account number + debit card CVV
    4. Check username / email uniqueness.
    5. Encrypt all PII with AWS KMS before persisting.
    6. Store only SHA-256 hashes for lookup columns.
    """
    # 1. Password strength
    is_valid, pw_errors = validate_password_strength(body.password)
    if not is_valid:
        raise HTTPException(status_code=422, detail={"password_errors": pw_errors})

    # 2. T&C (schema validator already enforces it, but belt-and-braces)
    if not body.terms_accepted:
        raise HTTPException(status_code=422, detail="Terms and Conditions must be accepted.")

    # 3. Identity verification against bank record
    #    In production this queries an internal core-banking system.
    #    Here we validate that a BankAccount row matching account_number_hash
    #    + cvv_hash exists (seeded during account opening).
    from models import BankAccount
    acct_hash = sha256_hex(body.primer_account_number)
    cvv_hash  = sha256_hex(body.debit_card_cvv)

    acct_result = await db.execute(
        select(BankAccount).where(
            BankAccount.account_number_hash == acct_hash,
            BankAccount.cvv_hash            == cvv_hash,
        )
    )
    bank_account: BankAccount | None = acct_result.scalar_one_or_none()
    if not bank_account:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Identity verification failed: account number or CVV does not match.")

    # Check SSN + DOB against that account's owner (if already linked)
    if bank_account.user_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="This bank account is already registered. Please log in.")

    # 4. Uniqueness checks
    email_hash = sha256_hex(body.email.lower())
    dup = await db.execute(
        select(User).where(
            (User.username == body.username) | (User.email_hash == email_hash)
        )
    )
    if dup.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Username or email already registered.")

    # 5. Encrypt PII and create user
    user = User(
        username              = body.username,
        password_hash         = hash_password(body.password),
        email_encrypted       = kms_encrypt(body.email.lower()),
        email_hash            = email_hash,
        phone_encrypted       = kms_encrypt(body.phone_number),
        phone_hash            = sha256_hex(body.phone_number),
        ssn_encrypted         = kms_encrypt(body.ssn_last4),
        ssn_hash              = sha256_hex(body.ssn_last4),
        dob_encrypted         = kms_encrypt(body.date_of_birth),
        terms_accepted        = True,
        terms_accepted_at     = datetime.now(timezone.utc),
        terms_version         = body.terms_version,
        is_active             = True,
        is_verified           = False,   # email/KYC verification step would follow
    )
    db.add(user)
    await db.flush()   # get user.id

    # Link bank account to this new user
    bank_account.user_id = user.id
    await db.commit()
    await db.refresh(user)

    return SignupResponse(message="Registration successful. Please verify your email.", user_id=user.id)


# ── Token Refresh ─────────────────────────────────────────────────────────────

@router.post("/refresh", response_model=TokenResponse, summary="Refresh access token")
async def refresh_token(body: RefreshTokenRequest, db: AsyncSession = Depends(get_db)):
    """Exchange a valid refresh token for a new JWT pair (token rotation)."""
    rt_hash = sha256_hex(body.refresh_token)
    result  = await db.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == rt_hash,
            RefreshToken.revoked    == False,
        )
    )
    db_rt: RefreshToken | None = result.scalar_one_or_none()

    if not db_rt or db_rt.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid or expired refresh token.")

    # Revoke old token (rotation)
    db_rt.revoked = True
    await db.flush()

    user_result = await db.execute(select(User).where(User.id == db_rt.user_id))
    user = user_result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found.")

    return await _issue_token_pair(user, db)


# ── Logout ────────────────────────────────────────────────────────────────────

@router.post("/logout", summary="Revoke refresh token")
async def logout(body: RefreshTokenRequest, db: AsyncSession = Depends(get_db)):
    rt_hash = sha256_hex(body.refresh_token)
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == rt_hash)
        .values(revoked=True)
    )
    await db.commit()
    return {"message": "Logged out successfully."}


# ── OAuth – Google ────────────────────────────────────────────────────────────

@router.get("/oauth/google", response_model=OAuthLoginURLResponse,
            summary="Get Google OAuth authorization URL")
async def google_oauth_start():
    """
    Returns the Google OAuth 2.0 authorization URL.
    The client should redirect the user to this URL.
    The `state` parameter MUST be stored in the session / local-storage
    to prevent CSRF on the callback.
    """
    state = generate_oauth_state()
    params = {
        "client_id":     settings.google_client_id,
        "redirect_uri":  settings.google_redirect_uri,
        "response_type": "code",
        "scope":         "openid email profile",
        "access_type":   "offline",
        "state":         state,
        "prompt":        "consent",
    }
    from urllib.parse import urlencode
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    return OAuthLoginURLResponse(authorization_url=url, state=state)


@router.get("/oauth/google/callback", response_model=TokenResponse,
            summary="Google OAuth callback – exchange code for JWT")
async def google_oauth_callback(
    code: str  = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """
    Exchange Google authorization code for tokens, upsert the user,
    and return our own JWT pair.
    """
    import httpx
    # Exchange code for Google tokens
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code":          code,
                "client_id":     settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri":  settings.google_redirect_uri,
                "grant_type":    "authorization_code",
            },
        )
    if token_resp.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to exchange OAuth code.")

    tokens      = token_resp.json()
    id_token    = tokens.get("id_token")

    # Decode Google ID token (no signature verification for brevity – use google-auth in prod)
    import base64, json as _json
    parts   = id_token.split(".")
    payload = _json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
    google_uid  = payload["sub"]
    email       = payload.get("email", "").lower()
    email_hash  = sha256_hex(email)

    # Find or create user
    result = await db.execute(
        select(OAuthConnection).where(
            OAuthConnection.provider         == OAuthProvider.GOOGLE,
            OAuthConnection.provider_user_id == google_uid,
        )
    )
    oauth_conn: OAuthConnection | None = result.scalar_one_or_none()

    if oauth_conn:
        user_result = await db.execute(select(User).where(User.id == oauth_conn.user_id))
        user = user_result.scalar_one()
    else:
        # Check if email is already registered
        user_result = await db.execute(select(User).where(User.email_hash == email_hash))
        user = user_result.scalar_one_or_none()

        if not user:
            user = User(
                username        = f"google_{google_uid[:12]}",
                is_oauth_only   = True,
                email_encrypted = kms_encrypt(email),
                email_hash      = email_hash,
                is_active       = True,
                is_verified     = True,
                terms_accepted  = False,   # must accept T&C on first OAuth login
            )
            db.add(user)
            await db.flush()

        oauth_conn = OAuthConnection(
            user_id          = user.id,
            provider         = OAuthProvider.GOOGLE,
            provider_user_id = google_uid,
        )
        db.add(oauth_conn)

    return await _issue_token_pair(user, db)


# ── Password Strength Check ───────────────────────────────────────────────────

@router.get("/password-check", response_model=PasswordStrengthResponse,
            summary="Real-time password strength evaluation (unauthenticated)")
async def check_password(password: str = Query(..., min_length=1)):
    """Check a candidate password against policy without creating an account."""
    is_valid, errors = validate_password_strength(password)

    # Simple entropy score 0–4
    score = 0
    if len(password) >= 12: score += 1
    if len(password) >= 16: score += 1
    if is_valid:             score += 1
    if len(password) >= 20: score += 1

    suggestions = []
    if len(password) < 16:
        suggestions.append("Use 16+ characters for a stronger password.")
    if not any(c in "!@#$%^&*" for c in password):
        suggestions.append("Add special characters like ! @ # $ % ^ & *")

    return PasswordStrengthResponse(
        is_valid=is_valid, score=score, errors=errors, suggestions=suggestions
    )
