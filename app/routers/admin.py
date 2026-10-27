import csv
import hashlib
import io
import json
import math
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
    Response,
    FileResponse,
)
from sqlalchemy import func, or_, case
from sqlalchemy.orm import Session, joinedload
from app.config import BASE_DIR
from app.database import get_db
from app.models import (
    User,
    JobProfile,
    ThematicAxis,
    Question,
    ExamSession,
    SessionAnswer,
)
from app.security import get_current_user, hash_password, flash
from app.pagination import Paginator
from app.templates_config import render_template

router = APIRouter(prefix="/admin")


def _require_admin(request: Request, db: Session) -> Optional[User]:
    user = get_current_user(request, db)
    if not user or not user.is_admin():
        return None
    return user


def _sync_questions_json(db: Session) -> None:
    try:
        data_dir = BASE_DIR / "database" / "seeders" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        q_path = data_dir / "questions.json"
        all_q = db.query(Question).order_by(Question.id).all()
        payload = [
            {
                "id": q.id,
                "job_profile_id": q.job_profile_id,
                "thematic_axis_id": q.thematic_axis_id,
                "question_type": q.question_type,
                "difficulty": q.difficulty,
                "case_text": q.case_text,
                "stem": q.stem,
                "options": q.options,
                "correct_option": q.correct_option,
                "legal_basis": q.legal_basis,
                "explanation_summary": q.explanation_summary,
                "is_active": q.is_active,
                "is_shared": q.is_shared,
                "scope_level": q.scope_level,
            }
            for q in all_q
        ]
        q_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def _build_full_backup_payload(db: Session) -> Dict[str, Any]:
    jobs = db.query(JobProfile).order_by(JobProfile.id).all()
    axes = (
        db.query(ThematicAxis)
        .options(joinedload(ThematicAxis.job_profile))
        .order_by(ThematicAxis.id)
        .all()
    )
    questions = (
        db.query(Question)
        .options(joinedload(Question.job_profile), joinedload(Question.thematic_axis))
        .order_by(Question.id)
        .all()
    )
    users = db.query(User).order_by(User.id).all()

    analista2_job = db.query(JobProfile).filter(JobProfile.name == "Analista II").first()
    analista3_job = db.query(JobProfile).filter(JobProfile.name == "Analista III").first()
    gestor1_job = db.query(JobProfile).filter(JobProfile.name == "Gestor I").first()
    gestor3_job = db.query(JobProfile).filter(JobProfile.name == "Gestor III").first()

    return {
        "metadata": {
            "sistema": "Simulador Oficial Concurso DIAN 2676 — CNSC",
            "tipo_backup": "BACKUP_TOTAL_BANCO_DE_PREGUNTAS_Y_SISTEMA",
            "fecha_generacion": datetime.now().isoformat(),
            "version_motor": "1.0.0-PRO-PY",
            "total_cargos": len(jobs),
            "total_ejes": len(axes),
            "total_preguntas": len(questions),
            "preguntas_analista_ii": Question.for_cargo_query(
                db, analista2_job.id if analista2_job else None
            ).count(),
            "preguntas_analista_iii": Question.for_cargo_query(
                db, analista3_job.id if analista3_job else None
            ).count(),
            "preguntas_gestor_i": Question.for_cargo_query(
                db, gestor1_job.id if gestor1_job else None
            ).count(),
            "preguntas_gestor_iii": Question.for_cargo_query(
                db, gestor3_job.id if gestor3_job else None
            ).count(),
            "preguntas_nivel_tecnico": sum(
                1 for q in questions if q.scope_level == "tecnico"
            ),
            "preguntas_compartidas_transversales": sum(
                1
                for q in questions
                if q.is_shared or q.scope_level == "transversal"
            ),
            "total_usuarios": len(users),
        },
        "job_profiles": [
            {
                "id": j.id,
                "code": j.code,
                "name": j.name,
                "level": j.level,
                "grade": j.grade,
                "process": j.process,
                "subprocess": j.subprocess,
                "purpose": j.purpose,
                "is_active": j.is_active,
            }
            for j in jobs
        ],
        "thematic_axes": [
            {
                "id": a.id,
                "job_profile_id": a.job_profile_id,
                "category": a.category,
                "name": a.name,
                "description": a.description,
                "weight_pct": float(a.weight_pct or 0),
            }
            for a in axes
        ],
        "questions": [
            {
                "id": q.id,
                "job_profile_id": q.job_profile_id,
                "job_profile_name": q.job_profile.name if q.job_profile else None,
                "is_shared": q.is_shared,
                "scope_level": q.scope_level,
                "thematic_axis_id": q.thematic_axis_id,
                "thematic_axis_name": q.thematic_axis.name if q.thematic_axis else None,
                "question_type": q.question_type,
                "difficulty": q.difficulty,
                "case_text": q.case_text,
                "stem": q.stem,
                "options": q.options,
                "correct_option": q.correct_option,
                "legal_basis": q.legal_basis,
                "explanation_summary": q.explanation_summary,
                "is_active": q.is_active,
            }
            for q in questions
        ],
        "users_metadata": [
            {
                "id": u.id,
                "name": u.name,
                "email": u.email,
                "role": u.role,
                "target_cargo": u.target_cargo,
                "job_profile_id": u.job_profile_id,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in users
        ],
    }


def _execute_backup_restore(
    db: Session, data: Any, overwrite: bool = False
) -> Dict[str, Any]:
    if isinstance(data, list) and data and isinstance(data[0], dict) and "stem" in data[0]:
        data = {"questions": data}

    if not isinstance(data, dict):
        return {
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
            "total": db.query(Question).count(),
            "message": "Formato de datos no reconocido.",
        }

    for jp in data.get("job_profiles") or []:
        name = jp.get("name")
        if not name:
            continue
        existing_jp = db.query(JobProfile).filter(JobProfile.name == name).first()
        if not existing_jp:
            db.add(
                JobProfile(
                    name=name,
                    code=jp.get("code") or "AT-000",
                    level=jp.get("level") or "Profesional",
                    grade=jp.get("grade") or "01",
                    process=jp.get("process"),
                    subprocess=jp.get("subprocess"),
                    purpose=jp.get("purpose"),
                    is_active=jp.get("is_active", True),
                    created_at=datetime.now(),
                )
            )
    db.commit()

    axis_map_by_name: Dict[str, int] = {}
    axis_map_by_id: Dict[int, int] = {}
    axes_processed = 0

    for ax in data.get("thematic_axes") or []:
        ax_name = ax.get("name")
        if not ax_name:
            continue
        axis = db.query(ThematicAxis).filter(ThematicAxis.name == ax_name).first()
        if not axis:
            axis = ThematicAxis(
                name=ax_name,
                category=ax.get("category") or "funcional_especifica",
                description=ax.get("description"),
                weight_pct=ax.get("weight_pct", 10.0),
                job_profile_id=ax.get("job_profile_id", 1),
                created_at=datetime.now(),
            )
            db.add(axis)
            db.commit()
            db.refresh(axis)
        axis_map_by_name[ax_name] = axis.id
        if ax.get("id") is not None:
            axis_map_by_id[int(ax["id"])] = axis.id
        axes_processed += 1

    inserted = 0
    updated = 0
    skipped = 0

    for q in data.get("questions") or []:
        stem = str(q.get("stem") or "").strip()
        if not stem:
            continue

        axis_id = None
        if q.get("thematic_axis_name") and q["thematic_axis_name"] in axis_map_by_name:
            axis_id = axis_map_by_name[q["thematic_axis_name"]]
        elif q.get("thematic_axis_id") and int(q["thematic_axis_id"]) in axis_map_by_id:
            axis_id = axis_map_by_id[int(q["thematic_axis_id"])]
        elif q.get("thematic_axis_id"):
            axis_id = int(q["thematic_axis_id"])

        options = q.get("options")
        if not options and q.get("option_a"):
            corr = str(q.get("correct_option") or "A").upper()
            options = [
                {"key": "A", "text": q.get("option_a"), "is_correct": corr == "A"},
                {"key": "B", "text": q.get("option_b"), "is_correct": corr == "B"},
                {"key": "C", "text": q.get("option_c"), "is_correct": corr == "C"},
            ]
            if q.get("option_d"):
                options.append(
                    {"key": "D", "text": q.get("option_d"), "is_correct": corr == "D"}
                )

        job_id = q.get("job_profile_id")
        eq_query = db.query(Question).filter(Question.stem == stem)
        if job_id:
            eq_query = eq_query.filter(Question.job_profile_id == job_id)
        else:
            eq_query = eq_query.filter(Question.job_profile_id.is_(None))
        if axis_id:
            eq_query = eq_query.filter(Question.thematic_axis_id == axis_id)
        else:
            eq_query = eq_query.filter(Question.thematic_axis_id.is_(None))

        existing = eq_query.first()
        if existing:
            if overwrite:
                existing.thematic_axis_id = axis_id or existing.thematic_axis_id
                existing.case_text = q.get("case_text", existing.case_text)
                existing.options = options or existing.options
                existing.correct_option = q.get(
                    "correct_option", existing.correct_option
                )
                existing.legal_basis = q.get("legal_basis", existing.legal_basis)
                existing.explanation_summary = q.get(
                    "explanation_summary",
                    q.get("rationale", existing.explanation_summary),
                )
                existing.question_type = q.get(
                    "question_type", existing.question_type
                )
                existing.difficulty = q.get("difficulty", existing.difficulty)
                existing.is_shared = q.get("is_shared", existing.is_shared)
                existing.scope_level = q.get("scope_level", existing.scope_level)
                existing.is_active = q.get("is_active", existing.is_active)
                updated += 1
            else:
                skipped += 1
        else:
            db.add(
                Question(
                    job_profile_id=job_id,
                    thematic_axis_id=axis_id,
                    case_text=q.get("case_text"),
                    stem=stem,
                    options=options or [],
                    correct_option=q.get("correct_option", "A"),
                    explanation_summary=q.get(
                        "explanation_summary", q.get("rationale")
                    ),
                    legal_basis=q.get("legal_basis"),
                    question_type=q.get("question_type", "situacional"),
                    difficulty=q.get("difficulty", "Media"),
                    is_active=q.get("is_active", True),
                    is_shared=q.get("is_shared", True),
                    scope_level=q.get("scope_level", "transversal"),
                    created_at=datetime.now(),
                )
            )
            inserted += 1

    db.commit()
    _sync_questions_json(db)

    total_in_db = db.query(Question).count()
    msg = (
        f"Se insertaron {inserted} reactivos nuevos, {updated} actualizados y "
        f"{skipped} se conservaron sin cambios. Total actual en el banco: {total_in_db} "
        f"preguntas (Ejes procesados: {axes_processed})."
    )
    return {
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "total": total_in_db,
        "message": msg,
    }


@router.get("")
@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = _require_admin(request, db)
    if not user:
        return RedirectResponse(url="/dashboard", status_code=302)

    total_users = db.query(User).count()
    total_aspirantes = db.query(User).filter(User.role == "aspirante").count()
    total_admins = (
        db.query(User).filter(User.role.in_(["admin", "superadmin"])).count()
    )

    total_questions = db.query(Question).count()
    active_questions = (
        db.query(Question).filter(Question.is_active.is_(True)).count()
    )
    transversal_questions = (
        db.query(Question)
        .filter(
            Question.is_active.is_(True),
            or_(Question.is_shared.is_(True), Question.job_profile_id.is_(None)),
        )
        .count()
    )

    all_completed = (
        db.query(ExamSession)
        .filter(ExamSession.completed_at.isnot(None))
        .all()
    )
    total_exams_completed = len(all_completed)
    passed_exams_count = sum(1 for s in all_completed if s.is_passed)
    global_pass_rate = (
        round((passed_exams_count / total_exams_completed) * 100, 1)
        if total_exams_completed > 0
        else 0
    )
    global_avg_score = (
        round(
            sum(float(s.score or 0) for s in all_completed) / total_exams_completed,
            2,
        )
        if total_exams_completed > 0
        else 0.0
    )

    cutoff_24h = datetime.now() - timedelta(hours=24)
    live_sessions = (
        db.query(ExamSession)
        .options(
            joinedload(ExamSession.user),
            joinedload(ExamSession.job_profile),
            joinedload(ExamSession.session_answers),
        )
        .filter(
            ExamSession.completed_at.is_(None),
            ExamSession.mode.in_(["simulacro_oficial", "simulacro_express"]),
            or_(
                ExamSession.updated_at >= cutoff_24h,
                ExamSession.created_at >= cutoff_24h,
            ),
            ExamSession.time_spent_seconds < ExamSession.time_limit_seconds,
        )
        .order_by(ExamSession.updated_at.desc())
        .all()
    )
    now_ts = datetime.now()
    for ls in live_sessions:
        ls.session_answers_count = len(ls.session_answers)
        ls.is_live_now = bool(
            ls.updated_at
            and ls.updated_at >= (now_ts - timedelta(minutes=3))
            and ls.updated_at <= (now_ts + timedelta(minutes=1))
        )

    jobs = (
        db.query(JobProfile)
        .filter(JobProfile.is_active.is_(True))
        .order_by(JobProfile.id)
        .all()
    )
    cargo_stats = []
    for j in jobs:
        j_sessions = [s for s in all_completed if s.job_profile_id == j.id]
        j_users_count = (
            db.query(User).filter(User.job_profile_id == j.id).count()
        )
        j_questions_count = (
            Question.for_cargo_query(db, j.id)
            .filter(Question.is_active.is_(True))
            .count()
        )
        j_total = len(j_sessions)
        j_passed = sum(1 for s in j_sessions if s.is_passed)
        cargo_stats.append(
            {
                "job": j,
                "users_count": j_users_count,
                "questions_count": j_questions_count,
                "exams_count": j_total,
                "avg_score": round(
                    sum(float(s.score or 0) for s in j_sessions) / j_total, 2
                )
                if j_total > 0
                else 0.0,
                "pass_rate": round((j_passed / j_total) * 100, 1)
                if j_total > 0
                else 0,
            }
        )

    recent_sessions = (
        db.query(ExamSession)
        .options(joinedload(ExamSession.user), joinedload(ExamSession.job_profile))
        .filter(ExamSession.completed_at.isnot(None))
        .order_by(ExamSession.completed_at.desc())
        .limit(10)
        .all()
    )

    return render_template(
        request,
        "admin/dashboard.html",
        {
            "totalUsers": total_users,
            "totalAspirantes": total_aspirantes,
            "totalAdmins": total_admins,
            "totalQuestions": total_questions,
            "activeQuestions": active_questions,
            "transversalQuestions": transversal_questions,
            "totalExamsCompleted": total_exams_completed,
            "globalPassRate": global_pass_rate,
            "globalAvgScore": global_avg_score,
            "liveSessions": live_sessions,
            "cargoStats": cargo_stats,
            "recentSessions": recent_sessions,
        },
        db,
    )


@router.get("/usuarios")
def users_list(
    request: Request,
    search: Optional[str] = None,
    job_id: Optional[str] = None,
    role: Optional[str] = None,
    db: Session = Depends(get_db),
):
    user = _require_admin(request, db)
    if not user:
        return RedirectResponse(url="/dashboard", status_code=302)

    query = db.query(User).options(
        joinedload(User.job_profile), joinedload(User.exam_sessions)
    )

    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.filter(
            or_(User.name.ilike(term), User.email.ilike(term))
        )

    resolved_job_id: Optional[int] = None
    if job_id and str(job_id).strip().isdigit():
        resolved_job_id = int(str(job_id).strip())
        query = query.filter(User.job_profile_id == resolved_job_id)

    if role and role.strip():
        query = query.filter(User.role == role.strip())

    query = query.order_by(User.id.desc())
    paginator = Paginator(query, request, per_page=15)

    from app.ai_service import get_ai_config
    ai_cfg = get_ai_config()
    unlimited_ids = {int(x) for x in (ai_cfg.get("unlimited_user_ids") or []) if str(x).isdigit()}
    daily_limit = int(ai_cfg.get("daily_limit_per_user") or 25)

    for u in paginator.items:
        completed = [s for s in u.exam_sessions if s.completed_at is not None]
        u.exams_count = len(completed)
        u.passed_exams_count = sum(1 for s in completed if s.is_passed)
        u.is_ai_unlimited = u.is_admin() or (u.id in unlimited_ids)

    jobs = (
        db.query(JobProfile)
        .filter(JobProfile.is_active.is_(True))
        .order_by(JobProfile.id)
        .all()
    )

    return render_template(
        request,
        "admin/users.html",
        {
            "users": paginator,
            "jobs": jobs,
            "search": search or "",
            "jobId": resolved_job_id,
            "role": role or "",
            "unlimitedIds": unlimited_ids,
            "dailyLimit": daily_limit,
        },
        db,
    )


@router.post("/usuarios")
def store_user(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    role: str = Form("aspirante"),
    job_profile_id: int = Form(1),
    db: Session = Depends(get_db),
):
    admin_user = _require_admin(request, db)
    if not admin_user:
        return RedirectResponse(url="/dashboard", status_code=302)

    email_clean = email.strip().lower()
    if db.query(User).filter(User.email == email_clean).first():
        flash(request, "error", "El correo electrónico ya está registrado.")
        return RedirectResponse(url="/admin/usuarios", status_code=302)

    job = db.query(JobProfile).filter(JobProfile.id == job_profile_id).first()
    if not job:
        flash(request, "error", "El cargo seleccionado no es válido.")
        return RedirectResponse(url="/admin/usuarios", status_code=302)

    new_user = User(
        name=name.strip(),
        email=email_clean,
        password=hash_password(password),
        role=role if role in ("aspirante", "admin", "superadmin") else "aspirante",
        job_profile_id=job.id,
        target_cargo=job.name,
        email_verified_at=datetime.now(),
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    db.add(new_user)
    db.commit()

    flash(
        request,
        "success",
        f"¡Usuario {new_user.name} creado exitosamente! Correo: {new_user.email}. Ya puede iniciar sesión en la plataforma.",
    )
    return RedirectResponse(url="/admin/usuarios", status_code=302)


@router.get("/usuarios/{user_id}")
def user_detail(user_id: int, request: Request, db: Session = Depends(get_db)):
    admin_user = _require_admin(request, db)
    if not admin_user:
        return RedirectResponse(url="/dashboard", status_code=302)

    aspirante = (
        db.query(User)
        .options(joinedload(User.job_profile))
        .filter(User.id == user_id)
        .first()
    )
    if not aspirante:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    jobs = (
        db.query(JobProfile)
        .filter(JobProfile.is_active.is_(True))
        .order_by(JobProfile.id)
        .all()
    )
    sessions = (
        db.query(ExamSession)
        .options(joinedload(ExamSession.job_profile))
        .filter(ExamSession.user_id == aspirante.id)
        .order_by(ExamSession.id.desc())
        .all()
    )
    completed = [s for s in sessions if s.completed_at is not None]
    total = len(completed)
    passed = sum(1 for s in completed if s.is_passed)
    avg_score = (
        round(sum(float(s.score or 0) for s in completed) / total, 2)
        if total > 0
        else 0.0
    )
    best_score = (
        max(float(s.score or 0) for s in completed) if total > 0 else 0.0
    )

    return render_template(
        request,
        "admin/user_detail.html",
        {
            "aspirante": aspirante,
            "jobs": jobs,
            "sessions": sessions,
            "total": total,
            "passed": passed,
            "avgScore": avg_score,
            "bestScore": best_score,
        },
        db,
    )


@router.put("/usuarios/{user_id}")
@router.delete("/usuarios/{user_id}")
@router.post("/usuarios/{user_id}")
@router.post("/usuarios/{user_id}/actualizar")
@router.post("/usuarios/{user_id}/eliminar")
async def update_or_delete_user(
    user_id: int, request: Request, db: Session = Depends(get_db)
):
    admin_user = _require_admin(request, db)
    if not admin_user:
        return RedirectResponse(url="/dashboard", status_code=302)

    form = await request.form()
    method = str(form.get("_method", request.method)).upper()
    if request.url.path.endswith("/eliminar"):
        method = "DELETE"

    if method == "DELETE":
        if admin_user.id == user_id:
            flash(
                request,
                "error",
                "No puedes eliminar tu propia cuenta de administrador.",
            )
            return RedirectResponse(url="/admin/usuarios", status_code=302)

        user_to_delete = db.query(User).filter(User.id == user_id).first()
        if not user_to_delete:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")

        user_name = user_to_delete.name
        sessions = (
            db.query(ExamSession)
            .filter(ExamSession.user_id == user_to_delete.id)
            .all()
        )
        for s in sessions:
            db.query(SessionAnswer).filter(
                SessionAnswer.exam_session_id == s.id
            ).delete()
            db.delete(s)
        db.delete(user_to_delete)
        db.commit()

        flash(
            request,
            "success",
            f"El usuario {user_name} ha sido eliminado permanentemente del sistema.",
        )
        return RedirectResponse(url="/admin/usuarios", status_code=302)

    # PUT / updateUser
    aspirante = db.query(User).filter(User.id == user_id).first()
    if not aspirante:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    name = str(form.get("name") or aspirante.name).strip()
    email = str(form.get("email") or aspirante.email).strip().lower()
    role = str(form.get("role") or aspirante.role).strip()
    job_profile_id = int(form.get("job_profile_id") or aspirante.job_profile_id or 1)
    password = str(form.get("password") or "").strip()

    job = db.query(JobProfile).filter(JobProfile.id == job_profile_id).first()
    if job:
        aspirante.job_profile_id = job.id
        aspirante.target_cargo = job.name

    aspirante.name = name
    aspirante.email = email
    if role in ("aspirante", "admin", "superadmin"):
        aspirante.role = role
    if password:
        aspirante.password = hash_password(password)
    aspirante.updated_at = datetime.now()
    db.commit()

    flash(request, "success", f"Usuario {aspirante.name} actualizado con éxito.")
    referer = request.headers.get("referer") or f"/admin/usuarios/{user_id}"
    return RedirectResponse(url=referer, status_code=302)


@router.post("/usuarios/{user_id}/reiniciar-intentos")
def reset_attempts(
    user_id: int, request: Request, db: Session = Depends(get_db)
):
    admin_user = _require_admin(request, db)
    if not admin_user:
        return RedirectResponse(url="/dashboard", status_code=302)

    aspirante = db.query(User).filter(User.id == user_id).first()
    if not aspirante:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    sessions = (
        db.query(ExamSession).filter(ExamSession.user_id == aspirante.id).all()
    )
    count = len(sessions)
    for s in sessions:
        db.query(SessionAnswer).filter(
            SessionAnswer.exam_session_id == s.id
        ).delete()
        db.delete(s)
    db.commit()

    flash(
        request,
        "success",
        f"Se han reiniciado los {count} simulacros de {aspirante.name}. El aspirante ahora puede comenzar pruebas desde cero.",
    )
    referer = request.headers.get("referer") or f"/admin/usuarios/{user_id}"
    return RedirectResponse(url=referer, status_code=302)


@router.post("/sesiones/{session_id}/eliminar")
@router.post("/simulacros/{session_id}/eliminar")
@router.delete("/sesiones/{session_id}")
@router.delete("/simulacros/{session_id}")
def delete_single_session(
    session_id: int, request: Request, db: Session = Depends(get_db)
):
    admin_user = _require_admin(request, db)
    if not admin_user:
        return RedirectResponse(url="/dashboard", status_code=302)

    session = db.query(ExamSession).filter(ExamSession.id == session_id).first()
    if not session:
        flash(
            request,
            "error",
            f"La sesión de simulacro #{session_id} no fue encontrada o ya fue eliminada.",
        )
        referer = request.headers.get("referer") or "/admin/sesiones"
        return RedirectResponse(url=referer, status_code=302)

    user_name = session.user.name if session.user else f"Usuario #{session.user_id}"
    user_id = session.user_id

    # Eliminar respuestas asociadas primero (evitar restricción de integridad referencial)
    db.query(SessionAnswer).filter(
        SessionAnswer.exam_session_id == session.id
    ).delete()

    db.delete(session)
    db.commit()

    flash(
        request,
        "success",
        f"Se ha eliminado exitosamente el simulacro #{session_id} de {user_name}.",
    )
    referer = request.headers.get("referer")
    if (
        referer
        and "/resultado" not in referer
        and f"/simulacro/{session_id}" not in referer
    ):
        return RedirectResponse(url=referer, status_code=302)
    if user_id:
        return RedirectResponse(url=f"/admin/usuarios/{user_id}", status_code=302)
    return RedirectResponse(url="/admin/sesiones", status_code=302)


@router.get("/simulacros")
@router.get("/sesiones")
def exam_sessions(
    request: Request,
    status: str = "all",
    job_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    admin_user = _require_admin(request, db)
    if not admin_user:
        return RedirectResponse(url="/dashboard", status_code=302)

    query = db.query(ExamSession).options(
        joinedload(ExamSession.user), joinedload(ExamSession.job_profile)
    )

    if status == "in_progress":
        cutoff_5h = datetime.now() - timedelta(hours=5)
        query = query.filter(
            ExamSession.completed_at.is_(None),
            ExamSession.created_at >= cutoff_5h,
        )
    elif status == "completed":
        query = query.filter(ExamSession.completed_at.isnot(None))
    elif status == "passed":
        query = query.filter(
            ExamSession.completed_at.isnot(None), ExamSession.is_passed.is_(True)
        )
    elif status == "failed":
        query = query.filter(
            ExamSession.completed_at.isnot(None), ExamSession.is_passed.is_(False)
        )

    resolved_job_id: Optional[int] = None
    if job_id and str(job_id).strip().isdigit():
        resolved_job_id = int(str(job_id).strip())
        query = query.filter(ExamSession.job_profile_id == resolved_job_id)

    query = query.order_by(ExamSession.id.desc())
    paginator = Paginator(query, request, per_page=20)
    jobs = (
        db.query(JobProfile)
        .filter(JobProfile.is_active.is_(True))
        .order_by(JobProfile.id)
        .all()
    )

    return render_template(
        request,
        "admin/sessions.html",
        {
            "sessions": paginator,
            "jobs": jobs,
            "status": status,
            "jobId": resolved_job_id,
        },
        db,
    )


@router.get("/analitica-preguntas")
@router.get("/preguntas-criticas")
def question_analytics(request: Request, db: Session = Depends(get_db)):
    admin_user = _require_admin(request, db)
    if not admin_user:
        return RedirectResponse(url="/dashboard", status_code=302)

    rows = (
        db.query(
            Question.id,
            func.count(SessionAnswer.id).label("total_attempts"),
            func.sum(
                case((SessionAnswer.is_correct.is_(False), 1), else_=0)
            ).label("fail_count"),
            func.sum(
                case((SessionAnswer.is_correct.is_(True), 1), else_=0)
            ).label("success_count"),
        )
        .join(SessionAnswer, SessionAnswer.question_id == Question.id)
        .group_by(Question.id)
        .having(func.count(SessionAnswer.id) > 0)
        .order_by(
            func.sum(
                case((SessionAnswer.is_correct.is_(False), 1), else_=0)
            ).desc()
        )
        .limit(25)
        .all()
    )

    q_ids = [r[0] for r in rows]
    q_objs = (
        db.query(Question)
        .options(joinedload(Question.thematic_axis), joinedload(Question.job_profile))
        .filter(Question.id.in_(q_ids))
        .all()
        if q_ids
        else []
    )
    q_by_id = {q.id: q for q in q_objs}

    failed_questions = []
    for qid, total_att, fail_c, succ_c in rows:
        q = q_by_id.get(qid)
        if q:
            q.total_attempts = int(total_att or 0)
            q.fail_count = int(fail_c or 0)
            q.success_count = int(succ_c or 0)
            failed_questions.append(q)

    return render_template(
        request,
        "admin/question_analytics.html",
        {"failedQuestions": failed_questions},
        db,
    )


@router.get("/preguntas")
@router.get("/preguntas-gestion")
def questions_index(
    request: Request,
    search: Optional[str] = None,
    job_id: Optional[str] = None,
    axis_id: Optional[str] = None,
    difficulty: Optional[str] = None,
    duplicates: Optional[str] = None,
    only_cases: Optional[str] = None,
    db: Session = Depends(get_db),
):
    admin_user = _require_admin(request, db)
    if not admin_user:
        return RedirectResponse(url="/dashboard", status_code=302)

    view_duplicates = str(duplicates or "").lower() in ("1", "true", "yes", "on")
    view_only_cases = str(only_cases or "").lower() in ("1", "true", "yes", "on")

    jobs = (
        db.query(JobProfile)
        .filter(JobProfile.is_active.is_(True))
        .order_by(JobProfile.id)
        .all()
    )
    axes = db.query(ThematicAxis).order_by(ThematicAxis.name).all()

    query = db.query(Question).options(
        joinedload(Question.job_profile), joinedload(Question.thematic_axis)
    )

    dup_rows = (
        db.query(Question.stem, func.count(Question.id).label("c"))
        .filter(Question.is_active.is_(True))
        .group_by(Question.stem)
        .having(func.count(Question.id) > 1)
        .all()
    )
    total_duplicates = sum(int(r[1]) for r in dup_rows)
    duplicate_stems = [r[0] for r in dup_rows]

    resolved_job_id: Any = job_id or ""
    if str(resolved_job_id).isdigit():
        resolved_job_id = int(resolved_job_id)

    resolved_axis_id: Optional[int] = (
        int(axis_id) if axis_id and str(axis_id).isdigit() else None
    )

    if view_duplicates:
        if duplicate_stems:
            query = query.filter(Question.stem.in_(duplicate_stems)).order_by(
                Question.stem, Question.id
            )
        else:
            query = query.filter(Question.id == -1)
    else:
        if search and search.strip():
            s_clean = search.strip()
            term = f"%{s_clean}%"
            conds = [
                Question.stem.ilike(term),
                Question.case_text.ilike(term),
                Question.legal_basis.ilike(term),
                Question.explanation_summary.ilike(term),
            ]
            if s_clean.isdigit():
                conds.insert(0, Question.id == int(s_clean))
            query = query.filter(or_(*conds))

        if resolved_job_id:
            if resolved_job_id == "globales":
                query = query.filter(Question.job_profile_id.is_(None))
            elif isinstance(resolved_job_id, int):
                query = query.filter(Question.job_profile_id == resolved_job_id)

        if resolved_axis_id:
            query = query.filter(Question.thematic_axis_id == resolved_axis_id)

        if difficulty and difficulty.strip():
            query = query.filter(Question.difficulty == difficulty.strip())

        if view_only_cases:
            query = query.filter(
                Question.case_text.isnot(None), Question.case_text != ""
            )

        query = query.order_by(Question.id.desc())

    paginator = Paginator(query, request, per_page=20)

    return render_template(
        request,
        "admin/questions/index.html",
        {
            "questions": paginator,
            "jobs": jobs,
            "axes": axes,
            "search": search or "",
            "jobId": resolved_job_id,
            "axisId": resolved_axis_id,
            "difficulty": difficulty or "",
            "viewDuplicates": view_duplicates,
            "onlyCases": view_only_cases,
            "totalDuplicates": total_duplicates,
        },
        db,
    )


@router.get("/preguntas/plantilla-csv")
def download_csv_template(request: Request, db: Session = Depends(get_db)):
    admin_user = _require_admin(request, db)
    if not admin_user:
        return RedirectResponse(url="/dashboard", status_code=302)

    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow(
        [
            "enunciado",
            "caso_estudio",
            "opcion_a",
            "opcion_b",
            "opcion_c",
            "opcion_d",
            "clave_correcta",
            "eje_tematico_id",
            "dificultad",
            "fundamento_legal",
            "explicacion_resumen",
        ]
    )
    writer.writerow(
        [
            "¿Cuál es el término legal para responder un derecho de petición de interés general?",
            "Un ciudadano radica una solicitud ante la DIAN requiriendo información sobre un trámite.",
            "15 días hábiles.",
            "10 días hábiles.",
            "30 días calendario.",
            "",
            "A",
            "6",
            "medio",
            "CPACA Ley 1437 de 2011 Artículo 14",
            "El término general para resolver peticiones de interés general es de 15 días hábiles siguientes a su recepción.",
        ]
    )
    return Response(
        content=output.getvalue().encode("utf-8"),
        media_type="text/csv; charset=UTF-8",
        headers={
            "Content-Disposition": 'attachment; filename="plantilla_preguntas_dian2676.csv"'
        },
    )


@router.post("/preguntas/importar-archivo")
async def import_questions_file(
    request: Request,
    questions_file: UploadFile = File(...),
    default_axis_id: Optional[str] = Form(None),
    default_job_id: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    admin_user = _require_admin(request, db)
    if not admin_user:
        return RedirectResponse(url="/dashboard", status_code=302)

    raw_bytes = await questions_file.read()
    filename = (questions_file.filename or "").lower()

    first_axis = db.query(ThematicAxis).first()
    resolved_axis_id = (
        int(default_axis_id)
        if default_axis_id and str(default_axis_id).isdigit()
        else (first_axis.id if first_axis else None)
    )
    resolved_job_id = (
        int(default_job_id)
        if default_job_id and str(default_job_id).isdigit()
        else None
    )

    inserted_count = 0
    skipped_count = 0

    if filename.endswith(".json"):
        try:
            data = json.loads(raw_bytes.decode("utf-8-sig"))
        except Exception:
            flash(request, "error", "El archivo JSON no tiene un formato válido.")
            return RedirectResponse(url="/admin/preguntas", status_code=302)

        q_list = data.get("questions", data) if isinstance(data, dict) else data
        if not isinstance(q_list, list):
            flash(request, "error", "El archivo JSON no contiene una lista de preguntas.")
            return RedirectResponse(url="/admin/preguntas", status_code=302)

        for row in q_list:
            if not isinstance(row, dict):
                continue
            stem = str(row.get("stem") or row.get("enunciado") or "").strip()
            if not stem:
                continue
            if db.query(Question.id).filter(Question.stem == stem).first():
                skipped_count += 1
                continue

            correct = str(
                row.get("correct_option") or row.get("clave_correcta") or "A"
            ).strip().upper()
            options = row.get("options")
            if not options:
                options = [
                    {
                        "key": "A",
                        "text": row.get("option_a") or row.get("opcion_a") or "",
                        "is_correct": correct == "A",
                    },
                    {
                        "key": "B",
                        "text": row.get("option_b") or row.get("opcion_b") or "",
                        "is_correct": correct == "B",
                    },
                    {
                        "key": "C",
                        "text": row.get("option_c") or row.get("opcion_c") or "",
                        "is_correct": correct == "C",
                    },
                ]
                opt_d = row.get("option_d") or row.get("opcion_d")
                if opt_d:
                    options.append(
                        {"key": "D", "text": opt_d, "is_correct": correct == "D"}
                    )

            db.add(
                Question(
                    job_profile_id=row.get("job_profile_id") or resolved_job_id,
                    thematic_axis_id=row.get("thematic_axis_id") or resolved_axis_id,
                    case_text=row.get("case_text") or row.get("caso_estudio"),
                    stem=stem,
                    options=options,
                    correct_option=correct,
                    legal_basis=row.get("legal_basis")
                    or row.get("fundamento_legal"),
                    explanation_summary=row.get("explanation_summary")
                    or row.get("explicacion_resumen"),
                    question_type=row.get("question_type") or "situacional",
                    difficulty=row.get("difficulty") or "medio",
                    is_active=True,
                    is_shared=True,
                    scope_level="transversal",
                    created_at=datetime.now(),
                )
            )
            inserted_count += 1
    else:
        text = raw_bytes.decode("utf-8-sig", errors="replace")
        lines = text.splitlines()
        if not lines:
            flash(request, "error", "El archivo CSV está vacío.")
            return RedirectResponse(url="/admin/preguntas", status_code=302)

        delimiter = ";" if lines[0].count(";") > lines[0].count(",") else ","
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        headers = next(reader, None)
        if not headers:
            flash(request, "error", "El archivo CSV está vacío.")
            return RedirectResponse(url="/admin/preguntas", status_code=302)

        clean_headers = [
            re.sub(r"[^a-zA-Z0-9_]", "", h).strip().lower() for h in headers
        ]

        for row in reader:
            if len(row) < 3:
                continue
            data_row = {
                clean_headers[i]: row[i].strip() if i < len(row) else ""
                for i in range(len(clean_headers))
            }
            stem = data_row.get("enunciado") or data_row.get("stem") or ""
            if not stem:
                continue
            if db.query(Question.id).filter(Question.stem == stem).first():
                skipped_count += 1
                continue

            correct = (
                data_row.get("clave_correcta")
                or data_row.get("correct_option")
                or "A"
            ).upper()
            if correct not in ("A", "B", "C", "D"):
                correct = "A"

            opt_a = data_row.get("opcion_a") or data_row.get("option_a") or ""
            opt_b = data_row.get("opcion_b") or data_row.get("option_b") or ""
            opt_c = data_row.get("opcion_c") or data_row.get("option_c") or ""
            opt_d = data_row.get("opcion_d") or data_row.get("option_d") or ""
            if not opt_a or not opt_b:
                continue

            options = [
                {"key": "A", "text": opt_a, "is_correct": correct == "A"},
                {"key": "B", "text": opt_b, "is_correct": correct == "B"},
            ]
            if opt_c:
                options.append(
                    {"key": "C", "text": opt_c, "is_correct": correct == "C"}
                )
            if opt_d:
                options.append(
                    {"key": "D", "text": opt_d, "is_correct": correct == "D"}
                )

            ax_raw = data_row.get("eje_tematico_id", "")
            axis_id = int(ax_raw) if ax_raw.isdigit() else resolved_axis_id

            db.add(
                Question(
                    job_profile_id=resolved_job_id,
                    thematic_axis_id=axis_id,
                    case_text=data_row.get("caso_estudio")
                    or data_row.get("case_text")
                    or None,
                    stem=stem,
                    options=options,
                    correct_option=correct,
                    legal_basis=data_row.get("fundamento_legal")
                    or data_row.get("legal_basis")
                    or None,
                    explanation_summary=data_row.get("explicacion_resumen")
                    or data_row.get("explanation_summary")
                    or None,
                    question_type="situacional",
                    difficulty=data_row.get("dificultad") or "medio",
                    is_active=True,
                    is_shared=True,
                    scope_level="transversal",
                    created_at=datetime.now(),
                )
            )
            inserted_count += 1

    db.commit()
    _sync_questions_json(db)

    flash(
        request,
        "success",
        f"¡Importación completada! Se insertaron {inserted_count} preguntas nuevas ({skipped_count} duplicadas omitidas).",
    )
    return RedirectResponse(url="/admin/preguntas", status_code=302)


@router.post("/preguntas/desactivar-duplicados")
def deactivate_duplicates(request: Request, db: Session = Depends(get_db)):
    admin_user = _require_admin(request, db)
    if not admin_user:
        return RedirectResponse(url="/dashboard", status_code=302)

    all_active = (
        db.query(Question)
        .filter(Question.is_active.is_(True))
        .order_by(Question.id.asc())
        .all()
    )
    groups: Dict[str, List[Question]] = {}
    for q in all_active:
        clean_case = (q.case_text or "").strip()
        clean_stem = (q.stem or "").strip()
        key = hashlib.md5(f"{clean_case}|||{clean_stem}".encode("utf-8")).hexdigest()
        groups.setdefault(key, []).append(q)

    deactivated_count = 0
    preserved_count = 0

    for items in groups.values():
        if len(items) > 1:
            primary = items[0]
            primary.is_shared = True
            primary.scope_level = "transversal"
            primary.is_active = True
            preserved_count += 1
            for dup in items[1:]:
                dup.is_active = False
                deactivated_count += 1

    db.commit()
    _sync_questions_json(db)

    flash(
        request,
        "success",
        f"¡Unificación exitosa! Se conservaron {preserved_count} preguntas como transversales únicas y se desactivaron {deactivated_count} copias redundantes.",
    )
    return RedirectResponse(url="/admin/preguntas", status_code=302)


@router.post("/preguntas")
async def store_question(request: Request, db: Session = Depends(get_db)):
    admin_user = _require_admin(request, db)
    if not admin_user:
        return RedirectResponse(url="/dashboard", status_code=302)

    form = await request.form()
    stem = str(form.get("stem") or "").strip()
    case_text = str(form.get("case_text") or "").strip() or None
    thematic_axis_id = int(form.get("thematic_axis_id") or 1)
    job_id_raw = str(form.get("job_profile_id") or "").strip()
    job_profile_id = int(job_id_raw) if job_id_raw.isdigit() else None
    difficulty = str(form.get("difficulty") or "medio").strip()
    question_type = str(form.get("question_type") or "situacional").strip()
    correct = str(form.get("correct_option") or "A").strip().upper()
    opt_a = str(form.get("option_a") or "").strip()
    opt_b = str(form.get("option_b") or "").strip()
    opt_c = str(form.get("option_c") or "").strip()
    opt_d = str(form.get("option_d") or "").strip()
    legal_basis = str(form.get("legal_basis") or "").strip() or None
    explanation_summary = str(form.get("explanation_summary") or "").strip() or None

    options = [
        {
            "key": "A",
            "text": opt_a,
            "is_correct": correct == "A",
            "justification": str(form.get("justification_a") or "")
            or (explanation_summary if correct == "A" else ""),
        },
        {
            "key": "B",
            "text": opt_b,
            "is_correct": correct == "B",
            "justification": str(form.get("justification_b") or "")
            or (explanation_summary if correct == "B" else ""),
        },
        {
            "key": "C",
            "text": opt_c,
            "is_correct": correct == "C",
            "justification": str(form.get("justification_c") or "")
            or (explanation_summary if correct == "C" else ""),
        },
    ]
    if opt_d:
        options.append(
            {
                "key": "D",
                "text": opt_d,
                "is_correct": correct == "D",
                "justification": str(form.get("justification_d") or "")
                or (explanation_summary if correct == "D" else ""),
            }
        )

    is_shared_raw = form.get("is_shared", "true")
    is_shared = str(is_shared_raw).lower() in ("1", "true", "yes", "on")
    scope_level = str(form.get("scope_level") or "").strip() or (
        "profesional" if job_profile_id else "transversal"
    )

    q = Question(
        job_profile_id=job_profile_id,
        thematic_axis_id=thematic_axis_id,
        case_text=case_text,
        stem=stem,
        options=options,
        correct_option=correct,
        explanation_summary=explanation_summary,
        legal_basis=legal_basis,
        question_type=question_type,
        difficulty=difficulty,
        is_active=True,
        is_shared=is_shared,
        scope_level=scope_level,
        created_at=datetime.now(),
    )
    db.add(q)
    db.commit()
    db.refresh(q)
    _sync_questions_json(db)

    flash(
        request,
        "success",
        f"¡Pregunta #{q.id} creada y agregada al banco con éxito!",
    )
    return RedirectResponse(url="/admin/preguntas", status_code=302)


@router.get("/preguntas/{question_id}/datos")
def get_question_data(
    question_id: int, request: Request, db: Session = Depends(get_db)
):
    admin_user = _require_admin(request, db)
    if not admin_user:
        return JSONResponse({"error": "No autorizado"}, status_code=403)

    q = (
        db.query(Question)
        .options(joinedload(Question.thematic_axis), joinedload(Question.job_profile))
        .filter(Question.id == question_id)
        .first()
    )
    if not q:
        return JSONResponse({"error": "No encontrada"}, status_code=404)

    return JSONResponse(
        {
            "id": q.id,
            "stem": q.stem,
            "case_text": q.case_text,
            "correct_option": q.correct_option,
            "option_a": q.option_a,
            "option_b": q.option_b,
            "option_c": q.option_c,
            "option_d": q.option_d,
            "legal_basis": q.legal_basis,
            "explanation_summary": q.explanation_summary,
            "thematic_axis_id": q.thematic_axis_id,
            "difficulty": q.difficulty,
            "is_active": bool(q.is_active),
            "job_profile_id": q.job_profile_id,
            "job_name": q.job_profile.name if q.job_profile else "Transversal / Todos",
            "axis_name": q.thematic_axis.name if q.thematic_axis else "General",
        }
    )


@router.put("/preguntas/{question_id}")
@router.post("/preguntas/{question_id}")
async def update_question(
    question_id: int, request: Request, db: Session = Depends(get_db)
):
    admin_user = _require_admin(request, db)
    if not admin_user:
        return RedirectResponse(url="/dashboard", status_code=302)

    q = db.query(Question).filter(Question.id == question_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="Pregunta no encontrada")

    form = await request.form()
    old_correct = (q.correct_option or "").strip().upper()
    new_correct = str(form.get("correct_option") or old_correct).strip().upper()

    opt_a = str(form.get("option_a") or q.option_a).strip()
    opt_b = str(form.get("option_b") or q.option_b).strip()
    opt_c = str(form.get("option_c") or q.option_c).strip()
    opt_d = str(form.get("option_d") or "").strip()

    updated_options = [
        {"key": "A", "text": opt_a, "is_correct": new_correct == "A"},
        {"key": "B", "text": opt_b, "is_correct": new_correct == "B"},
        {"key": "C", "text": opt_c, "is_correct": new_correct == "C"},
    ]
    if opt_d:
        updated_options.append(
            {"key": "D", "text": opt_d, "is_correct": new_correct == "D"}
        )

    q.stem = str(form.get("stem") or q.stem).strip()
    case_raw = str(form.get("case_text") or "").strip()
    q.case_text = case_raw if case_raw else None
    q.correct_option = new_correct
    q.options = updated_options
    q.legal_basis = form.get("legal_basis")
    q.explanation_summary = form.get("explanation_summary")
    ax_raw = str(form.get("thematic_axis_id") or "").strip()
    if ax_raw.isdigit():
        q.thematic_axis_id = int(ax_raw)
    if form.get("difficulty"):
        q.difficulty = str(form.get("difficulty")).strip()

    is_active_raw = form.get("is_active", "1")
    q.is_active = str(is_active_raw).lower() in ("1", "true", "yes", "on")

    recalc_raw = form.get("recalculate_scores", "1")
    recalc = str(recalc_raw).lower() in ("1", "true", "yes", "on")

    recalc_exams_count = 0
    updated_answers_count = 0

    if old_correct != new_correct and recalc:
        answers = (
            db.query(SessionAnswer)
            .filter(SessionAnswer.question_id == q.id)
            .all()
        )
        affected_sessions = set()
        for ans in answers:
            user_pick = (ans.selected_option or "").strip().upper()
            is_now_correct = bool(user_pick) and user_pick == new_correct
            if ans.is_correct != is_now_correct:
                ans.is_correct = is_now_correct
                if ans.exam_session_id:
                    affected_sessions.add(ans.exam_session_id)
                updated_answers_count += 1

        db.flush()
        for s_id in affected_sessions:
            s = db.query(ExamSession).filter(ExamSession.id == s_id).first()
            if s:
                total = (
                    s.total_questions
                    if (s.total_questions and s.total_questions > 0)
                    else len(s.question_ids or [])
                )
                if total > 0:
                    s_answers = (
                        db.query(SessionAnswer)
                        .filter(SessionAnswer.exam_session_id == s.id)
                        .all()
                    )
                    c_count = sum(1 for a in s_answers if a.is_correct)
                    i_count = sum(
                        1
                        for a in s_answers
                        if not a.is_correct and not a.is_omitted
                    )
                    o_count = max(0, total - len(s_answers))
                    raw_score = (c_count / total) * 100.0
                    score = math.floor(raw_score * 100) / 100
                    s.correct_answers = c_count
                    s.incorrect_answers = i_count
                    s.omitted_answers = o_count
                    s.score = score
                    s.is_passed = score >= 70.00
                    recalc_exams_count += 1

    db.commit()

    msg = f"Pregunta #{q.id} actualizada exitosamente."
    if old_correct != new_correct:
        msg += f" La opción correcta cambió de '{old_correct}' a '{new_correct}'."
        if recalc_exams_count > 0:
            msg += f" Se recalcularon automáticamente {recalc_exams_count} exámenes de aspirantes ({updated_answers_count} respuestas corregidas)."

    flash(request, "success", msg)
    referer = request.headers.get("referer") or "/admin/preguntas"
    return RedirectResponse(url=referer, status_code=302)


@router.get("/backup")
def backup_center(request: Request, db: Session = Depends(get_db)):
    admin_user = _require_admin(request, db)
    if not admin_user:
        return RedirectResponse(url="/dashboard", status_code=302)

    analista2_job = db.query(JobProfile).filter(JobProfile.name == "Analista II").first()
    analista3_job = db.query(JobProfile).filter(JobProfile.name == "Analista III").first()
    gestor1_job = db.query(JobProfile).filter(JobProfile.name == "Gestor I").first()
    gestor3_job = db.query(JobProfile).filter(JobProfile.name == "Gestor III").first()

    total_questions = db.query(Question).count()
    analista2_questions = Question.for_cargo_query(
        db, analista2_job.id if analista2_job else None
    ).count()
    analista3_questions = Question.for_cargo_query(
        db, analista3_job.id if analista3_job else None
    ).count()
    gestor1_questions = Question.for_cargo_query(
        db, gestor1_job.id if gestor1_job else None
    ).count()
    gestor3_questions = Question.for_cargo_query(
        db, gestor3_job.id if gestor3_job else None
    ).count()
    tecnico_questions = (
        db.query(Question).filter(Question.scope_level == "tecnico").count()
    )
    profesional_questions = (
        db.query(Question).filter(Question.scope_level == "profesional").count()
    )
    shared_questions = (
        db.query(Question)
        .filter(
            or_(
                Question.is_shared.is_(True),
                Question.scope_level == "transversal",
            )
        )
        .count()
    )
    total_axes = db.query(ThematicAxis).count()
    total_jobs = db.query(JobProfile).count()

    backup_dir = BASE_DIR / "storage" / "app" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    backups = []
    for file_path in backup_dir.glob("*.json"):
        stat = file_path.stat()
        size_bytes = stat.st_size
        size_formatted = (
            f"{size_bytes / 1048576:.2f} MB"
            if size_bytes > 1048576
            else f"{size_bytes / 1024:.1f} KB"
        )
        backups.append(
            {
                "filename": file_path.name,
                "path": str(file_path),
                "size": size_formatted,
                "created_at": datetime.fromtimestamp(stat.st_mtime).strftime(
                    "%d/%m/%Y %H:%M:%S"
                ),
                "timestamp": stat.st_mtime,
            }
        )
    backups.sort(key=lambda x: x["timestamp"], reverse=True)

    return render_template(
        request,
        "admin/backup.html",
        {
            "totalQuestions": total_questions,
            "analista2Questions": analista2_questions,
            "analista3Questions": analista3_questions,
            "gestor1Questions": gestor1_questions,
            "gestor3Questions": gestor3_questions,
            "tecnicoQuestions": tecnico_questions,
            "profesionalQuestions": profesional_questions,
            "sharedQuestions": shared_questions,
            "totalAxes": total_axes,
            "totalJobs": total_jobs,
            "backups": backups,
        },
        db,
    )


@router.post("/backup/generar")
def generate_backup_file(request: Request, db: Session = Depends(get_db)):
    admin_user = _require_admin(request, db)
    if not admin_user:
        return RedirectResponse(url="/dashboard", status_code=302)

    data = _build_full_backup_payload(db)
    backup_dir = BASE_DIR / "storage" / "app" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    filename = f"dian2676_backup_total_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.json"
    full_path = backup_dir / filename
    full_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    flash(
        request,
        "success",
        f"Copia de seguridad total generada con éxito ({filename}). Contiene {data['metadata']['total_preguntas']} preguntas y todos los ejes temáticos.",
    )
    return RedirectResponse(url="/admin/backup", status_code=302)


@router.get("/backup/descargar-directo")
def download_direct_backup(request: Request, db: Session = Depends(get_db)):
    admin_user = _require_admin(request, db)
    if not admin_user:
        return RedirectResponse(url="/dashboard", status_code=302)

    data = _build_full_backup_payload(db)
    filename = f"dian2676_backup_total_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.json"
    content = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/backup/descargar/{filename}")
def download_backup_file(
    filename: str, request: Request, db: Session = Depends(get_db)
):
    admin_user = _require_admin(request, db)
    if not admin_user:
        return RedirectResponse(url="/dashboard", status_code=302)

    clean_filename = os.path.basename(filename)
    full_path = BASE_DIR / "storage" / "app" / "backups" / clean_filename
    if not full_path.exists():
        flash(request, "error", "El archivo de backup solicitado no existe.")
        return RedirectResponse(url="/admin/backup", status_code=302)

    return FileResponse(
        path=str(full_path),
        filename=clean_filename,
        media_type="application/json",
    )


@router.post("/backup/importar")
async def import_backup_upload(
    request: Request,
    backup_file: UploadFile = File(...),
    overwrite: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    admin_user = _require_admin(request, db)
    if not admin_user:
        return RedirectResponse(url="/dashboard", status_code=302)

    raw_bytes = await backup_file.read()
    try:
        data = json.loads(raw_bytes.decode("utf-8-sig"))
    except Exception:
        flash(
            request,
            "error",
            "El archivo no contiene un formato JSON válido o está dañado.",
        )
        return RedirectResponse(url="/admin/backup", status_code=302)

    ov = str(overwrite or "").lower() in ("1", "true", "yes", "on")
    result = _execute_backup_restore(db, data, ov)
    flash(
        request,
        "success",
        f"Copia de seguridad importada con éxito. {result['message']}",
    )
    return RedirectResponse(url="/admin/backup", status_code=302)


@router.post("/backup/restaurar/{filename}")
async def restore_server_backup(
    filename: str, request: Request, db: Session = Depends(get_db)
):
    admin_user = _require_admin(request, db)
    if not admin_user:
        return RedirectResponse(url="/dashboard", status_code=302)

    clean_filename = os.path.basename(filename)
    full_path = BASE_DIR / "storage" / "app" / "backups" / clean_filename
    if not full_path.exists():
        flash(
            request,
            "error",
            "El archivo de backup seleccionado no existe en el servidor.",
        )
        return RedirectResponse(url="/admin/backup", status_code=302)

    try:
        data = json.loads(full_path.read_text(encoding="utf-8-sig"))
    except Exception:
        flash(
            request,
            "error",
            "El archivo de backup está dañado o no es un JSON válido.",
        )
        return RedirectResponse(url="/admin/backup", status_code=302)

    form = await request.form()
    ov = str(form.get("overwrite") or "").lower() in ("1", "true", "yes", "on")
    result = _execute_backup_restore(db, data, ov)
    flash(
        request,
        "success",
        f"Copia de seguridad '{clean_filename}' restaurada con éxito. {result['message']}",
    )
    return RedirectResponse(url="/admin/backup", status_code=302)
