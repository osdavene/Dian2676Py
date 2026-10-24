import json
from datetime import datetime
from typing import Dict, List, Optional, Any
from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    Text,
    Boolean,
    Numeric,
    DateTime,
    ForeignKey,
    TypeDecorator,
    or_,
    and_,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship, Session
from app.database import Base


class FlexibleJSON(TypeDecorator):
    """Cross-database JSON type compatible with PostgreSQL JSONB/JSON and MySQL/MariaDB LONGTEXT/JSON."""

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except Exception:
                    return value
            return value
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return value
        if isinstance(value, (str, bytes)):
            try:
                return json.loads(value)
            except Exception:
                return None
        return value


class JobProfile(Base):
    __tablename__ = "job_profiles"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), nullable=False)
    name = Column(String(255), nullable=False)
    level = Column(String(100), nullable=False)
    grade = Column(String(50), nullable=False)
    process = Column(String(255), nullable=True)
    subprocess = Column(String(255), nullable=True)
    purpose = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)

    thematic_axes = relationship("ThematicAxis", back_populates="job_profile", lazy="select")
    questions = relationship("Question", back_populates="job_profile", lazy="select")
    users = relationship("User", back_populates="job_profile", lazy="select")

    @property
    def thematicAxes(self):
        return self.thematic_axes


class ThematicAxis(Base):
    __tablename__ = "thematic_axes"

    id = Column(Integer, primary_key=True, index=True)
    job_profile_id = Column(Integer, ForeignKey("job_profiles.id"), nullable=True)
    category = Column(String(50), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    weight_pct = Column(Numeric(5, 2), default=0)
    created_at = Column(DateTime, default=datetime.now)

    job_profile = relationship("JobProfile", back_populates="thematic_axes")
    questions = relationship("Question", back_populates="thematic_axis")

    @property
    def jobProfile(self):
        return self.job_profile


class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, nullable=False, index=True)
    email_verified_at = Column(DateTime, nullable=True)
    password = Column(String(255), nullable=True)
    remember_token = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    job_profile_id = Column(BigInteger, ForeignKey("job_profiles.id"), nullable=True)
    target_cargo = Column(String(255), default="Analista III")
    role = Column(String(255), default="aspirante")
    google_id = Column(String(255), nullable=True)
    avatar = Column(String(255), nullable=True)

    job_profile = relationship("JobProfile", back_populates="users")
    exam_sessions = relationship("ExamSession", back_populates="user")

    @property
    def jobProfile(self):
        return self.job_profile

    @property
    def examSessions(self):
        return self.exam_sessions

    def is_admin(self) -> bool:
        return self.role in ("admin", "superadmin")

    def isAdmin(self) -> bool:
        return self.is_admin()

    def is_super_admin(self) -> bool:
        return self.role == "superadmin"

    def isSuperAdmin(self) -> bool:
        return self.is_super_admin()

    def has_verified_email(self) -> bool:
        return self.email_verified_at is not None

    def hasVerifiedEmail(self) -> bool:
        return self.has_verified_email()


class Question(Base):
    __tablename__ = "questions"

    id = Column(Integer, primary_key=True, index=True)
    job_profile_id = Column(Integer, ForeignKey("job_profiles.id"), nullable=True)
    thematic_axis_id = Column(Integer, ForeignKey("thematic_axes.id"), nullable=True)
    question_type = Column(String(50), default="situacional")
    difficulty = Column(String(20), default="medio")
    case_text = Column(Text, nullable=True)
    stem = Column(Text, nullable=False)
    options = Column(FlexibleJSON, nullable=False)
    correct_option = Column(String(10), nullable=False)
    legal_basis = Column(Text, nullable=True)
    explanation_summary = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    is_shared = Column(Boolean, default=False)
    scope_level = Column(String(50), nullable=True)

    job_profile = relationship("JobProfile", back_populates="questions")
    thematic_axis = relationship("ThematicAxis", back_populates="questions")
    session_answers = relationship("SessionAnswer", back_populates="question")

    @property
    def jobProfile(self):
        return self.job_profile

    @property
    def thematicAxis(self):
        return self.thematic_axis

    @property
    def sessionAnswers(self):
        return self.session_answers

    @classmethod
    def for_cargo_filter(cls, db: Session, job_id: Optional[int]):
        """Returns a SQLAlchemy filter expression for questions applicable to a job_id."""
        if not job_id:
            return cls.is_active.is_(True)
        job = db.query(JobProfile).filter(JobProfile.id == job_id).first()
        level = job.level if job else None

        conditions = [
            cls.job_profile_id == job_id,
            and_(cls.is_shared.is_(True), cls.scope_level == "todos"),
        ]
        if level:
            conditions.append(and_(cls.is_shared.is_(True), cls.scope_level == level))
        return or_(*conditions)

    @classmethod
    def for_cargo_query(cls, db: Session, job_id: Optional[int]):
        """Returns a base query on Question filtered for the given job_id."""
        q = db.query(cls)
        if not job_id:
            return q
        return q.filter(cls.for_cargo_filter(db, job_id))

    @property
    def options_map(self) -> Dict[str, str]:
        raw = self.options
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = []
        if not isinstance(raw, list):
            return {"A": "", "B": "", "C": "", "D": ""}
        mapped: Dict[str, str] = {}
        keys = ["A", "B", "C", "D"]
        for index, opt in enumerate(raw):
            if isinstance(opt, dict):
                key = str(opt.get("key", keys[index] if index < 4 else "X")).upper()
                mapped[key] = str(opt.get("text", ""))
        for k in keys:
            if k not in mapped:
                mapped[k] = ""
        return mapped

    @property
    def option_a(self) -> str:
        return self.options_map.get("A", "")

    @property
    def option_b(self) -> str:
        return self.options_map.get("B", "")

    @property
    def option_c(self) -> str:
        return self.options_map.get("C", "")

    @property
    def option_d(self) -> str:
        return self.options_map.get("D", "")

    @property
    def rationale(self) -> str:
        parts = []
        if self.legal_basis:
            parts.append(f"Base Legal: {self.legal_basis}")
        if self.explanation_summary:
            parts.append(self.explanation_summary)
        return " — ".join(parts)

    @property
    def competency_type(self) -> str:
        axis = self.thematic_axis
        if axis and axis.category:
            return axis.category
        return "funcional"


class ExamSession(Base):
    __tablename__ = "exam_sessions"

    id = Column(Integer, primary_key=True, index=True)
    job_profile_id = Column(Integer, ForeignKey("job_profiles.id"), nullable=True)
    mode = Column(String(50), default="simulacro_100")
    thematic_axis_id = Column(Integer, ForeignKey("thematic_axes.id"), nullable=True)
    total_questions = Column(Integer, default=0)
    correct_answers = Column(Integer, default=0)
    incorrect_answers = Column(Integer, default=0)
    omitted_answers = Column(Integer, default=0)
    score = Column(Numeric(5, 2), default=0)
    is_passed = Column(Boolean, default=False)
    time_spent_seconds = Column(Integer, default=0)
    time_limit_seconds = Column(Integer, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    question_ids = Column(FlexibleJSON, nullable=True)
    options_order = Column(FlexibleJSON, nullable=True)

    user = relationship("User", back_populates="exam_sessions")
    job_profile = relationship("JobProfile")
    thematic_axis = relationship("ThematicAxis")
    session_answers = relationship(
        "SessionAnswer",
        back_populates="exam_session",
        cascade="all, delete-orphan",
    )

    @property
    def jobProfile(self):
        return self.job_profile

    @property
    def thematicAxis(self):
        return self.thematic_axis

    @property
    def sessionAnswers(self):
        return self.session_answers

    def _get_order_for_q(self, q_or_id: Any) -> Optional[Dict[str, str]]:
        ord_map = self.options_order
        if isinstance(ord_map, str):
            try:
                ord_map = json.loads(ord_map)
            except Exception:
                ord_map = None
        if not ord_map or not isinstance(ord_map, dict):
            return None
        q_id = q_or_id.id if hasattr(q_or_id, "id") else q_or_id
        return ord_map.get(str(q_id)) or ord_map.get(q_id)

    def get_display_options_for(self, question: Question) -> Dict[str, str]:
        orig_map = question.options_map
        order = self._get_order_for_q(question.id)
        if not order or not isinstance(order, dict):
            return orig_map
        shuffled: Dict[str, str] = {}
        for display_key, orig_key in order.items():
            shuffled[display_key] = orig_map.get(orig_key, "")
        return shuffled

    def getDisplayOptionsFor(self, question: Question) -> Dict[str, str]:
        return self.get_display_options_for(question)

    def get_correct_display_option_for(self, question: Question) -> str:
        orig_correct = str(question.correct_option or "").strip().upper()
        order = self._get_order_for_q(question.id)
        if not order or not isinstance(order, dict):
            return orig_correct
        for display_key, orig_key in order.items():
            if str(orig_key).upper() == orig_correct:
                return str(display_key).upper()
        return orig_correct

    def getCorrectDisplayOptionFor(self, question: Question) -> str:
        return self.get_correct_display_option_for(question)

    def get_original_option_for(self, question_id: int, display_key: Optional[str]) -> Optional[str]:
        if not display_key:
            return None
        dk = str(display_key).strip().upper()
        order = self._get_order_for_q(question_id)
        if not order or not isinstance(order, dict):
            return dk
        return order.get(dk, dk)

    def getOriginalOptionFor(self, question_id: int, display_key: Optional[str]) -> Optional[str]:
        return self.get_original_option_for(question_id, display_key)


class SessionAnswer(Base):
    __tablename__ = "session_answers"

    id = Column(Integer, primary_key=True, index=True)
    question_id = Column(Integer, ForeignKey("questions.id"), nullable=False)
    selected_option = Column(String(10), nullable=True)
    is_correct = Column(Boolean, default=False)
    exam_session_id = Column(BigInteger, ForeignKey("exam_sessions.id"), nullable=True)
    is_omitted = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    exam_session = relationship("ExamSession", back_populates="session_answers")
    question = relationship("Question", back_populates="session_answers")

    @property
    def examSession(self):
        return self.exam_session


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    email = Column(String(255), primary_key=True)
    token = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.now)
