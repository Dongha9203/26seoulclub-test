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


@app.get("/monitoring/score-distribution")
def score_distribution(operator_email: str = Depends(get_current_operator)):
    from storage.supabase_store import get_score_distribution
    return {"distribution": get_score_distribution()}


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
    full = {cause: counts.get(cause, 0) for cause in ["지식DB공백", "검색실패", "질문모호성", "정책밖요청"]}
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


class ThresholdUpdate(BaseModel):
    similarity_threshold: float


@app.put("/settings/similarity-threshold")
def update_similarity_threshold(req: ThresholdUpdate,
                                 operator_email: str = Depends(get_current_operator)):
    if not (0.0 <= req.similarity_threshold <= 1.0):
        raise HTTPException(status_code=400, detail="similarity_threshold는 0.0~1.0 사이여야 합니다.")
    from storage.settings_store import update_settings
    return update_settings({"similarity_threshold": req.similarity_threshold})


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

    summary_parts = []
    for info in result.get("pages", {}).values():
        name = info.get("page_name", "알수없음")
        if info.get("skipped"):
            summary_parts.append(f"{name} 건너뜀({info.get('reason', '')})")
        else:
            summary_parts.append(f"{name} {info.get('doc_count', 0)}건 변경")
    result["summary_text"] = ", ".join(summary_parts) if summary_parts else "변경 없음"
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
            {"source_type": "Word(.docx)", "처리방식": "파일을 직접 업로드하면 자동 파싱됩니다"},
            {"source_type": "PDF", "처리방식": "파일을 직접 업로드하면 자동 파싱됩니다"},
            {"source_type": "Excel(.xlsx)", "처리방식": "파일을 직접 업로드하면 자동 파싱됩니다"},
            {"source_type": "HWP / HWPX", "처리방식": "직접 업로드 불가 — 사전에 PDF나 텍스트 파일로 변환한 뒤 업로드해야 합니다"},
            {"source_type": "구글 스프레드시트", "처리방식": "공개 공유 링크(누구나 보기) URL을 매번 새로 입력하면 가져옵니다. 비공개 시트는 지원되지 않습니다"},
        ]
    }
