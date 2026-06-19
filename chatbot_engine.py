"""
메인 챗봇 엔진. 확정된 처리 흐름(요구사항 6)을 그대로 구현합니다.

처리 흐름:
  1. 욕설/혐오표현 초경량 필터 (사전 매칭, API 호출 없음) — 매칭 시 즉시 에스컬레이션 응답
  2. 필터 통과 시 → 하이브리드 검색 (내부에서 Voyage AI 임베딩 수행)
  3. 검색 실패(신뢰도 미달) → 실패원인분석 + 운영팀 폴백
  4. 검색 성공 → 8상황 분류 (이 시점 sentiment_score는 항상 0.0 고정)
  5. 톤 지침 생성 → Claude API 1회 호출 ({"answer", "sentiment_score"} 동시 반환)
  6. 감정점수는 로깅 전용 (실시간 톤 전환에는 사용하지 않음)
  7. 로깅 (failure_cause + sentiment_score 포함)

주의: 욕설필터는 키워드 매칭만으로 판단되므로 임베딩/검색/Claude 호출보다
먼저 수행해 비용을 절감합니다 (요구사항 6에 명시된 비용 절감 취지).
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from failure_analyzer import FailureAnalysisInput, FailureCause, analyze_failure
from hybrid_search import HybridSearchEngine
from embedding_manager import get_embedding_provider
from morpheme_analyzer import analyze
from prompt_builder import build_system_prompt, call_claude, get_anthropic_client
from storage.supabase_store import get_recent_qa_logs, insert_qa_log
from tone_matrix import (
    SITUATION_TO_ATTITUDE,
    ResponseAttitude,
    Situation,
    SituationClassificationInput,
    SituationClassifier,
    ToneMatrixBuilder,
)

logger = logging.getLogger(__name__)

_FORBIDDEN_WORDS_PATH = Path(__file__).parent / "forbidden_words.json"

_REPEATED_LOOKBACK = 20
_REPEATED_OVERLAP_THRESHOLD = 0.6


@dataclass
class ChatbotResponse:
    answer: str
    situation: Optional[Situation]
    response_attitude: Optional[ResponseAttitude]
    failure_cause: Optional[FailureCause]
    sentiment_score: Optional[float]
    search_success: Optional[bool]
    blocked_by_filter: bool
    escalated_to_operation_team: bool
    top_score: float
    deep_link: Optional[str]
    matched_doc_ids: List[str] = field(default_factory=list)
    repeated_count: int = 0
    category: str = "미분류"
    keywords: List[str] = field(default_factory=list)


def _load_forbidden_words(categories: Optional[Dict[str, List[str]]] = None) -> List[str]:
    """categories를 넘기면 forbidden_words.json 파일 대신 이 값을 사용합니다
    (4단계 대시보드에서 운영자가 수정한 app_settings 값을 즉시 반영하기 위함)."""
    if categories is None:
        try:
            with open(_FORBIDDEN_WORDS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            logger.warning("forbidden_words.json을 찾을 수 없습니다: %s", _FORBIDDEN_WORDS_PATH)
            return []
        except json.JSONDecodeError as e:
            logger.error("forbidden_words.json 파싱 오류: %s", e)
            return []
        categories = data.get("categories", {})

    flat: List[str] = []
    for words in categories.values():
        flat.extend(words)
    return flat


def contains_forbidden_word(text: str, forbidden_words: List[str]) -> bool:
    return any(w in text for w in forbidden_words)


def _format_operation_team_contact(config: dict) -> str:
    team = config.get("operation_team", {})
    emails = ", ".join(team.get("email_list", []))
    return (
        f"서울 동아리ON 운영팀\n"
        f"- 전화: {team.get('phone', '')}\n"
        f"- 이메일: {emails}\n"
        f"- 운영시간: {team.get('operating_hours', '')}"
    )


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


class ChatbotEngine:
    def __init__(self, config: dict, conn=None,
                 search_engine: Optional[HybridSearchEngine] = None,
                 anthropic_client=None):
        self._config = config
        self._conn = conn
        self._forbidden_words = _load_forbidden_words(config.get("forbidden_words"))
        self._search_engine = search_engine or HybridSearchEngine(
            get_embedding_provider(config), config
        )
        self._classifier = SituationClassifier(
            repeat_threshold=config.get("repeat_threshold", 2),
            keywords=config.get("situation_keywords"),
        )
        self._tone_builder = ToneMatrixBuilder(tone_elements=config.get("tone_elements"))
        self._anthropic_client = anthropic_client or get_anthropic_client()
        self._llm_model = config["llm_model"]
        self._min_keywords = config.get("min_keywords_for_clarity", 1)

    # ------------------------------------------------------------------
    # 반복 질문 판단
    # ------------------------------------------------------------------
    def _count_repeated(self, session_id: str, keywords: List[str]) -> int:
        if not keywords:
            return 0

        kw_set = set(keywords)
        recent_entries = get_recent_qa_logs(_REPEATED_LOOKBACK, self._conn)

        count = 0
        for entry in recent_entries:
            if entry["session_id"] != session_id:
                continue
            entry_kw = set(entry.get("keywords") or [])
            if not entry_kw:
                continue
            union = kw_set | entry_kw
            if not union:
                continue
            overlap = len(kw_set & entry_kw) / len(union)
            if overlap >= _REPEATED_OVERLAP_THRESHOLD:
                count += 1
        return count

    # ------------------------------------------------------------------
    # 로깅
    # ------------------------------------------------------------------
    def _write_log(self, entry: Dict) -> None:
        insert_qa_log(entry, self._conn)

    # ------------------------------------------------------------------
    # 메인 엔트리포인트
    # ------------------------------------------------------------------
    def handle_question(self, question: str, session_id: str) -> ChatbotResponse:
        if not question or not question.strip():
            raise ValueError("질문이 비어있습니다.")

        start = time.monotonic()
        log_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()

        # ── step: 욕설/혐오표현 초경량 필터 ──────────────────────────────
        if contains_forbidden_word(question, self._forbidden_words):
            answer = (
                "불편을 드려 죄송합니다. 보다 정확한 안내를 위해 아래 운영팀으로 "
                "직접 문의해 주세요.\n\n" + _format_operation_team_contact(self._config)
            )
            response = ChatbotResponse(
                answer=answer,
                situation=Situation.EMOTIONAL_ESCALATION,
                response_attitude=ResponseAttitude.ESCALATION,
                failure_cause=None,
                sentiment_score=None,
                search_success=None,
                blocked_by_filter=True,
                escalated_to_operation_team=True,
                top_score=0.0,
                deep_link=None,
            )
            self._write_log({
                "log_id": log_id, "timestamp": timestamp, "session_id": session_id,
                "question": question, "keywords": [], "question_category": None,
                "blocked_by_filter": True, "search_success": None, "top_score": 0.0,
                "failure_cause": None,
                "situation": Situation.EMOTIONAL_ESCALATION.value,
                "response_attitude": ResponseAttitude.ESCALATION.value,
                "answer": answer, "sentiment_score": None, "repeated_count": 0,
                "matched_doc_ids": [], "deep_link": None,
                "escalated_to_operation_team": True, "latency_ms": _elapsed_ms(start),
            })
            return response

        analysis = analyze(question)
        keywords, category = analysis["keywords"], analysis["category"]
        repeated_count = self._count_repeated(session_id, keywords)

        # ── step: 하이브리드 검색 ─────────────────────────────────────────
        results = self._search_engine.search(question, self._conn)
        confident = self._search_engine.is_confident(results)

        if not confident:
            top_score = results[0].combined_score if results else 0.0
            cause = analyze_failure(FailureAnalysisInput(
                question=question, keywords=keywords, question_category=category,
                top_score=top_score, similarity_threshold=self._search_engine.similarity_threshold,
                min_keywords_for_clarity=self._min_keywords,
            ))
            answer = (
                "죄송합니다, 문의하신 내용을 정확히 확인하기 어려워요. "
                "아래 운영팀으로 직접 문의해 주시면 빠르게 도와드릴게요.\n\n"
                + _format_operation_team_contact(self._config)
            )
            response = ChatbotResponse(
                answer=answer, situation=None, response_attitude=None,
                failure_cause=cause, sentiment_score=None, search_success=False,
                blocked_by_filter=False, escalated_to_operation_team=True,
                top_score=top_score, deep_link=None, repeated_count=repeated_count,
                category=category, keywords=keywords,
            )
            self._write_log({
                "log_id": log_id, "timestamp": timestamp, "session_id": session_id,
                "question": question, "keywords": keywords, "question_category": category,
                "blocked_by_filter": False, "search_success": False, "top_score": top_score,
                "failure_cause": cause.value, "situation": None, "response_attitude": None,
                "answer": answer, "sentiment_score": None, "repeated_count": repeated_count,
                "matched_doc_ids": [], "deep_link": None,
                "escalated_to_operation_team": True, "latency_ms": _elapsed_ms(start),
            })
            return response

        # ── step: 8상황 분류 (sentiment_score는 항상 0.0 고정 입력) ───────
        situation = self._classifier.classify(SituationClassificationInput(
            question=question, keywords=keywords, question_category=category,
            top_result_category=results[0].category, repeated_count=repeated_count,
            sentiment_score=0.0,
        ))
        # ── 상담원 연결 요청은 연락처 정확성이 최우선이므로 Claude를 거치지
        # 않고 고정 템플릿으로 즉시 응답합니다 (욕설필터/검색실패와 동일한 원칙).
        if situation == Situation.ESCALATION_NEEDED:
            answer = (
                "네, 직접 상담을 도와드릴게요. 아래 운영팀 연락처로 문의해 주시면 "
                "담당자가 안내해 드립니다.\n\n" + _format_operation_team_contact(self._config)
            )
            response = ChatbotResponse(
                answer=answer, situation=situation, response_attitude=ResponseAttitude.ESCALATION,
                failure_cause=None, sentiment_score=None, search_success=True,
                blocked_by_filter=False, escalated_to_operation_team=True,
                top_score=results[0].combined_score, deep_link=None,
                repeated_count=repeated_count, category=category, keywords=keywords,
            )
            self._write_log({
                "log_id": log_id, "timestamp": timestamp, "session_id": session_id,
                "question": question, "keywords": keywords, "question_category": category,
                "blocked_by_filter": False, "search_success": True,
                "top_score": results[0].combined_score, "failure_cause": None,
                "situation": situation.value, "response_attitude": ResponseAttitude.ESCALATION.value,
                "answer": answer, "sentiment_score": None, "repeated_count": repeated_count,
                "matched_doc_ids": [], "deep_link": None,
                "escalated_to_operation_team": True, "latency_ms": _elapsed_ms(start),
            })
            return response

        attitude = SITUATION_TO_ATTITUDE[situation]

        # ── step: 톤 지침 생성 → Claude API 1회 호출 ─────────────────────
        tone_instruction = self._tone_builder.build_instruction(situation)
        low_confidence = situation == Situation.INFO_GAP
        # 정보부재 상황은 Claude가 운영팀 연락을 안내해야 하므로 실제 연락처를
        # 프롬프트에 그대로 박아 넣어 지어내지 못하게 합니다.
        operation_team_contact = (
            _format_operation_team_contact(self._config) if situation == Situation.INFO_GAP else None
        )
        system_prompt = build_system_prompt(tone_instruction, results, low_confidence, operation_team_contact)
        answer, sentiment_score = call_claude(
            self._anthropic_client, self._llm_model, system_prompt, question
        )

        deep_link = next(
            (r.deep_link_url() for r in results if r.source_type == "notion" and r.deep_link_url()),
            None,
        )
        matched_doc_ids = [r.doc_id for r in results]

        response = ChatbotResponse(
            answer=answer, situation=situation, response_attitude=attitude,
            failure_cause=None, sentiment_score=sentiment_score, search_success=True,
            blocked_by_filter=False, escalated_to_operation_team=False,
            top_score=results[0].combined_score, deep_link=deep_link,
            matched_doc_ids=matched_doc_ids, repeated_count=repeated_count,
            category=category, keywords=keywords,
        )
        self._write_log({
            "log_id": log_id, "timestamp": timestamp, "session_id": session_id,
            "question": question, "keywords": keywords, "question_category": category,
            "blocked_by_filter": False, "search_success": True,
            "top_score": results[0].combined_score, "failure_cause": None,
            "situation": situation.value, "response_attitude": attitude.value,
            "answer": answer, "sentiment_score": sentiment_score,
            "repeated_count": repeated_count, "matched_doc_ids": matched_doc_ids,
            "deep_link": deep_link, "escalated_to_operation_team": False,
            "latency_ms": _elapsed_ms(start),
        })
        return response
