from datetime import datetime
from typing import Any, Dict, Optional
from fastapi import Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy.orm import Session
from app.config import BASE_DIR, settings
from app.models import JobProfile, Question
from app.security import get_current_user, pop_flashes, pop_errors, pop_old_input

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def filter_number_format(value: Any, decimals: int = 0) -> str:
    try:
        num = float(value or 0)
        return f"{num:,.{decimals}f}"
    except (ValueError, TypeError):
        return "0"


def filter_gmdate(seconds: Any, fmt: str = "%H:%M:%S") -> str:
    try:
        sec = max(0, int(float(seconds or 0)))
    except (ValueError, TypeError):
        sec = 0
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    if fmt in ("i:s", "%M:%S"):
        total_m = sec // 60
        return f"{total_m:02d}:{s:02d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


def filter_limit(value: Any, length: int = 100, end: str = "...") -> str:
    text = str(value or "")
    if len(text) <= length:
        return text
    return text[:length] + end


def filter_nl2br(value: Any) -> Markup:
    if not value:
        return Markup("")
    escaped = escape(str(value))
    return Markup(str(escaped).replace("\r\n", "<br>").replace("\n", "<br>"))


def filter_format_date(value: Any, fmt: str = "%d/%m/%Y %H:%M") -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        # Convert PHP-like format tokens if passed
        php_to_py = {
            "d/m/Y H:i": "%d/%m/%Y %H:%M",
            "d/m/Y h:i A": "%d/%m/%Y %I:%M %p",
            "d/m/Y h:i a": "%d/%m/%Y %I:%M %p",
            "d/m/Y": "%d/%m/%Y",
            "d M Y, H:i": "%d %b %Y, %H:%M",
            "d M Y, h:i A": "%d %b %Y, %I:%M %p",
            "d M, H:i": "%d %b, %H:%M",
        }
        py_fmt = php_to_py.get(fmt, fmt)
        return value.strftime(py_fmt)
    return str(value)


def filter_diff_for_humans(value: Any) -> str:
    if not value or not isinstance(value, datetime):
        return ""
    now = datetime.now()
    diff = now - value
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return "hace unos segundos"
    minutes = seconds // 60
    if minutes < 60:
        return f"hace {minutes} min"
    hours = minutes // 60
    if hours < 24:
        return f"hace {hours} h"
    days = hours // 24
    if days < 30:
        return f"hace {days} días"
    months = days // 30
    return f"hace {months} meses"


class CallableDatetime(datetime):
    def __call__(self) -> "CallableDatetime":
        return self


def asset_helper(path: str) -> str:
    return "/" + str(path or "").lstrip("/")


templates.env.filters["number_format"] = filter_number_format
templates.env.filters["gmdate"] = filter_gmdate
templates.env.filters["limit"] = filter_limit
templates.env.filters["nl2br"] = filter_nl2br
templates.env.filters["format_date"] = filter_format_date
templates.env.filters["diff_for_humans"] = filter_diff_for_humans
templates.env.globals["asset"] = asset_helper


def render_template(
    request: Request,
    template_name: str,
    context: Optional[Dict[str, Any]] = None,
    db: Optional[Session] = None,
    status_code: int = 200,
):
    ctx: Dict[str, Any] = {}
    if context:
        ctx.update(context)

    user = ctx.get("user")
    if user is None and db is not None:
        user = get_current_user(request, db)

    all_jobs_list = []
    total_questions_global = 0
    total_questions_all = 0
    if db is not None:
        all_jobs_list = (
            db.query(JobProfile)
            .filter(JobProfile.is_active.is_(True))
            .order_by(JobProfile.level, JobProfile.grade)
            .all()
        )
        active_job_id = user.job_profile_id if user else None
        total_questions_global = (
            Question.for_cargo_query(db, active_job_id)
            .filter(Question.is_active.is_(True))
            .count()
        )
        total_questions_all = db.query(Question).count()

    flashes = pop_flashes(request)
    errors = pop_errors(request)
    old_input = pop_old_input(request)

    def old(key: str, default: Any = "") -> Any:
        return old_input.get(key, default)

    csrf_token = request.session.get("csrf_token", "")

    now_val = CallableDatetime.now()
    base_ctx = {
        "request": request,
        "user": user,
        "current_user": user,
        "auth_user": user,
        "all_jobs_list": all_jobs_list,
        "total_questions_global": total_questions_global,
        "total_questions_all": total_questions_all,
        "flashes": flashes,
        "errors": errors,
        "old": old,
        "csrf_token": csrf_token,
        "settings": settings,
        "app_debug": settings.APP_DEBUG,
        "app_timezone": "America/Bogota",
        "asset": asset_helper,
        "now": now_val,
    }
    base_ctx.update(ctx)
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context=base_ctx,
        status_code=status_code,
    )
