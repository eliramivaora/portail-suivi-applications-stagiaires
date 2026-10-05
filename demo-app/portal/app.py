import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from portal.models import Base, User
from portal.security import hash_password

ROLE_LABELS = {
    "admin": "Administrateur",
    "stagiaire": "Stagiaire",
    "lecteur": "Lecteur",
}
PORTAL_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(PORTAL_DIR / "templates"))
TEMPLATES.env.filters["loads"] = json.loads


def _create_engine(database_url: str) -> Engine:
    connect_args: dict[str, Any] = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine = create_engine(
        database_url,
        connect_args=connect_args,
        pool_pre_ping=True,
    )

    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def set_sqlite_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


def create_app(
    database_url: str | None = None,
    session_secret_key: str | None = None,
    initial_admin: dict[str, str] | None = None,
) -> FastAPI:
    resolved_database_url = database_url or os.getenv(
        "DATABASE_URL", "sqlite:////data/portal.db"
    )
    resolved_secret_key = session_secret_key or os.getenv("SESSION_SECRET_KEY")
    if not resolved_secret_key or len(resolved_secret_key) < 32:
        raise RuntimeError("SESSION_SECRET_KEY doit contenir au moins 32 caractères.")

    admin = initial_admin or {
        "name": os.getenv("PORTAL_ADMIN_NAME", "Administrateur"),
        "email": os.getenv("PORTAL_ADMIN_EMAIL", ""),
        "password": os.getenv("PORTAL_ADMIN_PASSWORD", ""),
    }
    normalized_email = admin["email"].strip().lower()
    if not normalized_email or "@" not in normalized_email:
        raise RuntimeError("PORTAL_ADMIN_EMAIL doit être une adresse valide.")
    if len(admin["password"]) < 12:
        raise RuntimeError("PORTAL_ADMIN_PASSWORD doit contenir au moins 12 caractères.")

    engine = _create_engine(resolved_database_url)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        Base.metadata.create_all(engine)
        with session_factory() as session:
            existing_admin = (
                session.query(User)
                .filter(User.email == normalized_email)
                .one_or_none()
            )
            if existing_admin is None:
                session.add(
                    User(
                        name=admin["name"].strip() or "Administrateur",
                        email=normalized_email,
                        password_hash=hash_password(admin["password"]),
                        role="admin",
                    )
                )
                session.commit()
            elif existing_admin.role != "admin":
                raise RuntimeError(
                    "L'adresse PORTAL_ADMIN_EMAIL est déjà utilisée par un compte non administrateur."
                )

        application.state.session_factory = session_factory
        application.state.engine = engine
        yield
        engine.dispose()

    application = FastAPI(
        title="Portail de suivi des applications",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    application.add_middleware(
        SessionMiddleware,
        secret_key=resolved_secret_key,
        session_cookie="stagiaires_session",
        max_age=60 * 60 * 8,
        same_site="lax",
        https_only=False,
    )
    application.mount(
        "/static",
        StaticFiles(directory=str(PORTAL_DIR / "static")),
        name="static",
    )
    application.state.prometheus_url = os.getenv(
        "PROMETHEUS_URL", "http://prometheus:9090"
    ).rstrip("/")
    from portal.web import router

    application.include_router(router)

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "form-action 'self'; frame-ancestors 'self'; base-uri 'self'; object-src 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @application.get("/health", include_in_schema=False)
    async def health():
        return {"status": "ok"}

    @application.get("/demo/error", include_in_schema=False)
    async def demo_error():
        from fastapi import HTTPException

        raise HTTPException(status_code=500, detail="Erreur de démonstration")

    @application.get("/demo/slow", include_in_schema=False)
    async def demo_slow(seconds: float = 1.2):
        import asyncio

        duration = min(max(seconds, 0.0), 5.0)
        await asyncio.sleep(duration)
        return {"simulated_duration_seconds": duration}

    return application


def get_database(request: Request):
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise RuntimeError("La base de données du portail n'est pas initialisée.")
    with factory() as session:
        yield session


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def render(
    request: Request,
    template: str,
    context: dict[str, Any] | None = None,
    status_code: int = 200,
):
    values = dict(context or {})
    values.setdefault("current_user", None)
    values.setdefault("role_labels", ROLE_LABELS)
    values.setdefault("csrf_token", csrf_token(request))
    values.setdefault("flash", request.session.pop("flash", None))
    return TEMPLATES.TemplateResponse(
        request=request,
        name=template,
        context=values,
        status_code=status_code,
    )


def redirect(
    request: Request,
    path: str,
    message: str | None = None,
) -> RedirectResponse:
    if message:
        request.session["flash"] = message
    return RedirectResponse(path, status_code=303)
