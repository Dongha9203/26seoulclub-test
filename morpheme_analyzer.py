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
