"""
main.py  –  FastAPI application entry point.

Run:
  uvicorn main:app --reload --port 8000
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from config import get_settings
from database import engine
from models import Base
from routers import auth, accounts

settings = get_settings()

# ── Rate Limiter ──────────────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])


# ── Lifespan (DB table creation) ──────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="SecureBank API",
    version="1.0.0",
    description="""
## SecureBank REST API

A production-grade banking backend with:
- **OAuth 2.0** (Google) and username/password login
- **JWT** access tokens (30 min) + refresh tokens (7 days, rotated)
- **AWS KMS** envelope encryption for all PII at rest
- **SHA-256** password pre-hash + **bcrypt** (cost=12) for final storage
- **Identity verification** during signup (SSN + DOB + account number + CVV)
- **RBAC-ready** with role claims in JWT

### Authentication
Use the `Authorization: Bearer <token>` header, or the `access_token` HttpOnly cookie for web clients.
    """,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── Middleware ────────────────────────────────────────────────────────────────
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins    = settings.cors_origins,
    allow_credentials= True,
    allow_methods    = ["*"],
    allow_headers    = ["*"],
)

if settings.app_env == "production":
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["yourdomain.com", "*.yourdomain.com"])


# ── Security Headers ──────────────────────────────────────────────────────────
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"]    = "nosniff"
    response.headers["X-Frame-Options"]           = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    response.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"]        = "camera=(), microphone=(), geolocation=()"
    return response


# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(auth.router,     prefix="/api/v1")
app.include_router(accounts.router, prefix="/api/v1")


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health", tags=["Health"])
async def health():
    return {"status": "ok", "service": settings.app_name}


@app.get("/", tags=["Health"])
async def root():
    return {"message": f"Welcome to {settings.app_name} API", "docs": "/docs"}
