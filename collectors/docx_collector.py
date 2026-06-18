"""
Word(.docx) 파일 수집 모듈.
python-docx를 사용해 heading 단위로 청킹합니다.
"""

import logging
from pathlib import Path
from typing import List, Optional

from docx import Document as DocxDocument
from docx.oxml.ns import qn

from models.document import Document
from utils.category_tagger import tag_category

logger = logging.getLogger(__name__)

HEADING_STYLES = {"heading 1", "heading 2", "heading 3", "제목 1", "제목 2", "제목 3"}
FALLBACK_CHUNK_SIZE = 800


def collect_docx(file_path: str, source_origin: Optional[str] = None) -> List[Document]:
    """
    .docx 파일을 파싱해 Document 리스트를 반환합니다.

    Args:
        file_path: .docx 파일 경로
        source_origin: 출처 이름 (미지정 시 파일명 사용)

    Raises:
        FileNotFoundError: 파일이 없을 경우
        ValueError: .docx가 아닌 파일일 경우
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {file_path}")
    if path.suffix.lower() != ".docx":
        raise ValueError(
            f"지원하지 않는 파일 형식입니다: {path.suffix}\n"
            ".docx(Word) 파일만 지원합니다."
        )

    origin = source_origin or path.name
    logger.info("DOCX 수집 시작: %s", origin)

    docx_doc = DocxDocument(str(path))
    documents = _chunk_docx(docx_doc, origin)

    logger.info("DOCX 수집 완료: %d개 Document 생성 (%s)", len(documents), origin)
    return documents


def _is_heading(paragraph) -> bool:
    style_name = paragraph.style.name.lower() if paragraph.style else ""
    return style_name in HEADING_STYLES or style_name.startswith("heading")


def _chunk_docx(docx_doc: DocxDocument, source_origin: str) -> List[Document]:
    paragraphs = docx_doc.paragraphs
    has_headings = any(_is_heading(p) for p in paragraphs if p.text.strip())

    if has_headings:
        return _chunk_by_headings(paragraphs, source_origin)
    else:
        return _chunk_fallback(paragraphs, source_origin)


def _chunk_by_headings(paragraphs, source_origin: str) -> List[Document]:
    documents: List[Document] = []
    current_title: Optional[str] = None
    content_parts: List[str] = []

    def flush():
        nonlocal current_title, content_parts
        if current_title is None and not content_parts:
            return
        title = current_title or source_origin
        content = "\n".join(content_parts)
        doc = Document.new(
            source_type="docx",
            source_origin=source_origin,
            title=title,
            content=content,
            is_editable=True,
        )
        doc.category = tag_category(doc.title, doc.content)
        documents.append(doc)
        current_title = None
        content_parts = []

    for para in paragraphs:
        text = para.text.strip()
        if not text:
            continue
        if _is_heading(para):
            flush()
            current_title = text
        else:
            content_parts.append(text)

    flush()
    return documents


def _chunk_fallback(paragraphs, source_origin: str) -> List[Document]:
    all_parts = [p.text.strip() for p in paragraphs if p.text.strip()]
    full_text = "\n".join(all_parts)

    if not full_text:
        return []

    if len(full_text) <= FALLBACK_CHUNK_SIZE:
        doc = Document.new(
            source_type="docx",
            source_origin=source_origin,
            title=source_origin,
            content=full_text,
            is_editable=True,
        )
        doc.category = tag_category(doc.title, doc.content)
        return [doc]

    documents = []
    for idx, start in enumerate(range(0, len(full_text), FALLBACK_CHUNK_SIZE)):
        chunk = full_text[start:start + FALLBACK_CHUNK_SIZE]
        doc = Document.new(
            source_type="docx",
            source_origin=source_origin,
            title=f"{source_origin} (파트 {idx + 1})",
            content=chunk,
            is_editable=True,
        )
        doc.category = tag_category(doc.title, doc.content)
        documents.append(doc)
    return documents
