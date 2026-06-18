"""
임베딩 백필 스크립트.

initial_setup.py로 적재된 문서 중 임베딩이 없거나(또는 모델이 교체되어
기존 임베딩이 무효화된) 문서를 Voyage AI로 임베딩하여 SQLite에 저장합니다.

실행 방법:
  python build_embeddings.py

전제 조건:
  1. .env 파일에 VOYAGE_API_KEY가 설정되어 있어야 합니다.
  2. initial_setup.py로 documents 테이블에 데이터가 적재되어 있어야 합니다.
"""

import json
import logging
import sys
from pathlib import Path

_root = Path(__file__).parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from dotenv import load_dotenv
load_dotenv(_root / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("build_embeddings")

_BATCH_SIZE = 100


def main():
    config_path = _root / "config.json"
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    from embedding_manager import get_embedding_provider
    from storage.sqlite_store import get_documents_missing_embedding, update_embedding

    model = config.get("embedding_model")
    print(f"임베딩 모델: {model}")

    provider = get_embedding_provider(config)
    docs = get_documents_missing_embedding(model)

    if not docs:
        print("백필 대상 문서가 없습니다 (모두 최신 모델로 임베딩되어 있음).")
        return

    print(f"백필 대상: {len(docs)}개 문서")

    total_done = 0
    for start in range(0, len(docs), _BATCH_SIZE):
        batch = docs[start:start + _BATCH_SIZE]
        texts = [d.title + " " + d.content for d in batch]
        try:
            embeddings = provider.embed_documents(texts)
        except Exception as e:
            logger.error("배치 임베딩 실패 (start=%d): %s", start, e)
            continue

        for doc, embedding in zip(batch, embeddings):
            update_embedding(doc.doc_id, embedding, model)
            total_done += 1

        print(f"  {total_done}/{len(docs)}개 완료")

    print(f"임베딩 백필 완료: {total_done}/{len(docs)}개")


if __name__ == "__main__":
    main()
