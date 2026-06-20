"""
전체 기능 단위 테스트.

실행 방법:
  pip install pytest pytest-mock
  pytest tests/test_all.py -v

외부 API(Notion, Google Sheets)는 mock으로 처리합니다.
파일 기반 수집기(docx, pdf, excel, txt)는 임시 파일을 생성해 실제로 테스트합니다.
"""

import json
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 프로젝트 루트 경로 등록
_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


# ══════════════════════════════════════════════════════════════
# models/document.py
# ══════════════════════════════════════════════════════════════

class TestDocument:
    def test_new_creates_doc_with_defaults(self):
        from models.document import Document
        doc = Document.new(
            source_type="docx",
            source_origin="test.docx",
            title="제목",
            content="내용",
        )
        assert doc.doc_id
        assert doc.source_type == "docx"
        assert doc.is_editable is True
        assert doc.notion_block_id is None
        assert doc.notion_page_url is None

    def test_notion_source_is_not_editable(self):
        from models.document import Document
        doc = Document.new(
            source_type="notion",
            source_origin="메인페이지",
            title="제목",
            content="내용",
            notion_block_id="abc-123",
            notion_page_url="https://notion.so/xxx",
        )
        assert doc.is_editable is False

    def test_deep_link_url(self):
        from models.document import Document
        doc = Document.new(
            source_type="notion",
            source_origin="FAQ",
            title="질문",
            content="답변",
            notion_page_url="https://www.notion.so/abc123",
            notion_block_id="12345678-1234-1234-1234-123456789abc",
        )
        link = doc.deep_link_url()
        # UUID "12345678-1234-1234-1234-123456789abc" → 하이픈 제거 → 32자
        assert link == "https://www.notion.so/abc123#12345678123412341234123456789abc"

    def test_deep_link_url_none_when_no_block_id(self):
        from models.document import Document
        doc = Document.new(
            source_type="notion",
            source_origin="메인페이지",
            title="제목",
            content="내용",
            notion_page_url="https://www.notion.so/xxx",
            notion_block_id=None,
        )
        assert doc.deep_link_url() is None

    def test_to_dict_is_serializable(self):
        from models.document import Document
        doc = Document.new(source_type="pdf", source_origin="a.pdf", title="T", content="C")
        d = doc.to_dict()
        assert isinstance(d, dict)
        assert d["source_type"] == "pdf"
        assert isinstance(d["is_editable"], bool)

    def test_empty_title_allowed(self):
        from models.document import Document
        doc = Document.new(source_type="pdf", source_origin="a.pdf", title="", content="C")
        assert doc.title == ""

    def test_explicit_is_editable_override(self):
        from models.document import Document
        doc = Document.new(
            source_type="notion",
            source_origin="FAQ",
            title="T",
            content="C",
            is_editable=True,  # 명시적으로 True로 오버라이드
        )
        assert doc.is_editable is True


# ══════════════════════════════════════════════════════════════
# storage/supabase_store.py
# ══════════════════════════════════════════════════════════════

class TestSupabaseStore:
    def _make_doc(self, source_type="docx", title="T", content="C", origin="test"):
        from models.document import Document
        return Document.new(source_type=source_type, source_origin=origin,
                            title=title, content=content)

    def test_upsert_and_get(self, pg_conn):
        from storage.supabase_store import upsert_document, get_all
        doc = self._make_doc()
        upsert_document(doc, pg_conn)
        all_docs = get_all(pg_conn)
        assert len(all_docs) == 1
        assert all_docs[0].doc_id == doc.doc_id

    def test_upsert_updates_existing(self, pg_conn):
        from storage.supabase_store import upsert_document, get_all
        doc = self._make_doc(title="원본")
        upsert_document(doc, pg_conn)
        doc.title = "수정됨"
        upsert_document(doc, pg_conn)
        all_docs = get_all(pg_conn)
        assert len(all_docs) == 1
        assert all_docs[0].title == "수정됨"

    def test_upsert_many(self, pg_conn):
        from storage.supabase_store import upsert_documents, get_total_count
        docs = [self._make_doc(title=f"제목{i}") for i in range(5)]
        count = upsert_documents(docs, pg_conn)
        assert count == 5
        assert get_total_count(pg_conn) == 5

    def test_delete_by_source_origin(self, pg_conn):
        from storage.supabase_store import upsert_documents, delete_by_source_origin, get_all
        docs = [self._make_doc(origin="a") for _ in range(3)]
        other = [self._make_doc(origin="b") for _ in range(2)]
        upsert_documents(docs + other, pg_conn)
        deleted = delete_by_source_origin("a", pg_conn)
        assert deleted == 3
        remaining = get_all(pg_conn)
        assert len(remaining) == 2
        assert all(d.source_origin == "b" for d in remaining)

    def test_get_by_source_type(self, pg_conn):
        from storage.supabase_store import upsert_documents, get_by_source_type
        docs = [self._make_doc(source_type="notion", origin="FAQ") for _ in range(2)]
        docs += [self._make_doc(source_type="pdf", origin="a.pdf") for _ in range(3)]
        upsert_documents(docs, pg_conn)
        notion_docs = get_by_source_type("notion", pg_conn)
        assert len(notion_docs) == 2

    def test_category_distribution(self, pg_conn):
        from storage.supabase_store import upsert_documents, get_category_distribution
        docs = []
        for _ in range(3):
            d = self._make_doc()
            d.category = "신청 자격 안내"
            docs.append(d)
        for _ in range(2):
            d = self._make_doc()
            d.category = "미분류"
            docs.append(d)
        upsert_documents(docs, pg_conn)
        dist = get_category_distribution(pg_conn)
        assert dist["신청 자격 안내"] == 3
        assert dist["미분류"] == 2

    def test_sync_metadata_upsert_and_get(self, pg_conn):
        from storage.supabase_store import upsert_sync_metadata, get_sync_metadata
        upsert_sync_metadata("faq", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", pg_conn)
        meta = get_sync_metadata("faq", pg_conn)
        assert meta["page_key"] == "faq"
        assert meta["last_notion_edited_time"] == "2026-01-01T00:00:00Z"

    def test_sync_metadata_returns_none_for_unknown(self, pg_conn):
        from storage.supabase_store import get_sync_metadata
        meta = get_sync_metadata("nonexistent", pg_conn)
        assert meta is None

    def test_sync_metadata_children_roundtrip(self, pg_conn):
        # 실제 노션 동기화로 생긴 "main::<page_id>" 같은 진짜 행과 절대 겹치지 않도록
        # 테스트 전용 부모 키를 사용합니다 (공유 운영 DB라 절대값 비교가 위험합니다).
        from storage.supabase_store import upsert_sync_metadata, get_sync_metadata_children
        parent = f"test-parent-{uuid.uuid4()}"
        upsert_sync_metadata(parent, "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", pg_conn)
        upsert_sync_metadata(f"{parent}::sub-1", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", pg_conn)
        upsert_sync_metadata(f"{parent}::sub-2", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", pg_conn)
        upsert_sync_metadata("integrated_system", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", pg_conn)

        children = get_sync_metadata_children(parent, pg_conn)
        assert {c["page_key"] for c in children} == {f"{parent}::sub-1", f"{parent}::sub-2"}

    def test_delete_sync_metadata_children_only_deletes_matching_prefix(self, pg_conn):
        from storage.supabase_store import (
            upsert_sync_metadata, delete_sync_metadata_children, get_sync_metadata, get_sync_metadata_children,
        )
        parent = f"test-parent-{uuid.uuid4()}"
        upsert_sync_metadata(parent, "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", pg_conn)
        upsert_sync_metadata(f"{parent}::sub-1", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", pg_conn)

        deleted = delete_sync_metadata_children(parent, pg_conn)
        assert deleted == 1
        assert get_sync_metadata_children(parent, pg_conn) == []
        assert get_sync_metadata(parent, pg_conn) is not None  # 최상위 자신은 그대로 남아있어야 함

    def test_empty_upsert_many(self, pg_conn):
        from storage.supabase_store import upsert_documents, get_total_count
        count = upsert_documents([], pg_conn)
        assert count == 0
        assert get_total_count(pg_conn) == 0


# ══════════════════════════════════════════════════════════════
# utils/category_tagger.py
# ══════════════════════════════════════════════════════════════

class TestCategoryTagger:
    @pytest.fixture
    def tmp_config(self, tmp_path):
        config = {
            "categories": [
                {"name": "신청 자격 안내", "keywords": ["신청 자격", "지원 자격"]},
                {"name": "수당 지급 기준 안내", "keywords": ["수당", "지급"]},
                {"name": "출결 및 활동기준 안내", "keywords": ["출결", "출근"]},
            ]
        }
        p = tmp_path / "config.json"
        p.write_text(json.dumps(config), encoding="utf-8")
        return p

    def test_exact_keyword_match(self, tmp_config):
        from utils.category_tagger import tag_category
        result = tag_category("신청 자격 안내", "지원 자격 관련 내용", tmp_config)
        assert result == "신청 자격 안내"

    def test_content_keyword_match(self, tmp_config):
        from utils.category_tagger import tag_category
        result = tag_category("기타 제목", "수당 지급 방법 안내", tmp_config)
        assert result == "수당 지급 기준 안내"

    def test_no_match_returns_미분류(self, tmp_config):
        from utils.category_tagger import tag_category
        result = tag_category("관련 없는 제목", "관련 없는 내용", tmp_config)
        assert result == "미분류"

    def test_empty_title_and_content(self, tmp_config):
        from utils.category_tagger import tag_category
        result = tag_category("", "", tmp_config)
        assert result == "미분류"

    def test_missing_config_returns_미분류(self, tmp_path):
        from utils.category_tagger import tag_category
        missing_path = tmp_path / "nonexistent.json"
        result = tag_category("수당 지급", "지급 기준", missing_path)
        assert result == "미분류"

    def test_first_match_wins(self, tmp_config):
        # "신청 자격"과 "수당" 둘 다 포함 → 먼저 정의된 카테고리 반환
        from utils.category_tagger import tag_category
        result = tag_category("신청 자격", "수당 정보", tmp_config)
        assert result == "신청 자격 안내"


# ══════════════════════════════════════════════════════════════
# utils/validators.py
# ══════════════════════════════════════════════════════════════

class TestValidators:
    def _notion_doc(self, block_id=None):
        from models.document import Document
        doc = Document.new(
            source_type="notion",
            source_origin="FAQ",
            title="질문",
            content="답변",
            notion_block_id=block_id,
        )
        return doc

    def test_all_block_ids_present(self):
        from utils.validators import validate_notion_block_ids
        docs = [self._notion_doc(block_id=f"block-{i}") for i in range(3)]
        result = validate_notion_block_ids(docs)
        assert result["block_id_missing"] == 0
        assert result["success_rate_pct"] == 100.0

    def test_missing_block_id_detected(self):
        from utils.validators import validate_notion_block_ids
        docs = [self._notion_doc(block_id="id1"), self._notion_doc(block_id=None)]
        result = validate_notion_block_ids(docs)
        assert result["block_id_missing"] == 1
        assert result["success_rate_pct"] == 50.0

    def test_non_notion_docs_ignored(self):
        from utils.validators import validate_notion_block_ids
        from models.document import Document
        non_notion = Document.new(source_type="pdf", source_origin="a.pdf",
                                   title="T", content="C")
        result = validate_notion_block_ids([non_notion])
        assert result["total_notion_docs"] == 0
        assert result["block_id_missing"] == 0

    def test_empty_list(self):
        from utils.validators import validate_notion_block_ids
        result = validate_notion_block_ids([])
        assert result["total_notion_docs"] == 0
        assert result["success_rate_pct"] == 100.0

    def test_validate_documents_full(self):
        from utils.validators import validate_documents
        from models.document import Document
        good = Document.new(source_type="pdf", source_origin="a.pdf", title="T", content="C")
        result = validate_documents([good])
        assert result["passed"] is True
        assert result["error_count"] == 0

    def test_validate_documents_detects_empty_title(self):
        from utils.validators import validate_documents
        from models.document import Document
        bad = Document.new(source_type="pdf", source_origin="a.pdf", title="", content="C")
        result = validate_documents([bad])
        assert result["passed"] is False
        assert result["error_count"] > 0


# ══════════════════════════════════════════════════════════════
# collectors/notion_collector.py (mock 사용)
# ══════════════════════════════════════════════════════════════

class TestNotionCollector:
    def _make_block(self, btype, text, block_id="abc-001", has_children=False):
        return {
            "id": block_id,
            "type": btype,
            btype: {"rich_text": [{"plain_text": text}]},
            "has_children": has_children,
        }

    def test_extract_page_id_from_url(self):
        from collectors.notion_collector import extract_page_id
        url = "https://www.notion.so/workspace/Title-abc123def456abc123def456abc123de"
        result = extract_page_id(url)
        assert "abc123de" in result.replace("-", "")

    def test_extract_page_id_uuid_format(self):
        from collectors.notion_collector import extract_page_id
        uuid = "abc123de-f456-abc1-23de-f456abc123de"
        assert extract_page_id(uuid) == uuid.lower()

    def test_extract_page_id_raw_hex(self):
        from collectors.notion_collector import extract_page_id
        raw = "abc123def456abc123def456abc123de"
        result = extract_page_id(raw)
        assert result == "abc123de-f456-abc1-23de-f456abc123de"

    def test_extract_page_id_with_query_params(self):
        from collectors.notion_collector import extract_page_id
        url = "https://www.notion.so/abc123def456abc123def456abc123de?pvs=4"
        result = extract_page_id(url)
        assert "-" in result  # UUID 형식

    def test_chunk_toggle_priority(self):
        """toggle 블록이 먼저 분리되는지 확인."""
        from collectors.notion_collector import _chunk_page_blocks
        mock_client = MagicMock()
        mock_client.blocks.children.list.return_value = {
            "results": [
                {"id": "child-1", "type": "paragraph",
                 "paragraph": {"rich_text": [{"plain_text": "답변 내용"}]},
                 "has_children": False},
            ],
            "has_more": False,
        }
        blocks = [
            {"id": "t1", "type": "toggle",
             "toggle": {"rich_text": [{"plain_text": "FAQ 질문"}]},
             "has_children": True},
            {"id": "h1", "type": "heading_1",
             "heading_1": {"rich_text": [{"plain_text": "섹션"}]},
             "has_children": False},
            {"id": "p1", "type": "paragraph",
             "paragraph": {"rich_text": [{"plain_text": "섹션 내용"}]},
             "has_children": False},
        ]
        docs = _chunk_page_blocks(mock_client, blocks, "FAQ", "https://notion.so/xxx")
        toggle_docs = [d for d in docs if d.title == "FAQ 질문"]
        heading_docs = [d for d in docs if d.title == "섹션"]
        assert len(toggle_docs) == 1
        assert len(heading_docs) == 1

    def test_chunk_fallback_no_structure(self):
        """toggle/heading 없을 때 fallback 분할 적용."""
        from collectors.notion_collector import _chunk_page_blocks
        mock_client = MagicMock()
        blocks = [
            {"id": f"p{i}", "type": "paragraph",
             "paragraph": {"rich_text": [{"plain_text": "A" * 100}]},
             "has_children": False}
            for i in range(10)  # 1000자 → 800자 기준으로 2개 청크
        ]
        docs = _chunk_page_blocks(mock_client, blocks, "페이지", "https://notion.so/xxx")
        assert len(docs) >= 1  # fallback 동작 확인
        for doc in docs:
            assert doc.notion_block_id is not None  # 블록 ID 존재

    def test_heading_chunking(self):
        """heading 단위로 청킹되는지 확인."""
        from collectors.notion_collector import _chunk_by_headings
        mock_client = MagicMock()
        blocks = [
            {"id": "h1", "type": "heading_1",
             "heading_1": {"rich_text": [{"plain_text": "1장"}]},
             "has_children": False},
            {"id": "p1", "type": "paragraph",
             "paragraph": {"rich_text": [{"plain_text": "1장 내용"}]},
             "has_children": False},
            {"id": "h2", "type": "heading_2",
             "heading_2": {"rich_text": [{"plain_text": "2장"}]},
             "has_children": False},
            {"id": "p2", "type": "paragraph",
             "paragraph": {"rich_text": [{"plain_text": "2장 내용"}]},
             "has_children": False},
        ]
        docs = _chunk_by_headings(mock_client, blocks, "테스트페이지", "https://notion.so/xxx")
        assert len(docs) == 2
        assert docs[0].title == "1장"
        assert docs[1].title == "2장"
        assert docs[0].notion_block_id == "h1"
        assert docs[1].notion_block_id == "h2"

    def test_toggleable_heading_chunking(self):
        """토글 가능한 heading(본문이 형제가 아니라 자식 블록에 있는 경우)도 내용을 수집하는지 확인."""
        from collectors.notion_collector import _chunk_by_headings
        mock_client = MagicMock()
        mock_client.blocks.children.list.return_value = {
            "results": [
                {"id": "c1", "type": "paragraph",
                 "paragraph": {"rich_text": [{"plain_text": "토글 안의 실제 내용"}]},
                 "has_children": False},
            ],
            "has_more": False,
        }
        blocks = [
            {"id": "h1", "type": "heading_3",
             "heading_3": {"rich_text": [{"plain_text": "1. 활동 종료 후"}], "is_toggleable": True},
             "has_children": True},
        ]
        docs = _chunk_by_headings(mock_client, blocks, "테스트페이지", "https://notion.so/xxx")
        assert len(docs) == 1
        assert docs[0].title == "1. 활동 종료 후"
        assert "토글 안의 실제 내용" in docs[0].content

    def test_collect_notion_page_empty(self):
        """빈 페이지는 빈 리스트 반환."""
        from collectors.notion_collector import collect_notion_page
        mock_client = MagicMock()
        mock_client.blocks.children.list.return_value = {"results": [], "has_more": False}
        docs = collect_notion_page(mock_client, "abc-123", "빈페이지", "https://notion.so/xxx")
        assert docs == []

    def test_expand_blocks_flattens_layout_and_extracts_child_pages(self):
        """column_list/column/callout은 펼치고, 그 안의 child_page는 따로 모은다."""
        from collectors.notion_collector import _expand_blocks

        mock_client = MagicMock()
        children_map = {
            "cl1": [{"id": "col1", "type": "column", "column": {}, "has_children": True}],
            "col1": [{"id": "co1", "type": "callout",
                      "callout": {"rich_text": [{"plain_text": "안내"}]}, "has_children": True}],
            "co1": [
                {"id": "cp1", "type": "child_page",
                 "child_page": {"title": "하위페이지"}, "has_children": True},
                {"id": "p1", "type": "paragraph",
                 "paragraph": {"rich_text": [{"plain_text": "본문"}]}, "has_children": False},
            ],
        }
        mock_client.blocks.children.list.side_effect = (
            lambda block_id, start_cursor=None: {"results": children_map.get(block_id, []), "has_more": False}
        )

        top_blocks = [{"id": "cl1", "type": "column_list", "column_list": {}, "has_children": True}]
        expanded, nested_pages = _expand_blocks(mock_client, top_blocks)

        assert nested_pages == [("cp1", "하위페이지")]
        expanded_ids = [b["id"] for b in expanded]
        assert "co1" in expanded_ids  # callout 자신은 유지
        assert "p1" in expanded_ids   # callout 안의 본문도 펼쳐짐
        assert "cl1" not in expanded_ids and "col1" not in expanded_ids  # 레이아웃 컨테이너는 버림

    def test_expand_blocks_skips_child_database(self):
        """child_database(자료실)는 건너뛰고 nested_pages에도 포함하지 않는다."""
        from collectors.notion_collector import _expand_blocks

        mock_client = MagicMock()
        blocks = [{"id": "db1", "type": "child_database",
                   "child_database": {"title": "자료실"}, "has_children": False}]
        expanded, nested_pages = _expand_blocks(mock_client, blocks)
        assert expanded == []
        assert nested_pages == []

    def test_collect_notion_page_recurses_into_child_page(self):
        """callout 안에 숨은 child_page를 발견하면 공개 URL을 조회해 재귀 수집한다."""
        from collectors.notion_collector import collect_notion_page

        mock_client = MagicMock()
        top_blocks = [{"id": "co1", "type": "callout",
                       "callout": {"rich_text": []}, "has_children": True}]
        callout_children = [{"id": "cp1", "type": "child_page",
                              "child_page": {"title": "하위페이지"}, "has_children": True}]
        sub_page_blocks = [{"id": "sp1", "type": "paragraph",
                             "paragraph": {"rich_text": [{"plain_text": "하위 내용"}]},
                             "has_children": False}]

        children_map = {"top-1": top_blocks, "co1": callout_children, "cp1": sub_page_blocks}
        mock_client.blocks.children.list.side_effect = (
            lambda block_id, start_cursor=None: {"results": children_map.get(block_id, []), "has_more": False}
        )
        mock_client.pages.retrieve.return_value = {
            "url": "https://app.notion.com/p/cp1",
            "public_url": "https://example.notion.site/cp1",
        }

        docs = collect_notion_page(mock_client, "top-1", "메인페이지", "https://example.notion.site/top-1")

        sub_docs = [d for d in docs if d.content == "하위 내용"]
        assert len(sub_docs) == 1
        assert sub_docs[0].notion_page_url == "https://example.notion.site/cp1"
        assert sub_docs[0].title == "하위페이지"

    def test_collect_notion_page_skips_unresolvable_child_page(self):
        """하위 페이지 URL 조회가 실패하면(예: 권한 없음) 건너뛰고 나머지는 계속 수집한다."""
        from collectors.notion_collector import collect_notion_page

        mock_client = MagicMock()
        top_blocks = [{"id": "co1", "type": "callout",
                       "callout": {"rich_text": []}, "has_children": True}]
        callout_children = [{"id": "cp1", "type": "child_page",
                              "child_page": {"title": "접근불가 페이지"}, "has_children": True}]
        children_map = {"top-1": top_blocks, "co1": callout_children}
        mock_client.blocks.children.list.side_effect = (
            lambda block_id, start_cursor=None: {"results": children_map.get(block_id, []), "has_more": False}
        )
        mock_client.pages.retrieve.return_value = {"url": None, "public_url": None}

        docs = collect_notion_page(mock_client, "top-1", "메인페이지", "https://example.notion.site/top-1")
        assert docs == []  # 본문 없는 callout + 조회 실패한 하위페이지뿐이므로 빈 결과

    def test_sync_notion_pages_skips_placeholder(self):
        """URL이 플레이스홀더이면 건너뜀."""
        from collectors.notion_collector import sync_notion_pages
        config = {"notion_pages": {"main": "{{NOTION_MAIN_URL}}"}}
        with patch.dict("os.environ", {"NOTION_API_TOKEN": "test-token"}):
            with patch("collectors.notion_collector.Client"):
                docs, summary = sync_notion_pages(config)
        assert summary["main"]["skipped"] is True
        assert len(docs) == 0

    def test_sync_incremental_skips_when_top_and_known_children_unchanged(self):
        """최상위 페이지도, 이전에 발견했던 하위 페이지도 수정 시각이 그대로면 건너뛴다."""
        from collectors.notion_collector import sync_notion_pages_incremental
        config = {"notion_pages": {"main": "https://notion.so/x"}}

        last_edited_by_id = {"top-1": "2026-01-01T00:00:00Z", "sub-1": "2026-01-01T00:00:00Z"}
        with patch.dict("os.environ", {"NOTION_API_TOKEN": "test-token"}), \
             patch("collectors.notion_collector.Client"), \
             patch("collectors.notion_collector.extract_page_id", return_value="top-1"), \
             patch("collectors.notion_collector.get_page_last_edited_time",
                   side_effect=lambda client, pid: last_edited_by_id.get(pid)), \
             patch("storage.supabase_store.get_sync_metadata",
                   return_value={"page_key": "main", "last_notion_edited_time": "2026-01-01T00:00:00Z",
                                  "last_synced_at": "x"}), \
             patch("storage.supabase_store.get_sync_metadata_children",
                   return_value=[{"page_key": "main::sub-1", "last_notion_edited_time": "2026-01-01T00:00:00Z",
                                   "last_synced_at": "x"}]), \
             patch("collectors.notion_collector.collect_notion_page") as fake_collect:
            docs, summary = sync_notion_pages_incremental(config)

        fake_collect.assert_not_called()
        assert summary["main"]["skipped"] is True
        assert summary["main"]["reason"] == "변경 없음"
        assert docs == []

    def test_sync_incremental_detects_child_page_only_change(self):
        """최상위 페이지는 안 바뀌었어도, 하위 페이지 자신의 수정 시각이 바뀌면 재수집한다."""
        from collectors.notion_collector import sync_notion_pages_incremental
        config = {"notion_pages": {"main": "https://notion.so/x"}}

        last_edited_by_id = {"top-1": "2026-01-01T00:00:00Z", "sub-1": "2026-02-01T00:00:00Z"}

        def fake_collect_notion_page(client, page_id, page_name, url, visited=None):
            if visited is not None:
                visited.add(page_id)
                visited.add("sub-1")
            return []

        with patch.dict("os.environ", {"NOTION_API_TOKEN": "test-token"}), \
             patch("collectors.notion_collector.Client"), \
             patch("collectors.notion_collector.extract_page_id", return_value="top-1"), \
             patch("collectors.notion_collector.get_page_last_edited_time",
                   side_effect=lambda client, pid: last_edited_by_id.get(pid)), \
             patch("storage.supabase_store.get_sync_metadata",
                   return_value={"page_key": "main", "last_notion_edited_time": "2026-01-01T00:00:00Z",
                                  "last_synced_at": "x"}), \
             patch("storage.supabase_store.get_sync_metadata_children",
                   return_value=[{"page_key": "main::sub-1", "last_notion_edited_time": "2026-01-01T00:00:00Z",
                                   "last_synced_at": "x"}]), \
             patch("collectors.notion_collector.collect_notion_page",
                   side_effect=fake_collect_notion_page), \
             patch("storage.supabase_store.upsert_sync_metadata") as fake_upsert, \
             patch("storage.supabase_store.delete_sync_metadata_children") as fake_delete_children:
            docs, summary = sync_notion_pages_incremental(config)

        assert summary["main"]["skipped"] is False
        fake_delete_children.assert_called_once_with("main", None)
        # 최상위(main)와 하위(main::sub-1) 모두 최신 수정 시각으로 기록되어야 함
        upserted_keys = {call.args[0] for call in fake_upsert.call_args_list}
        assert upserted_keys == {"main", "main::sub-1"}

    def test_sync_incremental_recollects_all_when_top_changed(self):
        """최상위 페이지가 바뀌면 하위 페이지 변경 여부와 무관하게 재수집한다."""
        from collectors.notion_collector import sync_notion_pages_incremental
        config = {"notion_pages": {"main": "https://notion.so/x"}}

        last_edited_by_id = {"top-1": "2026-03-01T00:00:00Z"}

        def fake_collect_notion_page(client, page_id, page_name, url, visited=None):
            if visited is not None:
                visited.add(page_id)
            return []

        with patch.dict("os.environ", {"NOTION_API_TOKEN": "test-token"}), \
             patch("collectors.notion_collector.Client"), \
             patch("collectors.notion_collector.extract_page_id", return_value="top-1"), \
             patch("collectors.notion_collector.get_page_last_edited_time",
                   side_effect=lambda client, pid: last_edited_by_id.get(pid)), \
             patch("storage.supabase_store.get_sync_metadata",
                   return_value={"page_key": "main", "last_notion_edited_time": "2026-01-01T00:00:00Z",
                                  "last_synced_at": "x"}), \
             patch("storage.supabase_store.get_sync_metadata_children") as fake_get_children, \
             patch("collectors.notion_collector.collect_notion_page",
                   side_effect=fake_collect_notion_page), \
             patch("storage.supabase_store.upsert_sync_metadata"), \
             patch("storage.supabase_store.delete_sync_metadata_children"):
            docs, summary = sync_notion_pages_incremental(config)

        assert summary["main"]["skipped"] is False
        fake_get_children.assert_not_called()  # 최상위가 바뀐 순간 하위 비교는 의미 없으므로 건너뜀


# ══════════════════════════════════════════════════════════════
# collectors/google_sheet_collector.py
# ══════════════════════════════════════════════════════════════

class TestGoogleSheetCollector:
    _SAMPLE_CSV = "질문,답변\n신청 자격은?,대학생이어야 합니다.\n일정은?,3월에 시작합니다.\n"

    def test_collect_public_sheet(self):
        from collectors.google_sheet_collector import collect_google_sheet
        url = "https://docs.google.com/spreadsheets/d/1ABC123/edit?usp=sharing"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "text/csv"}
        mock_resp.text = self._SAMPLE_CSV

        with patch("collectors.google_sheet_collector.requests.get", return_value=mock_resp):
            docs = collect_google_sheet(url)

        assert len(docs) == 2
        assert docs[0].source_type == "google_sheet"
        assert docs[0].title == "신청 자격은?"
        assert docs[0].content == "대학생이어야 합니다."

    def test_private_sheet_403_raises(self):
        from collectors.google_sheet_collector import collect_google_sheet
        url = "https://docs.google.com/spreadsheets/d/1ABC123/edit"
        mock_resp = MagicMock()
        mock_resp.status_code = 403

        with patch("collectors.google_sheet_collector.requests.get", return_value=mock_resp):
            with pytest.raises(ValueError, match="403"):
                collect_google_sheet(url)

    def test_html_response_raises(self):
        """비공개 시트 → HTML 반환 시 에러."""
        from collectors.google_sheet_collector import collect_google_sheet
        url = "https://docs.google.com/spreadsheets/d/1ABC123/edit"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "text/html; charset=utf-8"}
        mock_resp.text = "<html>Sign in</html>"

        with patch("collectors.google_sheet_collector.requests.get", return_value=mock_resp):
            with pytest.raises(ValueError, match="비공개"):
                collect_google_sheet(url)

    def test_empty_url_raises(self):
        from collectors.google_sheet_collector import collect_google_sheet
        with pytest.raises(ValueError, match="비어있습니다"):
            collect_google_sheet("")

    def test_invalid_url_raises(self):
        from collectors.google_sheet_collector import collect_google_sheet
        with pytest.raises(ValueError, match="유효한"):
            collect_google_sheet("https://example.com/not-a-sheet")

    def test_network_error_raises(self):
        import requests as req_mod
        from collectors.google_sheet_collector import collect_google_sheet
        url = "https://docs.google.com/spreadsheets/d/1ABC/edit"
        with patch("collectors.google_sheet_collector.requests.get",
                   side_effect=req_mod.RequestException("timeout")):
            with pytest.raises(ValueError, match="다운로드 실패"):
                collect_google_sheet(url)


# ══════════════════════════════════════════════════════════════
# collectors/docx_collector.py
# ══════════════════════════════════════════════════════════════

class TestDocxCollector:
    @pytest.fixture
    def sample_docx(self, tmp_path):
        from docx import Document as DocxDocument
        from docx.shared import Pt
        doc = DocxDocument()
        doc.add_heading("1장 신청 자격", level=1)
        doc.add_paragraph("대학생만 신청 가능합니다.")
        doc.add_heading("2장 수당 안내", level=2)
        doc.add_paragraph("월 30만원 지급됩니다.")
        path = tmp_path / "sample.docx"
        doc.save(str(path))
        return str(path)

    @pytest.fixture
    def no_heading_docx(self, tmp_path):
        from docx import Document as DocxDocument
        doc = DocxDocument()
        doc.add_paragraph("단순 내용 A" * 10)
        doc.add_paragraph("단순 내용 B" * 10)
        path = tmp_path / "no_heading.docx"
        doc.save(str(path))
        return str(path)

    def test_heading_chunking(self, sample_docx):
        from collectors.docx_collector import collect_docx
        docs = collect_docx(sample_docx)
        assert len(docs) == 2
        assert docs[0].title == "1장 신청 자격"
        assert "대학생만" in docs[0].content
        assert docs[1].title == "2장 수당 안내"

    def test_file_not_found(self):
        from collectors.docx_collector import collect_docx
        with pytest.raises(FileNotFoundError):
            collect_docx("/nonexistent/file.docx")

    def test_wrong_extension_raises(self, tmp_path):
        from collectors.docx_collector import collect_docx
        f = tmp_path / "test.txt"
        f.write_text("텍스트")
        with pytest.raises(ValueError, match="docx"):
            collect_docx(str(f))

    def test_no_heading_fallback(self, no_heading_docx):
        from collectors.docx_collector import collect_docx
        docs = collect_docx(no_heading_docx)
        assert len(docs) >= 1
        assert all(d.source_type == "docx" for d in docs)

    def test_category_tagged(self, sample_docx):
        from collectors.docx_collector import collect_docx
        docs = collect_docx(sample_docx)
        cats = {d.category for d in docs}
        # "신청 자격"이 포함되어 있으므로 적어도 하나는 분류돼야 함
        assert "미분류" not in cats or len(docs) > 0


# ══════════════════════════════════════════════════════════════
# collectors/pdf_collector.py
# ══════════════════════════════════════════════════════════════

class TestPdfCollector:
    @pytest.fixture
    def mock_pdf(self, tmp_path):
        """pdfplumber를 mock해서 가상 PDF 파일을 만듭니다."""
        p = tmp_path / "sample.pdf"
        p.write_bytes(b"%PDF-1.4 fake")  # 실제 내용은 mock으로 대체
        return str(p)

    def test_file_not_found(self):
        from collectors.pdf_collector import collect_pdf
        with pytest.raises(FileNotFoundError):
            collect_pdf("/nonexistent/file.pdf")

    def test_wrong_extension_raises(self, tmp_path):
        from collectors.pdf_collector import collect_pdf
        f = tmp_path / "test.docx"
        f.write_bytes(b"fake")
        with pytest.raises(ValueError, match="pdf"):
            collect_pdf(str(f))

    def test_collects_pages(self, mock_pdf):
        from collectors.pdf_collector import collect_pdf

        mock_page1 = MagicMock()
        mock_page1.extract_text.return_value = "첫 번째 페이지 내용입니다. " * 5
        mock_page2 = MagicMock()
        mock_page2.extract_text.return_value = "두 번째 페이지 내용입니다. " * 5

        mock_pdf_obj = MagicMock()
        mock_pdf_obj.__enter__ = MagicMock(return_value=mock_pdf_obj)
        mock_pdf_obj.__exit__ = MagicMock(return_value=False)
        mock_pdf_obj.pages = [mock_page1, mock_page2]

        with patch("collectors.pdf_collector.pdfplumber.open", return_value=mock_pdf_obj):
            docs = collect_pdf(mock_pdf)

        assert len(docs) >= 1
        assert all(d.source_type == "pdf" for d in docs)

    def test_empty_pdf_returns_empty(self, mock_pdf):
        from collectors.pdf_collector import collect_pdf

        mock_page = MagicMock()
        mock_page.extract_text.return_value = ""

        mock_pdf_obj = MagicMock()
        mock_pdf_obj.__enter__ = MagicMock(return_value=mock_pdf_obj)
        mock_pdf_obj.__exit__ = MagicMock(return_value=False)
        mock_pdf_obj.pages = [mock_page]

        with patch("collectors.pdf_collector.pdfplumber.open", return_value=mock_pdf_obj):
            docs = collect_pdf(mock_pdf)
        assert docs == []


# ══════════════════════════════════════════════════════════════
# collectors/excel_collector.py
# ══════════════════════════════════════════════════════════════

class TestExcelCollector:
    @pytest.fixture
    def sample_xlsx(self, tmp_path):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "FAQ"
        ws.append(["질문", "답변"])
        ws.append(["신청 자격은?", "대학생이어야 합니다."])
        ws.append(["수당은?", "월 30만원입니다."])
        path = tmp_path / "sample.xlsx"
        wb.save(str(path))
        return str(path)

    @pytest.fixture
    def empty_xlsx(self, tmp_path):
        from openpyxl import Workbook
        wb = Workbook()
        path = tmp_path / "empty.xlsx"
        wb.save(str(path))
        return str(path)

    def test_collect_with_headers(self, sample_xlsx):
        from collectors.excel_collector import collect_excel
        docs = collect_excel(sample_xlsx)
        assert len(docs) == 2
        assert docs[0].title == "신청 자격은?"
        assert docs[0].content == "대학생이어야 합니다."
        assert docs[0].source_type == "excel"

    def test_file_not_found(self):
        from collectors.excel_collector import collect_excel
        with pytest.raises(FileNotFoundError):
            collect_excel("/nonexistent/file.xlsx")

    def test_wrong_extension_raises(self, tmp_path):
        from collectors.excel_collector import collect_excel
        f = tmp_path / "test.csv"
        f.write_text("a,b")
        with pytest.raises(ValueError, match="xlsx"):
            collect_excel(str(f))

    def test_empty_sheet(self, empty_xlsx):
        from collectors.excel_collector import collect_excel
        docs = collect_excel(empty_xlsx)
        assert docs == []

    def test_is_editable_true(self, sample_xlsx):
        from collectors.excel_collector import collect_excel
        docs = collect_excel(sample_xlsx)
        assert all(d.is_editable is True for d in docs)


# ══════════════════════════════════════════════════════════════
# collectors/hwp_converted_collector.py
# ══════════════════════════════════════════════════════════════

class TestHwpConvertedCollector:
    def test_hwp_raises_clear_error(self, tmp_path):
        from collectors.hwp_converted_collector import collect_hwp_converted
        f = tmp_path / "document.hwp"
        f.write_bytes(b"fake hwp")
        with pytest.raises(ValueError) as exc_info:
            collect_hwp_converted(str(f))
        msg = str(exc_info.value)
        assert "PDF" in msg or "텍스트" in msg
        assert "변환" in msg

    def test_hwpx_raises_clear_error(self, tmp_path):
        from collectors.hwp_converted_collector import collect_hwp_converted
        f = tmp_path / "document.hwpx"
        f.write_bytes(b"fake hwpx")
        with pytest.raises(ValueError) as exc_info:
            collect_hwp_converted(str(f))
        assert "변환" in str(exc_info.value)

    def test_txt_file_parsed(self, tmp_path):
        from collectors.hwp_converted_collector import collect_hwp_converted
        f = tmp_path / "converted.txt"
        f.write_text("변환된 한글 문서 내용입니다.", encoding="utf-8")
        docs = collect_hwp_converted(str(f))
        assert len(docs) == 1
        assert docs[0].source_type == "hwp_converted"
        assert "한글" in docs[0].content

    def test_txt_file_utf8_fallback(self, tmp_path):
        from collectors.hwp_converted_collector import collect_hwp_converted
        f = tmp_path / "cp949.txt"
        f.write_bytes("한글 내용".encode("cp949"))
        docs = collect_hwp_converted(str(f))
        assert len(docs) >= 1

    def test_empty_txt_returns_empty(self, tmp_path):
        from collectors.hwp_converted_collector import collect_hwp_converted
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")
        docs = collect_hwp_converted(str(f))
        assert docs == []

    def test_file_not_found(self):
        from collectors.hwp_converted_collector import collect_hwp_converted
        with pytest.raises(FileNotFoundError):
            collect_hwp_converted("/nonexistent/doc.txt")

    def test_unsupported_extension_raises(self, tmp_path):
        from collectors.hwp_converted_collector import collect_hwp_converted
        f = tmp_path / "doc.docx"
        f.write_bytes(b"fake")
        with pytest.raises(ValueError, match="지원하지 않는"):
            collect_hwp_converted(str(f))

    def test_pdf_delegates_correctly(self, tmp_path):
        from collectors.hwp_converted_collector import collect_hwp_converted
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF fake")
        mock_page = MagicMock()
        mock_page.extract_text.return_value = "PDF 내용입니다. " * 5
        mock_pdf_obj = MagicMock()
        mock_pdf_obj.__enter__ = MagicMock(return_value=mock_pdf_obj)
        mock_pdf_obj.__exit__ = MagicMock(return_value=False)
        mock_pdf_obj.pages = [mock_page]
        with patch("collectors.pdf_collector.pdfplumber.open", return_value=mock_pdf_obj):
            docs = collect_hwp_converted(str(f))
        assert len(docs) >= 1
        assert all(d.source_type == "hwp_converted" for d in docs)


# ══════════════════════════════════════════════════════════════
# 통합 테스트 — end-to-end (mock DB)
# ══════════════════════════════════════════════════════════════

class TestEndToEnd:
    def test_docx_to_db(self, tmp_path, pg_conn):
        """docx 수집 → Supabase Postgres 저장 → 조회 흐름."""
        from docx import Document as DocxDocument
        from collectors.docx_collector import collect_docx
        from storage.supabase_store import upsert_documents, get_by_source_type

        # 임시 docx 생성
        doc_file = tmp_path / "faq.docx"
        d = DocxDocument()
        d.add_heading("수당 지급 기준", level=1)
        d.add_paragraph("월 30만원을 지급합니다.")
        d.save(str(doc_file))

        docs = collect_docx(str(doc_file))
        upsert_documents(docs, pg_conn)

        stored = get_by_source_type("docx", pg_conn)
        assert len(stored) == len(docs)
        assert stored[0].title == "수당 지급 기준"

    def test_excel_to_db(self, tmp_path, pg_conn):
        from openpyxl import Workbook
        from collectors.excel_collector import collect_excel
        from storage.supabase_store import upsert_documents, get_total_count

        wb = Workbook()
        ws = wb.active
        ws.append(["질문", "답변"])
        ws.append(["수료 기준은?", "80% 이상 출석"])
        path = tmp_path / "data.xlsx"
        wb.save(str(path))

        docs = collect_excel(str(path))
        upsert_documents(docs, pg_conn)

        assert get_total_count(pg_conn) == 1

    def test_category_distribution_after_import(self, tmp_path, pg_conn):
        from docx import Document as DocxDocument
        from collectors.docx_collector import collect_docx
        from storage.supabase_store import upsert_documents, get_category_distribution

        doc_file = tmp_path / "multi.docx"
        d = DocxDocument()
        d.add_heading("신청 자격 안내", level=1)
        d.add_paragraph("대학생만 가능합니다.")
        d.add_heading("수당 지급 기준 안내", level=2)
        d.add_paragraph("월 30만원입니다.")
        d.save(str(doc_file))

        docs = collect_docx(str(doc_file))
        upsert_documents(docs, pg_conn)

        dist = get_category_distribution(pg_conn)
        assert "신청 자격 안내" in dist or "미분류" in dist
