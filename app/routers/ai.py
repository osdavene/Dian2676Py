from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload
from app.database import get_db
from app.models import (
    User,
    JobProfile,
    ThematicAxis,
    Question,
    ExamSession,
)
from app.security import get_current_user, flash
from app.templates_config import render_template
from app.ai_service import (
    get_ai_config,
    save_ai_config,
    get_user_daily_usage,
    check_and_increment_quota,
    call_gemini,
    explain_question_with_ai,
    generate_study_plan_with_ai,
    answer_normative_chat_with_ai,
    generate_questions_with_ai,
    audit_and_improve_question_with_ai,
    select_adaptive_questions_for_user,
    extract_text_from_uploaded_file,
    add_custom_document_to_kb,
    load_knowledge_chunks,
)

router = APIRouter()


async def _parse_json_or_form(request: Request) -> Dict[str, Any]:
    ct = request.headers.get("content-type", "").lower()
    if "application/json" in ct:
        try:
            return await request.json()
        except Exception:
            return {}
    try:
        form = await request.form()
        return dict(form)
    except Exception:
        return {}


# ==============================================================================
# VISTA PRINCIPAL DEL ASISTENTE IA Y BUSCADOR NORMATIVO (ASPIRANTES)
# ==============================================================================
@router.get("/asistente-ia")
def assistant_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not user.has_verified_email():
        return RedirectResponse(url="/email/verify", status_code=302)

    usage = get_user_daily_usage(user)
    kb_chunks = load_knowledge_chunks()
    sources_set = sorted({c.get("source", "Documento DIAN") for c in kb_chunks if c.get("source")})

    last_session = (
        db.query(ExamSession)
        .filter(ExamSession.user_id == user.id, ExamSession.completed_at.isnot(None))
        .order_by(ExamSession.completed_at.desc())
        .first()
    )

    return render_template(
        request,
        "ai/assistant.html",
        {
            "usage": usage,
            "kb_chunks_count": len(kb_chunks),
            "kb_sources": sources_set,
            "last_session": last_session,
        },
        db,
    )


# ==============================================================================
# 1. ENDPOINT: TUTOR JURÍDICO IA EN CADA PREGUNTA
# ==============================================================================
@router.post("/ia/tutor-pregunta")
async def ai_question_tutor(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"success": False, "message": "Sesión expirada."}, status_code=401)

    allowed, msg = check_and_increment_quota(user)
    if not allowed:
        return JSONResponse({"success": False, "message": msg}, status_code=429)

    data = await _parse_json_or_form(request)
    q_id = int(data.get("question_id") or 0)
    selected_option = data.get("selected_option")
    followup = str(data.get("followup") or "").strip()
    session_id = data.get("session_id")

    question = (
        db.query(Question)
        .options(joinedload(Question.thematic_axis))
        .filter(Question.id == q_id)
        .first()
    )
    if not question:
        return JSONResponse(
            {"success": False, "message": "Pregunta no encontrada."}, status_code=404
        )

    session_obj = None
    if session_id:
        try:
            session_obj = (
                db.query(ExamSession).filter(ExamSession.id == int(session_id)).first()
            )
        except Exception:
            session_obj = None

    result = explain_question_with_ai(
        db=db,
        question=question,
        selected_option=selected_option,
        user_followup=followup or None,
        session_obj=session_obj,
    )
    usage = get_user_daily_usage(user)
    return JSONResponse(
        {
            "success": True,
            "explanation": result["explanation"],
            "source": result["source"],
            "usage": usage,
        }
    )


# ==============================================================================
# 2. ENDPOINT: PLAN DE ESTUDIO PERSONALIZADO POR IA
# ==============================================================================
@router.post("/ia/plan-estudio")
async def ai_study_plan(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"success": False, "message": "No autenticado."}, status_code=401)

    allowed, msg = check_and_increment_quota(user)
    if not allowed:
        return JSONResponse({"success": False, "message": msg}, status_code=429)

    data = await _parse_json_or_form(request)
    session_id = data.get("session_id")
    session_obj = None
    if session_id:
        try:
            session_obj = (
                db.query(ExamSession)
                .filter(ExamSession.id == int(session_id))
                .first()
            )
        except Exception:
            session_obj = None

    result = generate_study_plan_with_ai(db=db, user=user, session_obj=session_obj)
    usage = get_user_daily_usage(user)
    return JSONResponse(
        {
            "success": True,
            "plan": result["plan"],
            "source": result["source"],
            "usage": usage,
        }
    )


# ==============================================================================
# 3. ENDPOINT: CHAT Y BUSCADOR NORMATIVO DIAN 2676 (RAG)
# ==============================================================================
@router.post("/ia/chat-normativo")
async def ai_normative_chat(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"success": False, "message": "No autenticado."}, status_code=401)

    data = await _parse_json_or_form(request)
    query = str(data.get("query") or "").strip()
    if len(query) < 3:
        return JSONResponse(
            {"success": False, "message": "Escribe una consulta más específica."},
            status_code=400,
        )

    allowed, msg = check_and_increment_quota(user)
    if not allowed:
        return JSONResponse({"success": False, "message": msg}, status_code=429)

    result = answer_normative_chat_with_ai(db=db, user=user, query=query)
    usage = get_user_daily_usage(user)
    return JSONResponse(
        {
            "success": True,
            "answer": result["answer"],
            "sources": result["sources"],
            "related_questions": result["related_questions"],
            "source_engine": result["source_engine"],
            "usage": usage,
        }
    )


# ==============================================================================
# 4. ENDPOINT: MODO ENTRENAMIENTO ADAPTATIVO IA (REPETICIÓN ESPACIADA)
# ==============================================================================
@router.post("/practica/adaptativa-ia")
@router.get("/practica/adaptativa-ia")
def start_adaptive_ai_practice(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not user.has_verified_email():
        return RedirectResponse(url="/email/verify", status_code=302)

    questions, meta = select_adaptive_questions_for_user(db, user, limit=15)
    if not questions:
        flash(
            request,
            "error",
            "No hay suficientes preguntas disponibles para iniciar el entrenamiento adaptativo.",
        )
        return RedirectResponse(url="/practica", status_code=302)

    first_axis_id = questions[0].thematic_axis_id if questions else 1
    session = ExamSession(
        user_id=user.id,
        job_profile_id=user.job_profile_id or 1,
        mode="practica_eje",
        thematic_axis_id=first_axis_id,
        question_ids=[q.id for q in questions],
        total_questions=len(questions),
        correct_answers=0,
        incorrect_answers=0,
        omitted_answers=0,
        score=0,
        is_passed=False,
        time_spent_seconds=0,
        time_limit_seconds=len(questions) * 180,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    virtual_axis = SimpleNamespace(
        id=first_axis_id or 0,
        name=f"Entrenamiento Adaptativo IA ({meta['failed_included']} fallos previos + {meta['weak_axes_count']} ejes débiles)",
        category="adaptativo_ia",
    )

    return render_template(
        request,
        "exams/practice.html",
        {
            "session": session,
            "axis": virtual_axis,
            "questions": questions,
        },
        db,
    )


# ==============================================================================
# 5. PANEL SUPERADMINISTRADOR: CENTRO DE INTELIGENCIA ARTIFICIAL
# ==============================================================================
@router.get("/admin/ia")
def admin_ai_hub(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or not user.is_super_admin():
        return RedirectResponse(url="/dashboard", status_code=302)

    cfg = get_ai_config()
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_usage_map = cfg.get("usage", {}).get(today_str, {})

    user_usage_list = []
    if isinstance(today_usage_map, dict) and today_usage_map:
        u_ids = [int(uid) for uid in today_usage_map.keys() if str(uid).isdigit()]
        users_by_id = {
            u.id: u for u in db.query(User).filter(User.id.in_(u_ids)).all()
        } if u_ids else {}
        for uid_str, count in today_usage_map.items():
            if not str(uid_str).isdigit():
                continue
            u_obj = users_by_id.get(int(uid_str))
            user_usage_list.append(
                {
                    "id": int(uid_str),
                    "name": u_obj.name if u_obj else f"Usuario #{uid_str}",
                    "email": u_obj.email if u_obj else "",
                    "cargo": u_obj.target_cargo if u_obj else "",
                    "count": int(count),
                }
            )
        user_usage_list.sort(key=lambda x: x["count"], reverse=True)

    job_profiles = db.query(JobProfile).order_by(JobProfile.id.asc()).all()
    thematic_axes = db.query(ThematicAxis).order_by(ThematicAxis.name.asc()).all()
    kb_chunks = load_knowledge_chunks()
    kb_sources = sorted({c.get("source", "Documento") for c in kb_chunks if c.get("source")})

    masked_key = ""
    raw_key = str(cfg.get("gemini_api_key") or "").strip()
    if raw_key:
        masked_key = (
            raw_key[:6] + "•" * max(8, len(raw_key) - 10) + raw_key[-4:]
            if len(raw_key) > 10
            else "••••••••"
        )

    return render_template(
        request,
        "admin/ai_hub.html",
        {
            "ai_config": cfg,
            "masked_key": masked_key,
            "has_api_key": bool(raw_key),
            "today_str": today_str,
            "user_usage_list": user_usage_list,
            "total_queries_today": sum(x["count"] for x in user_usage_list),
            "job_profiles": job_profiles,
            "thematic_axes": thematic_axes,
            "kb_chunks_count": len(kb_chunks),
            "kb_sources_count": len(kb_sources),
            "kb_sources": kb_sources,
        },
        db,
    )


@router.post("/admin/ia/config")
async def save_admin_ai_config(
    request: Request,
    gemini_api_key: str = Form(""),
    gemini_model: str = Form("gemini-flash-lite-latest"),
    daily_limit_per_user: int = Form(15),
    ai_enabled: Optional[str] = Form(None),
    test_connection: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user or not user.is_super_admin():
        return RedirectResponse(url="/dashboard", status_code=302)

    current_cfg = get_ai_config()
    updates: Dict[str, Any] = {
        "gemini_model": gemini_model.strip() or "gemini-flash-lite-latest",
        "daily_limit_per_user": max(1, min(int(daily_limit_per_user or 15), 500)),
        "ai_enabled": str(ai_enabled or "").lower() in ("1", "true", "on", "yes"),
    }

    clean_key = gemini_api_key.strip()
    if clean_key and "•" not in clean_key:
        updates["gemini_api_key"] = clean_key
    elif clean_key == "CLEAR":
        updates["gemini_api_key"] = ""

    save_ai_config(updates)

    if test_connection:
        resp_text, err = call_gemini(
            "Responde únicamente: 'Conexión exitosa con Google Gemini para Simulador DIAN 2676'.",
            temperature=0.1,
        )
        if resp_text:
            flash(request, "success", f"¡Configuración guardada y probada con éxito! Respuesta IA: {resp_text}")
        else:
            flash(
                request,
                "error",
                f"Configuración guardada, pero la prueba con Gemini devolvió: {err}",
            )
    else:
        flash(
            request,
            "success",
            "Configuración de Inteligencia Artificial y límite diario actualizados correctamente.",
        )

    return RedirectResponse(url="/admin/ia", status_code=302)


@router.post("/admin/ia/generar-preguntas")
async def admin_generate_questions(
    request: Request,
    job_profile_id: int = Form(1),
    thematic_axis_id: int = Form(1),
    difficulty: str = Form("medio"),
    count: int = Form(3),
    source_text: str = Form(""),
    file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user or not user.is_super_admin():
        return JSONResponse({"success": False, "message": "No autorizado."}, status_code=403)

    extracted_text = source_text.strip()
    if file and file.filename:
        raw_bytes = await file.read()
        if raw_bytes:
            file_text = extract_text_from_uploaded_file(file.filename, raw_bytes)
            extracted_text = (extracted_text + "\n\n" + file_text).strip()

    if len(extracted_text) < 40:
        return JSONResponse(
            {
                "success": False,
                "message": "Por favor ingresa un texto normativo (mínimo 40 caracteres) o sube un archivo PDF/TXT.",
            },
            status_code=400,
        )

    job = db.query(JobProfile).filter(JobProfile.id == job_profile_id).first()
    axis = db.query(ThematicAxis).filter(ThematicAxis.id == thematic_axis_id).first()

    result = generate_questions_with_ai(
        db=db,
        source_text=extracted_text,
        job_profile=job,
        thematic_axis=axis,
        difficulty=difficulty,
        count=count,
    )

    return JSONResponse(
        {
            "success": True,
            "questions": result["questions"],
            "source": result["source"],
            "note": result.get("note"),
            "job_profile_id": job_profile_id,
            "thematic_axis_id": thematic_axis_id,
            "difficulty": difficulty,
        }
    )


@router.post("/admin/ia/guardar-preguntas-generadas")
async def admin_save_generated_questions(
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user or not user.is_super_admin():
        return JSONResponse({"success": False, "message": "No autorizado."}, status_code=403)

    data = await _parse_json_or_form(request)
    items = data.get("questions") or []
    job_profile_id = int(data.get("job_profile_id") or 1)
    thematic_axis_id = int(data.get("thematic_axis_id") or 1)
    difficulty = str(data.get("difficulty") or "medio")
    is_shared = bool(data.get("is_shared", True))

    saved_count = 0
    for item in items:
        stem = str(item.get("stem") or "").strip()
        if not stem:
            continue
        options = item.get("options")
        if not isinstance(options, list) or len(options) < 2:
            continue
        correct_opt = str(item.get("correct_option") or "A").strip().upper()
        q = Question(
            job_profile_id=job_profile_id,
            thematic_axis_id=thematic_axis_id,
            is_shared=is_shared,
            scope_level="todos" if is_shared else "exclusivo",
            question_type="situacional",
            difficulty=difficulty,
            case_text=str(item.get("case_text") or "").strip() or None,
            stem=stem,
            options=options,
            correct_option=correct_opt,
            legal_basis=str(item.get("legal_basis") or "").strip() or None,
            explanation_summary=str(item.get("explanation_summary") or "").strip() or None,
            is_active=True,
            created_at=datetime.now(),
        )
        db.add(q)
        saved_count += 1

    db.commit()
    return JSONResponse(
        {
            "success": True,
            "saved_count": saved_count,
            "message": f"¡Se guardaron {saved_count} pregunta(s) exitosamente en el Banco de Preguntas!",
        }
    )


@router.post("/admin/ia/subir-documento-rag")
async def admin_upload_rag_doc(
    request: Request,
    doc_title: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user or not user.is_super_admin():
        return RedirectResponse(url="/dashboard", status_code=302)

    raw_bytes = await file.read()
    title = doc_title.strip() or (Path(file.filename).stem if file.filename else "Documento DIAN")
    text_content = extract_text_from_uploaded_file(file.filename or "doc.pdf", raw_bytes)

    if len(text_content) < 80:
        flash(
            request,
            "error",
            "No se pudo extraer suficiente texto legible del archivo subido.",
        )
        return RedirectResponse(url="/admin/ia", status_code=302)

    chunks_added = add_custom_document_to_kb(title, text_content)
    flash(
        request,
        "success",
        f"Documento '{title}' indexado en la Biblioteca Normativa IA ({chunks_added} fragmentos añadidos).",
    )
    return RedirectResponse(url="/admin/ia", status_code=302)


@router.post("/admin/ia/auditar-pregunta/{question_id}")
async def admin_audit_question(
    question_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user or not user.is_super_admin():
        return JSONResponse({"success": False, "message": "No autorizado."}, status_code=403)

    question = (
        db.query(Question)
        .options(joinedload(Question.thematic_axis))
        .filter(Question.id == question_id)
        .first()
    )
    if not question:
        return JSONResponse(
            {"success": False, "message": "Pregunta no encontrada."}, status_code=404
        )

    data = await _parse_json_or_form(request)
    apply_changes = str(data.get("apply") or "").lower() in ("1", "true", "yes")

    if apply_changes and data.get("stem"):
        question.case_text = str(data.get("case_text") or "").strip() or None
        question.stem = str(data.get("stem")).strip()
        correct_opt = str(data.get("correct_option") or question.correct_option or "A").strip().upper()
        question.correct_option = correct_opt
        question.legal_basis = str(data.get("legal_basis") or "").strip() or None
        question.explanation_summary = str(data.get("explanation_summary") or "").strip() or None
        question.options = [
            {
                "key": "A",
                "text": str(data.get("option_a") or "").strip(),
                "is_correct": correct_opt == "A",
                "justification": "",
            },
            {
                "key": "B",
                "text": str(data.get("option_b") or "").strip(),
                "is_correct": correct_opt == "B",
                "justification": "",
            },
            {
                "key": "C",
                "text": str(data.get("option_c") or "").strip(),
                "is_correct": correct_opt == "C",
                "justification": "",
            },
            {
                "key": "D",
                "text": str(data.get("option_d") or "").strip(),
                "is_correct": correct_opt == "D",
                "justification": "",
            },
        ]
        db.commit()
        return JSONResponse(
            {
                "success": True,
                "applied": True,
                "message": f"Pregunta #{question.id} actualizada y mejorada exitosamente.",
            }
        )

    audit_result = audit_and_improve_question_with_ai(question)
    return JSONResponse({"success": True, "audit": audit_result})
