"""
2단계(검색엔진 + 톤매트릭스 + 실패분석) 전체 기능 단위 테스트.

외부 API(Voyage AI, Anthropic)는 모두 mock으로 처리합니다.
실행 방법:
  pytest tests/test_stage2_engine.py -v
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


# ══════════════════════════════════════════════════════════════
# morpheme_analyzer.py
# ══════════════════════════════════════════════════════════════

class TestMorphemeAnalyzer:
    def test_extract_keywords_normal(self):
        from morpheme_analyzer import extract_keywords
        kws = extract_keywords("동아리 활동 수당은 언제 지급되나요?")
        assert "수당" in kws
        assert "지급" in kws

    def test_extract_keywords_empty_string(self):
        from morpheme_analyzer import extract_keywords
        assert extract_keywords("") == []

    def test_extract_keywords_whitespace_only(self):
        from morpheme_analyzer import extract_keywords
        assert extract_keywords("   ") == []

    def test_extract_keywords_excludes_bare_light_verbs(self):
        """'하다/있다/없다/되다/같다'는 거의 모든 문장에 등장하는 경동사/형용사라
        그 자체로는 주제를 담지 못합니다. 단독으로 쓰이면(예: "있나요?") 키워드로
        잡히지 않아야 모호한 질문이 모호성 판정을 정상적으로 통과합니다."""
        from morpheme_analyzer import extract_keywords
        assert extract_keywords("있나요?") == []
        assert extract_keywords("이거 어떻게 해요?") == []
        assert extract_keywords("같아요") == []

    def test_extract_keywords_already_excludes_other_contentless_predicates(self):
        """'아니다'는 VCN, '그렇다/어떻다'는 Kiwi가 VA가 아닌 VA-I로 태깅해
        _KEEP_TAGS에 안 걸리므로 별도 처리 없이도 이미 키워드에서 제외됩니다."""
        from morpheme_analyzer import extract_keywords
        assert extract_keywords("아닌가요?") == []
        assert extract_keywords("그런가요?") == []
        assert extract_keywords("어때요?") == []

    def test_extract_keywords_keeps_light_verb_stem_within_real_content_words(self):
        """'지급되나요'처럼 명사에 붙은 접미사(XSV)는 원래도 키워드가 아니었고,
        '힘들어요'처럼 의미를 담은 형용사 어간은 경동사 제외 목록에 없어 그대로
        키워드로 남아야 합니다."""
        from morpheme_analyzer import extract_keywords
        assert extract_keywords("활동 수당은 언제 지급되나요?") == ["활동", "수당", "지급"]
        assert extract_keywords("힘들어요") == ["힘들"]

    def test_analyze_combines_keywords_and_category(self):
        from morpheme_analyzer import analyze
        result = analyze("수당 지급 기준이 뭐예요?")
        assert result["category"] == "수당 지급 기준 안내"
        assert "수당" in result["keywords"]

    def test_analyze_unmatched_category_returns_uncategorized(self):
        from morpheme_analyzer import analyze
        result = analyze("오늘 날씨 어때요?")
        assert result["category"] == "미분류"


# ══════════════════════════════════════════════════════════════
# embedding_manager.py
# ══════════════════════════════════════════════════════════════

class TestEmbeddingManager:
    def test_get_embedding_provider_missing_model_raises(self, monkeypatch):
        from embedding_manager import get_embedding_provider
        monkeypatch.setenv("VOYAGE_API_KEY", "fake-key")
        with pytest.raises(ValueError):
            get_embedding_provider({})

    def test_get_embedding_provider_missing_api_key_raises(self, monkeypatch):
        from embedding_manager import get_embedding_provider
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        with pytest.raises(EnvironmentError):
            get_embedding_provider({"embedding_model": "voyage-4"})

    def test_voyage_provider_embed_query_empty_text_raises(self):
        from embedding_manager import VoyageEmbeddingProvider
        provider = VoyageEmbeddingProvider(api_key="fake-key", model="voyage-4")
        with pytest.raises(ValueError):
            provider.embed_query("")

    def test_voyage_provider_embed_query_normal(self):
        from embedding_manager import VoyageEmbeddingProvider
        provider = VoyageEmbeddingProvider(api_key="fake-key", model="voyage-4")
        fake_result = MagicMock(embeddings=[[0.1, 0.2, 0.3]])
        provider._client.embed = MagicMock(return_value=fake_result)
        vec = provider.embed_query("수당 지급 기준이 뭐예요?")
        assert vec == [0.1, 0.2, 0.3]
        provider._client.embed.assert_called_once_with(
            ["수당 지급 기준이 뭐예요?"], model="voyage-4", input_type="query"
        )

    def test_voyage_provider_embed_documents_empty_list(self):
        from embedding_manager import VoyageEmbeddingProvider
        provider = VoyageEmbeddingProvider(api_key="fake-key", model="voyage-4")
        assert provider.embed_documents([]) == []
        assert provider.embed_documents(["", "  "]) == []

    def test_voyage_provider_embed_query_external_api_failure_propagates(self):
        from embedding_manager import VoyageEmbeddingProvider
        provider = VoyageEmbeddingProvider(api_key="fake-key", model="voyage-4")
        provider._client.embed = MagicMock(side_effect=ConnectionError("voyage api down"))
        with pytest.raises(ConnectionError):
            provider.embed_query("질문")

    def test_backfill_embeddings_success(self):
        from embedding_manager import backfill_embeddings
        from models.document import Document
        doc = Document.new(source_type="notion", source_origin="a", title="제목", content="내용")
        fake_provider = MagicMock()
        fake_provider.embed_documents.return_value = [[0.1, 0.2]]

        with patch("storage.supabase_store.update_embeddings_batch") as fake_update:
            embedded, failed = backfill_embeddings([doc], fake_provider, "voyage-4")

        assert (embedded, failed) == (1, 0)
        fake_update.assert_called_once_with([(doc.doc_id, [0.1, 0.2])], "voyage-4", conn=None)

    def test_backfill_embeddings_batch_failure_counts_as_failed(self):
        from embedding_manager import backfill_embeddings
        from models.document import Document
        doc = Document.new(source_type="notion", source_origin="a", title="제목", content="내용")
        fake_provider = MagicMock()
        fake_provider.embed_documents.side_effect = ConnectionError("voyage api down")

        with patch("storage.supabase_store.update_embeddings_batch") as fake_update:
            embedded, failed = backfill_embeddings([doc], fake_provider, "voyage-4")

        assert (embedded, failed) == (0, 1)
        fake_update.assert_not_called()

    def test_backfill_embeddings_empty_list_is_noop(self):
        from embedding_manager import backfill_embeddings
        fake_provider = MagicMock()
        embedded, failed = backfill_embeddings([], fake_provider, "voyage-4")
        assert (embedded, failed) == (0, 0)
        fake_provider.embed_documents.assert_not_called()


# ══════════════════════════════════════════════════════════════
# storage/supabase_store.py — 2단계 추가분 (임베딩 컬럼)
# ══════════════════════════════════════════════════════════════

class TestSupabaseStoreEmbeddings:
    @pytest.fixture
    def sample_doc(self):
        from models.document import Document
        return Document.new(
            source_type="notion",
            source_origin="FAQ",
            title="수당 지급 기준",
            content="활동 시간에 따라 수당이 차등 지급됩니다.",
            category="수당 지급 기준 안내",
            notion_page_url="https://notion.so/abc123",
            notion_block_id="12345678-1234-1234-1234-123456789abc",
        )

    def test_initialize_db_adds_embedding_columns(self, pg_conn):
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'documents'"
            )
            cols = {row["column_name"] for row in cur.fetchall()}
        assert "embedding" in cols
        assert "embedding_model" in cols

    def test_initialize_db_idempotent_on_existing_table(self, pg_conn):
        from storage.supabase_store import initialize_db
        initialize_db(pg_conn)  # 두 번째 호출도 에러 없이 통과해야 함

    def test_update_embedding_and_get_all_with_embeddings_roundtrip(self, pg_conn, sample_doc):
        # 공유 운영 DB라 get_all_with_embeddings()는 실제 KB 문서도 함께 반환하므로,
        # 전체 건수가 아니라 이번에 넣은 doc_id 항목만 골라서 검증합니다.
        from storage.supabase_store import upsert_document, update_embedding, get_all_with_embeddings
        upsert_document(sample_doc, pg_conn)
        update_embedding(sample_doc.doc_id, [0.1, 0.2, 0.3], "voyage-4", pg_conn)

        results = get_all_with_embeddings("voyage-4", pg_conn)
        matches = [r for r in results if r[0].doc_id == sample_doc.doc_id]
        assert len(matches) == 1
        doc, embedding = matches[0]
        assert embedding == [0.1, 0.2, 0.3]

    def test_get_all_with_embeddings_invalidates_on_model_change(self, pg_conn, sample_doc):
        from storage.supabase_store import upsert_document, update_embedding, get_all_with_embeddings
        upsert_document(sample_doc, pg_conn)
        update_embedding(sample_doc.doc_id, [0.1, 0.2, 0.3], "voyage-3", pg_conn)

        results = get_all_with_embeddings("voyage-4", pg_conn)  # 다른 모델로 조회
        matches = [r for r in results if r[0].doc_id == sample_doc.doc_id]
        assert len(matches) == 1
        doc, embedding = matches[0]
        assert embedding is None

    def test_get_documents_missing_embedding(self, pg_conn, sample_doc):
        from storage.supabase_store import upsert_document, update_embedding, get_documents_missing_embedding
        upsert_document(sample_doc, pg_conn)

        missing = get_documents_missing_embedding("voyage-4", pg_conn)
        assert len(missing) == 1

        update_embedding(sample_doc.doc_id, [0.1, 0.2], "voyage-4", pg_conn)
        missing_after = get_documents_missing_embedding("voyage-4", pg_conn)
        assert len(missing_after) == 0

    def test_get_all_with_embeddings_empty_db(self):
        # 실제 운영 DB의 documents에는 이미 데이터가 쌓여 있어 "빈 테이블"을
        # 실제 DB로 재현할 수 없으므로 cursor를 mock하여 빈 결과 분기를 검증합니다.
        from storage.supabase_store import get_all_with_embeddings
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        assert get_all_with_embeddings("voyage-4", mock_conn) == []


# ══════════════════════════════════════════════════════════════
# hybrid_search.py
# ══════════════════════════════════════════════════════════════

class TestHybridSearchHelpers:
    def test_cosine_similarity_identical_vectors(self):
        from hybrid_search import _cosine_similarity
        assert _cosine_similarity([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)

    def test_cosine_similarity_orthogonal_vectors(self):
        from hybrid_search import _cosine_similarity
        assert _cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)

    def test_cosine_similarity_zero_vector_returns_zero(self):
        from hybrid_search import _cosine_similarity
        assert _cosine_similarity([0, 0, 0], [1, 2, 3]) == 0.0

    def test_min_max_normalize_normal(self):
        from hybrid_search import _min_max_normalize
        assert _min_max_normalize([1, 2, 3]) == [0.0, 0.5, 1.0]

    def test_min_max_normalize_constant_values(self):
        from hybrid_search import _min_max_normalize
        assert _min_max_normalize([5, 5, 5]) == [0.0, 0.0, 0.0]

    def test_min_max_normalize_empty_list(self):
        from hybrid_search import _min_max_normalize
        assert _min_max_normalize([]) == []


class TestSearchResult:
    def test_deep_link_url_with_block_id(self):
        from hybrid_search import SearchResult
        r = SearchResult(
            doc_id="d1", title="FAQ", content="내용", category="미분류",
            source_type="notion", notion_block_id="12345678-1234-1234-1234-123456789abc",
            notion_page_url="https://notion.so/abc", vector_score=0.5, bm25_score=0.5,
            combined_score=0.5,
        )
        assert r.deep_link_url() == "https://notion.so/abc#12345678123412341234123456789abc"

    def test_deep_link_url_none_without_block_id(self):
        from hybrid_search import SearchResult
        r = SearchResult(
            doc_id="d1", title="docx", content="내용", category="미분류",
            source_type="docx", notion_block_id=None, notion_page_url=None,
            vector_score=0.5, bm25_score=0.5, combined_score=0.5,
        )
        assert r.deep_link_url() is None


class TestHybridSearchEngine:
    @pytest.fixture(autouse=True)
    def _clear_corpus_cache(self):
        from hybrid_search import clear_corpus_cache
        clear_corpus_cache()
        yield
        clear_corpus_cache()

    @pytest.fixture
    def config(self):
        return {
            "embedding_model": "voyage-4",
            "search_weights": {"vector": 0.6, "bm25": 0.4},
            "search_top_k": 5,
            "similarity_threshold": 0.55,
        }

    @pytest.fixture
    def fake_provider(self):
        provider = MagicMock()
        provider.embed_query = MagicMock(return_value=[1.0, 0.0, 0.0])
        return provider

    def test_search_empty_query_raises(self, fake_provider, config):
        from hybrid_search import HybridSearchEngine
        engine = HybridSearchEngine(fake_provider, config)
        with pytest.raises(ValueError):
            engine.search("")

    def test_search_no_documents_returns_empty(self, fake_provider, config):
        from hybrid_search import HybridSearchEngine
        engine = HybridSearchEngine(fake_provider, config)
        with patch("hybrid_search.get_documents_fingerprint", return_value=(0, None)), \
             patch("hybrid_search.get_all_with_embeddings", return_value=[]):
            results = engine.search("수당 지급 기준이 뭐예요?")
        assert results == []

    def test_search_ranks_by_combined_score(self, fake_provider, config):
        from hybrid_search import HybridSearchEngine
        from models.document import Document

        doc1 = Document.new(source_type="docx", source_origin="a", title="수당 지급 기준",
                             content="활동 수당 지급 기준 안내", category="수당 지급 기준 안내")
        doc2 = Document.new(source_type="docx", source_origin="b", title="동아리 소개",
                             content="동아리 활동 소개 자료", category="미분류")

        corpus = [(doc1, [1.0, 0.0, 0.0]), (doc2, [0.0, 1.0, 0.0])]

        engine = HybridSearchEngine(fake_provider, config)
        with patch("hybrid_search.get_documents_fingerprint", return_value=(2, "t1")), \
             patch("hybrid_search.get_all_with_embeddings", return_value=corpus):
            results = engine.search("수당 지급 기준이 뭐예요?")

        assert len(results) == 2
        assert results[0].doc_id == doc1.doc_id  # 벡터+BM25 모두 doc1에 유리해야 함

    def test_search_missing_embedding_document_scored_zero(self, fake_provider, config):
        from hybrid_search import HybridSearchEngine
        from models.document import Document

        doc1 = Document.new(source_type="docx", source_origin="a", title="제목",
                             content="내용", category="미분류")
        corpus = [(doc1, None)]  # 임베딩 누락

        engine = HybridSearchEngine(fake_provider, config)
        with patch("hybrid_search.get_documents_fingerprint", return_value=(1, "t1")), \
             patch("hybrid_search.get_all_with_embeddings", return_value=corpus):
            results = engine.search("질문")
        assert len(results) == 1
        assert results[0].vector_score == 0.0

    def test_is_confident_true(self, fake_provider, config):
        from hybrid_search import HybridSearchEngine, SearchResult
        engine = HybridSearchEngine(fake_provider, config)
        results = [SearchResult("d1", "t", "c", "cat", "docx", None, None, 0.9, 0.9, 0.9)]
        assert engine.is_confident(results) is True

    def test_is_confident_false_below_threshold(self, fake_provider, config):
        from hybrid_search import HybridSearchEngine, SearchResult
        engine = HybridSearchEngine(fake_provider, config)
        results = [SearchResult("d1", "t", "c", "cat", "docx", None, None, 0.1, 0.1, 0.1)]
        assert engine.is_confident(results) is False

    def test_is_confident_false_when_empty(self, fake_provider, config):
        from hybrid_search import HybridSearchEngine
        engine = HybridSearchEngine(fake_provider, config)
        assert engine.is_confident([]) is False

    def test_search_external_embedding_failure_propagates(self, fake_provider, config):
        from hybrid_search import HybridSearchEngine
        fake_provider.embed_query.side_effect = TimeoutError("voyage api timeout")
        engine = HybridSearchEngine(fake_provider, config)
        with patch("hybrid_search.get_documents_fingerprint", return_value=(1, "t1")), \
             patch("hybrid_search.get_all_with_embeddings", return_value=[(MagicMock(), [1.0])]):
            with pytest.raises(TimeoutError):
                engine.search("질문")


class TestHybridSearchCorpusCache:
    @pytest.fixture(autouse=True)
    def _clear_corpus_cache(self):
        from hybrid_search import clear_corpus_cache
        clear_corpus_cache()
        yield
        clear_corpus_cache()

    @pytest.fixture
    def config(self):
        return {
            "embedding_model": "voyage-4",
            "search_weights": {"vector": 0.6, "bm25": 0.4},
            "search_top_k": 5,
            "similarity_threshold": 0.55,
        }

    @pytest.fixture
    def fake_provider(self):
        provider = MagicMock()
        provider.embed_query = MagicMock(return_value=[1.0, 0.0, 0.0])
        return provider

    def _doc(self, source_origin, title):
        from models.document import Document
        return Document.new(source_type="docx", source_origin=source_origin, title=title,
                             content=title, category="미분류")

    def test_reuses_cache_when_fingerprint_unchanged(self, fake_provider, config):
        from hybrid_search import HybridSearchEngine
        corpus = [(self._doc("a", "문서1"), [1.0, 0.0, 0.0])]

        engine = HybridSearchEngine(fake_provider, config)
        with patch("hybrid_search.get_documents_fingerprint", return_value=(1, "t1")), \
             patch("hybrid_search.get_all_with_embeddings", return_value=corpus) as mock_fetch:
            engine.search("질문1")
            engine.search("질문2")

        # fingerprint가 같으면 두 번째 검색에서는 코퍼스를 다시 불러오지 않아야 함
        mock_fetch.assert_called_once()

    def test_refetches_when_fingerprint_changes(self, fake_provider, config):
        from hybrid_search import HybridSearchEngine
        corpus_v1 = [(self._doc("a", "문서1"), [1.0, 0.0, 0.0])]
        corpus_v2 = [(self._doc("a", "문서1"), [1.0, 0.0, 0.0]),
                     (self._doc("b", "문서2"), [0.0, 1.0, 0.0])]

        engine = HybridSearchEngine(fake_provider, config)
        with patch("hybrid_search.get_documents_fingerprint",
                   side_effect=[(1, "t1"), (2, "t2")]), \
             patch("hybrid_search.get_all_with_embeddings",
                   side_effect=[corpus_v1, corpus_v2]) as mock_fetch:
            first = engine.search("질문1")
            second = engine.search("질문2")

        assert mock_fetch.call_count == 2
        assert len(first) == 1
        assert len(second) == 2

    def test_different_models_cached_separately(self, fake_provider):
        from hybrid_search import HybridSearchEngine
        corpus = [(self._doc("a", "문서1"), [1.0, 0.0, 0.0])]

        config_v3 = {"embedding_model": "voyage-3", "search_top_k": 5,
                     "search_weights": {"vector": 0.6, "bm25": 0.4}, "similarity_threshold": 0.55}
        config_v4 = {"embedding_model": "voyage-4", "search_top_k": 5,
                     "search_weights": {"vector": 0.6, "bm25": 0.4}, "similarity_threshold": 0.55}

        with patch("hybrid_search.get_documents_fingerprint", return_value=(1, "t1")), \
             patch("hybrid_search.get_all_with_embeddings", return_value=corpus) as mock_fetch:
            HybridSearchEngine(fake_provider, config_v3).search("질문")
            HybridSearchEngine(fake_provider, config_v4).search("질문")

        # 모델명이 다르면 같은 fingerprint라도 별도로 캐싱되어 각각 한 번씩 불러와야 함
        assert mock_fetch.call_count == 2


# ══════════════════════════════════════════════════════════════
# tone_config.py
# ══════════════════════════════════════════════════════════════

class TestToneConfig:
    def test_build_brand_tone_guideline_contains_all_elements(self):
        from tone_config import build_brand_tone_guideline, BRAND_TONE_ELEMENTS
        guideline = build_brand_tone_guideline()
        for value in BRAND_TONE_ELEMENTS.values():
            assert value in guideline


# ══════════════════════════════════════════════════════════════
# tone_matrix.py
# ══════════════════════════════════════════════════════════════

class TestSituationClassifier:
    @pytest.fixture
    def classifier(self):
        from tone_matrix import SituationClassifier
        return SituationClassifier(repeat_threshold=2)

    def _inp(self, question="질문", keywords=None, category="미분류",
             top_category="미분류", repeated=0):
        from tone_matrix import SituationClassificationInput
        return SituationClassificationInput(
            question=question, keywords=keywords or [], question_category=category,
            top_result_category=top_category, repeated_count=repeated,
        )

    def test_classify_policy_violation(self, classifier):
        from tone_matrix import Situation
        result = classifier.classify(self._inp(question="현금으로 따로 줄 수 있어요?"))
        assert result == Situation.POLICY_VIOLATION

    def test_classify_escalation_request(self, classifier):
        from tone_matrix import Situation
        result = classifier.classify(self._inp(question="상담원 연결해주세요"))
        assert result == Situation.ESCALATION_NEEDED

    def test_classify_contact_info_request_is_escalation(self, classifier):
        """담당자/운영자 연락처 문의는 RAG가 무관한 문서(예: 오픈채팅 안내)를 섞어
        답변하지 않도록, 검색 없이 고정 연락처 템플릿(에스컬레이션)으로 처리해야 합니다."""
        from tone_matrix import Situation
        for question in ["담당자 연락처 알려줘", "운영자 연락처 알려줘", "운영팀 연락처 알려줘"]:
            assert classifier.classify(self._inp(question=question)) == Situation.ESCALATION_NEEDED

    def test_classify_repeated_question(self, classifier):
        from tone_matrix import Situation
        result = classifier.classify(self._inp(question="평범한 질문", repeated=2))
        assert result == Situation.REPEATED_QUESTION

    def test_classify_gratitude(self, classifier):
        from tone_matrix import Situation
        result = classifier.classify(self._inp(question="감사합니다 도움 많이 됐어요"))
        assert result == Situation.GRATITUDE

    def test_classify_simple_rejection(self, classifier):
        from tone_matrix import Situation
        result = classifier.classify(self._inp(question="아 됐어요 괜찮아요"))
        assert result == Situation.SIMPLE_REJECTION

    def test_classify_info_gap(self, classifier):
        from tone_matrix import Situation
        result = classifier.classify(self._inp(
            question="수당은 언제 들어와요?", category="수당 지급 기준 안내",
            top_category="프로그램 일정 안내",
        ))
        assert result == Situation.INFO_GAP

    def test_classify_normal_response_default(self, classifier):
        from tone_matrix import Situation
        result = classifier.classify(self._inp(
            question="수당은 언제 들어와요?", category="수당 지급 기준 안내",
            top_category="수당 지급 기준 안내",
        ))
        assert result == Situation.NORMAL_RESPONSE

    def test_priority_policy_violation_over_gratitude(self, classifier):
        """정책위반과 감사 키워드가 동시에 있으면 정책위반이 우선해야 함."""
        from tone_matrix import Situation
        result = classifier.classify(self._inp(
            question="감사한데 혹시 증빙서류 없이 처리해줄 수 있나요?"
        ))
        assert result == Situation.POLICY_VIOLATION

    def test_classify_empty_question_no_crash(self, classifier):
        from tone_matrix import Situation
        result = classifier.classify(self._inp(question=""))
        assert result == Situation.NORMAL_RESPONSE

    def test_classify_with_malformed_keywords_file_falls_back_gracefully(self, monkeypatch, tmp_path):
        import tone_matrix
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{ not valid json", encoding="utf-8")
        monkeypatch.setattr(tone_matrix, "_KEYWORDS_PATH", bad_file)

        from tone_matrix import SituationClassifier, Situation
        classifier = SituationClassifier(repeat_threshold=2)
        result = classifier.classify(self._inp(question="상담원 연결해주세요"))
        # 키워드 사전을 못 읽으면 어떤 카테고리도 매칭되지 않아 기본값(정상응답)으로 떨어짐
        assert result == Situation.NORMAL_RESPONSE


class TestToneMatrixBuilder:
    def test_build_instruction_for_all_situations(self):
        from tone_matrix import ToneMatrixBuilder, Situation, SITUATION_TO_ATTITUDE
        builder = ToneMatrixBuilder()
        for situation in Situation:
            instruction = builder.build_instruction(situation)
            assert SITUATION_TO_ATTITUDE[situation].value in instruction

    def test_situation_to_attitude_mapping_is_complete(self):
        from tone_matrix import Situation, SITUATION_TO_ATTITUDE
        for situation in Situation:
            assert situation in SITUATION_TO_ATTITUDE


# ══════════════════════════════════════════════════════════════
# failure_analyzer.py
# ══════════════════════════════════════════════════════════════

class TestFailureAnalyzer:
    def _inp(self, keywords, category, top_score, threshold=0.55, min_kw=1):
        from failure_analyzer import FailureAnalysisInput
        return FailureAnalysisInput(
            question="q", keywords=keywords, question_category=category,
            top_score=top_score, similarity_threshold=threshold,
            min_keywords_for_clarity=min_kw,
        )

    def test_question_ambiguity_too_few_keywords(self):
        from failure_analyzer import analyze_failure, FailureCause
        result = analyze_failure(self._inp(keywords=[], category="미분류", top_score=0.0))
        assert result == FailureCause.QUESTION_AMBIGUITY

    def test_out_of_policy_uncategorized(self):
        from failure_analyzer import analyze_failure, FailureCause
        result = analyze_failure(self._inp(keywords=["날씨"], category="미분류", top_score=0.0))
        assert result == FailureCause.OUT_OF_POLICY

    def test_knowledge_gap_zero_score(self):
        from failure_analyzer import analyze_failure, FailureCause
        result = analyze_failure(self._inp(
            keywords=["수당"], category="수당 지급 기준 안내", top_score=0.0
        ))
        assert result == FailureCause.KNOWLEDGE_GAP

    def test_search_failure_low_nonzero_score(self):
        from failure_analyzer import analyze_failure, FailureCause
        result = analyze_failure(self._inp(
            keywords=["수당"], category="수당 지급 기준 안내", top_score=0.2, threshold=0.55
        ))
        assert result == FailureCause.SEARCH_FAILURE

    def test_priority_ambiguity_over_out_of_policy(self):
        """키워드 부족이 미분류보다 먼저 체크되어야 함."""
        from failure_analyzer import analyze_failure, FailureCause
        result = analyze_failure(self._inp(keywords=[], category="미분류", top_score=0.0, min_kw=1))
        assert result == FailureCause.QUESTION_AMBIGUITY


# ══════════════════════════════════════════════════════════════
# prompt_builder.py
# ══════════════════════════════════════════════════════════════

class TestPromptBuilder:
    def test_get_anthropic_client_missing_key_raises(self, monkeypatch):
        from prompt_builder import get_anthropic_client
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(EnvironmentError):
            get_anthropic_client()

    def test_build_system_prompt_includes_deep_link_instruction_for_notion(self):
        from prompt_builder import build_system_prompt
        from hybrid_search import SearchResult
        results = [SearchResult("d1", "FAQ", "내용", "미분류", "notion",
                                 "12345678-1234-1234-1234-123456789abc",
                                 "https://notion.so/abc", 0.8, 0.8, 0.8)]
        prompt = build_system_prompt("톤지침", results, low_confidence=False)
        assert "바로가기" in prompt
        assert "notion.so/abc" in prompt

    def test_build_system_prompt_excludes_deep_link_for_non_notion(self):
        from prompt_builder import build_system_prompt
        from hybrid_search import SearchResult
        results = [SearchResult("d1", "문서", "내용", "미분류", "docx",
                                 None, None, 0.8, 0.8, 0.8)]
        prompt = build_system_prompt("톤지침", results, low_confidence=False)
        assert "바로가기" not in prompt

    def test_build_system_prompt_low_confidence_adds_uncertainty_clause(self):
        from prompt_builder import build_system_prompt
        prompt_low = build_system_prompt("톤지침", [], low_confidence=True)
        prompt_normal = build_system_prompt("톤지침", [], low_confidence=False)
        assert "불확실성" in prompt_low
        assert "불확실성" not in prompt_normal

    def test_build_system_prompt_empty_results(self):
        from prompt_builder import build_system_prompt
        prompt = build_system_prompt("톤지침", [], low_confidence=False)
        assert "(없음)" in prompt

    def test_build_system_prompt_with_contact_includes_verbatim_and_no_fabrication_clause(self):
        from prompt_builder import build_system_prompt
        contact = "서울 동아리ON 운영팀\n- 전화: 02-0000-0000\n- 이메일: ops@test.com"
        prompt = build_system_prompt("톤지침", [], low_confidence=False, operation_team_contact=contact)
        assert contact in prompt
        assert "지어내지" in prompt

    def test_build_system_prompt_without_contact_has_no_contact_clause(self):
        from prompt_builder import build_system_prompt
        prompt = build_system_prompt("톤지침", [], low_confidence=False)
        assert "지어내지" not in prompt

    def test_call_claude_parses_tool_use_response(self):
        from prompt_builder import call_claude

        block = MagicMock()
        block.type = "tool_use"
        block.name = "provide_answer"
        block.input = {"answer": "수당은 매월 말일 지급됩니다.", "sentiment_score": 0.2, "resolution_status": "해결됨"}

        fake_response = MagicMock(content=[block])
        fake_client = MagicMock()
        fake_client.messages.create = MagicMock(return_value=fake_response)

        answer, sentiment, resolution_status = call_claude(fake_client, "claude-sonnet-4-6", "시스템프롬프트", "질문")
        assert answer == "수당은 매월 말일 지급됩니다."
        assert sentiment == 0.2
        assert resolution_status == "해결됨"

    def test_call_claude_missing_tool_use_block_raises(self):
        from prompt_builder import call_claude
        text_block = MagicMock(type="text")
        fake_response = MagicMock(content=[text_block])
        fake_client = MagicMock()
        fake_client.messages.create = MagicMock(return_value=fake_response)
        with pytest.raises(RuntimeError):
            call_claude(fake_client, "claude-sonnet-4-6", "시스템프롬프트", "질문")

    def test_call_claude_external_api_failure_propagates(self):
        from prompt_builder import call_claude
        fake_client = MagicMock()
        fake_client.messages.create = MagicMock(side_effect=ConnectionError("anthropic api down"))
        with pytest.raises(ConnectionError):
            call_claude(fake_client, "claude-sonnet-4-6", "시스템프롬프트", "질문")

    def test_call_claude_stream_emits_incremental_deltas_then_done(self):
        """Anthropic SDK는 tool input을 raw partial_json 텍스트 조각으로 흘려주고,
        우리가 직접 누적해 "answer" 문자열 값을 부분 파싱합니다(SDK의 InputJsonEvent.
        snapshot은 jiter partial_mode=True라 닫히지 않은 문자열은 누락시켜 실시간
        스트리밍에 못 쓴다는 걸 실측으로 확인함). 매 조각마다 새로 늘어난 부분만큼만
        delta로 떼어내 보내야 합니다(이미 보낸 부분을 중복으로 다시 보내면 안 됨)."""
        from prompt_builder import call_claude_stream

        class _Event:
            def __init__(self, partial_json):
                self.type = "input_json"
                self.partial_json = partial_json

        class _FakeStream:
            def __init__(self, events, final_content):
                self._events = events
                self._final_message = MagicMock(content=final_content)

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def __iter__(self):
                return iter(self._events)

            def get_final_message(self):
                return self._final_message

        block = MagicMock()
        block.type = "tool_use"
        block.name = "provide_answer"
        block.input = {"answer": "안녕하세요 반갑습니다", "sentiment_score": 0.0, "resolution_status": "해결됨"}

        events = [
            _Event('{"answer": "안녕'),
            _Event('하세요'),
            _Event(' 반갑습니다", "sentiment_score": 0.0, "resolution_status": "해결됨"}'),
        ]
        fake_client = MagicMock()
        fake_client.messages.stream = MagicMock(return_value=_FakeStream(events, [block]))

        chunks = []
        final = None
        for kind, payload in call_claude_stream(fake_client, "claude-sonnet-4-6", "시스템프롬프트", "질문"):
            if kind == "delta":
                chunks.append(payload)
            else:
                final = payload

        assert "".join(chunks) == "안녕하세요 반갑습니다"
        assert chunks == ["안녕", "하세요", " 반갑습니다"]
        assert final == ("안녕하세요 반갑습니다", 0.0, "해결됨")

    def test_call_claude_stream_no_interim_events_emits_full_answer_at_end(self):
        """중간 delta 없이 스트림이 끝나도(예: 짧은 답변), 최종 tool_use 블록에서
        한 번에 전체 answer를 delta로 떼어내 보낸 뒤 done을 내보내야 합니다."""
        from prompt_builder import call_claude_stream

        block = MagicMock()
        block.type = "tool_use"
        block.name = "provide_answer"
        block.input = {"answer": "수당은 매월 말일 지급됩니다.", "sentiment_score": 0.2, "resolution_status": "해결됨"}

        class _FakeStream:
            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def __iter__(self):
                return iter(())

            def get_final_message(self):
                return MagicMock(content=[block])

        fake_client = MagicMock()
        fake_client.messages.stream = MagicMock(return_value=_FakeStream())

        events = list(call_claude_stream(fake_client, "claude-sonnet-4-6", "시스템프롬프트", "질문"))
        assert events == [
            ("delta", "수당은 매월 말일 지급됩니다."),
            ("done", ("수당은 매월 말일 지급됩니다.", 0.2, "해결됨")),
        ]

    def test_call_claude_stream_missing_tool_use_block_raises(self):
        from prompt_builder import call_claude_stream
        text_block = MagicMock(type="text")

        class _FakeStream:
            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def __iter__(self):
                return iter(())

            def get_final_message(self):
                return MagicMock(content=[text_block])

        fake_client = MagicMock()
        fake_client.messages.stream = MagicMock(return_value=_FakeStream())

        with pytest.raises(RuntimeError):
            list(call_claude_stream(fake_client, "claude-sonnet-4-6", "시스템프롬프트", "질문"))


# ══════════════════════════════════════════════════════════════
# chatbot_engine.py
# ══════════════════════════════════════════════════════════════

class TestForbiddenWords:
    def test_contains_forbidden_word_true(self):
        from chatbot_engine import _load_forbidden_words, contains_forbidden_word
        words = _load_forbidden_words()
        assert contains_forbidden_word("야 이 씨발 진짜 화나네", words) is True

    def test_contains_forbidden_word_false(self):
        from chatbot_engine import _load_forbidden_words, contains_forbidden_word
        words = _load_forbidden_words()
        assert contains_forbidden_word("수당은 언제 지급되나요?", words) is False

    def test_load_forbidden_words_missing_file_returns_empty(self, monkeypatch, tmp_path):
        import chatbot_engine
        monkeypatch.setattr(chatbot_engine, "_FORBIDDEN_WORDS_PATH", tmp_path / "nope.json")
        assert chatbot_engine._load_forbidden_words() == []

    def test_load_forbidden_words_malformed_json_returns_empty(self, monkeypatch, tmp_path):
        import chatbot_engine
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{ not valid json", encoding="utf-8")
        monkeypatch.setattr(chatbot_engine, "_FORBIDDEN_WORDS_PATH", bad_file)
        assert chatbot_engine._load_forbidden_words() == []


class TestFormatOperationTeamContact:
    def test_format_operation_team_contact_includes_fields(self):
        from chatbot_engine import _format_operation_team_contact
        config = {"operation_team": {
            "name": "동아리ON 운영팀", "address": "서울특별시 중구 수표로 12",
            "phone": "02-1234-5678",
            "email_list": ["a@test.com", "b@test.com"], "operating_hours": "평일 9-18시",
        }}
        text = _format_operation_team_contact(config)
        assert "동아리ON 운영팀" in text
        assert "서울특별시 중구 수표로 12" in text
        assert "02-1234-5678" in text
        assert "a@test.com" in text
        assert "평일 9-18시" in text

    def test_format_operation_team_contact_missing_keys_no_crash(self):
        from chatbot_engine import _format_operation_team_contact
        text = _format_operation_team_contact({})
        assert "운영팀" in text


class _FakeMessageStream:
    """anthropic SDK의 client.messages.stream() 컨텍스트 매니저를 모사합니다.
    중간 delta 이벤트 없이 최종 tool_use 블록만 흘려보내, call_claude_stream()이
    스트림 종료 후 한 번에 emit하는 폴백 경로를 타게 합니다(기존 messages.create
    기반 테스트와 결과적으로 동일한 answer/sentiment_score/resolution_status를 얻음)."""

    def __init__(self, content_blocks):
        self._final_message = MagicMock(content=content_blocks)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def __iter__(self):
        return iter(())

    def get_final_message(self):
        return self._final_message


def _mock_claude_stream(anthropic_client, block):
    """anthropic_client.messages.stream(...)이 block 하나를 담은 가짜 스트림을
    반환하도록 설정합니다."""
    anthropic_client.messages.stream = MagicMock(return_value=_FakeMessageStream([block]))


class _FakeMessageStreamWithEvents(_FakeMessageStream):
    """_FakeMessageStream과 달리 중간 input_json 이벤트도 흘려보낼 수 있어,
    handle_question_stream()이 실제로 여러 delta로 나눠 보내는지 검증할 때 씁니다."""

    def __init__(self, events, content_blocks):
        super().__init__(content_blocks)
        self._events = events

    def __iter__(self):
        return iter(self._events)


class TestChatbotEngine:
    @pytest.fixture
    def config(self):
        return {
            "embedding_model": "voyage-4",
            "llm_model": "claude-sonnet-4-6",
            "similarity_threshold": 0.55,
            "search_weights": {"vector": 0.6, "bm25": 0.4},
            "search_top_k": 5,
            "repeat_threshold": 2,
            "min_keywords_for_clarity": 1,
            "operation_team": {
                "name": "동아리ON 운영팀", "phone": "02-0000-0000",
                "email_list": ["ops@test.com"], "operating_hours": "평일 9-18시",
            },
        }

    @pytest.fixture
    def engine(self, config, pg_conn):
        from chatbot_engine import ChatbotEngine
        fake_search_engine = MagicMock()
        fake_anthropic_client = MagicMock()
        return ChatbotEngine(
            config, conn=pg_conn,
            search_engine=fake_search_engine, anthropic_client=fake_anthropic_client,
        )

    def test_handle_question_empty_raises(self, engine):
        with pytest.raises(ValueError):
            engine.handle_question("", "session-1")

    def test_handle_question_blocked_by_filter_skips_search_and_llm(self, engine):
        from tone_matrix import Situation, ResponseAttitude
        response = engine.handle_question("야 이 씨발 진짜 화나네", "session-1")

        assert response.blocked_by_filter is True
        assert response.situation == Situation.EMOTIONAL_ESCALATION
        assert response.response_attitude == ResponseAttitude.ESCALATION
        assert response.escalated_to_operation_team is True
        engine._search_engine.search.assert_not_called()
        engine._anthropic_client.messages.stream.assert_not_called()

    def test_handle_question_search_failure_path(self, engine):
        from failure_analyzer import FailureCause
        engine._search_engine.search.return_value = []
        engine._search_engine.is_confident.return_value = False

        response = engine.handle_question("오늘 날씨 어때요?", "session-2")

        assert response.search_success is False
        assert response.failure_cause == FailureCause.OUT_OF_POLICY
        assert response.escalated_to_operation_team is True
        engine._anthropic_client.messages.stream.assert_not_called()

    def test_handle_question_low_confidence_with_results_still_calls_claude(self, engine):
        """검색 결과가 있는데 신뢰도(점수)만 threshold 미달인 경우, 코퍼스 정규화
        한계 때문에 점수만으로 차단하지 않고 Claude에게 직접 판단을 맡겨야 합니다
        (점수가 비어있는 경우의 즉시 폴백과는 달라야 함)."""
        from hybrid_search import SearchResult

        results = [SearchResult(
            doc_id="d1", title="수당 지급 기준", content="활동 수당은 매월 말일 지급됩니다.",
            category="미분류", source_type="notion",
            notion_block_id="12345678-1234-1234-1234-123456789abc",
            notion_page_url="https://notion.so/abc", vector_score=0.1, bm25_score=0.1,
            combined_score=0.2,
        )]
        engine._search_engine.search.return_value = results
        engine._search_engine.is_confident.return_value = False

        block = MagicMock()
        block.type = "tool_use"
        block.name = "provide_answer"
        block.input = {
            "answer": "죄송하지만 관련 내용을 찾지 못했어요.",
            "sentiment_score": 0.0, "resolution_status": "지식DB공백",
        }
        _mock_claude_stream(engine._anthropic_client, block)

        response = engine.handle_question("오늘 서울 날씨 어때요?", "session-13")

        engine._anthropic_client.messages.stream.assert_called_once()
        from failure_analyzer import FailureCause
        assert response.failure_cause == FailureCause.KNOWLEDGE_GAP
        system_prompt = engine._anthropic_client.messages.stream.call_args.kwargs["system"]
        assert "02-0000-0000" in system_prompt  # low_confidence라 운영팀 연락처가 프롬프트에 포함됨

    def test_handle_question_claude_api_failure_escalates_and_logs(self, engine, pg_conn):
        """Claude API 호출 자체가 실패(레이트리밋/네트워크 오류 등)하면 일반 예외로
        새지 않고 운영팀 폴백으로 응답해야 하며, 대시보드에서 보이도록 failure_cause를
        남겨야 합니다 (이전에는 예외가 그대로 올라가 qa_log에 아무 기록도 안 남았음)."""
        from hybrid_search import SearchResult
        from failure_analyzer import FailureCause

        results = [SearchResult(
            doc_id="d1", title="동아리 가입 안내", content="가입 절차는 다음과 같습니다.",
            category="미분류", source_type="docx", notion_block_id=None, notion_page_url=None,
            vector_score=0.9, bm25_score=0.9, combined_score=0.9,
        )]
        engine._search_engine.search.return_value = results
        engine._search_engine.is_confident.return_value = True
        engine._anthropic_client.messages.stream = MagicMock(side_effect=ConnectionError("anthropic api down"))

        response = engine.handle_question("동아리 가입 어떻게 하나요?", "session-api-fail")

        assert response.failure_cause == FailureCause.API_ERROR
        assert response.search_success is False
        assert response.escalated_to_operation_team is True
        assert "02-0000-0000" in response.answer

        with pg_conn.cursor() as cur:
            cur.execute("SELECT failure_cause FROM qa_log WHERE session_id = %s", ("session-api-fail",))
            row = cur.fetchone()
        assert row["failure_cause"] == "API오류"

    def test_handle_question_success_path_with_deep_link(self, engine):
        from hybrid_search import SearchResult
        from tone_matrix import Situation

        results = [SearchResult(
            doc_id="d1", title="수당 지급 기준", content="활동 수당은 매월 말일 지급됩니다.",
            category="수당 지급 기준 안내", source_type="notion",
            notion_block_id="12345678-1234-1234-1234-123456789abc",
            notion_page_url="https://notion.so/abc", vector_score=0.8, bm25_score=0.8,
            combined_score=0.8,
        )]
        engine._search_engine.search.return_value = results
        engine._search_engine.is_confident.return_value = True

        block = MagicMock()
        block.type = "tool_use"
        block.name = "provide_answer"
        block.input = {"answer": "수당은 매월 말일 지급됩니다. (자세한 내용: [수당 지급 기준 바로가기])",
                       "sentiment_score": 0.1, "resolution_status": "해결됨"}
        _mock_claude_stream(engine._anthropic_client, block)

        response = engine.handle_question("수당 언제 들어와요?", "session-3")

        assert response.search_success is True
        assert response.situation == Situation.NORMAL_RESPONSE
        assert response.sentiment_score == 0.1
        assert response.failure_cause is None
        assert response.deep_link == "https://notion.so/abc#12345678123412341234123456789abc"
        assert response.escalated_to_operation_team is False

    def test_handle_question_stream_emits_deltas_then_done_matching_handle_question(self, engine):
        """handle_question_stream()이 handle_question()과 같은 분기/로깅을 거치되,
        답변을 delta로 나눠 내보내고 마지막에 done으로 최종 answer/deep_link를
        한 번만 내보내는지 확인합니다."""
        from hybrid_search import SearchResult

        results = [SearchResult(
            doc_id="d1", title="수당 지급 기준", content="활동 수당은 매월 말일 지급됩니다.",
            category="수당 지급 기준 안내", source_type="notion",
            notion_block_id="12345678-1234-1234-1234-123456789abc",
            notion_page_url="https://notion.so/abc", vector_score=0.8, bm25_score=0.8,
            combined_score=0.8,
        )]
        engine._search_engine.search.return_value = results
        engine._search_engine.is_confident.return_value = True

        class _Event:
            def __init__(self, partial_json):
                self.type = "input_json"
                self.partial_json = partial_json

        block = MagicMock()
        block.type = "tool_use"
        block.name = "provide_answer"
        block.input = {"answer": "수당은 매월 말일 지급됩니다.",
                       "sentiment_score": 0.1, "resolution_status": "해결됨"}
        engine._anthropic_client.messages.stream = MagicMock(
            return_value=_FakeMessageStreamWithEvents(
                [
                    _Event('{"answer": "수당은'),
                    _Event(' 매월 말일 지급됩니다.", "sentiment_score": 0.1, "resolution_status": "해결됨"}'),
                ],
                [block],
            )
        )

        events = list(engine.handle_question_stream("수당 언제 들어와요?", "session-stream-1"))

        deltas = [e["text"] for e in events if e["type"] == "delta"]
        done_events = [e for e in events if e["type"] == "done"]
        assert len(done_events) == 1
        assert "".join(deltas) == "수당은 매월 말일 지급됩니다."
        assert done_events[0]["answer"] == "수당은 매월 말일 지급됩니다."
        assert done_events[0]["deep_link"] == "https://notion.so/abc#12345678123412341234123456789abc"

    def test_handle_question_high_confidence_still_passes_real_contact_to_prompt(self, engine):
        """검색 신뢰도가 높아도 매칭된 문서 안에 오래된 연락처가 적혀 있을 수 있으므로,
        Claude가 문서 속 연락처보다 대시보드의 실제 연락처를 우선하도록 신뢰도와
        무관하게 항상 실제 연락처를 프롬프트에 넘겨야 합니다."""
        from hybrid_search import SearchResult

        results = [SearchResult(
            doc_id="d1", title="공휴일 운영 안내", content="공휴일에는 근무자가 없습니다.",
            category="미분류", source_type="notion",
            notion_block_id="12345678-1234-1234-1234-123456789abc",
            notion_page_url="https://notion.so/abc", vector_score=0.9, bm25_score=0.9,
            combined_score=0.9,
        )]
        engine._search_engine.search.return_value = results
        engine._search_engine.is_confident.return_value = True

        block = MagicMock()
        block.type = "tool_use"
        block.name = "provide_answer"
        block.input = {"answer": "공휴일에는 근무자가 없습니다.",
                       "sentiment_score": 0.0, "resolution_status": "해결됨"}
        _mock_claude_stream(engine._anthropic_client, block)

        engine.handle_question("공휴일에 근무자 없어?", "session-holiday")

        system_prompt = engine._anthropic_client.messages.stream.call_args.kwargs["system"]
        assert "02-0000-0000" in system_prompt

    def test_handle_question_claude_reports_unresolved_sets_failure_cause(self, engine):
        """is_confident()가 True여도 Claude가 resolution_status로 미해결을 보고하면
        failure_cause가 채워지고 딥링크 없이 운영팀으로 안내해야 합니다 (실제 발견된
        is_confident() 한계에 대한 보강 로직)."""
        from hybrid_search import SearchResult
        from failure_analyzer import FailureCause

        results = [SearchResult(
            doc_id="d1", title="수당 지급 기준", content="활동 수당은 매월 말일 지급됩니다.",
            category="미분류", source_type="notion",
            notion_block_id="12345678-1234-1234-1234-123456789abc",
            notion_page_url="https://notion.so/abc", vector_score=0.33, bm25_score=0.5,
            combined_score=0.6,
        )]
        engine._search_engine.search.return_value = results
        engine._search_engine.is_confident.return_value = True

        block = MagicMock()
        block.type = "tool_use"
        block.name = "provide_answer"
        block.input = {
            "answer": "죄송하지만 저는 동아리ON 운영 관련 문의만 도와드릴 수 있어요.",
            "sentiment_score": 0.0, "resolution_status": "정책밖요청",
        }
        _mock_claude_stream(engine._anthropic_client, block)

        response = engine.handle_question("오늘 서울 날씨 어때요?", "session-11")

        assert response.search_success is False
        assert response.failure_cause == FailureCause.OUT_OF_POLICY
        assert response.deep_link is None
        assert response.escalated_to_operation_team is True

    def test_handle_question_ambiguous_question_skips_search_and_llm(self, engine):
        """모호한 질문은 문서를 추측해서 답변하면 왜곡된 정보를 줄 위험이 있으므로,
        운영팀 안내 대신 질문을 구체화해 다시 물어보도록 유도해야 합니다."""
        from failure_analyzer import FailureCause

        response = engine.handle_question("음", "session-12")

        assert response.failure_cause == FailureCause.QUESTION_AMBIGUITY
        assert response.search_success is False
        assert response.escalated_to_operation_team is False
        assert "다시 한번" in response.answer
        assert "운영팀" not in response.answer
        engine._search_engine.search.assert_not_called()
        engine._anthropic_client.messages.stream.assert_not_called()

    def test_handle_question_bare_light_verb_question_is_also_ambiguous(self, engine):
        """'있나요?'처럼 의미 없는 경동사("있다") 하나만 있는 질문도 실질적인
        키워드가 없는 것이므로 모호성 분기를 타야 합니다 (형태소 분석기가
        "있"을 키워드로 잘못 잡아 일반 검색 경로로 빠지던 경계 케이스)."""
        from failure_analyzer import FailureCause

        response = engine.handle_question("있나요?", "session-light-verb")

        assert response.failure_cause == FailureCause.QUESTION_AMBIGUITY
        engine._search_engine.search.assert_not_called()
        engine._anthropic_client.messages.stream.assert_not_called()

    def test_handle_question_known_ambiguous_phrases_batch(self, engine):
        """실제 챗봇에 던져 모호성 판정이 정상 동작함을 확인했던 질문 묶음을
        고정 회귀 테스트로 남겨, 형태소 분석기나 라이브러리 버전이 바뀌어도
        운영 중 발견 대신 여기서 먼저 잡히도록 합니다."""
        from failure_analyzer import FailureCause

        known_ambiguous_phrases = [
            "음", "그게 뭐예요", "이거 뭐예요?", "뭐라고요?", "그래서요?",
            "네?", "그건 뭐죠", "ㅇㅋ", "뭐임", "있나요?", "같아요",
        ]
        for i, phrase in enumerate(known_ambiguous_phrases):
            response = engine.handle_question(phrase, f"session-amb-batch-{i}")
            assert response.failure_cause == FailureCause.QUESTION_AMBIGUITY, phrase
        engine._search_engine.search.assert_not_called()
        engine._anthropic_client.messages.stream.assert_not_called()

    def test_handle_question_search_failure_answer_contains_operation_team_contact(self, engine):
        engine._search_engine.search.return_value = []
        engine._search_engine.is_confident.return_value = False

        response = engine.handle_question("오늘 날씨 어때요?", "session-7")

        assert "02-0000-0000" in response.answer
        assert "ops@test.com" in response.answer

    def test_handle_question_blocked_by_filter_answer_contains_operation_team_contact(self, engine):
        response = engine.handle_question("야 이 씨발 진짜 화나네", "session-8")

        assert "02-0000-0000" in response.answer
        assert "ops@test.com" in response.answer

    def test_handle_question_escalation_request_skips_llm_and_returns_contact(self, engine):
        from hybrid_search import SearchResult
        from tone_matrix import Situation, ResponseAttitude

        results = [SearchResult(
            doc_id="d1", title="가입 절차", content="온라인 신청서를 작성하면 됩니다.",
            category="가입 및 자격 안내", source_type="notion",
            notion_block_id="12345678-1234-1234-1234-123456789abc",
            notion_page_url="https://notion.so/abc", vector_score=0.8, bm25_score=0.8,
            combined_score=0.8,
        )]
        engine._search_engine.search.return_value = results
        engine._search_engine.is_confident.return_value = True

        response = engine.handle_question("그냥 상담원 연결해주세요", "session-9")

        assert response.situation == Situation.ESCALATION_NEEDED
        assert response.response_attitude == ResponseAttitude.ESCALATION
        assert response.escalated_to_operation_team is True
        assert "02-0000-0000" in response.answer
        assert "ops@test.com" in response.answer
        engine._anthropic_client.messages.stream.assert_not_called()

    def test_handle_question_info_gap_passes_real_contact_into_prompt(self, engine):
        from hybrid_search import SearchResult
        from tone_matrix import Situation

        results = [SearchResult(
            doc_id="d1", title="출결 기준", content="정기모임 80% 이상 출석해야 합니다.",
            category="출결 및 활동기준 안내", source_type="notion",
            notion_block_id="12345678-1234-1234-1234-123456789abc",
            notion_page_url="https://notion.so/abc", vector_score=0.8, bm25_score=0.8,
            combined_score=0.8,
        )]
        engine._search_engine.search.return_value = results
        engine._search_engine.is_confident.return_value = True

        block = MagicMock()
        block.type = "tool_use"
        block.name = "provide_answer"
        block.input = {"answer": "해당 내용은 안내 자료에 없어 운영팀 문의가 필요합니다.",
                       "sentiment_score": 0.0, "resolution_status": "검색실패"}
        _mock_claude_stream(engine._anthropic_client, block)

        # 질문 카테고리(수당 지급 기준 안내)와 검색결과 카테고리(출결 및 활동기준 안내)를
        # 불일치시켜 INFO_GAP으로 분류되게 함
        response = engine.handle_question("수당 지급 기준이 뭐예요?", "session-10")

        assert response.situation == Situation.INFO_GAP
        from failure_analyzer import FailureCause
        assert response.failure_cause == FailureCause.SEARCH_FAILURE
        system_prompt = engine._anthropic_client.messages.stream.call_args.kwargs["system"]
        assert "02-0000-0000" in system_prompt
        assert "ops@test.com" in system_prompt
        assert "지어내지" in system_prompt

    def test_handle_question_logs_qa_log_entry(self, engine, pg_conn):
        engine._search_engine.search.return_value = []
        engine._search_engine.is_confident.return_value = False

        engine.handle_question("오늘 날씨 어때요?", "session-4")

        with pg_conn.cursor() as cur:
            cur.execute("SELECT * FROM qa_log WHERE session_id = %s", ("session-4",))
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0]["failure_cause"] == "정책밖요청"
        assert rows[0]["escalated_to_operation_team"] is True

    def test_handle_question_repeated_count_detected_via_log(self, engine, pg_conn):
        results_low = []
        engine._search_engine.search.return_value = results_low
        engine._search_engine.is_confident.return_value = False

        engine.handle_question("수당 지급 기준이 뭐예요?", "session-5")
        response2 = engine.handle_question("수당 지급 기준 알려주세요", "session-5")

        assert response2.repeated_count >= 1

    def test_handle_question_external_llm_failure_escalates_instead_of_raising(self, engine):
        """Claude API 호출 실패는 더 이상 그대로 전파되지 않고 운영팀 폴백으로
        흡수됩니다 (test_handle_question_claude_api_failure_escalates_and_logs 참고)."""
        from hybrid_search import SearchResult
        from failure_analyzer import FailureCause
        results = [SearchResult("d1", "t", "c", "수당 지급 기준 안내", "docx", None, None,
                                 0.8, 0.8, 0.8)]
        engine._search_engine.search.return_value = results
        engine._search_engine.is_confident.return_value = True
        engine._anthropic_client.messages.stream = MagicMock(side_effect=ConnectionError("api down"))

        response = engine.handle_question("수당 지급 기준이 뭐예요?", "session-6")

        assert response.failure_cause == FailureCause.API_ERROR

    def test_count_repeated_no_log_entries_returns_zero(self, engine):
        assert engine._count_repeated("session-x", ["수당"]) == 0

    def test_count_repeated_empty_keywords_returns_zero(self, engine):
        assert engine._count_repeated("session-x", []) == 0


# ══════════════════════════════════════════════════════════════
# build_embeddings.py
# ══════════════════════════════════════════════════════════════

class TestBuildEmbeddings:
    def test_main_no_missing_documents_skips_embedding_call(self, monkeypatch, tmp_path, capsys):
        import build_embeddings
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"embedding_model": "voyage-4"}), encoding="utf-8")
        monkeypatch.setattr(build_embeddings, "_root", tmp_path)

        fake_provider = MagicMock()
        with patch("embedding_manager.get_embedding_provider", return_value=fake_provider), \
             patch("storage.supabase_store.get_documents_missing_embedding", return_value=[]), \
             patch("storage.supabase_store.update_embedding") as fake_update:
            build_embeddings.main()

        fake_provider.embed_documents.assert_not_called()
        fake_update.assert_not_called()
        assert "백필 대상 문서가 없습니다" in capsys.readouterr().out

    def test_main_backfills_missing_documents(self, monkeypatch, tmp_path):
        import build_embeddings
        from models.document import Document

        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"embedding_model": "voyage-4"}), encoding="utf-8")
        monkeypatch.setattr(build_embeddings, "_root", tmp_path)

        doc = Document.new(source_type="docx", source_origin="a", title="제목", content="내용")
        fake_provider = MagicMock()
        fake_provider.embed_documents.return_value = [[0.1, 0.2]]

        with patch("embedding_manager.get_embedding_provider", return_value=fake_provider), \
             patch("storage.supabase_store.get_documents_missing_embedding", return_value=[doc]), \
             patch("storage.supabase_store.update_embedding") as fake_update:
            build_embeddings.main()

        fake_update.assert_called_once_with(doc.doc_id, [0.1, 0.2], "voyage-4")

    def test_main_batch_embedding_failure_skips_batch_without_crashing(self, monkeypatch, tmp_path):
        import build_embeddings
        from models.document import Document

        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"embedding_model": "voyage-4"}), encoding="utf-8")
        monkeypatch.setattr(build_embeddings, "_root", tmp_path)

        doc = Document.new(source_type="docx", source_origin="a", title="제목", content="내용")
        fake_provider = MagicMock()
        fake_provider.embed_documents.side_effect = ConnectionError("voyage api down")

        with patch("embedding_manager.get_embedding_provider", return_value=fake_provider), \
             patch("storage.supabase_store.get_documents_missing_embedding", return_value=[doc]), \
             patch("storage.supabase_store.update_embedding") as fake_update:
            build_embeddings.main()  # 예외를 던지지 않고 로그만 남기고 넘어가야 함

        fake_update.assert_not_called()
