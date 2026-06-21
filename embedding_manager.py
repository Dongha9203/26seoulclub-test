"""
임베딩 Provider 모듈.

Provider 추상클래스 구조를 두어 향후 다른 임베딩 서비스로 교체 가능하게 합니다.
현재는 Voyage AI(voyage-4)만 구현합니다. 모델명은 config.json에서 읽어오며
코드에 하드코딩하지 않습니다.
"""

import logging
import os
from abc import ABC, abstractmethod
from typing import List, Tuple, TYPE_CHECKING

import voyageai

if TYPE_CHECKING:
    from models.document import Document

logger = logging.getLogger(__name__)

_EMBED_BATCH_SIZE = 100


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


def backfill_embeddings(
    docs: List["Document"], provider: EmbeddingProvider, model: str, conn=None,
) -> Tuple[int, int]:
    """문서 목록을 배치로 임베딩해 저장합니다. 배치 하나가 실패해도 나머지는 계속
    진행하고, (성공 건수, 실패 건수)를 반환합니다.

    conn을 넘기면 문서마다 새 DB 커넥션을 여는 대신 그 커넥션을 재사용합니다
    (호출자가 연 커넥션이므로 여기서는 닫지 않습니다)."""
    from storage.supabase_store import update_embedding

    embedded, failed = 0, 0
    for start in range(0, len(docs), _EMBED_BATCH_SIZE):
        batch = docs[start:start + _EMBED_BATCH_SIZE]
        texts = [d.title + " " + d.content for d in batch]
        try:
            embeddings = provider.embed_documents(texts)
        except Exception:
            logger.exception("일괄 임베딩 배치 실패 (start=%d)", start)
            failed += len(batch)
            continue
        for doc, embedding in zip(batch, embeddings):
            update_embedding(doc.doc_id, embedding, model, conn=conn)
            embedded += 1

    return embedded, failed
