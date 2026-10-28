import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode, quote
import httpx
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from app.config import settings
from app.database import get_db
from app.models import User, JobProfile, PasswordResetToken
from app.security import (
    verify_password,
    hash_password,
    login_user,
    logout_user,
    get_current_user,
    flash,
    set_errors,
    set_old_input,
    get_email_verification_hash,
    generate_verification_url,
    get_base_url_from_request,
)
from app.mail import (
    send_verification_email,
    send_password_reset_email,
    send_welcome_email,
)
from app.templates_config import render_template

router = APIRouter()


@router.get("/login")
def show_login_form(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user:
        return RedirectResponse(url="/dashboard", status_code=302)
    return render_template(request, "auth/login.html", {}, db)


@router.post("/login")
def login(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    remember: str = Form(None),
    db: Session = Depends(get_db),
):
    email_clean = email.strip().lower()
    user = db.query(User).filter(User.email == email_clean).first()

    if not user or not verify_password(password, user.password):
        set_old_input(request, {"email": email})
        set_errors(
            request,
            {
                "email": [
                    "Las credenciales proporcionadas no coinciden con nuestros registros."
                ]
            },
        )
        return RedirectResponse(url="/login", status_code=302)

    login_user(request, user)
    if not user.has_verified_email():
        return RedirectResponse(url="/email/verify", status_code=302)
    return RedirectResponse(url="/dashboard", status_code=302)


@router.get("/register")
def show_register_form(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user:
        return RedirectResponse(url="/dashboard", status_code=302)
    jobs = (
        db.query(JobProfile)
        .filter(JobProfile.is_active.is_(True))
        .order_by(JobProfile.level, JobProfile.grade)
        .all()
    )
    return render_template(request, "auth/register.html", {"jobs": jobs}, db)


@router.post("/register")
def register(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    job_profile_id: int = Form(0),
    password: str = Form(""),
    password_confirmation: str = Form(""),
    db: Session = Depends(get_db),
):
    errors = {}
    name_clean = name.strip()
    email_clean = email.strip().lower()

    if not name_clean:
        errors.setdefault("name", []).append("El nombre completo es obligatorio.")
    if not email_clean or "@" not in email_clean:
        errors.setdefault("email", []).append("Ingresa un correo electrónico válido.")
    else:
        existing = db.query(User).filter(User.email == email_clean).first()
        if existing:
            errors.setdefault("email", []).append(
                "Este correo electrónico ya está registrado."
            )

    job = db.query(JobProfile).filter(JobProfile.id == job_profile_id).first()
    if not job:
        errors.setdefault("job_profile_id", []).append(
            "Debes seleccionar un cargo objetivo válido."
        )

    if len(password) < 8:
        errors.setdefault("password", []).append(
            "La contraseña debe tener al menos 8 caracteres."
        )
    elif password != password_confirmation:
        errors.setdefault("password", []).append(
            "La confirmación de la contraseña no coincide."
        )

    if errors:
        set_old_input(
            request,
            {"name": name_clean, "email": email_clean, "job_profile_id": job_profile_id},
        )
        set_errors(request, errors)
        return RedirectResponse(url="/register", status_code=302)

    user = User(
        name=name_clean,
        email=email_clean,
        password=hash_password(password),
        job_profile_id=job.id,
        target_cargo=job.name,
        role="aspirante",
        email_verified_at=None,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    login_user(request, user)

    # Enviar correo de activación con enlace de confirmación
    try:
        verify_url = generate_verification_url(request, user)
        send_verification_email(user, verify_url)
    except Exception as exc:
        pass

    flash(
        request,
        "info",
        f"¡Bienvenido(a) {user.name}! Tu cuenta para {job.name} ha sido registrada. Por favor confirma tu correo electrónico para activarla e ingresar a las pruebas.",
    )
    return RedirectResponse(url="/email/verify", status_code=302)


@router.post("/logout")
@router.get("/logout")
def logout(request: Request):
    logout_user(request)
    return RedirectResponse(url="/login", status_code=302)


@router.get("/email/verify")
def verify_notice(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.has_verified_email():
        return RedirectResponse(url="/dashboard", status_code=302)
    return render_template(request, "auth/verify-email.html", {"user": user}, db)


@router.get("/email/verify/{user_id}/{hash_str}")
def verify_email(
    user_id: int,
    hash_str: str,
    request: Request,
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    expected = get_email_verification_hash(user.email)
    if hash_str != expected:
        flash(request, "error", "El enlace de verificación no es válido.")
        return RedirectResponse(url="/email/verify", status_code=302)

    if not user.has_verified_email():
        user.email_verified_at = datetime.now()
        user.updated_at = datetime.now()
        db.commit()

    login_user(request, user)
    flash(
        request,
        "success",
        "¡Tu correo electrónico ha sido verificado exitosamente! Bienvenido al entrenamiento.",
    )
    return RedirectResponse(url="/dashboard", status_code=302)


@router.post("/email/verification-notification")
def resend_verification(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.has_verified_email():
        return RedirectResponse(url="/dashboard", status_code=302)

    verify_url = generate_verification_url(request, user)
    sent = send_verification_email(user, verify_url)
    if sent:
        flash(request, "status", "verification-link-sent")
    else:
        flash(
            request,
            "error",
            "No fue posible reenviar el correo en este momento. Por favor verifica tu conexión o intenta de nuevo.",
        )

    return RedirectResponse(url="/email/verify", status_code=302)


@router.get("/forgot-password")
def show_forgot_password(request: Request, db: Session = Depends(get_db)):
    return render_template(request, "auth/forgot-password.html", {}, db)


@router.post("/forgot-password")
def send_reset_link(
    request: Request,
    email: str = Form(""),
    db: Session = Depends(get_db),
):
    email_clean = email.strip().lower()
    user = db.query(User).filter(User.email == email_clean).first()
    if not user:
        set_old_input(request, {"email": email})
        set_errors(
            request,
            {"email": ["No encontramos ningún usuario con ese correo electrónico."]},
        )
        return RedirectResponse(url="/forgot-password", status_code=302)

    raw_token = secrets.token_urlsafe(32)
    hashed_token = hash_password(raw_token)

    existing = (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.email == email_clean)
        .first()
    )
    if existing:
        existing.token = hashed_token
        existing.created_at = datetime.now()
    else:
        db.add(
            PasswordResetToken(
                email=email_clean,
                token=hashed_token,
                created_at=datetime.now(),
            )
        )
    db.commit()

    base = get_base_url_from_request(request)
    reset_url = f"{base}/reset-password/{raw_token}?{urlencode({'email': email_clean})}"
    send_password_reset_email(user, reset_url)

    flash(
        request,
        "status",
        "Te hemos enviado un enlace por correo electrónico para restablecer tu contraseña.",
    )
    return RedirectResponse(url="/forgot-password", status_code=302)


@router.get("/reset-password/{token}")
def show_reset_form(
    token: str,
    request: Request,
    email: str = "",
    db: Session = Depends(get_db),
):
    return render_template(
        request,
        "auth/reset-password.html",
        {"token": token, "email": email},
        db,
    )


@router.post("/reset-password")
def reset_password(
    request: Request,
    token: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    password_confirmation: str = Form(""),
    db: Session = Depends(get_db),
):
    email_clean = email.strip().lower()
    errors = {}
    if len(password) < 8:
        errors.setdefault("password", []).append(
            "La contraseña debe tener al menos 8 caracteres."
        )
    elif password != password_confirmation:
        errors.setdefault("password", []).append(
            "La confirmación de la contraseña no coincide."
        )

    record = (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.email == email_clean)
        .first()
    )
    if not record:
        errors.setdefault("email", []).append(
            "Este token de restablecimiento no es válido o ha expirado."
        )
    else:
        if record.created_at and datetime.now() - record.created_at > timedelta(
            minutes=60
        ):
            db.delete(record)
            db.commit()
            errors.setdefault("email", []).append(
                "El enlace de restablecimiento ha expirado. Solicita uno nuevo."
            )
        elif not verify_password(token, record.token):
            errors.setdefault("email", []).append(
                "El token de restablecimiento es inválido."
            )

    user = db.query(User).filter(User.email == email_clean).first()
    if not user:
        errors.setdefault("email", []).append("No encontramos el usuario.")

    if errors:
        set_old_input(request, {"email": email_clean})
        set_errors(request, errors)
        return RedirectResponse(
            url=f"/reset-password/{token}?{urlencode({'email': email_clean})}",
            status_code=302,
        )

    user.password = hash_password(password)
    if not user.email_verified_at:
        user.email_verified_at = datetime.now()
    user.updated_at = datetime.now()
    db.delete(record)
    db.commit()

    login_user(request, user)
    flash(request, "success", "¡Tu contraseña ha sido actualizada exitosamente!")
    return RedirectResponse(url="/dashboard", status_code=302)


@router.get("/auth/google")
def redirect_to_google(request: Request):
    client_id = settings.GOOGLE_CLIENT_ID
    redirect_uri = settings.GOOGLE_REDIRECT_URI
    if not client_id:
        flash(
            request,
            "error",
            "El inicio de sesión con Google no está configurado en este entorno.",
        )
        return RedirectResponse(url="/login", status_code=302)

    params = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "access_type": "online",
            "prompt": "select_account",
        }
    )
    return RedirectResponse(
        url=f"https://accounts.google.com/o/oauth2/v2/auth?{params}",
        status_code=302,
    )


@router.get("/auth/google/callback")
def handle_google_callback(
    request: Request,
    code: str = "",
    db: Session = Depends(get_db),
):
    if not code:
        set_errors(
            request, {"email": ["El inicio de sesión con Google fue cancelado."]}
        )
        return RedirectResponse(url="/login", status_code=302)

    try:
        token_resp = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            timeout=10.0,
        )
        if token_resp.status_code != 200:
            set_errors(
                request,
                {"email": ["No fue posible autenticar con Google. Intenta de nuevo."]},
            )
            return RedirectResponse(url="/login", status_code=302)

        access_token = token_resp.json().get("access_token")
        user_resp = httpx.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10.0,
        )
        if user_resp.status_code != 200:
            set_errors(
                request,
                {"email": ["No se pudo obtener la información del perfil de Google."]},
            )
            return RedirectResponse(url="/login", status_code=302)

        g_user = user_resp.json()
        email = g_user.get("email", "").lower().strip()
        google_id = g_user.get("sub")
        name = g_user.get("name") or email.split("@")[0]
        avatar = g_user.get("picture")

        existing = db.query(User).filter(User.email == email).first()
        if existing:
            existing.google_id = google_id
            existing.avatar = avatar
            if not existing.email_verified_at:
                existing.email_verified_at = datetime.now()
            existing.updated_at = datetime.now()
            db.commit()
            login_user(request, existing)
            return RedirectResponse(url="/dashboard", status_code=302)

        request.session["google_pending_user"] = {
            "name": name,
            "email": email,
            "google_id": google_id,
            "avatar": avatar,
        }
        return RedirectResponse(url="/auth/google/completar-perfil", status_code=302)
    except Exception:
        set_errors(
            request, {"email": ["Error de conexión con Google. Intenta nuevamente."]}
        )
        return RedirectResponse(url="/login", status_code=302)


@router.get("/auth/google/completar-perfil")
def show_complete_google_profile(request: Request, db: Session = Depends(get_db)):
    pending = request.session.get("google_pending_user")
    if not pending:
        return RedirectResponse(url="/login", status_code=302)
    jobs = (
        db.query(JobProfile)
        .filter(JobProfile.is_active.is_(True))
        .order_by(JobProfile.level, JobProfile.grade)
        .all()
    )
    return render_template(
        request,
        "auth/google-complete.html",
        {"pendingUser": pending, "googleUser": pending, "jobs": jobs, "jobProfiles": jobs},
        db,
    )


@router.post("/auth/google/completar-perfil")
def complete_google_profile(
    request: Request,
    name: str = Form(""),
    job_profile_id: int = Form(0),
    db: Session = Depends(get_db),
):
    pending = request.session.get("google_pending_user")
    if not pending:
        return RedirectResponse(url="/login", status_code=302)

    final_name = name.strip() or pending.get("name", "Aspirante DIAN")
    job = db.query(JobProfile).filter(JobProfile.id == job_profile_id).first()
    if not job:
        set_errors(
            request,
            {"job_profile_id": ["Selecciona un cargo válido."]},
        )
        return RedirectResponse(url="/auth/google/completar-perfil", status_code=302)

    user = User(
        name=final_name,
        email=pending["email"],
        password=hash_password(secrets.token_urlsafe(24)),
        google_id=pending.get("google_id"),
        avatar=pending.get("avatar"),
        job_profile_id=job.id,
        target_cargo=job.name,
        role="aspirante",
        email_verified_at=datetime.now(),
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    request.session.pop("google_pending_user", None)
    login_user(request, user)

    base = get_base_url_from_request(request)
    send_welcome_email(user, job.name, f"{base}/dashboard")

    flash(
        request,
        "success",
        f"¡Bienvenido(a) {user.name}! Tu perfil ha sido configurado para el cargo {job.name}.",
    )
    return RedirectResponse(url="/dashboard", status_code=302)


@router.post("/cambiar-cargo/{job_id}")
@router.get("/cambiar-cargo/{job_id}")
def switch_cargo(job_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    job = db.query(JobProfile).filter(JobProfile.id == job_id).first()
    if job:
        user.job_profile_id = job.id
        user.target_cargo = job.name
        user.updated_at = datetime.now()
        db.commit()
        flash(
            request,
            "success",
            f"Cargo objetivo actualizado a: {job.name} ( Nivel {job.level} - Grado {job.grade} )",
        )
    referer = request.headers.get("referer") or "/dashboard"
    return RedirectResponse(url=referer, status_code=302)


@router.get("/soporte")
def support_whatsapp(
    request: Request,
    tipo: str = "general",
    ref: str = "",
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    cargo = user.target_cargo or "No especificado"
    if tipo == "pregunta":
        msg = (
            f"Hola Equipo de Soporte Simulador DIAN 2676. Soy {user.name} ({user.email}), "
            f"aspirante al cargo {cargo}. Deseo reportar una novedad o consulta en la pregunta {ref}."
        )
    elif tipo == "sugerencia":
        msg = (
            f"Hola Equipo de Soporte Simulador DIAN 2676. Soy {user.name} ({user.email}), "
            f"aspirante al cargo {cargo}. Quisiera compartir una sugerencia sobre la plataforma:"
        )
    else:
        msg = (
            f"Hola Equipo de Soporte Simulador DIAN 2676. Soy {user.name} ({user.email}), "
            f"aspirante al cargo {cargo}. Solicito asistencia técnica con la plataforma."
        )

    phone = settings.SUPPORT_WHATSAPP_PHONE
    url = f"https://wa.me/{phone}?text={quote(msg)}"
    return RedirectResponse(url=url, status_code=302)
