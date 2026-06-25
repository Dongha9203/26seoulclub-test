"""
임베딩 Provider 모듈.

Provider 추상클래스 구조를 두어 향후 다른 임베딩 서비스로 교체 가능하게 합니다.
현재는 Voyage AI(voyage-4)만 구현합니다. 모델명은 config.json에서 읽어오며
코드에 하드코딩하지 않습니다.
"""

import logging
import os
import time
from abc import ABC, abstractmethod
from typing import List, Tuple, TYPE_CHECKING

import voyageai

if TYPE_CHECKING:
    from models.document import Document

logger = logging.getLogger(__name__)

_EMBED_BATCH_SIZE = 100

# Voyage 결제수단 미등록 계정은 분당 3건/분당 1만 토큰으로 제한되는데, 100건짜리
# 배치는 거의 항상 1만 토큰을 넘어 첫 시도에 실패합니다. 같은 크기로 그냥 재시도해도
# 다시 걸리므로(대기만으로는 못 피함), 배치를 절반으로 쪼개며 재시도합니다.
# 재시도 횟수/대기시간은 Vercel 서버리스 함수 실행 제한 시간을 넘기지 않을 만큼
# 보수적으로 작게 잡았습니다 — 완전히 못 따라잡은 나머지는 실패로 집계하고 다음
# "지금 갱신"이나 다음날 자동 갱신에서 다시 시도하면 됩니다.
_MAX_RATE_LIMIT_RETRIES = 2
_RATE_LIMIT_BASE_WAIT_SECONDS = 12


class EmbeddingProvider(ABC):
    """임베딩 Provider 공통 인터페이스."""

    @abstractmethod
    def embed_query(self, text: str) -> List[float]:
        """검색 질의문 1건을 임베딩합니다."""
        raise NotImplementedError

    @abstractmethod
    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """문서(색인 대상) 다건을 임베딩합니다."""
        raise NotImplementedError


class VoyageEmbeddingProvider(EmbeddingProvider):
    """Voyage AI 임베딩 Provider. input_type을 query/document로 구분해 호출합니다."""

    def __init__(self, api_key: str, model: str):
        if not api_key:
            raise EnvironmentError("VOYAGE_API_KEY 환경변수가 설정되지 않았습니다.")
        if not model:
            raise ValueError("embedding model 이름이 비어있습니다.")
        self._model = model
        self._client = voyageai.Client(api_key=api_key)

    def embed_query(self, text: str) -> List[float]:
        if not text or not text.strip():
            raise ValueError("임베딩할 텍스트가 비어있습니다.")
        result = self._client.embed([text], model=self._model, input_type="query")
        return result.embeddings[0]

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        non_empty = [t for t in texts if t and t.strip()]
        if not non_empty:
            return []
        result = self._client.embed(non_empty, model=self._model, input_type="document")
        return result.embeddings


def get_embedding_provider(config: dict) -> EmbeddingProvider:
    """config.json의 embedding_model 값을 읽어 Provider 인스턴스를 생성합니다."""
    model = config.get("embedding_model")
    if not model:
        raise ValueError("config.json에 embedding_model이 설정되지 않았습니다.")
    api_key = os.environ.get("VOYAGE_API_KEY")
    return VoyageEmbeddingProvider(api_key=api_key, model=model)


def _embed_batch_with_retry(
    batch: List["Document"], provider: EmbeddingProvider, model: str, conn, attempt: int,
) -> Tuple[int, int]:
    """배치 하나를 임베딩 시도합니다. Rate limit이면 절반으로 쪼개 재시도하고,
    그 외 예외는 재시도해도 의미가 없으므로 바로 실패로 집계합니다."""
    from storage.supabase_store import update_embeddings_batch

    texts = [d.title + " " + d.content for d in batch]
    try:
        embeddings = provider.embed_documents(texts)
    except voyageai.error.RateLimitError:
        if attempt >= _MAX_RATE_LIMIT_RETRIES or len(batch) <= 1:
            logger.exception("일괄 임베딩 배치 실패 (rate limit 재시도 한도 초과, 문서 수=%d)", len(batch))
            return 0, len(batch)
        wait = _RATE_LIMIT_BASE_WAIT_SECONDS * (attempt + 1)
        logger.warning(
            "Voyage rate limit, %d초 대기 후 배치를 절반으로 쪼개 재시도 (문서 수=%d, 시도=%d/%d)",
            wait, len(batch), attempt + 1, _MAX_RATE_LIMIT_RETRIES,
        )
        time.sleep(wait)
        mid = len(batch) // 2
        e1, f1 = _embed_batch_with_retry(batch[:mid], provider, model, conn, attempt + 1)
        e2, f2 = _embed_batch_with_retry(batch[mid:], provider, model, conn, attempt + 1)
        return e1 + e2, f1 + f2
    except Exception:
        logger.exception("일괄 임베딩 배치 실패 (문서 수=%d)", len(batch))
        return 0, len(batch)

    items = [(doc.doc_id, embedding) for doc, embedding in zip(batch, embeddings)]
    update_embeddings_batch(items, model, conn=conn)
    return len(items), 0


def backfill_embeddings(
    docs: List["Document"], provider: EmbeddingProvider, model: str, conn=None,
) -> Tuple[int, int]:
    """문서 목록을 배치로 임베딩해 저장합니다. 배치 하나가 실패해도 나머지는 계속
    진행하고, (성공 건수, 실패 건수)를 반환합니다.

    conn을 넘기면 문서마다 새 DB 커넥션을 여는 대신 그 커넥션을 재사용합니다
    (호출자가 연 커넥션이므로 여기서는 닫지 않습니다).

    DB 저장은 문서별로 update_embedding을 반복 호출하지 않고
    update_embeddings_batch로 배치당 1회 왕복에 묶어서 보냅니다 — Vercel↔Supabase
    처럼 리전이 멀어 왕복마다 수백 ms가 드는 환경에서 문서 수만큼 지연이
    누적되는 걸 피하기 위함입니다."""
    embedded, failed = 0, 0
    for start in range(0, len(docs), _EMBED_BATCH_SIZE):
        batch = docs[start:start + _EMBED_BATCH_SIZE]
        batch_embedded, batch_failed = _embed_batch_with_retry(batch, provider, model, conn, attempt=0)
        embedded += batch_embedded
        failed += batch_failed

    return embedded, failed
