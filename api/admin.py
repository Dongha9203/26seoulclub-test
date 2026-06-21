"""
운영 대시보드 백엔드: /api/admin/*

3단계 `api/chat.py`(인증 없는 공개 엔드포인트)와는 별도 FastAPI 앱으로 분리합니다.
이 파일의 모든 라우트는 /login을 제외하면 JWT 인증이 필수입니다 — 별도 파일로
분리해두면 "이 파일의 모든 라우트는 인증 필수"라는 불변식을 지키기 쉽습니다.
"""

import json
import logging
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from dotenv import load_dotenv
load_dotenv(_root / ".env")

from auth import get_current_operator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

_static_config: Optional[dict] = None

_INCOMPLETE_CAUSES = ["검색실패", "질문모호성"]
_UNRESOLVED_CAUSES = ["지식DB공백", "정책밖요청"]
_MANUAL_SYNC_KEY = "_manual_sync_all"


def _load_static_config() -> dict:
    global _static_config
    if _static_config is None:
        with open(_root / "config.json", "r", encoding="utf-8") as f:
            _static_config = json.load(f)
    return _static_config


# ──────────────────────────────────────────────────────────────────
# 인증
# ──────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    access_token: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@app.post("/login", response_model=LoginResponse)
def login(req: LoginRequest):
    from auth import verify_password, create_access_token
    from storage.admin_store import get_operator_by_email

    operator = get_operator_by_email(req.email)
    if not operator or not verify_password(req.password, operator["password_hash"]):
        raise HTTPException(status_code=401, detail="이메일 또는 비밀번호가 올바르지 않습니다.")
    return LoginResponse(access_token=create_access_token(req.email))


@app.post("/change-password")
def change_password(req: ChangePasswordRequest, operator_email: str = Depends(get_current_operator)):
    from auth import verify_password, hash_password
    from storage.admin_store import get_operator_by_email, update_password

    if len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="새 비밀번호는 8자 이상이어야 합니다.")

    operator = get_operator_by_email(operator_email)
    if not operator or not verify_password(req.current_password, operator["password_hash"]):
        # 401은 "인증 자체가 안 됨"(JWT 무효/만료)을 뜻하며, 프론트엔드 api() 헬퍼가
        # 401을 받으면 무조건 로그아웃 후 로그인 화면으로 보냅니다. 현재 비밀번호가
        # 틀린 것은 이미 유효한 세션 안에서의 입력 오류이므로 403으로 구분합니다.
        raise HTTPException(status_code=403, detail="현재 비밀번호가 올바르지 않습니다.")

    update_password(operator_email, hash_password(req.new_password))
    return {"status": "ok"}


# ──────────────────────────────────────────────────────────────────
# ① 모니터링
# ──────────────────────────────────────────────────────────────────

@app.get("/monitoring/daily-counts")
def daily_counts(limit: int = 30, offset: int = 0, operator_email: str = Depends(get_current_operator)):
    from storage.supabase_store import get_daily_qa_counts
    if limit <= 0 or limit > 200:
        raise HTTPException(status_code=400, detail="limit은 1~200 사이여야 합니다.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset은 0 이상이어야 합니다.")
    return {"daily_counts": get_daily_qa_counts(limit, offset)}


@app.get("/monitoring/qa-logs")
def qa_logs(limit: int = 50, offset: int = 0, operator_email: str = Depends(get_current_operator)):
    from storage.supabase_store import get_qa_logs_paginated
    if limit <= 0 or limit > 200:
        raise HTTPException(status_code=400, detail="limit은 1~200 사이여야 합니다.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset은 0 이상이어야 합니다.")
    return {"logs": get_qa_logs_paginated(limit, offset)}


# ──────────────────────────────────────────────────────────────────
# ② 조치관리
# ──────────────────────────────────────────────────────────────────

@app.get("/actions/incomplete")
def incomplete_answers(limit: int = 50, offset: int = 0,
                        operator_email: str = Depends(get_current_operator)):
    from storage.supabase_store import get_logs_by_failure_causes
    return {"logs": get_logs_by_failure_causes(_INCOMPLETE_CAUSES, limit, offset)}


@app.get("/actions/unresolved")
def unresolved_answers(limit: int = 50, offset: int = 0,
                        operator_email: str = Depends(get_current_operator)):
    from storage.supabase_store import get_logs_by_failure_causes
    return {"logs": get_logs_by_failure_causes(_UNRESOLVED_CAUSES, limit, offset)}


@app.delete("/actions/{log_id}")
def resolve_action(log_id: str, operator_email: str = Depends(get_current_operator)):
    """운영자가 노션/데이터를 직접 수정해 처리를 완료했음을 표시합니다.
    qa_log 행은 지우지 않고(통계 보존) action_status만 '완료'로 남겨, 이후
    불완전/미해결 답변 목록 조회에서 제외되도록 합니다."""
    from storage.action_store import set_status
    try:
        set_status(log_id, "완료")
    except Exception:
        logger.exception("조치 완료 처리 중 오류 (log_id=%s)", log_id)
        raise HTTPException(status_code=404, detail="존재하지 않는 log_id이거나 처리 중 오류가 발생했습니다.")
    return {"status": "ok"}


@app.get("/actions/failure-report")
def failure_report(operator_email: str = Depends(get_current_operator)):
    from storage.supabase_store import get_failure_cause_counts
    counts = get_failure_cause_counts()
    full = {cause: counts.get(cause, 0)
            for cause in ["지식DB공백", "검색실패", "질문모호성", "정책밖요청", "API오류"]}
    return {"counts": full}


# ──────────────────────────────────────────────────────────────────
# ③ 운영설정
# ──────────────────────────────────────────────────────────────────

@app.get("/settings")
def get_all_settings(operator_email: str = Depends(get_current_operator)):
    from storage.settings_store import get_settings
    return get_settings()


class OperationTeamUpdate(BaseModel):
    name: str
    address: str
    phone: str
    email_list: List[str]
    operating_hours: str


@app.put("/settings/operation-team")
def update_operation_team(req: OperationTeamUpdate,
                           operator_email: str = Depends(get_current_operator)):
    from storage.settings_store import update_settings
    return update_settings({"operation_team": req.model_dump()})


class ToneUpdate(BaseModel):
    personality: str
    language_purity: str
    vip_consistency: str
    formality: str
    channel: str
    emotional_labor: str
    persona: str
    factuality: str


@app.put("/settings/tone")
def update_tone(req: ToneUpdate, operator_email: str = Depends(get_current_operator)):
    from storage.settings_store import update_settings
    return update_settings({"tone_elements": req.model_dump()})


def _clean_keyword_categories(raw: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """공백/중복 줄을 정리합니다 (대시보드 textarea에서 줄 단위로 입력받으므로)."""
    cleaned = {}
    for category, words in raw.items():
        seen = []
        for w in words:
            w = w.strip()
            if w and w not in seen:
                seen.append(w)
        cleaned[category] = seen
    return cleaned


class SituationKeywordsUpdate(BaseModel):
    policy_violation: List[str]
    escalation_request: List[str]
    gratitude: List[str]
    simple_rejection: List[str]


@app.put("/settings/situation-keywords")
def update_situation_keywords(req: SituationKeywordsUpdate,
                               operator_email: str = Depends(get_current_operator)):
    from storage.settings_store import update_settings
    return update_settings({"situation_keywords": _clean_keyword_categories(req.model_dump())})


class ForbiddenWordsUpdate(BaseModel):
    profanity: List[str]
    hate_speech: List[str]
    threats: List[str]


@app.put("/settings/forbidden-words")
def update_forbidden_words(req: ForbiddenWordsUpdate,
                            operator_email: str = Depends(get_current_operator)):
    from storage.settings_store import update_settings
    return update_settings({"forbidden_words": _clean_keyword_categories(req.model_dump())})


class ApiParamsUpdate(BaseModel):
    max_question_length: int
    rate_limit_per_minute: int


@app.put("/settings/api-params")
def update_api_params(req: ApiParamsUpdate, operator_email: str = Depends(get_current_operator)):
    if not (1 <= req.max_question_length <= 2000):
        raise HTTPException(status_code=400, detail="max_question_length는 1~2000 사이여야 합니다.")
    if not (1 <= req.rate_limit_per_minute <= 100):
        raise HTTPException(status_code=400, detail="rate_limit_per_minute는 1~100 사이여야 합니다.")
    from storage.settings_store import update_settings
    return update_settings({
        "max_question_length": req.max_question_length,
        "rate_limit_per_minute": req.rate_limit_per_minute,
    })


# ── Knowledge Base 조회/관리 ─────────────────────────────────────────

@app.get("/kb/documents")
def list_documents(operator_email: str = Depends(get_current_operator)):
    from storage.supabase_store import get_all
    docs = get_all()
    return {"documents": [d.to_dict() for d in docs]}


@app.delete("/kb/documents/{doc_id}")
def delete_document(doc_id: str, operator_email: str = Depends(get_current_operator)):
    from storage.supabase_store import get_connection

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT is_editable FROM documents WHERE doc_id = %s", (doc_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="존재하지 않는 문서입니다.")
            if not row["is_editable"]:
                raise HTTPException(status_code=403, detail="노션 소스 문서는 대시보드에서 삭제할 수 없습니다.")
            cur.execute("DELETE FROM documents WHERE doc_id = %s", (doc_id,))
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@app.post("/kb/documents/{doc_id}/embed")
def embed_document(doc_id: str, operator_email: str = Depends(get_current_operator)):
    from storage.supabase_store import get_connection, update_embedding
    from embedding_manager import get_embedding_provider

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT title, content, is_editable FROM documents WHERE doc_id = %s", (doc_id,)
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        raise HTTPException(status_code=404, detail="존재하지 않는 문서입니다.")
    if not row["is_editable"]:
        raise HTTPException(
            status_code=403,
            detail="노션 소스 문서는 이 기능으로 반영할 수 없습니다. '지금 갱신'을 사용해주세요.",
        )

    config = _load_static_config()
    model = config.get("embedding_model")
    try:
        provider = get_embedding_provider(config)
        embedding = provider.embed_documents([row["title"] + " " + row["content"]])[0]
    except Exception as e:
        logger.exception("문서 임베딩 생성 중 오류")
        raise HTTPException(status_code=502, detail=f"지식베이스 반영에 실패했습니다: {e}")

    update_embedding(doc_id, embedding, model)
    return {"status": "ok"}


@app.post("/kb/documents/embed-all")
def embed_all_documents(operator_email: str = Depends(get_current_operator)):
    """수동 업로드 문서(파일/구글시트) 중 임베딩이 반영되지 않은 문서를 한 번에 처리합니다.

    구글 스프레드시트처럼 한 번 업로드로 문서가 수백~수천 건 생기는 경우, 문서 1건당
    '갱신' 버튼을 일일이 누르는 게 비현실적이라 일괄 처리 경로가 필요합니다.
    노션 소스는 대상에서 제외합니다 (단일 문서 갱신과 동일한 제약 — '지금 갱신' 사용).

    구글시트 업로드처럼 문서가 수백~수천 건일 수 있어, 문서 1건당 DB에 새로
    왕복(round trip)하면 Vercel↔Supabase 간 리전 지연이 누적돼 함수 실행 제한
    시간을 넘길 수 있습니다. 커넥션을 한 번만 열어 재사용하고, 임베딩 쓰기도
    문서별 update_embedding 대신 배치당 1왕복인 update_embeddings_batch로
    처리합니다 (노션 동기화에 적용한 것과 동일한 수정)."""
    from storage.supabase_store import (
        get_connection, get_documents_missing_embedding, update_embeddings_batch,
    )
    from embedding_manager import get_embedding_provider

    config = _load_static_config()
    model = config.get("embedding_model")

    conn = get_connection()
    try:
        docs = [d for d in get_documents_missing_embedding(model, conn=conn) if d.is_editable]

        if not docs:
            return {"status": "ok", "embedded": 0, "failed": 0}

        provider = get_embedding_provider(config)
        embedded, failed = 0, 0
        batch_size = 100
        for start in range(0, len(docs), batch_size):
            batch = docs[start:start + batch_size]
            texts = [d.title + " " + d.content for d in batch]
            try:
                embeddings = provider.embed_documents(texts)
            except Exception:
                logger.exception("일괄 임베딩 배치 실패 (start=%d)", start)
                failed += len(batch)
                continue
            items = [(doc.doc_id, embedding) for doc, embedding in zip(batch, embeddings)]
            update_embeddings_batch(items, model, conn=conn)
            embedded += len(items)
    finally:
        conn.close()

    return {"status": "ok", "embedded": embedded, "failed": failed}


_COLLECTOR_BY_EXT = {".docx": "docx", ".pdf": "pdf", ".xlsx": "excel"}


@app.post("/kb/upload")
def upload_file(file: UploadFile = File(...), operator_email: str = Depends(get_current_operator)):
    from storage.supabase_store import delete_by_source_origin, upsert_documents

    suffix = Path(file.filename or "").suffix.lower()
    source_type = _COLLECTOR_BY_EXT.get(suffix)
    if source_type is None:
        if suffix in (".hwp", ".hwpx"):
            raise HTTPException(
                status_code=400,
                detail="HWP/HWPX는 직접 업로드할 수 없습니다. PDF나 텍스트 파일로 변환 후 업로드해주세요.",
            )
        raise HTTPException(status_code=400, detail=f"지원하지 않는 파일 형식입니다: {suffix or '(확장자 없음)'}")

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file.file.read())
        tmp_path = tmp.name

    try:
        if source_type == "docx":
            from collectors.docx_collector import collect_docx
            docs = collect_docx(tmp_path, source_origin=file.filename)
        elif source_type == "pdf":
            from collectors.pdf_collector import collect_pdf
            docs = collect_pdf(tmp_path, source_origin=file.filename)
        else:
            from collectors.excel_collector import collect_excel
            docs = collect_excel(tmp_path, source_origin=file.filename)
    except Exception as e:
        logger.exception("파일 업로드 처리 중 오류")
        raise HTTPException(status_code=400, detail=f"파일 처리 중 오류가 발생했습니다: {e}")
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    if not docs:
        raise HTTPException(status_code=400, detail="파일에서 추출된 내용이 없습니다.")

    delete_by_source_origin(file.filename)
    inserted = upsert_documents(docs)
    return {"status": "ok", "inserted": inserted}


class GoogleSheetUpload(BaseModel):
    url: str


@app.post("/kb/google-sheet")
def upload_google_sheet(req: GoogleSheetUpload, operator_email: str = Depends(get_current_operator)):
    from collectors.google_sheet_collector import collect_google_sheet
    from storage.supabase_store import delete_by_source_origin, upsert_documents

    if not req.url or not req.url.strip():
        raise HTTPException(status_code=400, detail="구글 스프레드시트 URL을 입력해주세요.")

    try:
        docs = collect_google_sheet(req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("구글 스프레드시트 수집 중 오류")
        raise HTTPException(status_code=502, detail=f"구글 스프레드시트를 가져오지 못했습니다: {e}")

    if not docs:
        raise HTTPException(status_code=400, detail="시트에서 추출된 내용이 없습니다.")

    delete_by_source_origin(docs[0].source_origin)
    inserted = upsert_documents(docs)
    return {"status": "ok", "inserted": inserted}


@app.post("/kb/notion/refresh")
def refresh_notion(operator_email: str = Depends(get_current_operator)):
    from api.sync_notion import _perform_sync
    from storage.supabase_store import upsert_sync_metadata

    try:
        result = _perform_sync()
    except (ConnectionError, TimeoutError) as e:
        logger.exception("노션 즉시 갱신 중 네트워크 오류")
        raise HTTPException(status_code=502, detail=f"노션 갱신에 실패했습니다: {e}")
    except EnvironmentError as e:
        raise HTTPException(status_code=503, detail=f"노션 연동 설정 오류: {e}")
    except Exception as e:
        logger.exception("노션 즉시 갱신 중 오류")
        raise HTTPException(status_code=502, detail=f"노션 갱신에 실패했습니다: {e}")

    now = datetime.now(timezone.utc).isoformat()
    upsert_sync_metadata(_MANUAL_SYNC_KEY, "", now)

    # "pages"는 등록된 최상위 페이지 키 기준이라, 그 안에서 재귀로 발견된 하위
    # 페이지 문서 수가 전부 하나로 합산되어 보입니다(예: "메인페이지 12건 변경"이
    # 실제로는 메인페이지 1건 + 하위 페이지 5곳의 11건을 합친 숫자). 운영자가
    # 실제로 어느 출처가 갱신됐는지 알 수 있도록 "sources"(출처별 건수)로 표시합니다.
    summary_parts = [f"{name} {count}건 변경" for name, count in result.get("sources", {}).items()]
    for info in result.get("pages", {}).values():
        # URL이 설정되지 않은 페이지(예: 미사용 FAQ)는 매번 똑같이 "건너뜀"으로
        # 떠서 운영자에게 의미 있는 정보가 아니므로 메시지에서 제외합니다.
        if info.get("skipped") and info.get("reason") != "URL 미설정":
            summary_parts.append(f"{info.get('page_name', '알수없음')} 건너뜀({info.get('reason', '')})")
    summary_text = ", ".join(summary_parts) if summary_parts else "변경 없음"

    embedding = result.get("embedding") or {}
    if embedding.get("embedded"):
        summary_text += f" / 임베딩 {embedding['embedded']}건 반영"
    if embedding.get("failed"):
        summary_text += f" (임베딩 실패 {embedding['failed']}건, 잠시 후 다시 시도해주세요)"
    result["summary_text"] = summary_text
    result["last_synced_at"] = now
    return result


@app.get("/kb/notion/last-sync")
def notion_last_sync(operator_email: str = Depends(get_current_operator)):
    from storage.supabase_store import get_connection

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT page_key, last_synced_at FROM sync_metadata "
                "WHERE last_synced_at IS NOT NULL AND last_synced_at != '' "
                "ORDER BY last_synced_at DESC LIMIT 1"
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        return {"last_synced_at": None, "mode": None}
    mode = "수동" if row["page_key"] == _MANUAL_SYNC_KEY else "자동"
    return {"last_synced_at": row["last_synced_at"], "mode": mode}


@app.get("/kb/notion-faq-url")
def notion_faq_url(operator_email: str = Depends(get_current_operator)):
    url = _load_static_config().get("notion_pages", {}).get("faq", "")
    return {"url": url if url and "{{" not in url else None}


@app.get("/kb/manual-source-guide")
def manual_source_guide(operator_email: str = Depends(get_current_operator)):
    return {
        "guide": [
            {"source_type": "Word(.docx) / PDF / Excel(.xlsx)", "처리방식": "파일을 직접 업로드하면 자동 파싱됩니다"},
            {"source_type": "HWP / HWPX", "처리방식": "직접 업로드 불가 — 사전에 PDF나 텍스트 파일로 변환한 뒤 업로드해야 합니다"},
            {"source_type": "구글 스프레드시트", "처리방식": "공개 공유 링크(누구나 보기) URL을 매번 새로 입력하면 가져옵니다. 비공개 시트는 지원되지 않습니다"},
        ]
    }
