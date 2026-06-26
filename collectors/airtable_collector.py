"""
Airtable 수집 모듈.

Airtable REST API v0를 사용해 Base의 테이블 레코드를 수집합니다.
인증: AIRTABLE_API_TOKEN (환경변수)
페이지네이션: offset 커서 방식 (최대 100건/페이지)

동일한 레코드는 Airtable record ID 기준으로 고정된 doc_id를 생성하므로
incremental sync 시 내용이 바뀌지 않은 레코드는 임베딩이 보존됩니다.
"""

import logging
import os
import uuid
from typing import List, Optional
from urllib.parse import quote

import requests

from models.document import Document
from utils.category_tagger import tag_category

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.airtable.com/v0"
_TIMEOUT_SECONDS = 15
_PAGE_SIZE = 100


def _get_api_token() -> str:
    token = os.environ.get("AIRTABLE_API_TOKEN", "").strip()
    if not token:
        raise EnvironmentError(
            "AIRTABLE_API_TOKEN 환경변수가 설정되지 않았습니다. "
            ".env 파일에 AIRTABLE_API_TOKEN을 추가하세요."
        )
    return token


def _stable_record_doc_id(source_origin: str, record_id: str) -> str:
    """Airtable record ID 기준으로 고정된 doc_id를 생성합니다.

    같은 레코드는 항상 같은 doc_id가 되어야 incremental sync에서
    내용이 안 바뀐 레코드의 임베딩이 보존됩니다.
    """
    key = f"{source_origin}:{record_id}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def collect_airtable(base_id: str, table_name: str, view: Optional[str] = None) -> List[Document]:
    """
    Airtable 테이블 레코드를 수집해 Document 리스트로 반환합니다.

    Args:
        base_id: Airtable Base ID (예: appXXXXXXXXXXXXXX)
        table_name: 테이블 이름 (예: "FAQ", "일정")
        view: 특정 뷰 이름 (미지정 시 기본 뷰)

    Raises:
        EnvironmentError: AIRTABLE_API_TOKEN 미설정
        ValueError: API 오류 또는 접근 권한 없음
    """
    token = _get_api_token()
    headers = {"Authorization": f"Bearer {token}"}
    source_origin = f"airtable:{base_id}:{table_name}"
    url = f"{_BASE_URL}/{base_id}/{quote(table_name, safe='')}"

    params: dict = {"pageSize": _PAGE_SIZE}
    if view:
        params["view"] = view

    all_records: list = []
    logger.info("Airtable 수집 시작: base=%s, table=%s", base_id, table_name)

    while True:
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=_TIMEOUT_SECONDS)
        except requests.RequestException as e:
            raise ValueError(f"Airtable API 요청 실패: {e}") from e

        if resp.status_code == 401:
            raise ValueError(
                "Airtable 인증 실패 (HTTP 401). "
                "AIRTABLE_API_TOKEN이 올바른지 확인하세요."
            )
        if resp.status_code == 403:
            raise ValueError(
                f"Airtable Base 접근 권한 없음 (HTTP 403). "
                f"API 토큰에 해당 Base의 읽기 권한이 있는지 확인하세요. base_id={base_id}"
            )
        if resp.status_code == 404:
            raise ValueError(
                f"Airtable Base 또는 테이블을 찾을 수 없습니다 (HTTP 404). "
                f"base_id={base_id}, table={table_name}"
            )
        if resp.status_code != 200:
            raise ValueError(
                f"Airtable API 오류 (HTTP {resp.status_code}): {resp.text[:300]}"
            )

        data = resp.json()
        all_records.extend(data.get("records", []))

        offset = data.get("offset")
        if not offset:
            break
        params["offset"] = offset

    documents = _records_to_documents(all_records, source_origin, table_name)
    logger.info(
        "Airtable 수집 완료: %d개 레코드 → %d개 Document", len(all_records), len(documents)
    )
    return documents


def _records_to_documents(records: list, source_origin: str, table_name: str) -> List[Document]:
    """Airtable 레코드를 Document 리스트로 변환합니다."""
    documents = []
    for record in records:
        fields: dict = record.get("fields", {})
        if not fields:
            continue

        field_names = list(fields.keys())
        title_col = _find_field(
            field_names, ["질문", "title", "제목", "question", "항목", "이름", "name"]
        )
        content_col = _find_field(
            field_names, ["답변", "content", "내용", "answer", "설명", "본문", "description"]
        )

        if title_col and content_col:
            title = str(fields.get(title_col, "")).strip()
            content = str(fields.get(content_col, "")).strip()
        elif field_names:
            first_key = field_names[0]
            title = str(fields.get(first_key, "")).strip()
            content = " | ".join(
                f"{k}: {v}"
                for k, v in fields.items()
                if k != first_key and str(v).strip()
            )
        else:
            continue

        if not title and not content:
            continue

        record_id = record.get("id", "")
        doc = Document.new(
            source_type="airtable",
            source_origin=source_origin,
            title=title or f"{table_name} #{record_id}",
            content=content,
            is_editable=True,
        )
        doc.doc_id = _stable_record_doc_id(source_origin, record_id)
        doc.category = tag_category(doc.title, doc.content)
        documents.append(doc)

    return documents


def _find_field(field_names: list, candidates: List[str]) -> Optional[str]:
    """필드명 목록에서 후보 필드명을 찾아 반환합니다 (대소문자 무시)."""
    lower_map = {n.lower(): n for n in field_names}
    for c in candidates:
        if c.lower() in lower_map:
            return lower_map[c.lower()]
    return None
