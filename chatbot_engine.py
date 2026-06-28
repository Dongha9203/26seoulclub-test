"""
메인 챗봇 엔진. 확정된 처리 흐름(요구사항 6)을 그대로 구현합니다.

처리 흐름:
  1. 욕설/혐오표현 초경량 필터 (사전 매칭, API 호출 없음) — 매칭 시 즉시 에스컬레이션 응답
  2. 질문모호성 사전 체크 (의미있는 키워드 없음) — 매칭 시 즉시 운영팀 폴백
  3. 필터 통과 시 → 하이브리드 검색 (내부에서 Voyage AI 임베딩 수행)
  4. 검색 결과가 전혀 없는 경우만 즉시 운영팀 폴백. 결과가 있으면 신뢰도
     점수가 낮아도 Claude까지 보내고, "확신이 낮을 수 있다"는 신호만 함께
     전달합니다 (코퍼스가 작을 때 신뢰도 점수가 질문 관련성과 무관하게
     통과되는 실제 발견된 한계 때문에, 점수를 차단 기준으로 쓰지 않음)
  5. 8상황 분류 (이 시점 sentiment_score는 항상 0.0 고정)
  6. 톤 지침 생성 → Claude API 1회 호출 ({"answer", "sentiment_score",
     "resolution_status"} 동시 반환) — 최종 해결 여부는 항상 이 resolution_status로 판단.
     이 호출 자체가 실패하면(레이트리밋/네트워크 오류 등) failure_cause="API오류"로
     남기고 운영팀 폴백 응답을 반환합니다 (검색/분류 결과와 무관한 별개 원인).
  7. 감정점수는 로깅 전용 (실시간 톤 전환에는 사용하지 않음)
  8. 로깅 (failure_cause + sentiment_score 포함)

주의: 욕설필터는 키워드 매칭만으로 판단되므로 임베딩/검색/Claude 호출보다
먼저 수행해 비용을 절감합니다 (요구사항 6에 명시된 비용 절감 취지).
"""

import json
import logging
import threading
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
from prompt_builder import build_system_prompt, call_claude_stream, get_anthropic_client
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
    lines = [team.get("name") or "서울 동아리ON 운영팀"]
    if team.get("address"):
        lines.append(f"- 주소: {team['address']}")
    lines.append(f"- 전화: {team.get('phone', '')}")
    lines.append(f"- 이메일: {emails}")
    lines.append(f"- 운영시간: {team.get('operating_hours', '')}")
    return "\n".join(lines)


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
        def _target():
            try:
                insert_qa_log(entry)
            except Exception:
                logger.exception("비동기 로그 기록 실패 (session_id=%s)", entry.get("session_id"))

        # conn=None으로 호출해 스레드 전용 커넥션 사용 (psycopg2 커넥션은 스레드 비안전)
        t = threading.Thread(target=_target, daemon=True)
        t.start()
        self._last_log_thread = t

    # ------------------------------------------------------------------
    # 메인 엔트리포인트
    # ------------------------------------------------------------------
    def handle_question(self, question: str, session_id: str) -> ChatbotResponse:
        """질문을 처리해 최종 ChatbotResponse를 한 번에 반환합니다.

        내부적으로 _handle_question_events()의 "done" 이벤트만 받아오는 얇은
        래퍼입니다 — 스트리밍 여부와 무관하게 분기/로깅 로직은 한 곳에만 존재합니다.
        """
        for kind, payload in self._handle_question_events(question, session_id):
            if kind == "done":
                return payload
        raise RuntimeError("핸들러가 최종 응답을 생성하지 못했습니다.")

    def handle_question_stream(self, question: str, session_id: str):
        """handle_question()의 스트리밍 버전.

        답변 텍스트가 생성되는 대로 {"type": "delta", "text": ...}를 여러 번 yield하고,
        마지막에 {"type": "done", "answer": ..., "deep_link": ...}를 한 번 yield합니다.
        """
        for kind, payload in self._handle_question_events(question, session_id):
            if kind == "delta":
                yield {"type": "delta", "text": payload}
            else:
                yield {"type": "done", "answer": payload.answer, "deep_link": payload.deep_link}

    def _handle_question_events(self, question: str, session_id: str):
        """handle_question()/handle_question_stream()이 공유하는 실제 처리 흐름.

        ("delta", 텍스트조각)을 0회 이상, 그리고 마지막에 ("done", ChatbotResponse)를
        정확히 1회 yield합니다. 고정 템플릿 분기는 답변 전체를 delta 1건으로 한 번에
        내보내고, Claude를 직접 호출하는 분기만 실제로 여러 delta로 나눠 내보냅니다 —
        클라이언트는 두 경우를 구분할 필요 없이 delta를 누적하기만 하면 됩니다.
        """
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
            yield ("delta", answer)
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
            yield ("done", response)
            return

        analysis = analyze(question)
        keywords, category = analysis["keywords"], analysis["category"]
        repeated_count = self._count_repeated(session_id, keywords)

        # ── step: 질문모호성 사전 체크 ────────────────────────────────────
        # 의미를 담은 키워드가 하나도 없는 질문은 검색/Claude 호출 결과와 무관하게
        # 항상 모호한 질문입니다. 검색 신뢰도(is_confident)는 코퍼스가 작을 때
        # 질문 관련성과 무관하게 통과되는 경우가 있어(실제 발견된 한계), 이 체크는
        # 신뢰도 결과를 기다리지 않고 먼저 수행합니다 — 검색/임베딩 호출도 절약됩니다.
        if len(keywords) < self._min_keywords:
            # 진짜로 모호한 질문은 검색 결과/문서를 추측해서 답변하면 왜곡된 정보를
            # 줄 위험이 있으므로(예: 무관한 문서 내용을 끌어다 쓰는 것), 운영팀 안내
            # 대신 사용자가 질문을 구체화하도록 유도합니다.
            answer = "죄송합니다, 질문 내용이 정확하지 않아 답변하기 어려워요. 질문하신 내용을 좀 더 구체적으로 다시 한번 말씀해 주시겠어요?"
            yield ("delta", answer)
            response = ChatbotResponse(
                answer=answer, situation=None, response_attitude=None,
                failure_cause=FailureCause.QUESTION_AMBIGUITY, sentiment_score=None, search_success=False,
                blocked_by_filter=False, escalated_to_operation_team=False,
                top_score=0.0, deep_link=None, repeated_count=repeated_count,
                category=category, keywords=keywords,
            )
            self._write_log({
                "log_id": log_id, "timestamp": timestamp, "session_id": session_id,
                "question": question, "keywords": keywords, "question_category": category,
                "blocked_by_filter": False, "search_success": False, "top_score": 0.0,
                "failure_cause": FailureCause.QUESTION_AMBIGUITY.value, "situation": None, "response_attitude": None,
                "answer": answer, "sentiment_score": None, "repeated_count": repeated_count,
                "matched_doc_ids": [], "deep_link": None,
                "escalated_to_operation_team": False, "latency_ms": _elapsed_ms(start),
            })
            yield ("done", response)
            return

        # ── step: 하이브리드 검색 ─────────────────────────────────────────
        results = self._search_engine.search(question, self._conn)

        if not results:
            # 검색 결과가 전혀 없는 경우(코퍼스가 비어있는 등)만 Claude 호출 없이
            # 즉시 운영팀 안내로 처리합니다. 결과가 있는 경우는 신뢰도 점수가
            # 낮더라도 Claude에게 보내 직접 판단하게 합니다 (아래 참고).
            cause = analyze_failure(FailureAnalysisInput(
                question=question, keywords=keywords, question_category=category,
                top_score=0.0, similarity_threshold=self._search_engine.similarity_threshold,
                min_keywords_for_clarity=self._min_keywords,
            ))
            answer = (
                "죄송합니다, 문의하신 내용을 정확히 확인하기 어려워요. "
                "아래 운영팀으로 직접 문의해 주시면 빠르게 도와드릴게요.\n\n"
                + _format_operation_team_contact(self._config)
            )
            yield ("delta", answer)
            response = ChatbotResponse(
                answer=answer, situation=None, response_attitude=None,
                failure_cause=cause, sentiment_score=None, search_success=False,
                blocked_by_filter=False, escalated_to_operation_team=True,
                top_score=0.0, deep_link=None, repeated_count=repeated_count,
                category=category, keywords=keywords,
            )
            self._write_log({
                "log_id": log_id, "timestamp": timestamp, "session_id": session_id,
                "question": question, "keywords": keywords, "question_category": category,
                "blocked_by_filter": False, "search_success": False, "top_score": 0.0,
                "failure_cause": cause.value, "situation": None, "response_attitude": None,
                "answer": answer, "sentiment_score": None, "repeated_count": repeated_count,
                "matched_doc_ids": [], "deep_link": None,
                "escalated_to_operation_team": True, "latency_ms": _elapsed_ms(start),
            })
            yield ("done", response)
            return

        # 검색 신뢰도 점수는 코퍼스가 작을 때 질문 관련성과 무관하게 통과되는
        # 경우가 있어(실제 발견된 한계) 더 이상 차단 기준으로 쓰지 않습니다.
        # 대신 Claude에게 "확신이 낮을 수 있다"는 참고 신호로만 전달하고,
        # 최종 해결 여부 판단은 항상 Claude의 resolution_status에 맡깁니다.
        low_confidence_search = not self._search_engine.is_confident(results)

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
            yield ("delta", answer)
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
            yield ("done", response)
            return

        attitude = SITUATION_TO_ATTITUDE[situation]

        # ── step: 톤 지침 생성 → Claude API 1회 호출 ─────────────────────
        tone_instruction = self._tone_builder.build_instruction(situation)
        low_confidence = low_confidence_search or situation == Situation.INFO_GAP
        # 검색된 문서 안에도 운영팀 연락처가 적혀 있을 수 있는데, 그 내용이 대시보드
        # 설정값과 어긋나면(예: 노션 문서에 남은 옛 전화번호) 신뢰도와 무관하게 잘못된
        # 연락처가 그대로 노출됩니다. 그래서 신뢰도 분기와 상관없이 항상 실제 연락처를
        # 프롬프트에 박아 넣어, 문서 내용보다 이 값을 우선하도록 강제합니다.
        operation_team_contact = _format_operation_team_contact(self._config)
        system_prompt = build_system_prompt(tone_instruction, results, low_confidence, operation_team_contact)
        try:
            answer = sentiment_score = resolution_status = None
            for kind, payload in call_claude_stream(
                self._anthropic_client, self._llm_model, system_prompt, question
            ):
                if kind == "delta":
                    yield ("delta", payload)
                else:
                    answer, sentiment_score, resolution_status = payload
        except Exception:
            # Claude API 자체가 실패한 경우(레이트리밋/네트워크 오류 등)는 검색/분류와는
            # 무관한 별개 원인이므로, 운영팀 폴백으로 응답하면서도 failure_cause를 남겨
            # 대시보드에서 보이게 합니다 (이전에는 예외가 그대로 올라가 로그가 전혀
            # 남지 않고 사용자에게도 일반 500 오류만 갔습니다).
            logger.exception("Claude API 호출 실패")
            answer = (
                "죄송합니다, 일시적으로 답변을 생성하지 못했습니다. 잠시 후 다시 시도해 주시거나, "
                "급하신 경우 아래 운영팀으로 문의해 주세요.\n\n" + _format_operation_team_contact(self._config)
            )
            yield ("delta", answer)
            response = ChatbotResponse(
                answer=answer, situation=None, response_attitude=None,
                failure_cause=FailureCause.API_ERROR, sentiment_score=None, search_success=False,
                blocked_by_filter=False, escalated_to_operation_team=True,
                top_score=results[0].combined_score, deep_link=None, repeated_count=repeated_count,
                category=category, keywords=keywords,
            )
            self._write_log({
                "log_id": log_id, "timestamp": timestamp, "session_id": session_id,
                "question": question, "keywords": keywords, "question_category": category,
                "blocked_by_filter": False, "search_success": False,
                "top_score": results[0].combined_score, "failure_cause": FailureCause.API_ERROR.value,
                "situation": None, "response_attitude": None,
                "answer": answer, "sentiment_score": None, "repeated_count": repeated_count,
                "matched_doc_ids": [], "deep_link": None,
                "escalated_to_operation_team": True, "latency_ms": _elapsed_ms(start),
            })
            yield ("done", response)
            return

        matched_doc_ids = [r.doc_id for r in results]

        # ── Claude 스스로 판단한 해결 여부를 failure_cause로 반영 ──────────
        # is_confident()는 코퍼스가 작을 때 질문 관련성과 무관하게 통과되는
        # 경우가 있어(실제 발견된 한계) 신뢰도 통과 여부만으로는 실제 해결 여부를
        # 알 수 없습니다. 같은 호출 안에서 Claude가 직접 보고하는 resolution_status를
        # 대신 사용하면 코퍼스 크기와 무관하게 정확한 분류가 가능합니다.
        resolved = resolution_status == "해결됨"
        failure_cause = None if resolved else FailureCause(resolution_status)
        deep_link = next(
            (r.deep_link_url() for r in results if r.source_type == "notion" and r.deep_link_url()),
            None,
        ) if resolved else None

        response = ChatbotResponse(
            answer=answer, situation=situation, response_attitude=attitude,
            failure_cause=failure_cause, sentiment_score=sentiment_score, search_success=resolved,
            blocked_by_filter=False, escalated_to_operation_team=not resolved,
            top_score=results[0].combined_score, deep_link=deep_link,
            matched_doc_ids=matched_doc_ids, repeated_count=repeated_count,
            category=category, keywords=keywords,
        )
        self._write_log({
            "log_id": log_id, "timestamp": timestamp, "session_id": session_id,
            "question": question, "keywords": keywords, "question_category": category,
            "blocked_by_filter": False, "search_success": resolved,
            "top_score": results[0].combined_score,
            "failure_cause": failure_cause.value if failure_cause else None,
            "situation": situation.value, "response_attitude": attitude.value,
            "answer": answer, "sentiment_score": sentiment_score,
            "repeated_count": repeated_count, "matched_doc_ids": matched_doc_ids,
            "deep_link": deep_link, "escalated_to_operation_team": not resolved,
            "latency_ms": _elapsed_ms(start),
        })
        yield ("done", response)
