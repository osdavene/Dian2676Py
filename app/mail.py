import logging
import httpx
from app.config import settings
from app.models import User

logger = logging.getLogger(__name__)


def _send_brevo_email(to_email: str, to_name: str, subject: str, html_content: str) -> bool:
    api_key = settings.BREVO_API_KEY
    if not api_key:
        logger.info(f"BREVO_API_KEY no configurada. Correo simulado para {to_email}: {subject}")
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
    return _send_brevo_email(
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
    return _send_brevo_email(
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
    return _send_brevo_email(
        user.email,
        user.name,
        "¡Bienvenido al Simulador DIAN 2676!",
        html,
    )
