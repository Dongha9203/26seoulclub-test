"""
8상황 분류 및 3톤(공감/거절/에스컬레이션) 매핑.

SituationClassifier는 검색 성공(신뢰도 통과) 분기에서만 호출되며,
7개 상황(정상응답/정보부재/정책위반요청/단순거절/에스컬레이션필요/반복질문/칭찬감사) 중
하나를 반환합니다. "감정격화"는 분류기가 직접 반환하지 않고,
chatbot_engine.py의 욕설/혐오 필터(step2)에서 매칭 시 직접 지정하는 값입니다.
"""

import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List

from tone_config import build_brand_tone_guideline

logger = logging.getLogger(__name__)

_KEYWORDS_PATH = Path(__file__).parent / "situation_keywords.json"


class Situation(str, Enum):
    NORMAL_RESPONSE = "정상응답"
    INFO_GAP = "정보부재"
    POLICY_VIOLATION = "정책위반요청"
    SIMPLE_REJECTION = "단순거절"
    ESCALATION_NEEDED = "에스컬레이션필요"
    REPEATED_QUESTION = "반복질문"
    GRATITUDE = "칭찬감사"
    EMOTIONAL_ESCALATION = "감정격화"  # 분류기가 아닌 욕설필터(step2)에서 직접 지정


class ResponseAttitude(str, Enum):
    EMPATHY = "공감"
    REJECTION = "거절"
    ESCALATION = "에스컬레이션"


SITUATION_TO_ATTITUDE: Dict[Situation, ResponseAttitude] = {
    Situation.NORMAL_RESPONSE: ResponseAttitude.EMPATHY,
    Situation.INFO_GAP: ResponseAttitude.REJECTION,
    Situation.POLICY_VIOLATION: ResponseAttitude.REJECTION,
    Situation.SIMPLE_REJECTION: ResponseAttitude.REJECTION,
    Situation.ESCALATION_NEEDED: ResponseAttitude.ESCALATION,
    Situation.REPEATED_QUESTION: ResponseAttitude.EMPATHY,
    Situation.GRATITUDE: ResponseAttitude.EMPATHY,
    Situation.EMOTIONAL_ESCALATION: ResponseAttitude.ESCALATION,
}

_ATTITUDE_INSTRUCTION: Dict[ResponseAttitude, str] = {
    ResponseAttitude.EMPATHY: "사용자의 입장에 공감하는 태도로 답변하세요. 친근하게 다가가되 정확한 정보를 전달하세요.",
    ResponseAttitude.REJECTION: "요청을 수용할 수 없음을 명확하지만 부드럽게 전달하세요. 이유를 간단히 설명하고, 가능한 대안이나 안내를 함께 제시하세요.",
    ResponseAttitude.ESCALATION: "이 문의는 사람의 직접 응대가 필요합니다. 운영팀 연락처를 안내하며 정중하게 마무리하세요.",
}

_SITUATION_EXTRA_INSTRUCTION: Dict[Situation, str] = {
    Situation.INFO_GAP: "해당 정보가 지식DB에 명확히 없다는 점을 안내하고, 운영팀에 직접 문의하도록 에스컬레이션 안내를 답변에 포함하세요.",
    Situation.REPEATED_QUESTION: "사용자가 같은 질문을 반복했음을 인지하고, 이전 답변과 다른 표현으로 더 명확하게 설명하세요.",
    Situation.GRATITUDE: "감사 표현에 짧고 따뜻하게 응답하세요.",
}


@dataclass
class SituationClassificationInput:
    question: str
    keywords: List[str]
    question_category: str
    top_result_category: str
    repeated_count: int
    sentiment_score: float = 0.0  # 요구사항 6 step5: 분류 시점엔 항상 0.0 고정


def _load_keywords() -> Dict[str, List[str]]:
    try:
        with open(_KEYWORDS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("categories", {})
    except FileNotFoundError:
        logger.warning("situation_keywords.json을 찾을 수 없습니다: %s", _KEYWORDS_PATH)
        return {}
    except json.JSONDecodeError as e:
        logger.error("situation_keywords.json 파싱 오류: %s", e)
        return {}


def _matches_any(text: str, keywords: List[str]) -> bool:
    return any(kw in text for kw in keywords)


class SituationClassifier:
    def __init__(self, repeat_threshold: int = 2, keywords: Dict[str, List[str]] = None):
        """keywords를 넘기면 situation_keywords.json 파일 대신 이 값을 사용합니다
        (4단계 대시보드에서 운영자가 수정한 app_settings 값을 즉시 반영하기 위함)."""
        self._keywords = keywords if keywords is not None else _load_keywords()
        self._repeat_threshold = repeat_threshold

    def classify(self, inp: SituationClassificationInput) -> Situation:
        text = inp.question

        if _matches_any(text, self._keywords.get("policy_violation", [])):
            return Situation.POLICY_VIOLATION

        if _matches_any(text, self._keywords.get("escalation_request", [])):
            return Situation.ESCALATION_NEEDED

        if inp.repeated_count >= self._repeat_threshold:
            return Situation.REPEATED_QUESTION

        if _matches_any(text, self._keywords.get("gratitude", [])):
            return Situation.GRATITUDE

        if _matches_any(text, self._keywords.get("simple_rejection", [])):
            return Situation.SIMPLE_REJECTION

        if inp.question_category != "미분류" and inp.top_result_category != inp.question_category:
            return Situation.INFO_GAP

        return Situation.NORMAL_RESPONSE


class ToneMatrixBuilder:
    def __init__(self, tone_elements: dict = None):
        """tone_elements를 넘기면 tone_config.py의 모듈 기본값 대신 이 값을 사용합니다
        (4단계 대시보드에서 운영자가 수정한 app_settings 값을 즉시 반영하기 위함)."""
        self._tone_elements = tone_elements

    def build_instruction(self, situation: Situation) -> str:
        """상황에 맞는 톤 지침(브랜드 톤 + 응답태도 + 상황별 추가지침)을 합쳐 반환합니다."""
        attitude = SITUATION_TO_ATTITUDE[situation]
        parts = [
            build_brand_tone_guideline(self._tone_elements),
            f"\n[현재 상황: {situation.value} / 응답 태도: {attitude.value}]",
            _ATTITUDE_INSTRUCTION[attitude],
        ]
        extra = _SITUATION_EXTRA_INSTRUCTION.get(situation)
        if extra:
            parts.append(extra)
        return "\n".join(parts)
