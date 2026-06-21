"""
한국어 형태소 분석 모듈 (Kiwi 기반).

조사/어미를 제거하고 핵심 명사·동사·형용사만 추출합니다.
검색 쿼리 전처리(BM25 토큰화) 및 카테고리 태깅 보조에 사용됩니다.
"""

import logging
from typing import Dict, List

from kiwipiepy import Kiwi

from utils.category_tagger import tag_category

logger = logging.getLogger(__name__)

# 핵심 의미를 담는 품사만 채택 (조사/어미/접사 등은 제외)
_KEEP_TAGS = {"NNG", "NNP", "NNB", "VV", "VA", "VX", "XR", "SL", "SN"}
_MIN_FORM_LEN = 1

# "하다/있다/없다/되다/같다"는 거의 모든 문장에 등장하는 의존적 경동사/형용사라
# 그 자체로는 주제를 담지 못합니다. 단독으로 추출되면(예: "이거 어떻게 해요?",
# "있나요?", "같아요") 키워드 개수 기반 모호성 판정과 BM25 검색 모두에서
# 잡음이 됩니다. 이 집합은 한국어에서 닫혀있는 작은 목록이라(나머지 후보인
# "아니다"/"그렇다"/"어떻다"는 Kiwi가 VCN/VA-I로 따로 태깅해 _KEEP_TAGS에
# 걸리지 않아 이미 제외됨) 앞으로 계속 늘어날 일은 거의 없습니다.
_CONTENTLESS_VERB_STEMS = {"하", "있", "없", "되", "같"}
_VERB_TAGS = {"VV", "VA", "VX"}

_kiwi: Kiwi = None


def _get_kiwi() -> Kiwi:
    global _kiwi
    if _kiwi is None:
        _kiwi = Kiwi()
    return _kiwi


def extract_keywords(text: str) -> List[str]:
    """
    텍스트에서 조사/어미를 제거한 핵심 키워드(명사/동사/형용사 어간)를 추출합니다.
    빈 문자열 입력 시 빈 리스트를 반환합니다.
    """
    if not text or not text.strip():
        return []

    kiwi = _get_kiwi()
    tokens = kiwi.tokenize(text)

    keywords = [
        t.form for t in tokens
        if t.tag in _KEEP_TAGS and len(t.form) >= _MIN_FORM_LEN
        and not (t.tag in _VERB_TAGS and t.form in _CONTENTLESS_VERB_STEMS)
    ]
    return keywords


def analyze(text: str) -> Dict:
    """
    텍스트에 대해 형태소 분석 + 카테고리 태깅을 함께 수행합니다.

    Returns:
        {"keywords": List[str], "category": str}
    """
    keywords = extract_keywords(text)
    category = tag_category(text, "")
    return {"keywords": keywords, "category": category}
