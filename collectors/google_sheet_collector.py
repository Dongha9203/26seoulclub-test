"""
구글 스프레드시트 수집 모듈.

공개 공유 링크(누구나 보기)에서 CSV로 다운로드합니다.
인증 불필요 — /export?format=csv 방식만 사용합니다.
비공개 시트일 경우 명확한 안내 메시지를 반환합니다.
"""

import csv
import io
import logging
import re
from typing import List, Optional
from urllib.parse import urlparse, parse_qs

import requests

from models.document import Document
from utils.category_tagger import tag_category

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 15


def _extract_sheet_id(url: str) -> Optional[str]:
    """URL에서 스프레드시트 ID를 추출합니다."""
    # 형식: https://docs.google.com/spreadsheets/d/{SHEET_ID}/...
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None


def _extract_gid(url: str) -> Optional[str]:
    """URL에서 시트 GID를 추출합니다 (기본값 없으면 None → 첫 번째 시트)."""
    parsed = urlparse(url)
    # fragment에 gid가 있는 경우: #gid=12345
    if parsed.fragment:
        m = re.search(r"gid=(\d+)", parsed.fragment)
        if m:
            return m.group(1)
    # query string에 gid가 있는 경우
    qs = parse_qs(parsed.query)
    if "gid" in qs:
        return qs["gid"][0]
    return None


def _build_csv_export_url(sheet_id: str, gid: Optional[str] = None) -> str:
    """CSV export URL을 구성합니다."""
    base = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    if gid:
        base += f"&gid={gid}"
    return base


def collect_google_sheet(url: str, source_origin: Optional[str] = None) -> List[Document]:
    """
    공개 구글 스프레드시트 URL을 받아 Document 리스트를 반환합니다.

    Args:
        url: 공개 공유 링크 (예: https://docs.google.com/spreadsheets/d/xxx/edit?usp=sharing)
        source_origin: 출처 이름 (미지정 시 URL에서 ID 사용)

    Raises:
        ValueError: URL 형식이 잘못되었거나 비공개 시트일 경우
    """
    if not url or not url.strip():
        raise ValueError("구글 스프레드시트 URL이 비어있습니다.")

    sheet_id = _extract_sheet_id(url)
    if not sheet_id:
        raise ValueError(
            f"유효한 구글 스프레드시트 URL이 아닙니다: {url}\n"
            "형식 예시: https://docs.google.com/spreadsheets/d/{{SHEET_ID}}/edit?usp=sharing"
        )

    gid = _extract_gid(url)
    csv_url = _build_csv_export_url(sheet_id, gid)
    origin = source_origin or f"google_sheet:{sheet_id}"

    logger.info("구글 시트 수집 시작: %s", csv_url)

    try:
        resp = requests.get(csv_url, timeout=_TIMEOUT_SECONDS, allow_redirects=True)
    except requests.RequestException as e:
        raise ValueError(f"구글 스프레드시트 다운로드 실패: {e}") from e

    # 공개 시트가 아니면 로그인 페이지로 리다이렉트 되거나 403/401 반환
    if resp.status_code == 403:
        raise ValueError(
            "구글 스프레드시트에 접근할 수 없습니다 (HTTP 403).\n"
            "해당 시트가 '링크가 있는 모든 사용자가 볼 수 있음'으로 설정되어 있는지 확인하세요.\n"
            f"URL: {url}"
        )
    if resp.status_code == 401:
        raise ValueError(
            "구글 스프레드시트 인증이 필요합니다 (HTTP 401).\n"
            "이 시스템은 공개 시트만 지원합니다. 시트 공유 설정을 '링크가 있는 모든 사용자'로 변경하세요."
        )
    if resp.status_code != 200:
        raise ValueError(
            f"구글 스프레드시트 다운로드 실패 (HTTP {resp.status_code}).\n"
            f"URL: {url}"
        )

    # 로그인 페이지로 리다이렉트된 경우 Content-Type이 text/html
    content_type = resp.headers.get("Content-Type", "")
    if "text/html" in content_type:
        raise ValueError(
            "구글 스프레드시트가 비공개 시트입니다 (HTML 응답 반환).\n"
            "시트 공유 설정을 '링크가 있는 모든 사용자가 볼 수 있음'으로 변경 후 다시 시도하세요.\n"
            f"URL: {url}"
        )

    return _parse_csv_to_documents(resp.text, origin, url)


def _parse_csv_to_documents(csv_text: str, source_origin: str, sheet_url: str) -> List[Document]:
    """CSV 텍스트를 Document 리스트로 변환합니다."""
    documents = []
    reader = csv.DictReader(io.StringIO(csv_text))
    headers = reader.fieldnames or []

    # 제목 컬럼 후보: '질문', 'title', '제목', 'question', '항목'
    title_col = _find_column(headers, ["질문", "title", "제목", "question", "항목", "내용"])
    # 본문 컬럼 후보: '답변', 'content', '내용', 'answer', '설명'
    content_col = _find_column(headers, ["답변", "content", "내용", "answer", "설명", "본문"])

    for row_idx, row in enumerate(reader):
        if not any(v.strip() for v in row.values()):
            continue  # 빈 행 스킵

        if title_col and content_col:
            title = row.get(title_col, "").strip()
            content = row.get(content_col, "").strip()
        elif headers:
            # 첫 번째 컬럼 → title, 나머지 → content
            first_key = headers[0]
            title = row.get(first_key, "").strip()
            content = " | ".join(
                f"{k}: {v}" for k, v in row.items()
                if k != first_key and v.strip()
            )
        else:
            continue

        if not title and not content:
            continue

        doc = Document.new(
            source_type="google_sheet",
            source_origin=source_origin,
            title=title or f"행 {row_idx + 1}",
            content=content,
            is_editable=True,
        )
        doc.category = tag_category(doc.title, doc.content)
        documents.append(doc)

    logger.info("구글 시트 수집 완료: %d개 Document 생성", len(documents))
    return documents


def _find_column(headers: list, candidates: List[str]) -> Optional[str]:
    """헤더 목록에서 후보 컬럼명을 찾아 반환합니다 (대소문자 무시)."""
    lower_headers = {h.lower(): h for h in headers}
    for c in candidates:
        if c.lower() in lower_headers:
            return lower_headers[c.lower()]
    return None
