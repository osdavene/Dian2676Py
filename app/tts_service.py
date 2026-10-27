import os
import re
from pathlib import Path
from typing import Optional
from sqlalchemy.orm import Session
import edge_tts
from app.config import BASE_DIR
from app.models import Question

AUDIO_CACHE_DIR = BASE_DIR / "storage" / "app" / "audio"
AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Voz neuronal colombiana de Microsoft Azure (100% gratuita y natural)
DEFAULT_VOICE = "es-CO-GonzaloNeural"


def clean_text_for_speech(text: str) -> str:
    if not text:
        return ""
    # Eliminar markdown asteriscos, enlaces y formato
    cleaned = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    cleaned = re.sub(r"#+\s*", "", cleaned)
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)
    # Normalizar espacios y saltos de línea
    cleaned = re.sub(r"\r\n|\r|\n", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def build_speech_text(question: Question, part: str) -> str:
    part_clean = (part or "").strip().lower()
    if part_clean == "caso":
        case_txt = clean_text_for_speech(question.case_text or "")
        if not case_txt:
            return ""
        return f"Caso de estudio situacional. {case_txt}"

    elif part_clean == "pregunta":
        stem_txt = clean_text_for_speech(question.stem or "")
        opts = getattr(question, "display_options", None) or question.options_map or {}
        parts = []
        if stem_txt:
            parts.append(f"Enunciado de la pregunta. {stem_txt}")

        if opts:
            parts.append("Opciones de respuesta:")
            for letter in ["A", "B", "C", "D"]:
                val = opts.get(letter)
                if val:
                    parts.append(f"Opción {letter}. {clean_text_for_speech(str(val))}")

        return " ".join(parts)

    return ""


async def get_or_generate_audio_file(question_id: int, part: str, db: Session) -> Optional[Path]:
    part_clean = (part or "").strip().lower()
    if part_clean not in ("caso", "pregunta"):
        return None

    file_path = AUDIO_CACHE_DIR / f"q_{question_id}_{part_clean}.mp3"

    # Si ya existe en caché y tiene un tamaño válido (> 500 bytes), reutilizarlo inmediatamente
    if file_path.exists() and file_path.stat().st_size > 500:
        return file_path

    # Obtener la pregunta de la base de datos
    question = db.query(Question).filter(Question.id == question_id).first()
    if not question:
        return None

    speech_text = build_speech_text(question, part_clean)
    if not speech_text:
        return None

    try:
        # Generar audio con voz neuronal en español de Colombia
        communicate = edge_tts.Communicate(
            text=speech_text,
            voice=DEFAULT_VOICE,
            rate="+2%",
            pitch="+0Hz"
        )
        temp_path = AUDIO_CACHE_DIR / f"q_{question_id}_{part_clean}.tmp"
        await communicate.save(str(temp_path))

        # Reemplazar de forma atómica para evitar archivos corruptos en concurrencia
        if temp_path.exists() and temp_path.stat().st_size > 500:
            if file_path.exists():
                file_path.unlink()
            temp_path.rename(file_path)
            return file_path
    except Exception as e:
        print(f"Error generando audio neuronal para pregunta #{question_id} ({part_clean}): {e}")
        if 'temp_path' in locals() and temp_path.exists():
            temp_path.unlink()

    return None
