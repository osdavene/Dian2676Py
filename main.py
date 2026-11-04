from urllib.parse import quote
from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from app.security import make_sso_token
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
import uvicorn

from app.config import BASE_DIR, settings
from app.database import SessionLocal
from app.security import ensure_default_users
from app.routers import auth, dashboard, exams, admin, ai


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = SessionLocal()
    try:
        ensure_default_users(db)
    except Exception as exc:
        print(f"[Startup] Aviso al verificar usuarios por defecto: {exc}")
    finally:
        db.close()
    yield




class CanonicalDomainMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        canonical = (
            settings.APP_URL.rstrip("/")
            if settings.APP_URL and "127.0.0.1" not in settings.APP_URL and "localhost" not in settings.APP_URL
            else ""
        )
        host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").lower()
        path = request.url.path

        # If accessed through duckdns.org or any non-canonical domain
        if canonical and "servigest.online" in canonical and "duckdns.org" in host:
            # Let Google OAuth callback pass so token can be received and processed
            if path.startswith("/auth/google"):
                return await call_next(request)

            # If user has an active session on duckdns, transfer them seamlessly to servigest.online
            session = request.scope.get("session") or {}
            user_id = session.get("user_id")
            if user_id:
                token = make_sso_token(user_id)
                safe_path = path if path.startswith("/") and not path.startswith("//") else "/dashboard"
                target = f"{canonical}/auth/sso-transfer?token={token}&next_url={quote(safe_path)}"
                return RedirectResponse(url=target, status_code=302)

            # If not logged in, permanently redirect to canonical servigest.online
            target = f"{canonical}{path}"
            if request.url.query:
                target += f"?{request.url.query}"
            return RedirectResponse(url=target, status_code=301)

        return await call_next(request)


app = FastAPI(
    title="Simulador Oficial Concurso DIAN 2676 — CNSC (Python / FastAPI)",
    description="Plataforma de preparación y simulacros para la Convocatoria DIAN 2676 conectada a PostgreSQL.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(CanonicalDomainMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SECRET_KEY,
    session_cookie="dian2676_py_session",
    max_age=86400 * 7,
    same_site="lax",
)

# Montar directorios estáticos compatibles con las rutas de Laravel public/
static_dir = BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
if (static_dir / "css").exists():
    app.mount("/css", StaticFiles(directory=str(static_dir / "css")), name="css")
if (static_dir / "js").exists():
    app.mount("/js", StaticFiles(directory=str(static_dir / "js")), name="js")
if (static_dir / "webfonts").exists():
    app.mount(
        "/webfonts",
        StaticFiles(directory=str(static_dir / "webfonts")),
        name="webfonts",
    )


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    fav_path = static_dir / "favicon.ico"
    if fav_path.exists():
        return FileResponse(str(fav_path))
    return FileResponse(str(static_dir / "css" / "app.css"))


@app.get("/tailwind2.min.css", include_in_schema=False)
def tailwind2_css():
    tw_path = static_dir / "tailwind2.min.css"
    return FileResponse(str(tw_path), media_type="text/css")


# Incluir todos los módulos de rutas
app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(exams.router)
app.include_router(admin.router)
app.include_router(ai.router)


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
