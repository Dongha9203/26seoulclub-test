"""
구글 캘린더 수집 모듈.

공개(public) 캘린더의 ICS 피드(/public/basic.ics)에서 일정을 가져옵니다.
구글 시트 수집기와 동일하게 OAuth/서비스 계정 인증이 필요 없습니다 — 단, 캘린더가
"공개" 설정이어야 합니다(노션에 embed된 캘린더는 임베드가 보이려면 이미 공개 설정인
경우가 대부분입니다).

반복 일정(RRULE)은 recurring_ical_events로 실제 발생일을 펼쳐서 각각 별도
Document로 만듭니다 — 그래야 "이번 달 며칠에 무슨 일정 있어?" 같은 질문에
구체적인 날짜로 답할 수 있습니다.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from urllib.parse import parse_qs, quote, urlparse

import icalendar
import recurring_ical_events
import requests

from models.document import Document
from utils.category_tagger import tag_category

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 15
_WINDOW_DAYS_PAST = 7
_WINDOW_DAYS_FUTURE = 180


def _extract_calendar_id(url_or_id: str) -> Optional[str]:
    """임베드 URL(?src=...)이면 src 파라미터를, 아니면 입력 자체를 캘린더 ID로 봅니다."""
    if "calendar.google.com" not in url_or_id:
        stripped = url_or_id.strip()
        return stripped or None
    qs = parse_qs(urlparse(url_or_id).query)
    return qs.get("src", [None])[0]


def _build_ics_url(calendar_id: str) -> str:
    return f"https://calendar.google.com/calendar/ical/{quote(calendar_id, safe='')}/public/basic.ics"


def _format_korean_date(d) -> str:
    """년/월/일에 0패딩을 넣지 않습니다 — "08월"은 형태소 분석기가 "8월"과 다른
    토큰으로 쪼개서, "8월 22일"로 묻는 사용자 질문과 BM25 매칭이 안 되기 때문입니다."""
    return f"{d.year}년 {d.month}월 {d.day}일"


def _format_date_range(dtstart, dtend) -> str:
    """일정 시작/종료를 사람이 읽기 좋은 문자열로 합칩니다 (종일 일정 vs 시간 지정 일정 구분).

    ISO 형식(YYYY-MM-DD)이 아니라 한국어 자연어 형식(YYYY년 M월 D일)을 씁니다.
    """
    if isinstance(dtstart, datetime):
        start_str = f"{_format_korean_date(dtstart)} {dtstart.strftime('%H:%M')}"
        if dtend is None:
            return start_str
        if dtstart.date() == dtend.date():
            return f"{start_str}~{dtend.strftime('%H:%M')}"
        return f"{start_str} ~ {_format_korean_date(dtend)} {dtend.strftime('%H:%M')}"

    # 종일 일정: DTEND는 종료 다음날(배타적 경계)이라 실제 마지막 날은 하루 전.
    if dtend is None or dtend <= dtstart:
        return _format_korean_date(dtstart)
    last_day = dtend - timedelta(days=1)
    if last_day == dtstart:
        return _format_korean_date(dtstart)
    return f"{_format_korean_date(dtstart)}~{_format_korean_date(last_day)}"


def _stable_event_doc_id(source_origin: str, event) -> str:
    """이벤트(UID + 발생 시작 시각) 기준으로 고정된 doc_id를 만듭니다.

    매번 새 UUID를 쓰면(기존 Document.new() 기본값) 내용이 안 바뀐 일정도 매
    동기화마다 다른 행이 되어 임베딩을 잃습니다. 같은 캘린더의 같은 일정(반복
    일정의 같은 회차 포함, RECURRENCE-ID로 구분됨)은 항상 같은 doc_id가 되어야
    incremental 동기화(내용 비교로 변경분만 갱신)가 가능합니다.
    """
    uid = str(event.get("UID", ""))
    dtstart = event["DTSTART"].dt
    key = f"{source_origin}|{uid}|{dtstart.isoformat()}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def _make_event_document(event, source_origin: str) -> Document:
    summary = str(event.get("SUMMARY", "") or "제목 없음")
    dtstart = event["DTSTART"].dt
    dtend = event["DTEND"].dt if event.get("DTEND") else None
    location = str(event.get("LOCATION", "") or "").strip()
    description = str(event.get("DESCRIPTION", "") or "").strip()

    date_str = _format_date_range(dtstart, dtend)
    title = f"{summary} ({date_str})"

    content_lines = [f"일시: {date_str}"]
    if location:
        content_lines.append(f"장소: {location}")
    if description:
        content_lines.append(f"설명: {description}")

    doc = Document.new(
        source_type="google_calendar",
        source_origin=source_origin,
        title=title,
        content="\n".join(content_lines),
        is_editable=False,
    )
    doc.doc_id = _stable_event_doc_id(source_origin, event)
    doc.category = tag_category(doc.title, doc.content)
    return doc


def calendar_source_origin(url_or_id: str) -> str:
    """캘린더 URL/ID로부터 source_origin을 계산합니다.

    동기화 쪽(_sync_calendars)이 일정이 0건으로 수집되는 경우(전부 취소/만료)에도
    "이 캘린더에 더 이상 남은 일정이 없다"는 걸 알고 기존 행을 정리할 수 있도록,
    수집 결과와 무관하게 미리 알 수 있어야 합니다.
    """
    calendar_id = _extract_calendar_id(url_or_id)
    if not calendar_id:
        raise ValueError(
            f"유효한 구글 캘린더 URL/ID가 아닙니다: {url_or_id}\n"
            "형식 예시: https://calendar.google.com/calendar/embed?src={{CALENDAR_ID}}"
        )
    return f"google_calendar:{calendar_id}"


def collect_google_calendar(url_or_id: str, source_origin: Optional[str] = None) -> List[Document]:
    """
    공개 구글 캘린더의 임베드 URL(또는 캘린더 ID)을 받아, 오늘 기준
    -{_WINDOW_DAYS_PAST}일 ~ +{_WINDOW_DAYS_FUTURE}일 범위의 일정을 Document
    리스트로 반환합니다. 반복 일정은 실제 발생일별로 펼쳐집니다.

    Raises:
        ValueError: URL/ID가 비어있거나, 캘린더가 비공개거나, ICS 형식이 깨진 경우
    """
    if not url_or_id or not url_or_id.strip():
        raise ValueError("구글 캘린더 URL/ID가 비어있습니다.")

    calendar_id = _extract_calendar_id(url_or_id)
    if not calendar_id:
        raise ValueError(
            f"유효한 구글 캘린더 URL/ID가 아닙니다: {url_or_id}\n"
            "형식 예시: https://calendar.google.com/calendar/embed?src={{CALENDAR_ID}}"
        )

    ics_url = _build_ics_url(calendar_id)
    origin = source_origin or f"google_calendar:{calendar_id}"

    logger.info("구글 캘린더 수집 시작: %s", ics_url)

    try:
        resp = requests.get(ics_url, timeout=_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        raise ValueError(f"구글 캘린더 다운로드 실패: {e}") from e

    if resp.status_code == 404:
        raise ValueError(
            "구글 캘린더를 찾을 수 없습니다 (HTTP 404).\n"
            "캘린더 설정 → 액세스 권한에서 '공개 사용 설정'이 켜져 있는지 확인하세요.\n"
            f"URL: {url_or_id}"
        )
    if resp.status_code != 200:
        raise ValueError(f"구글 캘린더 다운로드 실패 (HTTP {resp.status_code}).\nURL: {url_or_id}")

    try:
        calendar = icalendar.Calendar.from_ical(resp.text)
    except ValueError as e:
        raise ValueError(f"ICS 형식을 해석할 수 없습니다: {e}") from e

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=_WINDOW_DAYS_PAST)
    window_end = now + timedelta(days=_WINDOW_DAYS_FUTURE)
    events = recurring_ical_events.of(calendar).between(window_start, window_end)

    documents = [_make_event_document(event, origin) for event in events]
    logger.info("구글 캘린더 수집 완료: %d개 일정 Document 생성", len(documents))
    return documents
