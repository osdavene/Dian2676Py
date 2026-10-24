from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
import uvicorn

from app.config import BASE_DIR, settings
from app.database import SessionLocal
from app.security import ensure_default_users
from app.routers import auth, dashboard, exams, admin


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


app = FastAPI(
    title="Simulador Oficial Concurso DIAN 2676 — CNSC (Python / FastAPI)",
    description="Plataforma de preparación y simulacros para la Convocatoria DIAN 2676 conectada a PostgreSQL.",
    version="1.0.0",
    lifespan=lifespan,
)

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


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
