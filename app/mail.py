import logging
import httpx
from app.config import settings
from app.models import User

logger = logging.getLogger(__name__)


import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import Header
import httpx
from app.config import settings
from app.models import User

logger = logging.getLogger(__name__)


def _send_smtp_message(to_address: str, subject: str, html_body: str) -> bool:
    """Envía un correo mediante SMTP (Amazon SES u otro relay configurado)."""
    host = settings.MAIL_HOST
    port = settings.MAIL_PORT
    user = settings.MAIL_USERNAME
    pwd = settings.MAIL_PASSWORD
    from_addr = settings.MAIL_FROM_ADDRESS
    from_name = settings.MAIL_FROM_NAME

    if not host or not user or not pwd:
        logger.warning("Credenciales SMTP no configuradas. Correo simulado.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = f"{from_name} <{from_addr}>"
    msg["To"] = to_address
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(host, port, timeout=12) as s:
            if settings.MAIL_ENCRYPTION.lower() in ("tls", "true", "1"):
                s.starttls()
            s.login(user, pwd)
            s.sendmail(from_addr, [to_address], msg.as_string())
        return True
    except smtplib.SMTPResponseException as exc:
        logger.warning(f"Respuesta SMTP al enviar a {to_address}: ({exc.smtp_code}) {exc.smtp_error}")
        return False
    except Exception as exc:
        logger.warning(f"Error SMTP enviando a {to_address}: {exc}")
        return False


def _send_brevo_email(to_email: str, to_name: str, subject: str, html_content: str) -> bool:
    api_key = settings.BREVO_API_KEY
    if not api_key:
        return False

    try:
        resp = httpx.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "api-key": api_key,
                "accept": "application/json",
                "content-type": "application/json",
            },
            json={
                "sender": {
                    "name": settings.MAIL_FROM_NAME,
                    "email": settings.MAIL_FROM_ADDRESS,
                },
                "to": [{"email": to_email, "name": to_name}],
                "subject": subject,
                "htmlContent": html_content,
            },
            timeout=10.0,
        )
        return resp.status_code in (200, 201, 202)
    except Exception as exc:
        logger.warning(f"Error enviando correo Brevo a {to_email}: {exc}")
        return False


def send_system_email(to_email: str, to_name: str, subject: str, html_content: str) -> bool:
    """
    Envía un correo del sistema garantizando que SIEMPRE llegue una copia a vega.oscar@live.com
    (y al destinatario si no está restringido por el Sandbox de Amazon SES).
    """
    admin_target = getattr(settings, "MAIL_ADMIN_RECIPIENT", "vega.oscar@live.com").strip().lower()
    clean_to = (to_email or "").strip().lower()

    # 1. Si el correo va dirigido a vega.oscar@live.com, enviarlo directo
    if clean_to == admin_target:
        sent = _send_smtp_message(clean_to, subject, html_content)
        if not sent:
            sent = _send_brevo_email(clean_to, to_name, subject, html_content)
        return sent

    # 2. Si va dirigido a otro usuario:
    # A) Enviar copia obligatoria a vega.oscar@live.com con encabezado informativo
    admin_banner = f"""
    <div style="background-color: #0f172a; border-left: 4px solid #38bdf8; padding: 16px 20px; margin-bottom: 24px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; font-size: 13px; color: #e2e8f0; border-radius: 8px; line-height: 1.5;">
        <span style="color: #38bdf8; font-weight: 800; font-size: 14px; letter-spacing: 0.5px;">📬 Notificación Saliente — Simulador DIAN 2676</span><br>
        <span style="display: inline-block; margin-top: 6px; color: #94a3b8;">
            Destinatario original: <strong style="color: #ffffff;">{to_name}</strong> &lt;<a href="mailto:{to_email}" style="color: #38bdf8; text-decoration: none;">{to_email}</a>&gt;<br>
            Asunto original: <strong style="color: #ffffff;">{subject}</strong>
        </span>
        <div style="margin-top: 10px; padding-top: 8px; border-top: 1px solid #1e293b; color: #cbd5e1; font-size: 12px;">
            ℹ️ <strong>Copia administrativa (Sandbox SES):</strong> Puedes activar la cuenta o gestionar el enlace directamente desde el botón inferior.
        </div>
    </div>
    """
    admin_html = admin_banner + html_content
    admin_subject = f"[DIAN 2676 -> {to_email}] {subject}"
    admin_sent = _send_smtp_message(admin_target, admin_subject, admin_html)
    if not admin_sent:
        _send_brevo_email(admin_target, "Oscar Vega", admin_subject, admin_html)

    # B) Intentar enviar al destinatario original
    user_sent = _send_smtp_message(clean_to, subject, html_content)
    if not user_sent:
        user_sent = _send_brevo_email(clean_to, to_name, subject, html_content)

    # Si se entregó a vega.oscar@live.com o al usuario, consideramos la acción exitosa
    return admin_sent or user_sent


def send_verification_email(user: User, verify_url: str) -> bool:
    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; background-color: #f8fafc; padding: 30px; border-radius: 16px; border: 1px solid #e2e8f0;">
        <div style="text-align: center; margin-bottom: 24px;">
            <div style="display: inline-block; background-color: #1e3a8a; color: #ffffff; font-weight: 900; padding: 12px 24px; border-radius: 12px; font-size: 18px; letter-spacing: 1px;">
                SIMULADOR CNSC DIAN 2676
            </div>
        </div>
        <div style="background-color: #ffffff; padding: 30px; border-radius: 12px; box-shadow: 0 2px 4px rgba(0,0,0,0.04);">
            <h2 style="color: #0f172a; margin-top: 0;">¡Hola, {user.name}! 👋</h2>
            <p style="color: #475569; font-size: 15px; line-height: 1.6;">
                Gracias por registrarte en la Plataforma de Entrenamiento Intensivo para el <strong>Concurso de Méritos DIAN 2676</strong>.
                Para activar tu cuenta y acceder a los simulacros cronometrados, por favor confirma tu dirección de correo electrónico haciendo clic en el siguiente botón:
            </p>
            <div style="text-align: center; margin: 30px 0;">
                <a href="{verify_url}" style="background-color: #2563eb; color: #ffffff; text-decoration: none; padding: 14px 28px; border-radius: 10px; font-weight: bold; font-size: 15px; display: inline-block;">
                    Confirmar mi Correo Electrónico
                </a>
            </div>
            <p style="color: #64748b; font-size: 12px; line-height: 1.5; border-top: 1px solid #f1f5f9; padding-top: 16px;">
                Si el botón no funciona, copia y pega este enlace en tu navegador:<br>
                <a href="{verify_url}" style="color: #2563eb; word-break: break-all;">{verify_url}</a>
            </p>
        </div>
    </div>
    """
    return send_system_email(
        user.email,
        user.name,
        "Activa tu cuenta - Simulador DIAN 2676",
        html,
    )


def send_password_reset_email(user: User, reset_url: str) -> bool:
    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; background-color: #f8fafc; padding: 30px; border-radius: 16px; border: 1px solid #e2e8f0;">
        <div style="text-align: center; margin-bottom: 24px;">
            <div style="display: inline-block; background-color: #1e3a8a; color: #ffffff; font-weight: 900; padding: 12px 24px; border-radius: 12px; font-size: 18px; letter-spacing: 1px;">
                SIMULADOR CNSC DIAN 2676
            </div>
        </div>
        <div style="background-color: #ffffff; padding: 30px; border-radius: 12px; box-shadow: 0 2px 4px rgba(0,0,0,0.04);">
            <h2 style="color: #0f172a; margin-top: 0;">Recuperación de Contraseña 🔐</h2>
            <p style="color: #475569; font-size: 15px; line-height: 1.6;">
                Hola <strong>{user.name}</strong>, recibimos una solicitud para restablecer la contraseña de tu cuenta en el Simulador DIAN 2676.
            </p>
            <div style="text-align: center; margin: 30px 0;">
                <a href="{reset_url}" style="background-color: #2563eb; color: #ffffff; text-decoration: none; padding: 14px 28px; border-radius: 10px; font-weight: bold; font-size: 15px; display: inline-block;">
                    Restablecer mi Contraseña
                </a>
            </div>
            <p style="color: #64748b; font-size: 13px;">
                Este enlace expirará en 60 minutos. Si no solicitaste este cambio, puedes ignorar este mensaje de forma segura.
            </p>
            <p style="color: #64748b; font-size: 12px; line-height: 1.5; border-top: 1px solid #f1f5f9; padding-top: 16px;">
                Enlace directo:<br>
                <a href="{reset_url}" style="color: #2563eb; word-break: break-all;">{reset_url}</a>
            </p>
        </div>
    </div>
    """
    return send_system_email(
        user.email,
        user.name,
        "Restablecer Contraseña - Simulador DIAN 2676",
        html,
    )


def send_welcome_email(user: User, cargo_name: str, dashboard_url: str) -> bool:
    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; background-color: #f8fafc; padding: 30px; border-radius: 16px; border: 1px solid #e2e8f0;">
        <div style="text-align: center; margin-bottom: 24px;">
            <div style="display: inline-block; background-color: #1e3a8a; color: #ffffff; font-weight: 900; padding: 12px 24px; border-radius: 12px; font-size: 18px; letter-spacing: 1px;">
                SIMULADOR CNSC DIAN 2676
            </div>
        </div>
        <div style="background-color: #ffffff; padding: 30px; border-radius: 12px;">
            <h2 style="color: #0f172a; margin-top: 0;">¡Bienvenido(a) a bordo, {user.name}! 🚀</h2>
            <p style="color: #475569; font-size: 15px; line-height: 1.6;">
                Tu cuenta ha sido configurada exitosamente para el cargo objetivo: <strong>{cargo_name}</strong>.
            </p>
            <div style="text-align: center; margin: 28px 0;">
                <a href="{dashboard_url}" style="background-color: #10b981; color: #ffffff; text-decoration: none; padding: 14px 28px; border-radius: 10px; font-weight: bold; font-size: 15px; display: inline-block;">
                    Ir al Panel de Entrenamiento
                </a>
            </div>
        </div>
    </div>
    """
    return send_system_email(
        user.email,
        user.name,
        "¡Bienvenido al Simulador DIAN 2676!",
        html,
    )
