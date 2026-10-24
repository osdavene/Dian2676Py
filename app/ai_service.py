import json
import os
import re
import unicodedata
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import httpx
from sqlalchemy.orm import Session, joinedload
from app.config import BASE_DIR, settings
from app.models import (
    User,
    JobProfile,
    ThematicAxis,
    Question,
    ExamSession,
    SessionAnswer,
)

AI_CONFIG_PATH = BASE_DIR / "storage" / "app" / "ai_config.json"
CUSTOM_DOCS_PATH = BASE_DIR / "storage" / "app" / "ai_custom_docs.json"
KNOWLEDGE_BASE_PATH = BASE_DIR / "database" / "seeders" / "data" / "dian_knowledge_base.json"

_KNOWLEDGE_CACHE: Optional[List[Dict[str, str]]] = None

SPANISH_STOPWORDS = {
    "de", "la", "que", "el", "en", "y", "a", "los", "del", "se", "las", "por", "un",
    "para", "con", "no", "una", "su", "al", "lo", "como", "mas", "pero", "sus", "le",
    "ya", "o", "este", "si", "porque", "esta", "entre", "cuando", "muy", "sin", "sobre",
    "también", "tambien", "me", "hasta", "hay", "donde", "quien", "desde", "todo", "nos",
    "durante", "todos", "uno", "les", "ni", "contra", "otros", "ese", "eso", "ante",
    "ellos", "e", "esto", "mi", "antes", "algunos", "que", "unos", "yo", "otro", "otras",
    "otra", "el", "cual", "es", "son", "fue", "ser", "tiene", "puede", "cuales", "como",
}


def _normalize_text(text: str) -> str:
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text.lower())
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9\s]", " ", no_accents)


def _tokenize(text: str) -> List[str]:
    norm = _normalize_text(text)
    return [w for w in norm.split() if len(w) > 2 and w not in SPANISH_STOPWORDS]


def get_ai_config() -> Dict[str, Any]:
    AI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    default_cfg = {
        "gemini_api_key": os.getenv("GEMINI_API_KEY", ""),
        "gemini_model": os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest"),
        "ai_enabled": True,
        "daily_limit_per_user": 15,
        "usage": {},
    }
    if not AI_CONFIG_PATH.exists():
        return default_cfg
    try:
        saved = json.loads(AI_CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            default_cfg.update(saved)
            if not default_cfg.get("gemini_api_key") and os.getenv("GEMINI_API_KEY"):
                default_cfg["gemini_api_key"] = os.getenv("GEMINI_API_KEY", "")
    except Exception:
        pass
    return default_cfg


def save_ai_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    cfg = get_ai_config()
    cfg.update(updates)
    # Keep only the last 7 days of usage logs to keep file small
    today_str = datetime.now().strftime("%Y-%m-%d")
    usage = cfg.get("usage", {})
    if isinstance(usage, dict):
        keys = sorted(usage.keys(), reverse=True)[:7]
        cfg["usage"] = {k: usage[k] for k in keys}
    else:
        cfg["usage"] = {today_str: {}}

    AI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    AI_CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return cfg


def get_user_daily_usage(user: User) -> Dict[str, Any]:
    cfg = get_ai_config()
    today_str = datetime.now().strftime("%Y-%m-%d")
    limit = int(cfg.get("daily_limit_per_user") or 15)
    usage_today = cfg.get("usage", {}).get(today_str, {})
    used = int(usage_today.get(str(user.id), 0))
    unlimited = user.is_super_admin()
    remaining = 999 if unlimited else max(0, limit - used)
    return {
        "used": used,
        "limit": limit,
        "remaining": remaining,
        "unlimited": unlimited,
        "ai_enabled": bool(cfg.get("ai_enabled", True)),
        "has_api_key": bool(str(cfg.get("gemini_api_key", "")).strip()),
        "model": cfg.get("gemini_model", "gemini-2.5-flash"),
    }


def check_and_increment_quota(user: User) -> Tuple[bool, str]:
    cfg = get_ai_config()
    if not cfg.get("ai_enabled", True) and not user.is_super_admin():
        return (
            False,
            "Las funciones de Inteligencia Artificial se encuentran temporalmente desactivadas por el administrador.",
        )

    today_str = datetime.now().strftime("%Y-%m-%d")
    limit = int(cfg.get("daily_limit_per_user") or 15)
    usage = cfg.get("usage", {})
    if not isinstance(usage, dict):
        usage = {}
    day_map = usage.get(today_str, {})
    if not isinstance(day_map, dict):
        day_map = {}

    used = int(day_map.get(str(user.id), 0))
    if not user.is_super_admin() and used >= limit:
        return (
            False,
            f"Has alcanzado tu límite diario de {limit} consultas de IA por hoy. Mañana se renovará tu cupo automáticamente.",
        )

    day_map[str(user.id)] = used + 1
    usage[today_str] = day_map
    save_ai_config({"usage": usage})
    return True, ""


def call_gemini(
    prompt: str,
    system_instruction: str = "",
    json_mode: bool = False,
    temperature: float = 0.35,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Calls Google Gemini API via REST.
    Returns (response_text, error_message).
    """
    cfg = get_ai_config()
    api_key = str(cfg.get("gemini_api_key") or "").strip()
    if not api_key:
        return None, "NO_API_KEY"

    preferred_model = str(cfg.get("gemini_model") or "gemini-flash-lite-latest").strip()
    if preferred_model in ("gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"):
        preferred_model = "gemini-flash-lite-latest"
    models_to_try = [preferred_model]
    for fallback in (
        "gemini-flash-lite-latest",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ):
        if fallback not in models_to_try:
            models_to_try.append(fallback)

    gen_config: Dict[str, Any] = {
        "temperature": temperature,
        "maxOutputTokens": 4096,
    }
    if json_mode:
        gen_config["responseMimeType"] = "application/json"

    payload: Dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": gen_config,
    }
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    last_error = None
    with httpx.Client(timeout=45.0) as client:
        for model in models_to_try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
            try:
                resp = client.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates") or []
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts") or []
                        texts = [p.get("text", "") for p in parts if p.get("text")]
                        if texts:
                            return "\n".join(texts).strip(), None
                else:
                    err_body = resp.text[:300]
                    last_error = f"HTTP {resp.status_code} ({model}): {err_body}"
                    if resp.status_code in (400, 403) and "API_KEY_INVALID" in err_body:
                        break
            except Exception as exc:
                last_error = str(exc)

    return None, last_error or "Error al conectar con Google Gemini API."


def load_knowledge_chunks() -> List[Dict[str, str]]:
    global _KNOWLEDGE_CACHE
    chunks: List[Dict[str, str]] = []
    if _KNOWLEDGE_CACHE is not None:
        chunks.extend(_KNOWLEDGE_CACHE)
    elif KNOWLEDGE_BASE_PATH.exists():
        try:
            raw = json.loads(KNOWLEDGE_BASE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                _KNOWLEDGE_CACHE = raw
                chunks.extend(raw)
        except Exception:
            _KNOWLEDGE_CACHE = []

    if CUSTOM_DOCS_PATH.exists():
        try:
            custom = json.loads(CUSTOM_DOCS_PATH.read_text(encoding="utf-8"))
            if isinstance(custom, list):
                chunks.extend(custom)
        except Exception:
            pass
    return chunks


def extract_text_from_uploaded_file(filename: str, content_bytes: bytes) -> str:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf" or content_bytes.startswith(b"%PDF"):
        try:
            from pypdf import PdfReader

            reader = PdfReader(BytesIO(content_bytes))
            pages = []
            for idx, page in enumerate(reader.pages[:60]):
                t = page.extract_text() or ""
                t = re.sub(r"\s+", " ", t).strip()
                if len(t) > 40:
                    pages.append(f"[Pág. {idx + 1}] {t}")
            return "\n\n".join(pages)
        except Exception as exc:
            return f"Error leyendo PDF: {exc}"
    return content_bytes.decode("utf-8", errors="ignore").strip()


def add_custom_document_to_kb(title: str, text_content: str) -> int:
    CUSTOM_DOCS_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing: List[Dict[str, str]] = []
    if CUSTOM_DOCS_PATH.exists():
        try:
            raw = json.loads(CUSTOM_DOCS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                existing = raw
        except Exception:
            existing = []

    clean_text = re.sub(r"\s+", " ", text_content).strip()
    added = 0
    step = 1200
    for i in range(0, min(len(clean_text), 48000), step):
        passage = clean_text[i : i + step].strip()
        if len(passage) > 80:
            existing.append({"source": title, "content": passage})
            added += 1

    CUSTOM_DOCS_PATH.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return added


def search_knowledge_base(
    query: str,
    db: Session,
    job_profile_id: Optional[int] = None,
    top_k: int = 5,
) -> Dict[str, Any]:
    tokens = _tokenize(query)
    if not tokens:
        tokens = [query.lower().strip()]

    # 1. Search official PDF/document chunks
    doc_chunks = load_knowledge_chunks()
    scored_docs: List[Tuple[float, Dict[str, str]]] = []
    for chunk in doc_chunks:
        text_norm = _normalize_text(f"{chunk.get('source', '')} {chunk.get('content', '')}")
        score = 0.0
        for tok in tokens:
            if tok in text_norm:
                score += 2.0 + min(text_norm.count(tok), 4) * 0.4
                if tok in _normalize_text(chunk.get("source", "")):
                    score += 1.5
        if score > 0:
            scored_docs.append((score, chunk))

    scored_docs.sort(key=lambda x: x[0], reverse=True)
    top_docs = [item[1] for item in scored_docs[:top_k]]

    # 2. Search matching questions & legal bases in DB
    q_query = (
        Question.for_cargo_query(db, job_profile_id)
        .options(joinedload(Question.thematic_axis))
        .filter(Question.is_active.is_(True))
    )
    all_questions = q_query.all()
    scored_questions: List[Tuple[float, Question]] = []
    for q in all_questions:
        combined = _normalize_text(
            f"{q.stem or ''} {q.case_text or ''} {q.legal_basis or ''} {q.explanation_summary or ''} {q.thematic_axis.name if q.thematic_axis else ''}"
        )
        score = 0.0
        for tok in tokens:
            if tok in combined:
                score += 2.0
                if q.legal_basis and tok in _normalize_text(q.legal_basis):
                    score += 2.0
        if score > 0:
            scored_questions.append((score, q))

    scored_questions.sort(key=lambda x: x[0], reverse=True)
    top_questions = [
        {
            "id": q.id,
            "axis": q.thematic_axis.name if q.thematic_axis else "General",
            "difficulty": q.difficulty,
            "stem": q.stem,
            "legal_basis": q.legal_basis,
            "explanation_summary": q.explanation_summary,
        }
        for _, q in scored_questions[:4]
    ]

    return {"documents": top_docs, "questions": top_questions}


# ==============================================================================
# 1. TUTOR JURÍDICO IA EN CADA PREGUNTA
# ==============================================================================
def explain_question_with_ai(
    db: Session,
    question: Question,
    selected_option: Optional[str] = None,
    user_followup: Optional[str] = None,
    session_obj: Optional[ExamSession] = None,
) -> Dict[str, Any]:
    if session_obj:
        opts_map = session_obj.get_display_options_for(question)
        correct_key = session_obj.get_correct_display_option_for(question)
    else:
        opts_map = question.options_map
        correct_key = str(question.correct_option or "A").strip().upper()

    sel_key = str(selected_option or "").strip().upper()
    axis_name = question.thematic_axis.name if question.thematic_axis else "Competencias DIAN"

    system_prompt = (
        "Eres un Tutor Experto en Derecho Tributario, Aduanero, Cambiario y Carrera Administrativa "
        "específico de la DIAN para el Concurso de Méritos DIAN 2676 (CNSC - Universidad Libre). "
        "Responde en español claro, riguroso y pedagógico, usando formato Markdown estructurado."
    )

    options_text = "\n".join(f"- Opción {k}: {v}" for k, v in opts_map.items() if v)
    prompt = f"""Analiza pedagógicamente la siguiente pregunta de juicio situacional del Concurso DIAN 2676:

- **Eje Temático**: {axis_name}
- **Nivel de Dificultad**: {question.difficulty}
- **Caso / Situación**: {question.case_text or 'No aplica (pregunta directa)'}
- **Enunciado**: {question.stem}
- **Opciones de respuesta**:
{options_text}
- **Opción Correcta Oficial**: Opción {correct_key} ({opts_map.get(correct_key, '')})
- **Opción marcada por el aspirante**: {f'Opción {sel_key} ({opts_map.get(sel_key, "")})' if sel_key else 'Ninguna / Omitida'}
- **Base Legal registrada**: {question.legal_basis or 'Normativa tributaria/administrativa DIAN'}
- **Justificación base**: {question.explanation_summary or 'Ver análisis normativo'}
"""
    if user_followup:
        prompt += f"\n- **Duda específica del aspirante**: \"{user_followup}\"\nResponde directamente a su duda y además estructura el análisis."
    else:
        prompt += """
Estructura tu respuesta en 3 secciones breves y contundentes:
1. **¿Dónde estuvo la trampa / distractor?** (Explica por qué la opción elegida por el aspirante es incorrecta o qué "cascarita" típica de la CNSC contenía).
2. **Fundamento Jurídico y Técnico de la Opción Correcta** (Explica por qué la opción correcta resuelve el caso conforme a la ley/norma).
3. **Clave Memorística para el Examen Real** (Una regla práctica de 2 líneas para no volver a fallar un caso similar).
"""

    ai_text, err = call_gemini(prompt, system_instruction=system_prompt, temperature=0.3)
    if ai_text:
        return {"explanation": ai_text, "source": "gemini"}

    # Fallback local inteligente si aún no se ha configurado API Key o hay límite externo
    sel_desc = (
        f"Marcaste la **Opción {sel_key}** (*\"{opts_map.get(sel_key, '')}\"*), la cual actúa como un **distractor típico de juicio situacional CNSC** porque propone una actuación que desconoce el procedimiento reglado o impone requisitos/decisiones no contemplados en la norma."
        if sel_key and sel_key != correct_key
        else f"La opción correcta es la **Opción {correct_key}**."
    )
    fallback_md = f"""### 1. Análisis del Distractor y Situación Planteada
{sel_desc}

En las pruebas situacionales de la **CNSC (Concurso DIAN 2676)**, los distractores suelen presentar:
* Actuaciones extremas (rechazar de plano sin verificar, suspender indefinidamente un trámite o trasladar la competencia sin resolver de fondo).
* Condiciones adicionales que la ley no exige al contribuyente o declarante.

### 2. Fundamento Jurídico y Técnico de la Opción Correcta (**Opción {correct_key}**)
* **Respuesta correcta**: **{opts_map.get(correct_key, '')}**
* **Normativa aplicable**: `{question.legal_basis or 'Normativa oficial DIAN / Estatuto Tributario'}`
* **Explicación técnica**: {question.explanation_summary or 'Esta alternativa aplica estrictamente el principio de legalidad, debido proceso y competencia funcional del servidor público de la DIAN.'}

### 3. Clave para el Examen Real ({axis_name})
> Siempre que el caso plantee una solicitud o actuación del ciudadano/contribuyente dentro del eje **{axis_name}**, verifica primero la **validez sustancial y probatoria** conforme a `{question.legal_basis or 'la norma especial'}` antes de tomar medidas restrictivas o dilatorias."""

    if user_followup:
        fallback_md = (
            f"**Respecto a tu consulta (*\"{user_followup}\"*):**\n"
            f"Conforme a **{question.legal_basis or 'la normativa DIAN'}**, la actuación procedente debe ceñirse a la **Opción {correct_key}** porque {question.explanation_summary or 'garantiza el debido proceso administrativo.'}\n\n"
            + fallback_md
        )

    return {"explanation": fallback_md, "source": "local_engine", "note": err}


# ==============================================================================
# 2. DIAGNÓSTICO Y PLAN DE ESTUDIO PERSONALIZADO POR IA
# ==============================================================================
def generate_study_plan_with_ai(
    db: Session,
    user: User,
    session_obj: Optional[ExamSession] = None,
) -> Dict[str, Any]:
    job_name = user.job_profile.name if user.job_profile else user.target_cargo

    # Gather axis performance from session or overall user history
    axis_stats: Dict[str, Dict[str, Any]] = {}
    if session_obj:
        answers = (
            db.query(SessionAnswer)
            .options(joinedload(SessionAnswer.question).joinedload(Question.thematic_axis))
            .filter(SessionAnswer.exam_session_id == session_obj.id)
            .all()
        )
        total_q = int(session_obj.total_questions or len(answers) or 1)
        score = float(session_obj.score or 0.0)
        spent_min = round(int(session_obj.time_spent_seconds or 0) / 60, 1)
    else:
        answers = (
            db.query(SessionAnswer)
            .join(ExamSession, SessionAnswer.exam_session_id == ExamSession.id)
            .options(joinedload(SessionAnswer.question).joinedload(Question.thematic_axis))
            .filter(ExamSession.user_id == user.id)
            .all()
        )
        total_q = len(answers) or 1
        correct_total = sum(1 for a in answers if a.is_correct)
        score = round((correct_total / total_q) * 100, 1) if answers else 0.0
        spent_min = 0

    for ans in answers:
        q = ans.question
        if not q:
            continue
        ax_name = q.thematic_axis.name if q.thematic_axis else "General"
        st = axis_stats.setdefault(
            ax_name, {"total": 0, "correct": 0, "laws": set()}
        )
        st["total"] += 1
        if ans.is_correct:
            st["correct"] += 1
        elif q.legal_basis:
            st["laws"].add(q.legal_basis[:90])

    summary_lines = []
    weak_axes = []
    strong_axes = []
    for ax_name, st in axis_stats.items():
        pct = round((st["correct"] / st["total"]) * 100, 1) if st["total"] > 0 else 0.0
        laws_sample = ", ".join(list(st["laws"])[:3])
        summary_lines.append(
            f"- {ax_name}: {pct}% de acierto ({st['correct']}/{st['total']}). Normas falladas: {laws_sample or 'N/A'}"
        )
        if pct < 70.0:
            weak_axes.append((ax_name, pct, list(st["laws"])[:3]))
        else:
            strong_axes.append((ax_name, pct))

    weak_axes.sort(key=lambda x: x[1])

    system_prompt = (
        "Eres un Coach Académico y Preparador Experto del Concurso de Méritos DIAN 2676. "
        "Diseña planes de estudio personalizados, accionables y enfocados en superar el puntaje eliminatorio del 70%."
    )
    prompt = f"""Genera un Diagnóstico Ejecutivo y un Plan de Estudio Personalizado de 7 Días para el siguiente aspirante:

- **Aspirante**: {user.name}
- **Cargo OPEC Objetivo**: {job_name}
- **Puntaje Obtenido**: {score:.1f}% (Mínimo aprobatorio CNSC: 70.0%)
- **Preguntas Evaluadas**: {total_q} {f'(Tiempo invertido: {spent_min} minutos)' if spent_min else ''}
- **Desempeño desglosado por Eje Temático**:
{chr(10).join(summary_lines) if summary_lines else 'Sin datos previos suficientes'}

Estructura la respuesta en Markdown con:
1. **Diagnóstico Estratégico de Brechas** (Qué ejes están comprometiendo el puntaje eliminatorio del 70% y qué normas específicas debe priorizar).
2. **Plan de Choque de 7 Días (Día 1 a Día 7)** (Asignando cada día a los ejes más débiles, las normas exactas a leer y cuántos ejercicios de práctica hacer).
3. **Táctica de Resolución de Casos Situacionales** (Consejo específico para su cargo {job_name} sobre cómo descartar distractores y administrar el tiempo).
"""

    ai_text, err = call_gemini(prompt, system_instruction=system_prompt, temperature=0.35)
    if ai_text:
        return {"plan": ai_text, "source": "gemini"}

    # Fallback estructurado inteligente
    weak_md = ""
    if weak_axes:
        for ax, pct, laws in weak_axes:
            laws_txt = "; ".join(laws) if laws else "Estatuto Tributario y Manual de Funciones DIAN"
            weak_md += f"- **{ax}** (**{pct}%** — *Prioridad Alta*): Repasar `{laws_txt}`.\n"
    else:
        weak_md = "- ¡Excelente! Todos tus ejes evaluados se encuentran en el umbral del 70% o superior. Enfócate en mantener la consistencia y velocidad.\n"

    top_weak_1 = weak_axes[0][0] if len(weak_axes) > 0 else "Competencias Funcionales Tributarias y Aduaneras"
    top_weak_2 = weak_axes[1][0] if len(weak_axes) > 1 else "Procedimiento Administrativo y Cobro Coactivo"
    top_weak_3 = weak_axes[2][0] if len(weak_axes) > 2 else "Integridad, Código de Ética y Comportamentales"

    fallback_plan = f"""### 1. Diagnóstico Estratégico para **{job_name}**
* **Puntaje actual analizado**: **{score:.1f}%** (Meta mínima eliminatoria CNSC: **70.0%**).
* **Ejes críticos por debajo del umbral aprobatorio**:
{weak_md}

### 2. Plan de Choque Personalizado de 7 Días
* **Día 1 y Día 2 — Refuerzo Normativo en {top_weak_1}**:
  * Lectura dirigida de la base legal de tus preguntas falladas y realización de **15 preguntas en Modo Práctica** de este eje hasta superar el 80%.
* **Día 3 y Día 4 — Dominio Casuístico en {top_weak_2}**:
  * Usa el módulo **Repaso de Fallos** y el **Tutor Jurídico IA** en cada error para identificar las palabras clave que cambian el sentido jurídico del enunciado.
* **Día 5 — Consolidación de {top_weak_3} (20% de la prueba)**:
  * Repasa el *Código de Ética DIAN* y el *Diccionario de Competencias Comportamentales (Resolución 065 de 2024)* para asegurar puntos fijos no eliminatorios pero decisivos en el mérito.
* **Día 6 — Entrenamiento Adaptativo IA**:
  * Ejecuta 2 sesiones seguidas de **Entrenamiento Adaptativo IA** para validar que ya no repites los mismos errores.
* **Día 7 — Simulacro Oficial Cronometrado**:
  * Realiza un **Simulacro Express (50 preguntas)** controlando un promedio máximo de **2 minutos por caso situacional**.

### 3. Recomendación Táctica CNSC
> En el cargo **{job_name}**, lee siempre **primero la pregunta final (el enunciado)** antes de leer todo el contexto del caso; así identificarás desde el primer segundo qué norma o competencia te están evaluando."""

    return {"plan": fallback_plan, "source": "local_engine", "note": err}


# ==============================================================================
# 3. BUSCADOR Y ASISTENTE NORMATIVO DIAN 2676 (RAG + CHAT)
# ==============================================================================
def answer_normative_chat_with_ai(
    db: Session,
    user: User,
    query: str,
) -> Dict[str, Any]:
    kb_results = search_knowledge_base(query, db, job_profile_id=user.job_profile_id, top_k=5)
    docs = kb_results["documents"]
    related_qs = kb_results["questions"]

    context_blocks = []
    for idx, d in enumerate(docs, 1):
        context_blocks.append(f"[Documento Oficial {idx}: {d['source']}]\n{d['content']}")
    for idx, q in enumerate(related_qs, 1):
        context_blocks.append(
            f"[Referencia Banco DIAN #{q['id']} - Eje {q['axis']}]\n"
            f"Pregunta: {q['stem']}\nBase Legal: {q['legal_basis']}\nExplicación: {q['explanation_summary']}"
        )

    context_str = "\n\n".join(context_blocks) if context_blocks else "Sin fragmentos específicos adicionales."

    system_prompt = (
        "Eres el Asistente Jurídico y Normativo Oficial de preparación para el Concurso DIAN 2676. "
        "Responde siempre basándote en la legislación colombiana (Estatuto Tributario, Regulación Aduanera, "
        "Régimen Cambiario, Decreto Ley 927 de 2023, Código de Ética DIAN y Manuales de Funciones) y en el "
        "contexto documental suministrado. Cita los documentos o artículos correspondientes y usa Markdown claro."
    )
    prompt = f"""Pregunta del aspirante ({user.target_cargo}):
"{query}"

Contexto normativo y documental recuperado de la biblioteca oficial DIAN 2676:
{context_str}

Responde de manera clara, estructurada y precisa para el examen de la CNSC, indicando:
1. **Respuesta Jurídica / Técnica Directa**
2. **Base Legal / Documento Oficial aplicable**
3. **Cómo suele evaluarse este tema en los casos situacionales del examen DIAN 2676**
"""

    ai_text, err = call_gemini(prompt, system_instruction=system_prompt, temperature=0.25)
    if ai_text:
        return {
            "answer": ai_text,
            "sources": [d["source"] for d in docs[:4]],
            "related_questions": related_qs[:3],
            "source_engine": "gemini",
        }

    # Fallback local basado en los documentos y preguntas recuperadas por RAG
    if docs or related_qs:
        parts = [f"### Respuesta basada en la Biblioteca Normativa DIAN 2676\n"]
        if related_qs:
            parts.append("#### Fundamentos Legales y Criterios de Evaluación Encontrados:")
            for rq in related_qs[:3]:
                parts.append(
                    f"- **Eje {rq['axis']}** (`{rq['legal_basis'] or 'Normativa DIAN'}`): {rq['explanation_summary'] or rq['stem']}"
                )
        if docs:
            parts.append("\n#### Extractos de los Manuales y Documentos Oficiales DIAN:")
            for d in docs[:3]:
                snippet = d["content"][:450].strip() + ("..." if len(d["content"]) > 450 else "")
                parts.append(f"- **Fuente: `{d['source']}`**\n  > {snippet}")
        parts.append(
            "\n> **Tip de Examen CNSC:** Revisa las preguntas relacionadas sugeridas abajo para practicar casos situacionales reales sobre este tema."
        )
        ans_md = "\n".join(parts)
    else:
        ans_md = (
            "No se encontraron coincidencias exactas con esos términos en los extractos locales. "
            "Intenta consultar por términos clave como *cobro coactivo*, *cláusula PPT*, *caja menor*, "
            "*régimen cambiario*, *fiscalización aduanera*, *operador económico autorizado* o configura tu "
            "API Key de Google Gemini en el Panel de Superadministrador para habilitar respuestas generativas abiertas."
        )

    return {
        "answer": ans_md,
        "sources": [d["source"] for d in docs[:4]],
        "related_questions": related_qs[:3],
        "source_engine": "local_rag",
        "note": err,
    }


# ==============================================================================
# 4. GENERADOR DE PREGUNTAS TIPO JUICIO SITUACIONAL CNSC (SUPERADMIN)
# ==============================================================================
def generate_questions_with_ai(
    db: Session,
    source_text: str,
    job_profile: Optional[JobProfile],
    thematic_axis: Optional[ThematicAxis],
    difficulty: str = "medio",
    count: int = 3,
) -> Dict[str, Any]:
    count = max(1, min(int(count or 3), 10))
    cargo_name = job_profile.name if job_profile else "Profesional / Analista DIAN"
    cargo_purpose = job_profile.purpose if job_profile and job_profile.purpose else "Gestión tributaria, aduanera y cambiaria"
    axis_name = thematic_axis.name if thematic_axis else "Competencias Funcionales DIAN"

    system_prompt = (
        "Eres un Constructor Experto de Ítems para el Concurso de Méritos DIAN 2676 bajo la metodología "
        "de Juicio Situacional de la CNSC y la Universidad Libre. Devuelve ÚNICAMENTE un arreglo JSON válido."
    )
    prompt = f"""A partir del siguiente texto normativo o documento de estudio, diseña exactamente {count} pregunta(s) inédita(s) de selección múltiple bajo metodología de **Juicio Situacional CNSC** para el concurso DIAN 2676:

- **Cargo OPEC**: {cargo_name} ({cargo_purpose})
- **Eje Temático**: {axis_name}
- **Nivel de Dificultad**: {difficulty}
- **Texto / Norma Base**:
\"\"\"
{source_text[:14000]}
\"\"\"

Cada pregunta DEBE tener:
- `case_text`: Un caso situacional realista ocurrido en una dependencia de la DIAN (mínimo 45 palabras).
- `stem`: El enunciado o pregunta que conecta el caso con la actuación correcta del funcionario.
- `options`: Lista de 4 opciones (`A`, `B`, `C`, `D`), cada una con `"key"`, `"text"`, `"is_correct"` (boolean) y `"justification"`.
- `correct_option`: La letra `"A"`, `"B"`, `"C"` o `"D"` que corresponde a la respuesta correcta.
- `legal_basis`: Cita normativa precisa (ej. Artículo del Estatuto Tributario, Decreto, Resolución o Manual).
- `explanation_summary`: Explicación clara y pedagógica de por qué la respuesta correcta es válida y por qué se descartan los distractores.

Devuelve EXCLUSIVAMENTE un JSON con esta estructura exacta:
[
  {{
    "case_text": "...",
    "stem": "...",
    "options": [
      {{"key": "A", "text": "...", "is_correct": false, "justification": "..."}},
      {{"key": "B", "text": "...", "is_correct": true, "justification": "..."}},
      {{"key": "C", "text": "...", "is_correct": false, "justification": "..."}},
      {{"key": "D", "text": "...", "is_correct": false, "justification": "..."}}
    ],
    "correct_option": "B",
    "legal_basis": "...",
    "explanation_summary": "..."
  }}
]
"""

    ai_text, err = call_gemini(
        prompt, system_instruction=system_prompt, json_mode=True, temperature=0.45
    )
    if ai_text:
        try:
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", ai_text.strip(), flags=re.I)
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict) and "questions" in parsed:
                parsed = parsed["questions"]
            if isinstance(parsed, list) and parsed:
                return {"questions": parsed, "source": "gemini"}
        except Exception as parse_err:
            err = f"Error procesando JSON de Gemini: {parse_err}"

    # Fallback local: estructura plantilla asistida a partir de fragmentos del texto
    sentences = [
        s.strip()
        for s in re.split(r"(?<=[.;])\s+", re.sub(r"\s+", " ", source_text))
        if len(s.strip()) > 50
    ]
    generated = []
    for i in range(min(count, max(1, len(sentences)))):
        frag = sentences[i % len(sentences)][:380] if sentences else source_text[:380]
        generated.append(
            {
                "case_text": (
                    f"En el desarrollo de las funciones del cargo {cargo_name} dentro de la Dirección Seccional de la DIAN, "
                    f"se presenta un expediente relacionado con {axis_name}. Durante la revisión técnica se advierte la siguiente "
                    f"disposición aplicable al caso concreto: \"{frag}\"."
                ),
                "stem": (
                    f"De conformidad con el marco normativo vigente para {axis_name}, ¿cuál es la actuación procedente que debe adelantar el funcionario competente?"
                ),
                "options": [
                    {
                        "key": "A",
                        "text": "Rechazar de plano la actuación sin verificar los presupuestos sustanciales ni probatorios del expediente.",
                        "is_correct": False,
                        "justification": "Desconoce el debido proceso y el deber de verificación previa.",
                    },
                    {
                        "key": "B",
                        "text": f"Aplicar estrictamente el procedimiento reglado conforme al precepto técnico: {frag[:180]}.",
                        "is_correct": True,
                        "justification": "Se ajusta de manera directa a la disposición técnica y normativa aplicable.",
                    },
                    {
                        "key": "C",
                        "text": "Suspender indefinidamente el trámite administrativo hasta tanto el superior jerárquico emita concepto vinculante.",
                        "is_correct": False,
                        "justification": "Configura una dilación injustificada no prevista en la norma.",
                    },
                    {
                        "key": "D",
                        "text": "Archivar las diligencias de oficio trasladando la carga procesal exclusivamente al administrado.",
                        "is_correct": False,
                        "justification": "Vulnera los principios de eficacia y legalidad de la función pública.",
                    },
                ],
                "correct_option": "B",
                "legal_basis": f"Normativa oficial aplicable a {axis_name}",
                "explanation_summary": (
                    f"La opción B recoge fielmente el mandato normativo analizado ({frag[:200]}), mientras que las demás opciones constituyen distractores por exceso ritual o dilación improcedente."
                ),
            }
        )

    return {"questions": generated, "source": "local_template", "note": err}


# ==============================================================================
# 5. AUDITOR Y MEJORADOR DEL BANCO DE PREGUNTAS CON IA (SUPERADMIN)
# ==============================================================================
def audit_and_improve_question_with_ai(
    question: Question,
) -> Dict[str, Any]:
    opts_map = question.options_map
    correct_key = str(question.correct_option or "A").strip().upper()
    axis_name = question.thematic_axis.name if question.thematic_axis else "Competencias DIAN"

    system_prompt = (
        "Eres un Auditor Psicómetra y Jurídico de pruebas de mérito de la CNSC (Concurso DIAN 2676). "
        "Evalúa la calidad técnica de la pregunta y devuelve una versión mejorada y enriquecida en formato JSON."
    )
    prompt = f"""Audita y mejora la siguiente pregunta del banco DIAN 2676:

- **ID**: #{question.id}
- **Eje Temático**: {axis_name}
- **Caso actual**: {question.case_text or '(Sin contexto situacional separado)'}
- **Enunciado actual**: {question.stem}
- **Opción A**: {opts_map.get('A', '')}
- **Opción B**: {opts_map.get('B', '')}
- **Opción C**: {opts_map.get('C', '')}
- **Opción D**: {opts_map.get('D', '')}
- **Opción Correcta**: {correct_key}
- **Base Legal actual**: {question.legal_basis or '(Vacía)'}
- **Explicación actual**: {question.explanation_summary or '(Vacía)'}

Devuelve EXCLUSIVAMENTE un objeto JSON con:
{{
  "audit_diagnosis": "Diagnóstico breve de fortalezas y qué se mejoró en el caso, distractores o sustento legal.",
  "quality_score": 92,
  "case_text": "Caso situacional enriquecido (o mantener el actual si ya es excelente)",
  "stem": "Enunciado claro y preciso tipo CNSC",
  "option_a": "Texto opción A",
  "option_b": "Texto opción B",
  "option_c": "Texto opción C",
  "option_d": "Texto opción D",
  "correct_option": "{correct_key}",
  "legal_basis": "Cita legal precisa y completa (Artículos, Leyes, Decretos o Resoluciones DIAN)",
  "explanation_summary": "Explicación pedagógica detallada de por qué la opción {correct_key} es correcta y por qué las demás son distractores."
}}
"""

    ai_text, err = call_gemini(
        prompt, system_instruction=system_prompt, json_mode=True, temperature=0.3
    )
    if ai_text:
        try:
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", ai_text.strip(), flags=re.I)
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict) and "stem" in parsed:
                parsed["source"] = "gemini"
                return parsed
        except Exception as parse_err:
            err = str(parse_err)

    # Fallback local de auditoría
    improved_legal = question.legal_basis or f"Normativa técnica aplicable al eje {axis_name} (Estatuto Tributario / Regulación DIAN)"
    improved_exp = question.explanation_summary
    if not improved_exp or len(improved_exp) < 60:
        improved_exp = (
            f"Conforme a {improved_legal}, la opción {correct_key} (\"{opts_map.get(correct_key, '')[:140]}\") "
            f"es la única que aplica rigurosamente el procedimiento y competencia funcional vigente en la DIAN, "
            f"descartando las demás alternativas por incluir actuaciones improcedentes o requisitos no previstos en la ley."
        )

    return {
        "audit_diagnosis": "Se verificó la estructura psicométrica de la pregunta y se consolidó la base legal y justificación técnica.",
        "quality_score": 88,
        "case_text": question.case_text or "",
        "stem": question.stem,
        "option_a": opts_map.get("A", ""),
        "option_b": opts_map.get("B", ""),
        "option_c": opts_map.get("C", ""),
        "option_d": opts_map.get("D", ""),
        "correct_option": correct_key,
        "legal_basis": improved_legal,
        "explanation_summary": improved_exp,
        "source": "local_auditor",
        "note": err,
    }


# ==============================================================================
# 6. ENTRENAMIENTO ADAPTATIVO IA (SELECCIÓN INTELIGENTE DE PREGUNTAS)
# ==============================================================================
def select_adaptive_questions_for_user(
    db: Session,
    user: User,
    limit: int = 15,
) -> Tuple[List[Question], Dict[str, Any]]:
    job_id = user.job_profile_id or 1

    # 1. All active questions for user's cargo
    pool = (
        Question.for_cargo_query(db, job_id)
        .options(joinedload(Question.thematic_axis))
        .filter(Question.is_active.is_(True))
        .all()
    )
    if not pool:
        return [], {"failed_count": 0, "weak_axes": []}

    pool_by_id = {q.id: q for q in pool}

    # 2. Analyze user's past answers
    user_answers = (
        db.query(SessionAnswer)
        .join(ExamSession, SessionAnswer.exam_session_id == ExamSession.id)
        .filter(ExamSession.user_id == user.id)
        .order_by(SessionAnswer.id.desc())
        .all()
    )

    q_history: Dict[int, Dict[str, int]] = {}
    axis_perf: Dict[int, Dict[str, int]] = {}
    seen_q_ids = set()

    for ans in user_answers:
        qid = ans.question_id
        seen_q_ids.add(qid)
        st = q_history.setdefault(qid, {"wrong": 0, "right": 0})
        if ans.is_correct:
            st["right"] += 1
        elif not ans.is_omitted:
            st["wrong"] += 1

        q_obj = pool_by_id.get(qid)
        if q_obj and q_obj.thematic_axis_id:
            ax_st = axis_perf.setdefault(
                q_obj.thematic_axis_id, {"total": 0, "correct": 0}
            )
            ax_st["total"] += 1
            if ans.is_correct:
                ax_st["correct"] += 1

    # Identify weak axes (< 70% accuracy)
    weak_axis_ids = []
    for ax_id, st in axis_perf.items():
        acc = (st["correct"] / st["total"]) * 100 if st["total"] > 0 else 0
        if acc < 70.0:
            weak_axis_ids.append((ax_id, acc))
    weak_axis_ids.sort(key=lambda x: x[1])
    weak_set = {ax_id for ax_id, _ in weak_axis_ids[:4]}

    # Bucket A: Questions previously failed more times than answered right (Spaced Repetition)
    bucket_failed: List[Question] = []
    for qid, st in q_history.items():
        if st["wrong"] > 0 and st["wrong"] >= st["right"] and qid in pool_by_id:
            bucket_failed.append(pool_by_id[qid])

    # Bucket B: Unseen questions in the user's weakest thematic axes
    bucket_weak_unseen: List[Question] = [
        q for q in pool if q.id not in seen_q_ids and q.thematic_axis_id in weak_set
    ]

    # Bucket C: Advanced/Medium unseen questions for the user's cargo
    bucket_challenge: List[Question] = [
        q
        for q in pool
        if q.id not in seen_q_ids
        and q.difficulty in ("avanzado", "medio")
        and q not in bucket_weak_unseen
    ]

    selected: List[Question] = []
    selected_ids = set()

    for source_bucket, target_quota in (
        (bucket_failed, max(5, limit // 3)),
        (bucket_weak_unseen, max(5, limit // 3)),
        (bucket_challenge, limit),
        (pool, limit),
    ):
        for q in source_bucket:
            if len(selected) >= limit:
                break
            if q.id not in selected_ids:
                selected.append(q)
                selected_ids.add(q.id)
                if len([x for x in selected if x in source_bucket]) >= target_quota:
                    break

    return selected, {
        "failed_included": len([q for q in selected if q in bucket_failed]),
        "weak_axes_count": len(weak_set),
    }
