"""
HWP/HWPX 변환 파일 수집 모듈.

HWP/HWPX 파일을 직접 파싱하지 않습니다.
사용자가 사전에 PDF 또는 .txt로 변환한 파일을 받아 파싱합니다.

.hwp / .hwpx 확장자로 업로드하면 안내 메시지를 반환합니다.
"""

import logging
from pathlib import Path
from typing import List, Optional

from models.document import Document
from utils.category_tagger import tag_category

logger = logging.getLogger(__name__)

_HWP_EXTENSIONS = {".hwp", ".hwpx"}
FALLBACK_CHUNK_SIZE = 800


def collect_hwp_converted(file_path: str, source_origin: Optional[str] = None) -> List[Document]:
    """
    HWP를 사전 변환한 .pdf 또는 .txt 파일을 파싱해 Document 리스트를 반환합니다.

    .hwp / .hwpx 파일이 직접 전달되면 변환 안내 메시지와 함께 ValueError를 발생시킵니다.

    Args:
        file_path: 변환된 .pdf 또는 .txt 파일 경로
        source_origin: 출처 이름 (미지정 시 파일명 사용)

    Raises:
        ValueError: .hwp/.hwpx 파일이 전달된 경우, 또는 지원하지 않는 확장자
        FileNotFoundError: 파일이 없을 경우
    """
    path = Path(file_path)
    suffix = path.suffix.lower()

    # HWP/HWPX 직접 업로드 차단
    if suffix in _HWP_EXTENSIONS:
        raise ValueError(
            f"HWP/HWPX 파일을 직접 업로드할 수 없습니다: {path.name}\n"
            "\n"
            "한글 파일을 업로드하려면 다음 단계를 따르세요:\n"
            "  1. 한글(HWP) 프로그램에서 파일을 열어주세요.\n"
            "  2. [파일] → [다른 이름으로 저장] → PDF 또는 텍스트(.txt)로 저장하세요.\n"
            "  3. 변환된 PDF 또는 .txt 파일을 업로드해 주세요.\n"
            "\n"
            "또는 LibreOffice를 사용해 변환할 수 있습니다:\n"
            "  libreoffice --headless --convert-to pdf your_file.hwp"
        )

    if not path.exists():
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {file_path}")

    origin = source_origin or path.name

    if suffix == ".pdf":
        from collectors.pdf_collector import collect_pdf
        docs = collect_pdf(file_path, source_origin=origin)
        # source_type을 hwp_converted로 재지정
        for doc in docs:
            doc.source_type = "hwp_converted"
        return docs

    elif suffix == ".txt":
        return _collect_txt(path, origin)

    else:
        raise ValueError(
            f"지원하지 않는 파일 형식입니다: {suffix}\n"
            "HWP 변환 파일은 .pdf 또는 .txt만 지원합니다."
        )


def _collect_txt(path: Path, source_origin: str) -> List[Document]:
    """단순 텍스트 파일을 읽어 Document 리스트로 반환합니다."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="cp949", errors="replace")

    text = text.strip()
    if not text:
        logger.warning("텍스트 파일이 비어있습니다: %s", path.name)
        return []

    if len(text) <= FALLBACK_CHUNK_SIZE:
        doc = Document.new(
            source_type="hwp_converted",
            source_origin=source_origin,
            title=source_origin,
            content=text,
            is_editable=True,
        )
        doc.category = tag_category(doc.title, doc.content)
        return [doc]

    # 800자 단위 분할
    documents = []
    for idx, start in enumerate(range(0, len(text), FALLBACK_CHUNK_SIZE)):
        chunk = text[start:start + FALLBACK_CHUNK_SIZE]
        doc = Document.new(
            source_type="hwp_converted",
            source_origin=source_origin,
            title=f"{source_origin} (파트 {idx + 1})",
            content=chunk,
            is_editable=True,
        )
        doc.category = tag_category(doc.title, doc.content)
        documents.append(doc)

    logger.info("HWP 변환 텍스트 수집 완료: %d개 Document (%s)", len(documents), source_origin)
    return documents
