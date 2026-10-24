# Simulador Concurso DIAN 2676 — Versión Python (FastAPI + SQLAlchemy + Jinja2)

Versión completa en **Python** del proyecto `C:\Proyectos\Dian2676` (originalmente en Laravel 13), diseñada para ejecutarse como aplicación web moderna manteniendo **exactamente la misma interfaz visual** y conectándose de forma directa y transparente a la **misma base de datos PostgreSQL (`dian2676_db`)**.

---

## 🚀 Características Principales

- **Conexión Directa a la Base de Datos Existente (`dian2676_db`)**:
  - Comparte las mismas tablas (`users`, `job_profiles`, `thematic_axes`, `questions`, `exam_sessions`, `session_answers`, `password_reset_tokens`) sin alterar ni perder ningún registro.
  - **Compatibilidad Total de Contraseñas Bcrypt (`$2y$` / `$2b$`)**: Los usuarios creados en Laravel pueden iniciar sesión en Python y viceversa sin cambiar su contraseña.
- **Módulos para Aspirantes**:
  - **Dashboard de Rendimiento**: Estadísticas globales, desglose por competencias (Básicas, Funcionales, Conductuales), ejes temáticos y reanudación de simulacros en curso.
  - **Simulacro Oficial CNSC (100 preguntas / 5 horas)** y **Simulacro Express (50 preguntas / 2.5 horas)**:
    - Agrupación inteligente de preguntas por Caso Situacional consecutivo.
    - Barajado dinámico de opciones (`A, B, C, D`) persistido por sesión (`options_order`).
    - Cronómetro pausable, autoguardado en tiempo real, hoja óptica interactiva, detección de inactividad y soporte offline local.
  - **Resultados y Diagnóstico Pedagógico**: Calificación oficial truncada a 2 decimales (umbral aprobatorio `70.00`), plan de mejora con referencias normativas y reporte imprimible / PDF.
  - **Práctica por Ejes Temáticos**, **Repaso de Fallos**, **Historial Completo** y **Banco de Preguntas de Estudio**.
  - **Autenticación Completa**: Login, Registro, Recuperación de Contraseña por correo (Brevo API), Verificación de Email, Google OAuth y Perfil de Usuario.
- **Panel de Superadministrador (`/admin` - Control Central)**:
  - Monitoreo en vivo de simulacros activos y métricas por cargo (*Analista II, Analista III, Gestor I, Gestor III*).
  - Gestión de aspirantes, creación/edición de usuarios y reinicio de intentos.
  - Gestión, creación, edición de preguntas con **recalificación automática de exámenes históricos**, importación masiva CSV/JSON y desactivación de duplicados.
  - Analítica de las 25 preguntas con mayor tasa de error.
  - Centro de Copias de Seguridad (generación, descarga e importación/restauración de Backups JSON).

---

## 🛠️ Requisitos e Instalación

1. Asegúrate de tener **Python 3.10+** y el servidor **PostgreSQL** corriendo con la base de datos `dian2676_db`.
2. Instala las dependencias (si aún no están instaladas):
   ```powershell
   pip install -r requirements.txt
   ```
3. Verifica las credenciales en el archivo `.env` (preconfigurado con la conexión local a `dian2676_db`).

---

## ▶️ Ejecución

Puedes iniciar el servidor haciendo doble clic en **`iniciar_simulador.bat`** o ejecutando en la terminal:

```powershell
python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

Luego abre en tu navegador:
👉 **http://127.0.0.1:8000**

### Credenciales por Defecto
- **Super Administrador**: `admin@dian.gov.co` / `admin12345`
- **Aspirante Demo**: `aspirante@dian.gov.co` / `password123`
