import logging
from typing import List, Dict

from models.document import Document

logger = logging.getLogger(__name__)


def validate_notion_block_ids(documents: List[Document]) -> Dict:
    """
    노션 소스 Document 중 notion_block_id가 누락된 건을 검사합니다.
    누락 시 경고 로그를 남기고 검사 결과 dict를 반환합니다.
    """
    notion_docs = [d for d in documents if d.source_type == "notion"]
    missing = [d for d in notion_docs if not d.notion_block_id]

    for doc in missing:
        logger.warning(
            "[BLOCK ID MISSING] doc_id=%s | origin=%s | title=%s",
            doc.doc_id, doc.source_origin, doc.title[:60],
        )

    total = len(notion_docs)
    success = total - len(missing)
    rate = (success / total * 100) if total > 0 else 100.0

    return {
        "total_notion_docs": total,
        "block_id_present": success,
        "block_id_missing": len(missing),
        "success_rate_pct": round(rate, 1),
        "missing_titles": [d.title[:60] for d in missing],
    }


def validate_documents(documents: List[Document]) -> Dict:
    """
    전체 Document 목록에 대한 무결성 검사를 수행합니다.
    - 필수 필드 누락
    - 노션 소스의 block_id 누락
    - 빈 title/content
    """
    errors = []

    for i, doc in enumerate(documents):
        prefix = f"[{i}] doc_id={doc.doc_id}"

        if not doc.doc_id:
            errors.append(f"{prefix}: doc_id 누락")
        if not doc.source_type:
            errors.append(f"{prefix}: source_type 누락")
        if not doc.source_origin:
            errors.append(f"{prefix}: source_origin 누락")
        if not doc.title or not doc.title.strip():
            errors.append(f"{prefix}: title 비어있음")
        if not doc.content or not doc.content.strip():
            errors.append(f"{prefix}: content 비어있음")
        if doc.source_type == "notion" and not doc.notion_block_id:
            errors.append(f"{prefix}: notion block_id 누락 (title={doc.title[:40]})")

    notion_result = validate_notion_block_ids(documents)

    return {
        "total_documents": len(documents),
        "validation_errors": errors,
        "error_count": len(errors),
        "notion_block_id_check": notion_result,
        "passed": len(errors) == 0,
    }
