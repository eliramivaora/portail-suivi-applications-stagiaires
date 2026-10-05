import csv
import io
import json
import logging
import math
import re
import secrets
from datetime import date
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from portal.app import ROLE_LABELS, get_database, redirect, render
from portal.models import Application, ApplicationHistory, User
from portal.security import hash_password, needs_rehash, verify_password

logger = logging.getLogger(__name__)
router = APIRouter()
SERVICE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
VALID_ROLES = frozenset(ROLE_LABELS)
HEARTBEAT_WARNING_SECONDS = 60
HEARTBEAT_UNAVAILABLE_SECONDS = 300
VALID_HEALTH_STATES = frozenset(
    {"healthy", "warning", "unavailable", "no-data", "unknown"}
)
HEALTH_LABELS = {
    "healthy": "Opérationnel",
    "warning": "À surveiller",
    "unavailable": "Indisponible",
    "no-data": "Aucun signal",
    "unknown": "État inconnu",
}


def health_snapshot(status: str, age_seconds: float | None = None) -> dict:
    if status == "healthy":
        label = "Opérationnel"
        description = "Dernier battement de cœur reçu il y a moins d’une minute."
    elif status == "warning":
        label = "À surveiller"
        description = "Le dernier battement de cœur date de plus d’une minute."
    elif status == "unavailable":
        label = "Indisponible"
        description = "Aucun battement de cœur récent depuis plus de cinq minutes."
    elif status == "no-data":
        label = "Aucun signal"
        description = "Aucun battement de cœur n’a encore été reçu pour ce service."
    else:
        status = "unknown"
        label = "État inconnu"
        description = "Prometheus est indisponible; le statut ne peut pas être vérifié."
    return {
        "status": status,
        "label": label,
        "description": description,
        "age_seconds": age_seconds,
    }


async def fetch_application_health(
    service_names: list[str],
    prometheus_url: str,
) -> dict[str, dict]:
    if not service_names:
        return {}

    health = {
        service_name: health_snapshot("no-data")
        for service_name in service_names
    }
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(
                f"{prometheus_url}/api/v1/query",
                params={
                    "query": "time() - stagiaires_app_heartbeat_timestamp_seconds"
                },
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        logger.warning("Impossible de récupérer la santé applicative depuis Prometheus.", exc_info=True)
        return {
            service_name: health_snapshot("unknown")
            for service_name in service_names
        }

    if (
        not isinstance(payload, dict)
        or payload.get("status") != "success"
        or not isinstance(payload.get("data"), dict)
        or not isinstance(payload["data"].get("result"), list)
    ):
        logger.warning("Prometheus a renvoyé une réponse de santé invalide.")
        return {
            service_name: health_snapshot("unknown")
            for service_name in service_names
        }

    requested_services = set(service_names)
    for sample in payload["data"]["result"]:
        if not isinstance(sample, dict) or not isinstance(sample.get("metric"), dict):
            continue
        service_name = sample["metric"].get("service_name")
        value = sample.get("value")
        if (
            not isinstance(service_name, str)
            or service_name not in requested_services
            or not isinstance(value, list)
            or len(value) < 2
        ):
            continue
        try:
            age_seconds = max(0.0, float(value[1]))
        except (TypeError, ValueError):
            health[service_name] = health_snapshot("unknown")
            continue
        if not math.isfinite(age_seconds):
            health[service_name] = health_snapshot("unknown")
        else:
            previous_age = health[service_name]["age_seconds"]
            if previous_age is not None:
                age_seconds = min(age_seconds, previous_age)
            if age_seconds > HEARTBEAT_UNAVAILABLE_SECONDS:
                health[service_name] = health_snapshot("unavailable", age_seconds)
            elif age_seconds > HEARTBEAT_WARNING_SECONDS:
                health[service_name] = health_snapshot("warning", age_seconds)
            else:
                health[service_name] = health_snapshot("healthy", age_seconds)
    return health


def get_current_user(
    request: Request,
    session: Session = Depends(get_database),
) -> User:
    user_id = request.session.get("user_id")
    if not isinstance(user_id, int):
        raise HTTPException(status_code=401, detail="Connexion requise.")
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Connexion requise.")
    return user


def verify_csrf(
    request: Request,
    token: str | None = Form(default=None, alias="csrf_token"),
) -> None:
    expected = request.session.get("csrf_token")
    if not expected or token is None or not secrets_compare(expected, token):
        raise HTTPException(status_code=403, detail="Jeton de sécurité invalide. Rechargez la page.")


def secrets_compare(left: str, right: str) -> bool:
    return secrets.compare_digest(left, right)


def require_writer(user: User) -> None:
    if user.role not in {"admin", "stagiaire"}:
        raise HTTPException(status_code=403, detail="Votre rôle autorise uniquement la consultation.")


def get_visible_application(
    session: Session,
    application_id: int,
    user: User,
) -> Application:
    application = (
        session.query(Application)
        .options(joinedload(Application.owner))
        .filter(Application.id == application_id)
        .one_or_none()
    )
    if application is None or (
        user.role == "stagiaire" and application.owner_id != user.id
    ):
        raise HTTPException(status_code=404, detail="Application introuvable.")
    return application


def record_history(
    session: Session,
    application: Application,
    actor: User,
    action: str,
    changes: dict[str, dict[str, str | None]] | None = None,
) -> None:
    session.add(
        ApplicationHistory(
            application=application,
            actor_id=actor.id,
            action=action,
            changes_json=json.dumps(changes or {}, ensure_ascii=False),
        )
    )


def serialize_value(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def application_form_values(application: Application | None = None) -> dict:
    if application is None:
        return {
            "name": "",
            "service_name": "",
            "description": "",
            "language": "",
            "repository_url": "",
            "version": "",
            "internship_start": "",
            "internship_end": "",
            "owner_id": "",
        }
    return {
        "name": application.name,
        "service_name": application.service_name,
        "description": application.description,
        "language": application.language,
        "repository_url": application.repository_url,
        "version": application.version,
        "internship_start": serialize_value(application.internship_start) or "",
        "internship_end": serialize_value(application.internship_end) or "",
        "owner_id": str(application.owner_id),
    }


def application_form(
    request: Request,
    user: User,
    session: Session,
    application: Application | None = None,
    values: dict | None = None,
    error: str | None = None,
    status_code: int = 200,
):
    interns = (
        session.query(User)
        .filter(User.role == "stagiaire", User.is_active.is_(True))
        .order_by(User.name)
        .all()
        if user.role == "admin"
        else []
    )
    return render(
        request,
        "application_form.html",
        {
            "current_user": user,
            "application": application,
            "values": values or application_form_values(application),
            "interns": interns,
            "error": error,
            "page_title": "Modifier l’application" if application else "Nouvelle application",
        },
        status_code=status_code,
    )


def filtered_applications(
    session: Session,
    user: User,
    q: str,
    status: str,
    owner_id: int | None,
    period: str,
) -> list[Application]:
    if status not in {"active", "archived", "all"}:
        status = "active"
    if period not in {"all", "current", "future", "ended"}:
        period = "all"

    query = session.query(Application).options(joinedload(Application.owner))
    if user.role == "stagiaire":
        query = query.filter(Application.owner_id == user.id)
    if status == "active":
        query = query.filter(Application.archived.is_(False))
    elif status == "archived":
        query = query.filter(Application.archived.is_(True))
    if q:
        pattern = f"%{q.strip()}%"
        query = query.join(Application.owner).filter(
            or_(
                Application.name.ilike(pattern),
                Application.service_name.ilike(pattern),
                Application.description.ilike(pattern),
                User.name.ilike(pattern),
            )
        )
    elif user.role != "stagiaire":
        query = query.join(Application.owner)
    if owner_id is not None and user.role == "admin":
        query = query.filter(Application.owner_id == owner_id)
    today = date.today()
    if period == "current":
        query = query.filter(
            (Application.internship_start.is_(None) | (Application.internship_start <= today)),
            (Application.internship_end.is_(None) | (Application.internship_end >= today)),
        )
    elif period == "future":
        query = query.filter(Application.internship_start > today)
    elif period == "ended":
        query = query.filter(Application.internship_end < today)
    return query.order_by(Application.archived, Application.updated_at.desc()).all()


def safe_csv_cell(value: str | None) -> str:
    if value is None:
        return ""
    if re.match(r"^[\t\r\n ]*[=+\-@]", value):
        return f"'{value}"
    return value


def parse_date(value: str, label: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"La date « {label} » n’est pas valide.") from error


def validate_application(
    name: str,
    service_name: str,
    description: str,
    language: str,
    repository_url: str,
    version: str,
    internship_start: str,
    internship_end: str,
) -> tuple[dict, str | None]:
    values = {
        "name": name.strip(),
        "service_name": service_name.strip(),
        "description": description.strip(),
        "language": language.strip(),
        "repository_url": repository_url.strip(),
        "version": version.strip(),
        "internship_start": internship_start.strip(),
        "internship_end": internship_end.strip(),
    }
    if not values["name"]:
        return values, "Le nom de l’application est obligatoire."
    if not SERVICE_NAME_PATTERN.fullmatch(values["service_name"]):
        return values, "Le nom de service doit utiliser 1 à 120 lettres, chiffres, points, tirets ou tirets bas."
    if values["repository_url"]:
        parsed_url = urlparse(values["repository_url"])
        if parsed_url.scheme not in {"http", "https", "ssh"} or not parsed_url.netloc:
            return values, "Saisissez une adresse de dépôt HTTP(S) ou SSH valide."
    if len(values["description"]) > 4000:
        return values, "La description ne peut pas dépasser 4 000 caractères."
    if len(values["name"]) > 120 or len(values["service_name"]) > 120:
        return values, "Le nom et le nom de service sont limités à 120 caractères."
    if len(values["language"]) > 40 or len(values["version"]) > 80:
        return values, "Le langage est limité à 40 caractères et la version à 80."
    try:
        start = parse_date(values["internship_start"], "début du stage")
        end = parse_date(values["internship_end"], "fin du stage")
    except ValueError as error:
        return values, str(error)
    if start and end and end < start:
        return values, "La fin du stage doit être postérieure ou égale à son début."
    values["_start"] = start
    values["_end"] = end
    return values, None


@router.get("/", include_in_schema=False)
async def home(request: Request):
    if request.session.get("user_id"):
        return redirect(request, "/applications")
    return redirect(request, "/login")


@router.get("/login", include_in_schema=False)
async def login_page(
    request: Request,
    error: str | None = Query(default=None),
):
    if request.session.get("user_id"):
        return redirect(request, "/applications")
    return render(request, "login.html", {"error": error})


@router.post("/login", include_in_schema=False)
async def login(
    request: Request,
    session: Session = Depends(get_database),
    _: None = Depends(verify_csrf),
    email: str = Form(...),
    password: str = Form(...),
):
    normalized_email = email.strip().lower()
    user = session.query(User).filter(User.email == normalized_email).one_or_none()
    if user is None or not user.is_active or not verify_password(user.password_hash, password):
        return render(
            request,
            "login.html",
            {"error": "Adresse e-mail ou mot de passe incorrect."},
            status_code=400,
        )
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        session.commit()
    request.session.clear()
    request.session["user_id"] = user.id
    return redirect(request, "/applications", "Connexion réussie.")


@router.post("/logout", include_in_schema=False)
async def logout(
    request: Request,
    _: None = Depends(verify_csrf),
):
    request.session.clear()
    return redirect(request, "/login", "Vous êtes déconnecté.")


@router.get("/account/password", include_in_schema=False)
async def password_change_page(
    request: Request,
    user: User = Depends(get_current_user),
):
    return render(
        request,
        "password_form.html",
        {"current_user": user, "page_title": "Modifier mon mot de passe"},
    )


@router.post("/account/password", include_in_schema=False)
async def password_change(
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_database),
    _: None = Depends(verify_csrf),
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    if not verify_password(user.password_hash, current_password):
        return render(
            request,
            "password_form.html",
            {
                "current_user": user,
                "error": "Le mot de passe actuel est incorrect.",
                "page_title": "Modifier mon mot de passe",
            },
            status_code=422,
        )
    if len(new_password) < 12:
        error = "Le nouveau mot de passe doit contenir au moins 12 caractères."
    elif new_password != confirm_password:
        error = "Les nouveaux mots de passe ne correspondent pas."
    elif new_password == current_password:
        error = "Choisissez un nouveau mot de passe différent de l’actuel."
    else:
        error = None
    if error:
        return render(
            request,
            "password_form.html",
            {
                "current_user": user,
                "error": error,
                "page_title": "Modifier mon mot de passe",
            },
            status_code=422,
        )
    active_user = session.get(User, user.id)
    if active_user is None or not active_user.is_active:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Connexion requise.")
    active_user.password_hash = hash_password(new_password)
    session.commit()
    return redirect(request, "/applications", "Mot de passe modifié.")


@router.get("/applications", include_in_schema=False)
async def application_list(
    request: Request,
    session: Session = Depends(get_database),
    user: User = Depends(get_current_user),
    q: str = Query(default="", max_length=120),
    status: str = Query(default="active"),
    owner_id: int | None = Query(default=None),
    period: str = Query(default="all"),
    health: str = Query(default="all"),
):
    logger.info("Registre des applications consulté")
    if status not in {"active", "archived", "all"}:
        status = "active"
    if period not in {"all", "current", "future", "ended"}:
        period = "all"
    if health not in VALID_HEALTH_STATES | {"all"}:
        health = "all"
    applications = filtered_applications(
        session, user, q, status, owner_id, period
    )
    health_by_service = await fetch_application_health(
        [application.service_name for application in applications],
        request.app.state.prometheus_url,
    )
    if health != "all":
        applications = [
            application
            for application in applications
            if health_by_service[application.service_name]["status"] == health
        ]

    owner_scope = (
        session.query(Application.owner_id)
        .distinct()
        .subquery()
    )
    interns = (
        session.query(User)
        .filter(
            User.role == "stagiaire",
            User.is_active.is_(True),
            User.id.in_(session.query(owner_scope.c.owner_id)),
        )
        .order_by(User.name)
        .all()
        if user.role == "admin"
        else []
    )
    total = len(applications)
    active_count = sum(not application.archived for application in applications)
    archived_count = total - active_count
    return render(
        request,
        "applications.html",
        {
            "current_user": user,
            "applications": applications,
            "health_by_service": health_by_service,
            "interns": interns,
            "filters": {
                "q": q,
                "status": status,
                "owner_id": str(owner_id or "") if user.role == "admin" else "",
                "period": period,
                "health": health,
            },
            "active_count": active_count,
            "archived_count": archived_count,
            "page_title": "Applications suivies",
        },
    )


@router.get("/applications/export.csv", include_in_schema=False)
async def application_export(
    request: Request,
    session: Session = Depends(get_database),
    user: User = Depends(get_current_user),
    q: str = Query(default="", max_length=120),
    status: str = Query(default="active"),
    owner_id: int | None = Query(default=None),
    period: str = Query(default="all"),
    health: str = Query(default="all"),
):
    if health not in VALID_HEALTH_STATES | {"all"}:
        health = "all"
    applications = filtered_applications(
        session, user, q, status, owner_id, period
    )
    health_by_service = await fetch_application_health(
        [application.service_name for application in applications],
        request.app.state.prometheus_url,
    )
    if health != "all":
        applications = [
            application
            for application in applications
            if health_by_service[application.service_name]["status"] == health
        ]

    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";", lineterminator="\r\n")
    writer.writerow(
        [
            "Application",
            "Service OpenTelemetry",
            "Responsable",
            "État de supervision",
            "Langage",
            "Version",
            "Dépôt Git",
            "Début du stage",
            "Fin du stage",
            "Archivage",
        ]
    )
    for application in applications:
        writer.writerow(
            safe_csv_cell(value)
            for value in (
                application.name,
                application.service_name,
                application.owner.name,
                HEALTH_LABELS[health_by_service[application.service_name]["status"]],
                application.language,
                application.version,
                application.repository_url,
                serialize_value(application.internship_start),
                serialize_value(application.internship_end),
                "Archivée" if application.archived else "En suivi",
            )
        )
    content = "\ufeff" + output.getvalue()
    return Response(
        content,
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="applications-suivies.csv"'
        },
    )


@router.get("/applications/new", include_in_schema=False)
async def application_new_page(
    request: Request,
    session: Session = Depends(get_database),
    user: User = Depends(get_current_user),
):
    require_writer(user)
    return application_form(request, user, session)


@router.post("/applications/new", include_in_schema=False)
async def application_create(
    request: Request,
    session: Session = Depends(get_database),
    user: User = Depends(get_current_user),
    _: None = Depends(verify_csrf),
    name: str = Form(...),
    service_name: str = Form(...),
    description: str = Form(default=""),
    language: str = Form(default=""),
    repository_url: str = Form(default=""),
    version: str = Form(default=""),
    internship_start: str = Form(default=""),
    internship_end: str = Form(default=""),
    owner_id: str = Form(default=""),
):
    require_writer(user)
    values, error = validate_application(
        name,
        service_name,
        description,
        language,
        repository_url,
        version,
        internship_start,
        internship_end,
    )
    owner = user
    if user.role == "admin":
        try:
            owner = session.get(User, int(owner_id))
        except (TypeError, ValueError):
            owner = None
        if owner is None or owner.role != "stagiaire" or not owner.is_active:
            error = "Sélectionnez un compte stagiaire actif comme responsable."
    if error:
        return application_form(request, user, session, values=values, error=error, status_code=422)
    if session.query(Application.id).filter(Application.service_name == values["service_name"]).first():
        return application_form(
            request,
            user,
            session,
            values=values,
            error="Ce nom de service OpenTelemetry est déjà utilisé.",
            status_code=409,
        )
    application = Application(
        name=values["name"],
        service_name=values["service_name"],
        description=values["description"],
        language=values["language"],
        repository_url=values["repository_url"],
        version=values["version"],
        internship_start=values["_start"],
        internship_end=values["_end"],
        owner=owner,
    )
    session.add(application)
    record_history(session, application, user, "created")
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        logger.exception("Conflit lors de la création d’une application")
        return application_form(
            request,
            user,
            session,
            values=values,
            error="Le nom de service est déjà enregistré. Rechargez la page et réessayez.",
            status_code=409,
        )
    return redirect(request, f"/applications/{application.id}", "Application enregistrée.")


@router.get("/applications/{application_id}", include_in_schema=False)
async def application_detail(
    application_id: int,
    request: Request,
    session: Session = Depends(get_database),
    user: User = Depends(get_current_user),
):
    application = get_visible_application(session, application_id, user)
    health = (
        await fetch_application_health(
            [application.service_name],
            request.app.state.prometheus_url,
        )
    )[application.service_name]
    history_entries = (
        session.query(ApplicationHistory)
        .options(joinedload(ApplicationHistory.actor))
        .filter(ApplicationHistory.application_id == application.id)
        .order_by(ApplicationHistory.created_at.desc())
        .limit(20)
        .all()
    )
    return render(
        request,
        "application_detail.html",
        {
            "current_user": user,
            "application": application,
            "health": health,
            "history_entries": history_entries,
            "can_edit": user.role == "admin"
            or (user.role == "stagiaire" and application.owner_id == user.id),
            "page_title": application.name,
        },
    )


@router.get("/applications/{application_id}/health", include_in_schema=False)
async def application_health(
    application_id: int,
    request: Request,
    session: Session = Depends(get_database),
    user: User = Depends(get_current_user),
):
    application = get_visible_application(session, application_id, user)
    health = (
        await fetch_application_health(
            [application.service_name],
            request.app.state.prometheus_url,
        )
    )[application.service_name]
    return JSONResponse(health)


@router.get("/applications/{application_id}/edit", include_in_schema=False)
async def application_edit_page(
    application_id: int,
    request: Request,
    session: Session = Depends(get_database),
    user: User = Depends(get_current_user),
):
    require_writer(user)
    application = get_visible_application(session, application_id, user)
    return application_form(request, user, session, application=application)


@router.post("/applications/{application_id}/edit", include_in_schema=False)
async def application_update(
    application_id: int,
    request: Request,
    session: Session = Depends(get_database),
    user: User = Depends(get_current_user),
    _: None = Depends(verify_csrf),
    name: str = Form(...),
    service_name: str = Form(...),
    description: str = Form(default=""),
    language: str = Form(default=""),
    repository_url: str = Form(default=""),
    version: str = Form(default=""),
    internship_start: str = Form(default=""),
    internship_end: str = Form(default=""),
    owner_id: str = Form(default=""),
):
    require_writer(user)
    application = get_visible_application(session, application_id, user)
    values, error = validate_application(
        name,
        service_name,
        description,
        language,
        repository_url,
        version,
        internship_start,
        internship_end,
    )
    owner = application.owner
    if user.role == "admin":
        try:
            owner = session.get(User, int(owner_id))
        except (TypeError, ValueError):
            owner = None
        if owner is None or owner.role != "stagiaire" or not owner.is_active:
            error = "Sélectionnez un compte stagiaire actif comme responsable."
    if error:
        return application_form(
            request,
            user,
            session,
            application=application,
            values=values,
            error=error,
            status_code=422,
        )
    duplicate = (
        session.query(Application.id)
        .filter(
            Application.service_name == values["service_name"],
            Application.id != application.id,
        )
        .first()
    )
    if duplicate:
        return application_form(
            request,
            user,
            session,
            application=application,
            values=values,
            error="Ce nom de service OpenTelemetry est déjà utilisé.",
            status_code=409,
        )

    updated = {
        "name": values["name"],
        "service_name": values["service_name"],
        "description": values["description"],
        "language": values["language"],
        "repository_url": values["repository_url"],
        "version": values["version"],
        "internship_start": values["_start"],
        "internship_end": values["_end"],
        "owner_id": owner.id,
    }
    changes = {}
    for field, new_value in updated.items():
        old_value = getattr(application, field)
        if old_value != new_value:
            changes[field] = {
                "old": serialize_value(old_value),
                "new": serialize_value(new_value),
            }
            setattr(application, field, new_value)
    if changes:
        record_history(session, application, user, "updated", changes)
        session.commit()
    return redirect(request, f"/applications/{application.id}", "Modifications enregistrées.")


@router.post("/applications/{application_id}/archive", include_in_schema=False)
async def application_archive(
    application_id: int,
    request: Request,
    session: Session = Depends(get_database),
    user: User = Depends(get_current_user),
    _: None = Depends(verify_csrf),
):
    require_writer(user)
    application = get_visible_application(session, application_id, user)
    archived = not application.archived
    application.archived = archived
    record_history(session, application, user, "archived" if archived else "restored")
    session.commit()
    message = "Application archivée." if archived else "Application réactivée."
    return redirect(request, f"/applications/{application.id}", message)


@router.get("/users", include_in_schema=False)
async def user_list(
    request: Request,
    session: Session = Depends(get_database),
    user: User = Depends(get_current_user),
):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Réservé aux administrateurs.")
    users = session.query(User).order_by(User.role, User.name).all()
    return render(
        request,
        "users.html",
        {
            "current_user": user,
            "users": users,
            "page_title": "Comptes utilisateurs",
        },
    )


@router.get("/users/new", include_in_schema=False)
async def user_new_page(
    request: Request,
    user: User = Depends(get_current_user),
):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Réservé aux administrateurs.")
    return render(
        request,
        "user_form.html",
        {
            "current_user": user,
            "values": {"name": "", "email": "", "role": "stagiaire"},
            "page_title": "Créer un compte",
        },
    )


@router.post("/users/new", include_in_schema=False)
async def user_create(
    request: Request,
    session: Session = Depends(get_database),
    user: User = Depends(get_current_user),
    _: None = Depends(verify_csrf),
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Réservé aux administrateurs.")
    values = {"name": name.strip(), "email": email.strip().lower(), "role": role}
    error = None
    if not values["name"] or len(values["name"]) > 120:
        error = "Le nom est obligatoire et limité à 120 caractères."
    elif len(values["email"]) > 254 or "@" not in values["email"]:
        error = "Saisissez une adresse e-mail valide."
    elif role not in VALID_ROLES:
        error = "Sélectionnez un rôle autorisé."
    elif len(password) < 12:
        error = "Le mot de passe temporaire doit contenir au moins 12 caractères."
    elif session.query(User.id).filter(User.email == values["email"]).first():
        error = "Un compte utilise déjà cette adresse e-mail."
    if error:
        return render(
            request,
            "user_form.html",
            {
                "current_user": user,
                "values": values,
                "error": error,
                "page_title": "Créer un compte",
            },
            status_code=422 if "déjà" not in error else 409,
        )
    new_user = User(
        name=values["name"],
        email=values["email"],
        role=role,
        password_hash=hash_password(password),
    )
    session.add(new_user)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        logger.exception("Conflit lors de la création d’un utilisateur")
        return render(
            request,
            "user_form.html",
            {
                "current_user": user,
                "values": values,
                "error": "Cette adresse e-mail est déjà utilisée.",
                "page_title": "Créer un compte",
            },
            status_code=409,
        )
    return redirect(request, "/users", f"Compte créé pour {new_user.name}.")


@router.post("/users/{user_id}/toggle", include_in_schema=False)
async def user_toggle(
    user_id: int,
    request: Request,
    session: Session = Depends(get_database),
    user: User = Depends(get_current_user),
    _: None = Depends(verify_csrf),
):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Réservé aux administrateurs.")
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Compte introuvable.")
    if target.id == user.id:
        raise HTTPException(status_code=400, detail="Vous ne pouvez pas désactiver votre propre compte.")
    if target.role == "admin" and target.is_active:
        admins_remaining = (
            session.query(func.count(User.id))
            .filter(User.role == "admin", User.is_active.is_(True))
            .scalar()
        )
        if admins_remaining <= 1:
            raise HTTPException(status_code=400, detail="Le dernier administrateur actif ne peut pas être désactivé.")
    target.is_active = not target.is_active
    session.commit()
    message = "Compte activé." if target.is_active else "Compte désactivé."
    return redirect(request, "/users", message)
