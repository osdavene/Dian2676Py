import math
import random
import re
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse
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
from app.security import get_current_user, flash
from app.pagination import Paginator
from app.templates_config import render_template

router = APIRouter()


def _auto_finalize_expired_session(db: Session, session: ExamSession) -> None:
    q_ids = session.question_ids
    if q_ids and isinstance(q_ids, list):
        questions = (
            db.query(Question)
            .filter(Question.id.in_(q_ids), Question.is_active.is_(True))
            .all()
        )
    else:
        questions = (
            Question.for_cargo_query(db, session.job_profile_id)
            .filter(Question.is_active.is_(True))
            .limit(session.total_questions or 100)
            .all()
        )

    existing_ans = (
        db.query(SessionAnswer)
        .filter(SessionAnswer.exam_session_id == session.id)
        .all()
    )
    saved_by_q = {a.question_id: a for a in existing_ans}

    correct_count = 0
    incorrect_count = 0
    omitted_count = 0

    for q in questions:
        ans = saved_by_q.get(q.id)
        if ans and ans.selected_option:
            if ans.is_correct:
                correct_count += 1
            else:
                incorrect_count += 1
        else:
            omitted_count += 1
            if ans:
                ans.selected_option = None
                ans.is_correct = False
                ans.is_omitted = True
            else:
                db.add(
                    SessionAnswer(
                        exam_session_id=session.id,
                        question_id=q.id,
                        selected_option=None,
                        is_correct=False,
                        is_omitted=True,
                        created_at=datetime.now(),
                        updated_at=datetime.now(),
                    )
                )

    total = len(questions)
    raw_score = (correct_count / total) * 100.0 if total > 0 else 0.0
    truncated_score = math.floor(raw_score * 100) / 100

    session.correct_answers = correct_count
    session.incorrect_answers = incorrect_count
    session.omitted_answers = omitted_count
    session.score = truncated_score
    session.is_passed = truncated_score >= 70.00
    session.time_spent_seconds = session.time_limit_seconds or 18000
    session.completed_at = datetime.now()
    session.updated_at = datetime.now()
    db.commit()


async def _parse_request_data(request: Request) -> Dict[str, Any]:
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        try:
            return await request.json()
        except Exception:
            return {}
    try:
        form = await request.form()
        return dict(form)
    except Exception:
        return {}


@router.post("/simulacro/iniciar")
@router.get("/simulacro/iniciar")
async def start_simulacro(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not user.has_verified_email():
        return RedirectResponse(url="/email/verify", status_code=302)

    data = await _parse_request_data(request)
    mode_raw = str(data.get("mode") or request.query_params.get("mode", "")).lower()
    q_count_raw = data.get("question_count") or request.query_params.get(
        "question_count", 50 if "express" in mode_raw else 100
    )
    try:
        question_count = int(q_count_raw)
    except (ValueError, TypeError):
        question_count = 50 if "express" in mode_raw else 100
    if question_count not in (50, 100):
        question_count = 100

    force_new_raw = data.get("force_new") or request.query_params.get("force_new", "")
    force_new = str(force_new_raw).lower() in ("1", "true", "yes", "on")

    job_id = user.job_profile_id or 1

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
            _auto_finalize_expired_session(db, active_session)
        elif not force_new:
            flash(
                request,
                "info",
                "Retomando tu simulacro en curso. Tu tiempo transcurrido (pausado) y respuestas se mantienen guardados.",
            )
            return RedirectResponse(
                url=f"/simulacro/{active_session.id}", status_code=302
            )
        else:
            _auto_finalize_expired_session(db, active_session)

    time_limit_seconds = 9000 if question_count == 50 else 18000
    mode = "simulacro_express" if question_count == 50 else "simulacro_oficial"

    raw_pool = (
        Question.for_cargo_query(db, job_id)
        .options(joinedload(Question.thematic_axis))
        .filter(Question.is_active.is_(True))
        .all()
    )

    if not raw_pool:
        flash(
            request,
            "error",
            "No hay preguntas disponibles para el cargo seleccionado.",
        )
        return RedirectResponse(url="/dashboard", status_code=302)

    # Deduplicate by normalized stem
    seen_stems = set()
    unique_pool: List[Question] = []
    for q in raw_pool:
        cleaned = re.sub(r"^\d+[\.\)]\s*", "", (q.stem or "").strip()).lower()
        if cleaned not in seen_stems:
            seen_stems.add(cleaned)
            unique_pool.append(q)

    target_count = min(question_count, len(unique_pool))

    # Anti-repetition across previous sessions for this user
    prev_sessions = (
        db.query(ExamSession.question_ids)
        .filter(ExamSession.user_id == user.id)
        .all()
    )
    seen_question_ids = set()
    for (qids,) in prev_sessions:
        if isinstance(qids, list):
            for qid in qids:
                seen_question_ids.add(qid)

    # Group by situational case
    grouped_by_case: Dict[str, List[Question]] = {}
    for q in unique_pool:
        c = (q.case_text or "").strip()
        key = c if len(c) > 50 else f"__SOLO_{q.id}"
        grouped_by_case.setdefault(key, []).append(q)

    unseen_case_blocks: List[List[Question]] = []
    seen_case_blocks: List[List[Question]] = []
    unseen_solo_items: List[Question] = []
    seen_solo_items: List[Question] = []

    for key, items in grouped_by_case.items():
        is_case_block = not key.startswith("__SOLO_") and len(items) > 1
        has_unseen = any(q.id not in seen_question_ids for q in items)
        if is_case_block:
            if has_unseen:
                unseen_case_blocks.append(items)
            else:
                seen_case_blocks.append(items)
        else:
            for item in items:
                if item.id not in seen_question_ids:
                    unseen_solo_items.append(item)
                else:
                    seen_solo_items.append(item)

    random.shuffle(unseen_case_blocks)
    random.shuffle(seen_case_blocks)
    random.shuffle(unseen_solo_items)
    random.shuffle(seen_solo_items)

    selected: List[Question] = []
    selected_ids = set()

    # Phase A: Unseen case blocks
    for block in unseen_case_blocks:
        if len(selected) + len(block) <= target_count:
            for q in block:
                if q.id not in selected_ids:
                    selected.append(q)
                    selected_ids.add(q.id)
        if len(selected) >= target_count:
            break

    # Phase B: Unseen solo items
    for solo in unseen_solo_items:
        if len(selected) >= target_count:
            break
        if solo.id not in selected_ids:
            selected.append(solo)
            selected_ids.add(solo.id)

    # Phase C: Seen case blocks
    if len(selected) < target_count:
        for block in seen_case_blocks:
            if len(selected) + len(block) <= target_count:
                for q in block:
                    if q.id not in selected_ids:
                        selected.append(q)
                        selected_ids.add(q.id)
            if len(selected) >= target_count:
                break

    # Phase D: Seen solo items
    if len(selected) < target_count:
        for solo in seen_solo_items:
            if len(selected) >= target_count:
                break
            if solo.id not in selected_ids:
                selected.append(solo)
                selected_ids.add(solo.id)

    # Phase E: Fallback from unique_pool
    if len(selected) < target_count:
        extra_pool = list(unique_pool)
        random.shuffle(extra_pool)
        for extra in extra_pool:
            if len(selected) >= target_count:
                break
            if extra.id not in selected_ids:
                selected.append(extra)
                selected_ids.add(extra.id)

    # Keep questions with same case strictly consecutive
    final_groups: Dict[str, List[Question]] = {}
    for q in selected:
        c = (q.case_text or "").strip()
        key = c if len(c) > 50 else f"__SOLO_{q.id}"
        final_groups.setdefault(key, []).append(q)

    group_list = list(final_groups.values())
    random.shuffle(group_list)
    ordered_questions: List[Question] = [q for grp in group_list for q in grp]
    question_ids = [q.id for q in ordered_questions]

    # Dynamic option shuffling per question
    options_order: Dict[str, Dict[str, str]] = {}
    for q in ordered_questions:
        orig_map = q.options_map
        orig_keys = list(orig_map.keys())
        if len(orig_keys) > 1:
            shuffled_keys = list(orig_keys)
            random.shuffle(shuffled_keys)
            options_order[str(q.id)] = {
                disp_letter: shuffled_keys[i]
                for i, disp_letter in enumerate(orig_keys)
            }

    new_session = ExamSession(
        user_id=user.id,
        job_profile_id=job_id,
        mode=mode,
        question_ids=question_ids,
        options_order=options_order,
        total_questions=len(question_ids),
        time_limit_seconds=time_limit_seconds,
        time_spent_seconds=0,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    db.add(new_session)
    db.commit()
    db.refresh(new_session)

    return RedirectResponse(url=f"/simulacro/{new_session.id}", status_code=302)


@router.get("/simulacro/{session_id}")
def show_simulacro(
    session_id: int, request: Request, db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not user.has_verified_email():
        return RedirectResponse(url="/email/verify", status_code=302)

    query = db.query(ExamSession).options(joinedload(ExamSession.job_profile))
    if not user.is_admin():
        query = query.filter(ExamSession.user_id == user.id)
    session = query.filter(ExamSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    if session.completed_at:
        return RedirectResponse(
            url=f"/simulacro/{session.id}/resultado", status_code=302
        )

    time_limit_seconds = session.time_limit_seconds or 18000
    elapsed_seconds = int(session.time_spent_seconds or 0)
    remaining_seconds = max(0, time_limit_seconds - elapsed_seconds)

    if remaining_seconds <= 0:
        _auto_finalize_expired_session(db, session)
        flash(
            request,
            "info",
            "El tiempo límite oficial ha concluido para esta sesión.",
        )
        return RedirectResponse(
            url=f"/simulacro/{session.id}/resultado", status_code=302
        )

    q_ids = session.question_ids
    if q_ids and isinstance(q_ids, list):
        q_list = (
            db.query(Question)
            .options(joinedload(Question.thematic_axis))
            .filter(Question.id.in_(q_ids), Question.is_active.is_(True))
            .all()
        )
        q_by_id = {q.id: q for q in q_list}
        questions = [q_by_id[qid] for qid in q_ids if qid in q_by_id]
    else:
        questions = (
            Question.for_cargo_query(db, session.job_profile_id)
            .options(joinedload(Question.thematic_axis))
            .filter(Question.is_active.is_(True))
            .limit(session.total_questions or 100)
            .all()
        )

    for q in questions:
        q.display_options = session.get_display_options_for(q)

    ans_rows = (
        db.query(SessionAnswer)
        .filter(SessionAnswer.exam_session_id == session.id)
        .all()
    )
    saved_answers: Dict[int, str] = {}
    for a in ans_rows:
        if a.selected_option:
            saved_answers[a.question_id] = a.selected_option

    initial_question_number = 1
    for idx, q in enumerate(questions):
        val = saved_answers.get(q.id)
        if not val:
            initial_question_number = idx + 1
            break

    return render_template(
        request,
        "exams/simulator.html",
        {
            "session": session,
            "questions": questions,
            "user": user,
            "remainingSeconds": remaining_seconds,
            "elapsedSeconds": elapsed_seconds,
            "savedAnswers": saved_answers,
            "initialQuestionNumber": initial_question_number,
        },
        db,
    )


@router.post("/simulacro/{session_id}/guardar-respuesta")
async def save_single_answer(
    session_id: int, request: Request, db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "No autenticado"}, status_code=401)

    session = (
        db.query(ExamSession)
        .filter(ExamSession.id == session_id, ExamSession.user_id == user.id)
        .first()
    )
    if not session:
        return JSONResponse({"error": "Sesión no encontrada"}, status_code=404)
    if session.completed_at:
        return JSONResponse({"error": "Examen ya finalizado."}, status_code=400)

    data = await _parse_request_data(request)
    try:
        q_id = int(data.get("question_id", 0))
    except (ValueError, TypeError):
        q_id = 0
    selected = str(data.get("selected_option") or "").strip().upper()

    q = (
        db.query(Question)
        .filter(Question.id == q_id, Question.is_active.is_(True))
        .first()
    )
    if not q:
        return JSONResponse({"error": "Pregunta no encontrada."}, status_code=404)

    orig_selected = session.get_original_option_for(q, selected)
    is_correct = (orig_selected or "").strip().upper() == (
        q.correct_option or ""
    ).strip().upper()

    existing = (
        db.query(SessionAnswer)
        .filter(
            SessionAnswer.exam_session_id == session.id,
            SessionAnswer.question_id == q.id,
        )
        .first()
    )
    if existing:
        existing.selected_option = selected or None
        existing.is_correct = is_correct
        existing.is_omitted = not bool(selected)
        existing.updated_at = datetime.now()
    else:
        db.add(
            SessionAnswer(
                exam_session_id=session.id,
                question_id=q.id,
                selected_option=selected or None,
                is_correct=is_correct,
                is_omitted=not bool(selected),
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
        )

    if "time_spent_seconds" in data:
        try:
            spent = int(float(data["time_spent_seconds"]))
            if spent > 0:
                limit = session.time_limit_seconds or 18000
                session.time_spent_seconds = min(
                    limit, max(int(session.time_spent_seconds or 0), spent)
                )
        except (ValueError, TypeError):
            pass

    session.updated_at = datetime.now()
    db.commit()
    return JSONResponse({"success": True})


@router.post("/simulacro/{session_id}/heartbeat")
async def sync_heartbeat(
    session_id: int, request: Request, db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"error": "No autenticado"}, status_code=401)

    session = (
        db.query(ExamSession)
        .filter(ExamSession.id == session_id, ExamSession.user_id == user.id)
        .first()
    )
    if not session:
        return JSONResponse({"error": "Sesión no encontrada"}, status_code=404)
    if session.completed_at:
        return JSONResponse({"completed": True})

    data = await _parse_request_data(request)
    try:
        spent = int(
            float(data.get("time_spent_seconds", session.time_spent_seconds or 0))
        )
    except (ValueError, TypeError):
        spent = int(session.time_spent_seconds or 0)

    limit = session.time_limit_seconds or 18000
    new_spent = min(limit, max(int(session.time_spent_seconds or 0), spent))
    session.time_spent_seconds = new_spent
    session.updated_at = datetime.now()
    db.commit()

    return JSONResponse(
        {
            "success": True,
            "time_spent_seconds": new_spent,
            "remaining_seconds": max(0, limit - new_spent),
        }
    )


@router.post("/simulacro/{session_id}/pausar")
async def pause_simulacro(
    session_id: int, request: Request, db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    session = (
        db.query(ExamSession)
        .filter(ExamSession.id == session_id, ExamSession.user_id == user.id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    if not session.completed_at:
        data = await _parse_request_data(request)
        try:
            spent = int(
                float(data.get("time_spent_seconds", session.time_spent_seconds or 0))
            )
        except (ValueError, TypeError):
            spent = int(session.time_spent_seconds or 0)
        limit = session.time_limit_seconds or 18000
        session.time_spent_seconds = min(
            limit, max(int(session.time_spent_seconds or 0), spent)
        )
        session.updated_at = datetime.now()
        db.commit()

    flash(
        request,
        "success",
        "Simulacro pausado exitosamente. Tu tiempo y tus respuestas quedaron guardados. Puedes reanudarlo cuando desees desde el Inicio.",
    )
    return RedirectResponse(url="/dashboard", status_code=302)


@router.post("/simulacro/{session_id}/finalizar")
async def submit_simulacro(
    session_id: int, request: Request, db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    session = (
        db.query(ExamSession)
        .filter(ExamSession.id == session_id, ExamSession.user_id == user.id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    if session.completed_at:
        return RedirectResponse(
            url=f"/simulacro/{session.id}/resultado", status_code=302
        )

    form_data = await _parse_request_data(request)
    answers: Dict[int, str] = {}
    for k, v in form_data.items():
        m = re.match(r"^answers\[(\d+)\]$", str(k))
        if m and v:
            answers[int(m.group(1))] = str(v).strip().upper()
    if isinstance(form_data.get("answers"), dict):
        for k, v in form_data["answers"].items():
            if str(k).isdigit() and v:
                answers[int(k)] = str(v).strip().upper()

    try:
        time_spent = int(
            float(form_data.get("time_spent_seconds", session.time_spent_seconds or 0))
        )
    except (ValueError, TypeError):
        time_spent = int(session.time_spent_seconds or 0)

    q_ids = session.question_ids
    if q_ids and isinstance(q_ids, list):
        questions = (
            db.query(Question)
            .filter(Question.id.in_(q_ids), Question.is_active.is_(True))
            .all()
        )
    else:
        questions = (
            Question.for_cargo_query(db, session.job_profile_id)
            .filter(Question.is_active.is_(True))
            .limit(session.total_questions or 100)
            .all()
        )

    existing_rows = (
        db.query(SessionAnswer)
        .filter(SessionAnswer.exam_session_id == session.id)
        .all()
    )
    existing_by_q = {a.question_id: a for a in existing_rows}

    correct_count = 0
    incorrect_count = 0
    omitted_count = 0

    for q in questions:
        selected = answers.get(q.id)
        if not selected and q.id in existing_by_q:
            selected = existing_by_q[q.id].selected_option

        if not selected:
            is_omitted = True
            is_correct = False
            omitted_count += 1
        else:
            is_omitted = False
            orig_selected = session.get_original_option_for(q, selected)
            is_correct = (orig_selected or "").strip().upper() == (
                q.correct_option or ""
            ).strip().upper()
            if is_correct:
                correct_count += 1
            else:
                incorrect_count += 1

        if q.id in existing_by_q:
            ans_obj = existing_by_q[q.id]
            ans_obj.selected_option = selected
            ans_obj.is_correct = is_correct
            ans_obj.is_omitted = is_omitted
            ans_obj.updated_at = datetime.now()
        else:
            db.add(
                SessionAnswer(
                    exam_session_id=session.id,
                    question_id=q.id,
                    selected_option=selected,
                    is_correct=is_correct,
                    is_omitted=is_omitted,
                    created_at=datetime.now(),
                    updated_at=datetime.now(),
                )
            )

    total = len(questions)
    raw_score = (correct_count / total) * 100.0 if total > 0 else 0.0
    truncated_score = math.floor(raw_score * 100) / 100
    is_passed = truncated_score >= 70.00

    limit = session.time_limit_seconds or 18000
    session.correct_answers = correct_count
    session.incorrect_answers = incorrect_count
    session.omitted_answers = omitted_count
    session.score = truncated_score
    session.is_passed = is_passed
    session.time_spent_seconds = min(limit, max(int(session.time_spent_seconds or 0), time_spent))
    session.completed_at = datetime.now()
    session.updated_at = datetime.now()
    db.commit()

    flash(request, "success", "¡Simulacro finalizado y calificado con éxito!")
    return RedirectResponse(url=f"/simulacro/{session.id}/resultado", status_code=302)


@router.get("/simulacro/{session_id}/resultado")
def show_result(session_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not user.has_verified_email():
        return RedirectResponse(url="/email/verify", status_code=302)

    query = db.query(ExamSession).options(
        joinedload(ExamSession.job_profile),
        joinedload(ExamSession.thematic_axis),
        joinedload(ExamSession.user),
    )
    if not user.is_admin():
        query = query.filter(ExamSession.user_id == user.id)
    session = query.filter(ExamSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    answers = (
        db.query(SessionAnswer)
        .options(joinedload(SessionAnswer.question).joinedload(Question.thematic_axis))
        .filter(SessionAnswer.exam_session_id == session.id)
        .all()
    )

    axis_stats: Dict[str, Dict[str, Any]] = {}
    competency_stats = {
        "BASICA": {
            "total": 0,
            "correct": 0,
            "name": "Competencias Básicas / Transversales",
            "percent": 0.0,
        },
        "FUNCIONAL": {
            "total": 0,
            "correct": 0,
            "name": "Competencias Funcionales",
            "percent": 0.0,
        },
        "CONDUCTUAL": {
            "total": 0,
            "correct": 0,
            "name": "Competencias Conductuales / Blandas",
            "percent": 0.0,
        },
    }

    improvement_plan: List[Dict[str, Any]] = []

    for a in answers:
        q = a.question
        if not q:
            continue
        axis = q.thematic_axis
        axis_id = axis.id if axis else 0
        axis_name = axis.name if axis else "General / Transversal"

        if axis_name not in axis_stats:
            axis_stats[axis_name] = {
                "id": axis_id,
                "name": axis_name,
                "category": axis.category if axis and axis.category else "FUNCIONAL",
                "total": 0,
                "correct": 0,
                "incorrect": 0,
                "legal_references": [],
            }
        axis_stats[axis_name]["total"] += 1
        if a.is_correct:
            axis_stats[axis_name]["correct"] += 1
        else:
            axis_stats[axis_name]["incorrect"] += 1
            if (
                q.legal_basis
                and q.legal_basis not in axis_stats[axis_name]["legal_references"]
            ):
                axis_stats[axis_name]["legal_references"].append(q.legal_basis)

        comp_type = (q.question_type or "FUNCIONAL").upper()
        if any(k in comp_type for k in ("BAS", "INTEG", "CONST")):
            comp_key = "BASICA"
        elif any(k in comp_type for k in ("COND", "COMPORT")):
            comp_key = "CONDUCTUAL"
        else:
            comp_key = "FUNCIONAL"

        competency_stats[comp_key]["total"] += 1
        if a.is_correct:
            competency_stats[comp_key]["correct"] += 1

    for c_st in competency_stats.values():
        c_st["percent"] = (
            round((c_st["correct"] / c_st["total"]) * 100, 1)
            if c_st["total"] > 0
            else 0
        )

    for axis_name, st in axis_stats.items():
        st["percent"] = (
            round((st["correct"] / st["total"]) * 100, 1) if st["total"] > 0 else 0
        )
        if st["percent"] < 70.00 and st["total"] > 0:
            priority = "CRÍTICA" if st["percent"] < 50.00 else "MEDIA"
            improvement_plan.append(
                {
                    "axis_id": st["id"],
                    "axis_name": axis_name,
                    "category": st["category"],
                    "percent": st["percent"],
                    "correct": st["correct"],
                    "incorrect": st["incorrect"],
                    "total": st["total"],
                    "priority": priority,
                    "legal_references": st["legal_references"][:3],
                }
            )

    improvement_plan.sort(key=lambda x: x["percent"])

    return render_template(
        request,
        "exams/results.html",
        {
            "session": session,
            "answers": answers,
            "axisStats": axis_stats,
            "competencyStats": competency_stats,
            "improvementPlan": improvement_plan,
            "user": user,
        },
        db,
    )


@router.get("/simulacro/{session_id}/reporte-impresion")
def print_report(session_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    session = (
        db.query(ExamSession)
        .options(
            joinedload(ExamSession.job_profile),
            joinedload(ExamSession.user),
            joinedload(ExamSession.session_answers)
            .joinedload(SessionAnswer.question)
            .joinedload(Question.thematic_axis),
        )
        .filter(ExamSession.id == session_id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    if session.user_id != user.id and not user.is_admin():
        raise HTTPException(
            status_code=403, detail="No tiene permiso para consultar este reporte."
        )

    answers = session.session_answers
    axis_stats: Dict[str, Dict[str, Any]] = {}
    for a in answers:
        q = a.question
        if not q:
            continue
        axis = q.thematic_axis
        axis_name = axis.name if axis else "General / Transversal"
        if axis_name not in axis_stats:
            axis_stats[axis_name] = {
                "name": axis_name,
                "total": 0,
                "correct": 0,
                "incorrect": 0,
            }
        axis_stats[axis_name]["total"] += 1
        if a.is_correct:
            axis_stats[axis_name]["correct"] += 1
        else:
            axis_stats[axis_name]["incorrect"] += 1

    for st in axis_stats.values():
        st["percent"] = (
            round((st["correct"] / st["total"]) * 100, 1) if st["total"] > 0 else 0
        )

    return render_template(
        request,
        "reports/exam_report.html",
        {"session": session, "answers": answers, "axisStats": axis_stats},
        db,
    )


@router.get("/practica")
def practice_selection(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not user.has_verified_email():
        return RedirectResponse(url="/email/verify", status_code=302)

    job_id = user.job_profile_id or 1
    axes = (
        db.query(ThematicAxis)
        .filter(ThematicAxis.job_profile_id == job_id)
        .order_by(ThematicAxis.category, ThematicAxis.id)
        .all()
    )
    for axis in axes:
        axis.questions_count = (
            db.query(Question)
            .filter(
                Question.is_active.is_(True),
                or_(
                    Question.thematic_axis_id == axis.id,
                    (
                        or_(
                            Question.is_shared.is_(True),
                            Question.scope_level == "profesional",
                            Question.scope_level == "transversal",
                        )
                        & Question.thematic_axis.has(ThematicAxis.name == axis.name)
                    ),
                ),
            )
            .count()
        )

    return render_template(
        request,
        "exams/practice_selection.html",
        {"axes": axes, "user": user},
        db,
    )


@router.get("/practica/eje/{axis_id}")
def practice_axis(axis_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not user.has_verified_email():
        return RedirectResponse(url="/email/verify", status_code=302)

    axis = (
        db.query(ThematicAxis)
        .options(joinedload(ThematicAxis.job_profile))
        .filter(ThematicAxis.id == axis_id)
        .first()
    )
    if not axis:
        raise HTTPException(status_code=404, detail="Eje temático no encontrado")

    questions = (
        db.query(Question)
        .filter(
            Question.is_active.is_(True),
            or_(
                Question.thematic_axis_id == axis.id,
                (
                    or_(
                        Question.is_shared.is_(True),
                        Question.scope_level == "profesional",
                        Question.scope_level == "transversal",
                    )
                    & Question.thematic_axis.has(ThematicAxis.name == axis.name)
                ),
            ),
        )
        .order_by(Question.id)
        .all()
    )

    if not questions:
        flash(request, "error", "No hay preguntas disponibles en este eje temático.")
        return RedirectResponse(url="/practica", status_code=302)

    return render_template(
        request,
        "exams/practice.html",
        {"axis": axis, "questions": questions, "user": user},
        db,
    )


@router.get("/repaso-fallos")
def review_mistakes(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not user.has_verified_email():
        return RedirectResponse(url="/email/verify", status_code=302)

    failed_q_ids = [
        r[0]
        for r in db.query(SessionAnswer.question_id)
        .join(ExamSession, ExamSession.id == SessionAnswer.exam_session_id)
        .filter(
            ExamSession.user_id == user.id,
            SessionAnswer.is_correct.is_(False),
        )
        .distinct()
        .all()
    ]

    failed_questions = (
        db.query(Question)
        .options(joinedload(Question.thematic_axis))
        .filter(Question.id.in_(failed_q_ids), Question.is_active.is_(True))
        .all()
        if failed_q_ids
        else []
    )

    if not failed_questions:
        flash(
            request,
            "info",
            "¡Felicitaciones! No tienes preguntas falladas pendientes de repasar.",
        )
        return RedirectResponse(url="/dashboard", status_code=302)

    return render_template(
        request,
        "exams/mistakes.html",
        {"failedQuestions": failed_questions, "user": user},
        db,
    )


@router.get("/simulacros/historial")
def history(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not user.has_verified_email():
        return RedirectResponse(url="/email/verify", status_code=302)

    sessions_query = (
        db.query(ExamSession)
        .options(
            joinedload(ExamSession.job_profile),
            joinedload(ExamSession.thematic_axis),
        )
        .filter(
            ExamSession.user_id == user.id,
            ExamSession.completed_at.isnot(None),
        )
        .order_by(ExamSession.completed_at.desc())
    )
    sessions = Paginator(sessions_query, request, per_page=12)

    all_completed = (
        db.query(ExamSession)
        .filter(
            ExamSession.user_id == user.id,
            ExamSession.completed_at.isnot(None),
        )
        .all()
    )

    total_count = len(all_completed)
    passed_count = sum(1 for s in all_completed if s.is_passed)
    pass_rate = round((passed_count / total_count) * 100, 1) if total_count > 0 else 0
    if total_count > 0:
        avg_raw = sum(float(s.score or 0) for s in all_completed) / total_count
        avg_score = math.floor(avg_raw * 100) / 100
        best_score = max(float(s.score or 0) for s in all_completed)
    else:
        avg_score = 0.0
        best_score = 0.0
    total_time_spent = sum(int(s.time_spent_seconds or 0) for s in all_completed)

    official_count = sum(1 for s in all_completed if s.mode == "simulacro_oficial")
    express_count = sum(1 for s in all_completed if s.mode == "simulacro_express")

    # Axis performance
    ans_rows = (
        db.query(
            ThematicAxis.id,
            ThematicAxis.name,
            ThematicAxis.category,
            SessionAnswer.is_correct,
        )
        .select_from(SessionAnswer)
        .join(ExamSession, ExamSession.id == SessionAnswer.exam_session_id)
        .join(Question, Question.id == SessionAnswer.question_id)
        .outerjoin(ThematicAxis, ThematicAxis.id == Question.thematic_axis_id)
        .filter(
            ExamSession.user_id == user.id,
            ExamSession.completed_at.isnot(None),
        )
        .all()
    )

    grouped_axes: Dict[Any, Dict[str, Any]] = {}
    for ax_id, ax_name, ax_cat, is_corr in ans_rows:
        key = (ax_id, ax_name or "Eje Temático General", ax_cat or "FUNCIONAL")
        if key not in grouped_axes:
            grouped_axes[key] = {"total": 0, "correct": 0}
        grouped_axes[key]["total"] += 1
        if is_corr:
            grouped_axes[key]["correct"] += 1

    axis_performance = []
    for (ax_id, ax_name, ax_cat), counts in grouped_axes.items():
        total = counts["total"]
        correct = counts["correct"]
        pct = round((correct / total) * 100, 1) if total > 0 else 0.0
        priority = (
            "CRÍTICA" if pct < 50.0 else ("MEDIA" if pct < 70.0 else "DOMINADA")
        )
        axis_performance.append(
            SimpleNamespace(
                axis_id=ax_id,
                name=ax_name,
                category=ax_cat,
                total=total,
                correct=correct,
                incorrect=total - correct,
                percent=pct,
                needs_improvement=(pct < 70.0),
                priority=priority,
            )
        )

    axis_performance.sort(key=lambda x: x.percent)
    critical_axes = [a for a in axis_performance if a.needs_improvement]

    return render_template(
        request,
        "exams/history.html",
        {
            "user": user,
            "sessions": sessions,
            "totalCount": total_count,
            "passedCount": passed_count,
            "passRate": pass_rate,
            "avgScore": avg_score,
            "bestScore": best_score,
            "totalTimeSpent": total_time_spent,
            "officialCount": official_count,
            "expressCount": express_count,
            "axisPerformance": axis_performance,
            "criticalAxes": critical_axes,
        },
        db,
    )


@router.api_route("/audio/pregunta/{question_id}/{part}", methods=["GET", "HEAD"])
async def stream_question_audio(
    question_id: int,
    part: str,
    db: Session = Depends(get_db),
):
    from app.tts_service import get_or_generate_audio_file
    audio_path = await get_or_generate_audio_file(question_id, part, db)
    if not audio_path or not audio_path.exists():
        raise HTTPException(status_code=404, detail="Audio no disponible para esta pregunta o parte.")

    return FileResponse(
        path=str(audio_path),
        media_type="audio/mpeg",
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=864000",
        },
    )
