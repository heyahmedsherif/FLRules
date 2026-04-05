"""
Google OAuth 2.0 authentication via authlib.

Flow:
  1. User clicks "Sign in with Google" → /auth/login
  2. Redirect to Google's consent screen
  3. Google redirects back to /auth/callback with auth code
  4. We exchange code for tokens, fetch user profile
  5. Create or update User in DB, store user_id in session
  6. Redirect to dashboard
"""

from datetime import datetime

from authlib.integrations.starlette_client import OAuth
from fastapi import HTTPException, Request
from sqlalchemy import select
from starlette.responses import RedirectResponse

from flrules.config import settings
from flrules.db import async_session
from flrules.models import User

# ── OAuth client setup ───────────────────────────────
oauth = OAuth()
oauth.register(
    name="google",
    client_id=settings.google_client_id,
    client_secret=settings.google_client_secret,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


async def get_current_user(request: Request) -> User | None:
    """Extract the logged-in user from the session. Returns None if not logged in."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None

    async with async_session() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()


async def require_login(request: Request) -> User:
    """Dependency that requires authentication. Redirects to login if not authenticated."""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/auth/login"})
    return user


async def require_admin(request: Request) -> User:
    """Dependency that requires admin role."""
    user = await require_login(request)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def is_admin_email(email: str) -> bool:
    """Check if an email is in the admin list."""
    admin_list = [e.strip().lower() for e in settings.admin_emails.split(",")]
    return email.lower() in admin_list


async def handle_login(request: Request):
    """Initiate Google OAuth flow."""
    redirect_uri = f"{settings.app_url}/auth/callback"
    return await oauth.google.authorize_redirect(request, redirect_uri)


async def handle_callback(request: Request):
    """Handle Google OAuth callback — create/update user, set session."""
    token = await oauth.google.authorize_access_token(request)
    userinfo = token.get("userinfo")
    if not userinfo:
        raise HTTPException(400, "Failed to get user info from Google")

    google_id = userinfo["sub"]
    email = userinfo["email"]
    name = userinfo.get("name", "")
    picture = userinfo.get("picture", "")

    async with async_session() as session:
        result = await session.execute(select(User).where(User.google_id == google_id))
        user = result.scalar_one_or_none()

        if user:
            user.name = name
            user.picture = picture
            user.last_login = datetime.utcnow()
        else:
            role = "admin" if is_admin_email(email) else "member"
            user = User(
                google_id=google_id,
                email=email,
                name=name,
                picture=picture,
                role=role,
            )
            session.add(user)

        await session.commit()
        await session.refresh(user)

        request.session["user_id"] = user.id

    return RedirectResponse(url="/", status_code=302)


async def handle_logout(request: Request):
    """Clear the session and redirect to login."""
    request.session.clear()
    return RedirectResponse(url="/auth/login", status_code=302)
