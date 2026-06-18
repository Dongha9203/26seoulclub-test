"""
PDF 파일 수집 모듈.
pdfplumber를 사용해 페이지 단위로 텍스트를 추출합니다.
"""

import logging
from pathlib import Path
from typing import List, Optional

import pdfplumber

from models.document import Document
from utils.category_tagger import tag_category

logger = logging.getLogger(__name__)

MIN_CHARS_PER_PAGE = 50  # 이보다 짧은 페이지는 이전 Document에 병합
FALLBACK_CHUNK_SIZE = 800


def collect_pdf(file_path: str, source_origin: Optional[str] = None) -> List[Document]:
    """
    .pdf 파일을 파싱해 Document 리스트를 반환합니다.
    페이지 텍스트가 MIN_CHARS_PER_PAGE보다 짧으면 직전 Document에 병합합니다.

    Args:
        file_path: .pdf 파일 경로
        source_origin: 출처 이름 (미지정 시 파일명 사용)

    Raises:
        FileNotFoundError: 파일이 없을 경우
        ValueError: .pdf가 아닌 파일일 경우
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {file_path}")
    if path.suffix.lower() != ".pdf":
        raise ValueError(
            f"지원하지 않는 파일 형식입니다: {path.suffix}\n"
            ".pdf 파일만 지원합니다."
        )

    origin = source_origin or path.name
    logger.info("PDF 수집 시작: %s", origin)

    documents = []
    accumulated: List[str] = []
    accumulated_start_page = 1

    with pdfplumber.open(str(path)) as pdf:
        total_pages = len(pdf.pages)
        for page_num, page in enumerate(pdf.pages, start=1):
            text = (page.extract_text() or "").strip()
            if not text:
                continue

            accumulated.append(text)

            if len("\n".join(accumulated)) >= MIN_CHARS_PER_PAGE:
                content = "\n".join(accumulated)
                title = (
                    f"{origin} — {accumulated_start_page}p"
                    if accumulated_start_page == page_num
                    else f"{origin} — {accumulated_start_page}-{page_num}p"
                )
                doc = Document.new(
                    source_type="pdf",
                    source_origin=origin,
                    title=title,
                    content=content,
                    is_editable=True,
                )
                doc.category = tag_category(doc.title, doc.content)
                documents.append(doc)
                accumulated = []
                accumulated_start_page = page_num + 1

    # 남은 텍스트 처리
    if accumulated:
        content = "\n".join(accumulated)
        title = (
            f"{origin} — {accumulated_start_page}p"
            if accumulated_start_page == total_pages
            else f"{origin} — {accumulated_start_page}-{total_pages}p"
        )
        doc = Document.new(
            source_type="pdf",
            source_origin=origin,
            title=title,
            content=content,
            is_editable=True,
        )
        doc.category = tag_category(doc.title, doc.content)
        documents.append(doc)

    if not documents:
        logger.warning("PDF에서 텍스트를 추출하지 못했습니다: %s", origin)

    logger.info("PDF 수집 완료: %d개 Document 생성 (%s)", len(documents), origin)
    return documents
