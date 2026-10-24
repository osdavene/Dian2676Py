import hashlib
import secrets
from datetime import datetime
from typing import Optional, Dict, Any, List
import bcrypt
from fastapi import Request
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.config import settings
from app.models import User, JobProfile


def hash_password(plain_password: str) -> str:
    salt = bcrypt.gensalt(rounds=12)
    hashed = bcrypt.hashpw(plain_password.encode("utf-8"), salt).decode("utf-8")
    # Use $2y$ prefix for 100% compatibility with PHP/Laravel password_verify
    if hashed.startswith("$2b$"):
        hashed = "$2y$" + hashed[4:]
    return hashed


def verify_password(plain_password: str, hashed_password: Optional[str]) -> bool:
    if not hashed_password or not plain_password:
        return False
    try:
        hp = hashed_password
        if hp.startswith("$2y$"):
            hp = "$2b$" + hp[4:]
        return bcrypt.checkpw(plain_password.encode("utf-8"), hp.encode("utf-8"))
    except Exception:
        return False


def login_user(request: Request, user: User) -> None:
    request.session["user_id"] = int(user.id)
    if "csrf_token" not in request.session:
        request.session["csrf_token"] = secrets.token_hex(16)


def logout_user(request: Request) -> None:
    request.session.clear()


def get_current_user(request: Request, db: Session) -> Optional[User]:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.query(User).filter(User.id == user_id).first()


def flash(request: Request, key: str, message: Any) -> None:
    flashes = dict(request.session.get("_flashes", {}))
    flashes[key] = message
    request.session["_flashes"] = flashes


def pop_flashes(request: Request) -> Dict[str, Any]:
    return request.session.pop("_flashes", {})


def set_errors(request: Request, errors: Dict[str, List[str]]) -> None:
    request.session["_errors"] = errors


def pop_errors(request: Request) -> Dict[str, List[str]]:
    return request.session.pop("_errors", {})


def set_old_input(request: Request, old_data: Dict[str, Any]) -> None:
    request.session["_old"] = old_data


def pop_old_input(request: Request) -> Dict[str, Any]:
    return request.session.pop("_old", {})


def get_email_verification_hash(email: str) -> str:
    return hashlib.sha1(email.lower().strip().encode("utf-8")).hexdigest()


def generate_verification_url(request: Request, user: User) -> str:
    h = get_email_verification_hash(user.email)
    base = str(request.base_url).rstrip("/")
    return f"{base}/email/verify/{user.id}/{h}"


def ensure_default_users(db: Session) -> None:
    """Ensures default users from DianSeeder exist, have verified emails, and syncs sequences."""
    try:
        for tbl in (
            "users",
            "job_profiles",
            "thematic_axes",
            "questions",
            "exam_sessions",
            "session_answers",
        ):
            db.execute(
                text(
                    f"SELECT setval(pg_get_serial_sequence('{tbl}', 'id'), COALESCE((SELECT MAX(id) FROM {tbl}), 1), true)"
                )
            )
        db.commit()
    except Exception:
        db.rollback()

    try:
        job = db.query(JobProfile).filter(JobProfile.code == "202059").first()
        if not job:
            job = db.query(JobProfile).first()
        job_id = job.id if job else None
        job_name = job.name if job else "Analista III"

        aspirante = db.query(User).filter(User.email == "aspirante@dian.gov.co").first()
        if not aspirante:
            aspirante = User(
                name="Aspirante DIAN 2676",
                email="aspirante@dian.gov.co",
                password=hash_password("password123"),
                job_profile_id=job_id,
                target_cargo=job_name,
                role="aspirante",
                email_verified_at=datetime.now(),
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
            db.add(aspirante)
        elif not aspirante.email_verified_at:
            aspirante.email_verified_at = datetime.now()

        admin = db.query(User).filter(User.email == "admin@dian.gov.co").first()
        if not admin:
            admin = User(
                name="Super Administrador DIAN",
                email="admin@dian.gov.co",
                password=hash_password("admin12345"),
                job_profile_id=job_id,
                target_cargo=job_name,
                role="superadmin",
                email_verified_at=datetime.now(),
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
            db.add(admin)
        else:
            if admin.role != "superadmin":
                admin.role = "superadmin"
            if not admin.email_verified_at:
                admin.email_verified_at = datetime.now()

        db.commit()
    except Exception:
        db.rollback()
