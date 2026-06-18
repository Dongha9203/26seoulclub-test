"""
검색 실패 원인 분석.

검색이 신뢰도 threshold를 통과하지 못했을 때(chatbot_engine.py step4)만 호출됩니다.
4가지 원인: 지식DB공백 / 검색실패 / 질문모호성 / 정책밖요청
"""

from dataclasses import dataclass
from enum import Enum
from typing import List


class FailureCause(str, Enum):
    KNOWLEDGE_GAP = "지식DB공백"
    SEARCH_FAILURE = "검색실패"
    QUESTION_AMBIGUITY = "질문모호성"
    OUT_OF_POLICY = "정책밖요청"


@dataclass
class FailureAnalysisInput:
    question: str
    keywords: List[str]
    question_category: str
    top_score: float
    similarity_threshold: float
    min_keywords_for_clarity: int = 1


def analyze_failure(inp: FailureAnalysisInput) -> FailureCause:
    """판별 순서: 질문모호성 → 정책밖요청 → 지식DB공백 → 검색실패."""
    if len(inp.keywords) < inp.min_keywords_for_clarity:
        return FailureCause.QUESTION_AMBIGUITY

    if inp.question_category == "미분류":
        return FailureCause.OUT_OF_POLICY

    if inp.top_score <= 0.0:
        return FailureCause.KNOWLEDGE_GAP

    return FailureCause.SEARCH_FAILURE
