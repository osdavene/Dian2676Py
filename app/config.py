import os
from pathlib import Path
from urllib.parse import quote_plus
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class Settings:
    APP_NAME: str = os.getenv("APP_NAME", "Simulador DIAN 2676")
    APP_ENV: str = os.getenv("APP_ENV", "local")
    APP_KEY: str = os.getenv("APP_KEY", "secret-dian-2676-key-python-edition")
    SECRET_KEY: str = APP_KEY
    APP_DEBUG: bool = os.getenv("APP_DEBUG", "true").lower() in ("true", "1", "yes")
    APP_URL: str = os.getenv("APP_URL", "http://127.0.0.1:8000")
    APP_TIMEZONE: str = os.getenv("APP_TIMEZONE", "America/Bogota")

    DB_CONNECTION: str = os.getenv("DB_CONNECTION", "pgsql").lower()
    DB_HOST: str = os.getenv("DB_HOST", "127.0.0.1")
    DB_PORT: str = os.getenv("DB_PORT", "5432")
    DB_DATABASE: str = os.getenv("DB_DATABASE", "dian2676_db")
    DB_USERNAME: str = os.getenv("DB_USERNAME", "postgres")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "7420")

    GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
    GOOGLE_REDIRECT_URI: str = os.getenv(
        "GOOGLE_REDIRECT_URI", "http://127.0.0.1:8000/auth/google/callback"
    )

    BREVO_API_KEY: str = os.getenv("BREVO_API_KEY", "")

    # Configuración de Correo Electrónico (Amazon SES / SMTP)
    MAIL_MAILER: str = os.getenv("MAIL_MAILER", "smtp")
    MAIL_HOST: str = os.getenv("MAIL_HOST") or os.getenv("EMAIL_HOST", "email-smtp.us-east-1.amazonaws.com")
    MAIL_PORT: int = int(os.getenv("MAIL_PORT") or os.getenv("EMAIL_PORT", "587"))
    MAIL_USERNAME: str = os.getenv("MAIL_USERNAME") or os.getenv("EMAIL_HOST_USER", "")
    MAIL_PASSWORD: str = os.getenv("MAIL_PASSWORD") or os.getenv("EMAIL_HOST_PASSWORD", "")
    MAIL_ENCRYPTION: str = os.getenv("MAIL_ENCRYPTION") or os.getenv("EMAIL_USE_TLS", "tls")
    MAIL_FROM_ADDRESS: str = os.getenv("MAIL_FROM_ADDRESS") or os.getenv("DEFAULT_FROM_EMAIL", "notificaciones@servigest.online")
    MAIL_FROM_NAME: str = os.getenv("MAIL_FROM_NAME", "Simulador DIAN 2676")
    MAIL_ADMIN_RECIPIENT: str = os.getenv("MAIL_ADMIN_RECIPIENT", "vega.oscar@live.com")

    SUPPORT_WHATSAPP_PHONE: str = os.getenv("SUPPORT_WHATSAPP_PHONE", "573192570557")

    @property
    def DATABASE_URL(self) -> str:
        password_encoded = quote_plus(self.DB_PASSWORD)
        if self.DB_CONNECTION in ("mysql", "mariadb"):
            return (
                f"mysql+pymysql://{self.DB_USERNAME}:{password_encoded}"
                f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_DATABASE}?charset=utf8mb4"
            )
        return (
            f"postgresql+psycopg2://{self.DB_USERNAME}:{password_encoded}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_DATABASE}"
        )


settings = Settings()
