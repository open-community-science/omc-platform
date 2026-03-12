"""GitHub OAuth authentication."""
from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import RedirectResponse
from authlib.integrations.starlette_client import OAuth
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime

from .config import get_settings
from .database import get_db, User

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()

# OAuth setup
oauth = OAuth()
oauth.register(
    name="github",
    client_id=settings.github_client_id,
    client_secret=settings.github_client_secret,
    access_token_url="https://github.com/login/oauth/access_token",
    authorize_url="https://github.com/login/oauth/authorize",
    api_base_url="https://api.github.com/",
    client_kwargs={"scope": "user:email read:user"},
)


@router.get("/login")
async def login(request: Request):
    """Redirect to GitHub OAuth."""
    if not settings.github_client_id:
        raise HTTPException(status_code=500, detail="GitHub OAuth not configured")
    redirect_uri = settings.github_redirect_uri
    return await oauth.github.authorize_redirect(request, redirect_uri)


@router.get("/callback")
async def auth_callback(request: Request, db: AsyncSession = Depends(get_db)):
    """Handle GitHub OAuth callback."""
    try:
        token = await oauth.github.authorize_access_token(request)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"OAuth error: {str(e)}")

    # Get user info from GitHub
    resp = await oauth.github.get("user", token=token)
    github_user = resp.json()

    # Find or create user
    stmt = select(User).where(User.github_id == github_user["id"])
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user:
        # Update existing user
        user.github_login = github_user["login"]
        user.github_name = github_user.get("name")
        user.github_email = github_user.get("email")
        user.github_avatar_url = github_user.get("avatar_url")
        user.last_login = datetime.utcnow()
    else:
        # Create new user
        user = User(
            github_id=github_user["id"],
            github_login=github_user["login"],
            github_name=github_user.get("name"),
            github_email=github_user.get("email"),
            github_avatar_url=github_user.get("avatar_url"),
        )
        db.add(user)

    await db.commit()

    # Store user in session
    request.session["user_id"] = user.id
    request.session["github_login"] = user.github_login
    request.session["github_avatar"] = user.github_avatar_url

    return RedirectResponse(url="/dashboard", status_code=302)


@router.get("/logout")
async def logout(request: Request):
    """Clear session and logout."""
    request.session.clear()
    return RedirectResponse(url="/", status_code=302)


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User | None:
    """Get current logged-in user from session."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None

    stmt = select(User).where(User.id == user_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def require_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    """Require authenticated user - raises 401 if not logged in."""
    user = await get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user
