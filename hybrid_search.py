"""
하이브리드 검색 엔진 (벡터 검색 + BM25 키워드 검색 가중합).

벡터: Voyage AI 임베딩 코사인 유사도
키워드: 형태소 분석(morpheme_analyzer) 토큰 기반 BM25
가중치/threshold는 config.json에서 로드합니다 (하드코딩 금지).
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
from rank_bm25 import BM25Okapi

from embedding_manager import EmbeddingProvider
from morpheme_analyzer import extract_keywords
from storage.supabase_store import get_all_with_embeddings, get_documents_fingerprint

logger = logging.getLogger(__name__)

# 모델명별 코퍼스(문서+임베딩) + BM25 인덱스 캐시. 매 검색마다 전체 코퍼스를
# 다시 불러오고 BM25를 처음부터 재구축하는 비용을 피하기 위함입니다. documents
# 테이블의 fingerprint(건수+최근수정시각)가 이전과 같으면 그대로 재사용하고,
# 바뀌었을 때만 다시 불러옵니다 (지식 베이스 변경 시 자동 무효화).
_corpus_cache: Dict[str, dict] = {}


def clear_corpus_cache() -> None:
    """코퍼스 캐시를 전부 비웁니다 (테스트, 또는 같은 프로세스 안에서 즉시 반영이 필요할 때)."""
    _corpus_cache.clear()


def _load_corpus(model_name: str, conn=None) -> dict:
    fingerprint = get_documents_fingerprint(conn)
    cached = _corpus_cache.get(model_name)
    if cached is not None and cached["fingerprint"] == fingerprint:
        return cached

    corpus = get_all_with_embeddings(model_name, conn)
    doc_tokens = [extract_keywords(doc.title + " " + doc.content) for doc, _ in corpus]
    bm25 = BM25Okapi(doc_tokens) if any(doc_tokens) else None
    cached = {"fingerprint": fingerprint, "corpus": corpus, "bm25": bm25}
    _corpus_cache[model_name] = cached
    return cached


@dataclass
class SearchResult:
    doc_id: str
    title: str
    content: str
    category: str
    source_type: str
    notion_block_id: Optional[str]
    notion_page_url: Optional[str]
    vector_score: float
    bm25_score: float
    combined_score: float

    def deep_link_url(self) -> Optional[str]:
        if self.notion_page_url and self.notion_block_id:
            anchor = self.notion_block_id.replace("-", "")
            return f"{self.notion_page_url}#{anchor}"
        return None


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    va, vb = np.array(a, dtype=float), np.array(b, dtype=float)
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    if denom == 0:
        return 0.0
    return float(np.dot(va, vb) / denom)


def _min_max_normalize(scores: List[float]) -> List[float]:
    if not scores:
        return []
    lo, hi = min(scores), max(scores)
    if hi == lo:
        return [0.0 for _ in scores]
    return [(s - lo) / (hi - lo) for s in scores]


class HybridSearchEngine:
    def __init__(self, embedding_provider: EmbeddingProvider, config: dict):
        self._provider = embedding_provider
        self._embedding_model = config.get("embedding_model")
        self._weights = config.get("search_weights", {"vector": 0.6, "bm25": 0.4})
        self._top_k = config.get("search_top_k", 5)
        self.similarity_threshold = config.get("similarity_threshold", 0.55)

    def search(self, query: str, conn=None) -> List[SearchResult]:
        """질의문에 대해 하이브리드 검색을 수행하고 상위 top_k개 결과를 반환합니다."""
        if not query or not query.strip():
            raise ValueError("검색어가 비어있습니다.")

        cached = _load_corpus(self._embedding_model, conn)
        corpus, bm25 = cached["corpus"], cached["bm25"]
        if not corpus:
            logger.warning("검색 대상 문서가 없습니다 (지식DB 비어있음).")
            return []

        query_embedding = self._provider.embed_query(query)
        vector_scores = []
        for doc, embedding in corpus:
            if embedding is None:
                logger.warning("임베딩 누락 문서 (검색에서 0점 처리): doc_id=%s title=%s",
                                doc.doc_id, doc.title[:40])
                vector_scores.append(0.0)
            else:
                vector_scores.append(max(0.0, _cosine_similarity(query_embedding, embedding)))

        # bm25가 None이면(코퍼스 전체가 빈 토큰) BM25 인스턴스화가 무의미하므로 전부 0점 처리
        if bm25 is not None:
            query_tokens = extract_keywords(query)
            raw_bm25_scores = [float(s) for s in bm25.get_scores(query_tokens)]
        else:
            raw_bm25_scores = [0.0] * len(corpus)

        vector_scores_norm = _min_max_normalize(vector_scores)
        bm25_scores_norm = _min_max_normalize(raw_bm25_scores)

        w_vec = self._weights.get("vector", 0.6)
        w_bm25 = self._weights.get("bm25", 0.4)

        results = []
        for i, (doc, _) in enumerate(corpus):
            combined = w_vec * vector_scores_norm[i] + w_bm25 * bm25_scores_norm[i]
            results.append(SearchResult(
                doc_id=doc.doc_id,
                title=doc.title,
                content=doc.content,
                category=doc.category,
                source_type=doc.source_type,
                notion_block_id=doc.notion_block_id,
                notion_page_url=doc.notion_page_url,
                vector_score=vector_scores[i],
                bm25_score=raw_bm25_scores[i],
                combined_score=combined,
            ))

        results.sort(key=lambda r: r.combined_score, reverse=True)
        return results[: self._top_k]

    def is_confident(self, results: List[SearchResult]) -> bool:
        if not results:
            return False
        return results[0].combined_score >= self.similarity_threshold
