import math
from datetime import datetime
from typing import Optional, Any
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload
from app.database import get_db
from app.models import (
    User,
    JobProfile,
    ThematicAxis,
    Question,
    ExamSession,
    SessionAnswer,
)
from app.security import (
    get_current_user,
    verify_password,
    hash_password,
    flash,
    set_errors,
)
from app.pagination import Paginator
from app.templates_config import render_template

router = APIRouter()


def _count_questions_for_axis(db: Session, axis: ThematicAxis) -> int:
    return (
        db.query(Question)
        .filter(
            Question.is_active.is_(True),
            or_(
                Question.thematic_axis_id == axis.id,
                (
                    or_(
                        Question.is_shared.is_(True),
                        Question.scope_level == "profesional",
                        Question.scope_level == "tecnico",
                        Question.scope_level == "transversal",
                    )
                    & Question.thematic_axis.has(ThematicAxis.name == axis.name)
                ),
            ),
        )
        .count()
    )


@router.get("/")
@router.get("/dashboard")
def index(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not user.has_verified_email():
        return RedirectResponse(url="/email/verify", status_code=302)

    all_jobs = (
        db.query(JobProfile)
        .filter(JobProfile.is_active.is_(True))
        .order_by(JobProfile.id)
        .all()
    )

    if not user.job_profile_id:
        default_job = (
            db.query(JobProfile).filter(JobProfile.code == "AT-FL-2011").first()
            or (all_jobs[0] if all_jobs else None)
        )
        if default_job:
            user.job_profile_id = default_job.id
            user.target_cargo = default_job.name
            db.commit()

    active_job = (
        db.query(JobProfile).filter(JobProfile.id == user.job_profile_id).first()
        if user.job_profile_id
        else (all_jobs[0] if all_jobs else None)
    )
    job_id = active_job.id if active_job else 1

    user_sessions = (
        db.query(ExamSession)
        .filter(
            ExamSession.user_id == user.id,
            ExamSession.completed_at.isnot(None),
        )
        .order_by(ExamSession.id.desc())
        .all()
    )

    total_exams = len(user_sessions)
    passed_exams = sum(1 for s in user_sessions if s.is_passed)
    pass_rate = round((passed_exams / total_exams) * 100, 1) if total_exams > 0 else 0
    if total_exams > 0:
        raw_avg = sum(float(s.score or 0) for s in user_sessions) / total_exams
        avg_score = math.floor(raw_avg * 100) / 100
        best_score = max(float(s.score or 0) for s in user_sessions)
    else:
        avg_score = 0.0
        best_score = 0.0

    failed_questions_count = (
        db.query(SessionAnswer.question_id)
        .join(ExamSession, ExamSession.id == SessionAnswer.exam_session_id)
        .filter(
            ExamSession.user_id == user.id,
            SessionAnswer.is_correct.is_(False),
        )
        .distinct()
        .count()
    )

    thematic_axes = (
        db.query(ThematicAxis)
        .filter(ThematicAxis.job_profile_id == job_id)
        .order_by(ThematicAxis.category, ThematicAxis.id)
        .all()
    )
    for axis in thematic_axes:
        axis.questions_count = _count_questions_for_axis(db, axis)

    total_job_questions = (
        Question.for_cargo_query(db, job_id)
        .filter(Question.is_active.is_(True))
        .count()
    )

    recent_sessions = (
        db.query(ExamSession)
        .options(
            joinedload(ExamSession.thematic_axis),
            joinedload(ExamSession.job_profile),
        )
        .filter(ExamSession.user_id == user.id)
        .order_by(ExamSession.id.desc())
        .limit(5)
        .all()
    )

    active_session = (
        db.query(ExamSession)
        .filter(
            ExamSession.user_id == user.id,
            ExamSession.completed_at.is_(None),
            ExamSession.mode.in_(["simulacro_oficial", "simulacro_express"]),
        )
        .order_by(ExamSession.id.desc())
        .first()
    )
    if active_session:
        time_limit = active_session.time_limit_seconds or 18000
        spent = int(active_session.time_spent_seconds or 0)
        if spent >= time_limit:
            active_session = None

    # Ranking Anónimo y Percentil por Cargo OPEC ("Lista de Elegibles Simulada")
    cargo_sessions = (
        db.query(ExamSession)
        .options(joinedload(ExamSession.user))
        .filter(
            ExamSession.completed_at.isnot(None),
            or_(
                ExamSession.job_profile_id == job_id,
                ExamSession.user.has(User.job_profile_id == job_id),
            ),
        )
        .all()
    )
    user_stats_map: dict[int, dict[str, Any]] = {}
    for cs in cargo_sessions:
        if not cs.user_id:
            continue
        uid = int(cs.user_id)
        sc = float(cs.score or 0.0)
        if uid not in user_stats_map:
            u_name = (cs.user.name if cs.user and cs.user.name else f"Aspirante {uid}").strip()
            parts = [p for p in u_name.split() if p]
            initials = ". ".join(p[0].upper() for p in parts[:2]) + "." if parts else "A."
            user_stats_map[uid] = {
                "user_id": uid,
                "alias": f"Aspirante #{100 + uid} ({initials})",
                "scores": [],
            }
        user_stats_map[uid]["scores"].append(sc)

    leaderboard = []
    for uid, st in user_stats_map.items():
        scores_list = st["scores"]
        u_avg = round(sum(scores_list) / len(scores_list), 2)
        u_best = round(max(scores_list), 2)
        leaderboard.append(
            {
                "user_id": uid,
                "alias": st["alias"],
                "is_me": uid == user.id,
                "avg_score": u_avg,
                "best_score": u_best,
                "exams_count": len(scores_list),
            }
        )

    leaderboard.sort(
        key=lambda x: (x["avg_score"], x["best_score"], x["exams_count"]),
        reverse=True,
    )
    user_rank = None
    for idx_r, row_r in enumerate(leaderboard, 1):
        row_r["rank"] = idx_r
        if row_r["is_me"]:
            user_rank = idx_r

    total_competitors = len(leaderboard)
    total_registered_cargo = (
        db.query(User).filter(User.job_profile_id == job_id).count()
    )
    cargo_avg_score = (
        round(sum(x["avg_score"] for x in leaderboard) / total_competitors, 2)
        if total_competitors > 0
        else 0.0
    )
    percentile = (
        max(1, min(99, round(((total_competitors - user_rank + 1) / total_competitors) * 100)))
        if (user_rank and total_competitors > 1)
        else (99 if user_rank == 1 else None)
    )

    opec_ranking = {
        "user_rank": user_rank,
        "total_competitors": total_competitors,
        "total_registered_cargo": max(total_registered_cargo, total_competitors),
        "cargo_avg_score": cargo_avg_score,
        "percentile": percentile,
        "top_5": leaderboard[:5],
    }

    return render_template(
        request,
        "dashboard.html",
        {
            "user": user,
            "activeJob": active_job,
            "allJobs": all_jobs,
            "totalExams": total_exams,
            "passedExams": passed_exams,
            "passRate": pass_rate,
            "avgScore": avg_score,
            "bestScore": best_score,
            "failedQuestionsCount": failed_questions_count,
            "thematicAxes": thematic_axes,
            "totalJobQuestions": total_job_questions,
            "recentSessions": recent_sessions,
            "activeSession": active_session,
            "opecRanking": opec_ranking,
        },
        db,
    )


@router.get("/perfil")
def show_profile(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not user.has_verified_email():
        return RedirectResponse(url="/email/verify", status_code=302)

    job_profiles = (
        db.query(JobProfile)
        .filter(JobProfile.is_active.is_(True))
        .order_by(JobProfile.id)
        .all()
    )

    return render_template(
        request,
        "profile/show.html",
        {"user": user, "jobProfiles": job_profiles, "jobs": job_profiles},
        db,
    )


@router.post("/perfil")
@router.put("/perfil")
def update_profile(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    job_profile_id: Optional[int] = Form(None),
    current_password: Optional[str] = Form(None),
    new_password: Optional[str] = Form(None),
    new_password_confirmation: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    errors = {}
    name_clean = name.strip()
    email_clean = email.strip().lower()

    if not name_clean:
        errors.setdefault("name", []).append("El nombre es obligatorio.")
    if not email_clean or "@" not in email_clean:
        errors.setdefault("email", []).append("El correo electrónico no es válido.")
    else:
        other = (
            db.query(User)
            .filter(User.email == email_clean, User.id != user.id)
            .first()
        )
        if other:
            errors.setdefault("email", []).append(
                "Este correo electrónico ya está en uso por otra cuenta."
            )

    if new_password:
        if not current_password or not verify_password(current_password, user.password):
            errors.setdefault("current_password", []).append(
                "La contraseña actual no es correcta."
            )
        elif len(new_password) < 6:
            errors.setdefault("new_password", []).append(
                "La nueva contraseña debe tener al menos 6 caracteres."
            )
        elif new_password != new_password_confirmation:
            errors.setdefault("new_password", []).append(
                "La confirmación de la nueva contraseña no coincide."
            )

    if errors:
        set_errors(request, errors)
        return RedirectResponse(url="/perfil", status_code=302)

    user.name = name_clean
    user.email = email_clean

    if user.is_admin() and job_profile_id:
        job = db.query(JobProfile).filter(JobProfile.id == job_profile_id).first()
        if job:
            user.job_profile_id = job.id
            user.target_cargo = job.name

    if new_password:
        user.password = hash_password(new_password)

    user.updated_at = datetime.now()
    db.commit()

    flash(
        request,
        "success",
        "Tu información de perfil ha sido actualizada exitosamente.",
    )
    return RedirectResponse(url="/perfil", status_code=302)


@router.get("/banco-preguntas")
def question_bank(
    request: Request,
    job_id: Optional[str] = None,
    axis_id: Optional[str] = None,
    search: Optional[str] = None,
    difficulty: Optional[str] = None,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not user.has_verified_email():
        return RedirectResponse(url="/email/verify", status_code=302)

    jobs = (
        db.query(JobProfile)
        .filter(JobProfile.is_active.is_(True))
        .order_by(JobProfile.id)
        .all()
    )

    resolved_job_id: Any = job_id if job_id else (user.job_profile_id or 1)
    if str(resolved_job_id) != "todos" and str(resolved_job_id).isdigit():
        resolved_job_id = int(resolved_job_id)

    if resolved_job_id == "todos":
        axes = db.query(ThematicAxis).order_by(ThematicAxis.name).all()
        query = (
            db.query(Question)
            .options(joinedload(Question.thematic_axis), joinedload(Question.job_profile))
            .filter(Question.is_active.is_(True))
        )
    else:
        axes = (
            db.query(ThematicAxis)
            .filter(ThematicAxis.job_profile_id == int(resolved_job_id))
            .order_by(ThematicAxis.name)
            .all()
        )
        query = (
            Question.for_cargo_query(db, int(resolved_job_id))
            .options(joinedload(Question.thematic_axis), joinedload(Question.job_profile))
            .filter(Question.is_active.is_(True))
        )

    resolved_axis_id: Optional[int] = None
    if axis_id and str(axis_id).strip().isdigit():
        resolved_axis_id = int(str(axis_id).strip())
        query = query.filter(Question.thematic_axis_id == resolved_axis_id)

    if difficulty and difficulty.strip():
        query = query.filter(Question.difficulty == difficulty.strip())

    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.filter(
            or_(
                Question.stem.ilike(term),
                Question.case_text.ilike(term),
                Question.legal_basis.ilike(term),
            )
        )

    query = query.order_by(Question.id)
    paginator = Paginator(query, request, per_page=15)

    return render_template(
        request,
        "question_bank/index.html",
        {
            "questions": paginator,
            "jobs": jobs,
            "axes": axes,
            "jobId": resolved_job_id,
            "axisId": resolved_axis_id,
            "search": search or "",
            "difficulty": difficulty or "",
            "user": user,
        },
        db,
    )
