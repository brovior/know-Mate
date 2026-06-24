"""Phase 4 보안 모듈 pytest 테스트 -- fake 모드 기준, 사외 환경 전부 통과."""
from pathlib import Path
import pytest
from knowmate.rag.embedding import EmbeddingClient, VECTOR_DIM
from knowmate.secure.crypto import FakeCryptoManager, get_crypto_manager


def _fake_embed_client() -> EmbeddingClient:
    """테스트용 fake EmbeddingClient를 반환한다."""
    return EmbeddingClient(base_url="http://localhost", host_header="embed.internal", fake=True)


class TestFakeCrypto:
    def test_encrypt_returns_same(self):
        """FakeCryptoManager.encrypt()는 입력 문자열을 그대로 반환한다."""
        mgr = FakeCryptoManager()
        assert mgr.encrypt("텍스트") == "텍스트"

    def test_decrypt_returns_same(self):
        """FakeCryptoManager.decrypt()는 입력 문자열을 그대로 반환한다."""
        mgr = FakeCryptoManager()
        assert mgr.decrypt("암호문") == "암호문"

    def test_roundtrip_restores_original(self):
        """encrypt -> decrypt 왕복 후 원문이 복원된다."""
        mgr = FakeCryptoManager()
        original = "A설비 알람 폭주 처리 절차"
        assert mgr.decrypt(mgr.encrypt(original)) == original

    def test_encrypt_empty_string(self):
        """빈 문자열도 그대로 반환한다."""
        mgr = FakeCryptoManager()
        assert mgr.encrypt("") == ""
        assert mgr.decrypt("") == ""

    def test_encrypt_multiline(self):
        """여러 줄 텍스트도 그대로 반환한다."""
        mgr = FakeCryptoManager()
        text = "첫째 줄" + chr(10) + "A" + chr(10) + "셋째 줄"
        assert mgr.encrypt(text) == text


class TestCryptoInterface:
    def test_fake_mode_returns_fake_manager(self):
        """extractor=fake이면 FakeCryptoManager를 반환한다."""
        mgr = get_crypto_manager({"extractor": "fake"})
        assert isinstance(mgr, FakeCryptoManager)

    def test_default_mode_returns_fake_manager(self):
        """extractor 키 없으면 FakeCryptoManager를 반환한다."""
        mgr = get_crypto_manager({})
        assert isinstance(mgr, FakeCryptoManager)

    def test_plain_mode_requires_win32crypt(self):
        """extractor=plain이면 win32crypt 없는 환경에서 CryptoUnavailableError."""
        try:
            import win32crypt
            pytest.skip("win32crypt가 있는 환경")
        except ImportError:
            from knowmate.secure.crypto import CryptoUnavailableError
            with pytest.raises(CryptoUnavailableError):
                get_crypto_manager({"extractor": "plain"})

    def test_auto_mode_requires_win32crypt(self):
        """extractor=auto이면 win32crypt 없는 환경에서 CryptoUnavailableError."""
        try:
            import win32crypt
            pytest.skip("win32crypt가 있는 환경")
        except ImportError:
            from knowmate.secure.crypto import CryptoUnavailableError
            with pytest.raises(CryptoUnavailableError):
                get_crypto_manager({"extractor": "auto"})

class TestIndexerWithCrypto:
    def test_indexer_accepts_crypto_param(self, tmp_path: Path):
        """FakeCryptoManager를 crypto 파라미터로 Indexer 생성이 성공한다."""
        from knowmate.rag.indexer import Indexer
        embed = _fake_embed_client()
        crypto = FakeCryptoManager()
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed, crypto=crypto)
        assert indexer is not None

    def test_indexer_default_crypto_is_fake(self, tmp_path: Path):
        """crypto 파라미터 생략 시 FakeCryptoManager가 사용된다."""
        from knowmate.rag.indexer import Indexer
        embed = _fake_embed_client()
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
        assert isinstance(indexer._crypto, FakeCryptoManager)

    def test_encrypt_called_on_index_file(self, tmp_path: Path):
        """index_file() 시 crypto.encrypt가 청크마다 호출된다."""
        from knowmate.rag.indexer import Indexer
        from knowmate.secure.fake_reader import FakeReader
        embed = _fake_embed_client()
        call_log = []
        class SpyCrypto(FakeCryptoManager):
            def encrypt(self, plaintext: str) -> str:
                call_log.append(plaintext)
                return super().encrypt(plaintext)
        crypto = SpyCrypto()
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed, crypto=crypto)
        text = FakeReader().extract("sample.docx")
        ids = indexer.index_file(path="C:/sample/test.docx", text=text, mtime=1000.0, scope="local")
        assert len(ids) >= 1
        assert len(call_log) == len(ids)

    def test_stored_text_equals_encrypted(self, tmp_path: Path):
        """FakeCrypto 모드에서 저장된 text는 청크 평문과 동일하다."""
        from knowmate.rag.indexer import Indexer
        from knowmate.secure.fake_reader import FakeReader
        from knowmate.rag.chunker import chunk_text
        embed = _fake_embed_client()
        crypto = FakeCryptoManager()
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed, crypto=crypto)
        text = FakeReader().extract("sample.txt")
        indexer.index_file(path="C:/sample/test.txt", text=text, mtime=1000.0, scope="local")
        df = indexer.table.to_arrow().to_pandas()
        assert len(df) >= 1
        expected_chunks = chunk_text(text, "txt")
        stored_texts = sorted(df["text"].tolist())
        expected_sorted = sorted(expected_chunks)
        assert stored_texts == expected_sorted

class TestRetrieverWithCrypto:
    def _make_stack(self, tmp_path: Path, crypto=None):
        """Indexer + Retriever 스택을 반환한다."""
        from knowmate.rag.indexer import Indexer
        from knowmate.rag.retriever import Retriever
        if crypto is None:
            crypto = FakeCryptoManager()
        embed = _fake_embed_client()
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed, crypto=crypto)
        retriever = Retriever(indexer=indexer, embed_client=embed, top_k=10, score_threshold=-1.0, crypto=crypto)
        return indexer, retriever

    def test_retriever_accepts_crypto_param(self, tmp_path: Path):
        """FakeCryptoManager를 crypto 파라미터로 Retriever 생성이 성공한다."""
        _, retriever = self._make_stack(tmp_path)
        assert isinstance(retriever._crypto, FakeCryptoManager)

    def test_retriever_default_crypto_is_fake(self, tmp_path: Path):
        """crypto 파라미터 생략 시 FakeCryptoManager가 사용된다."""
        from knowmate.rag.indexer import Indexer
        from knowmate.rag.retriever import Retriever
        embed = _fake_embed_client()
        indexer = Indexer(db_path=tmp_path / "db", embed_client=embed)
        retriever = Retriever(indexer=indexer, embed_client=embed)
        assert isinstance(retriever._crypto, FakeCryptoManager)

    def test_search_returns_decrypted_text(self, tmp_path: Path):
        """search() 결과의 text가 복호화된 값이다 (FakeCrypto -> 원문 그대로)."""
        from knowmate.secure.fake_reader import FakeReader
        crypto = FakeCryptoManager()
        indexer, retriever = self._make_stack(tmp_path, crypto=crypto)
        text = FakeReader().extract("sample.docx")
        indexer.index_file(path="C:/sample/doc.docx", text=text, mtime=1000.0, scope="local")
        results = retriever.search("알람")
        assert isinstance(results, list)
        assert len(results) >= 1
        assert all(isinstance(r["text"], str) for r in results)
        assert all(len(r["text"]) > 0 for r in results)

    def test_decrypt_called_on_search_results(self, tmp_path: Path):
        """search() 시 crypto.decrypt가 결과 청크마다 호출된다."""
        from knowmate.secure.fake_reader import FakeReader
        decrypt_log = []
        class SpyCrypto(FakeCryptoManager):
            def decrypt(self, ciphertext: str) -> str:
                decrypt_log.append(ciphertext)
                return super().decrypt(ciphertext)
        crypto = SpyCrypto()
        indexer, retriever = self._make_stack(tmp_path, crypto=crypto)
        text = FakeReader().extract("sample.docx")
        indexer.index_file(path="C:/sample/doc.docx", text=text, mtime=1000.0, scope="local")
        results = retriever.search("알람")
        assert len(results) >= 1
        assert len(decrypt_log) == len(results)


class TestComReader:
    def test_com_reader_unavailable_without_win32com(self):
        """win32com 없는 환경에서 ComReader.extract() 시 ComUnavailableError."""
        try:
            import win32com.client
            pytest.skip("win32com이 있는 환경")
        except ImportError:
            from knowmate.secure.com_reader import ComReader, ComUnavailableError
            reader = ComReader()
            with pytest.raises(ComUnavailableError):
                reader.extract("C:/dummy/file.doc")

    def test_com_reader_unsupported_ext_raises(self):
        """지원하지 않는 확장자(.docx)는 ComReader에서 ValueError."""
        from knowmate.secure.com_reader import ComReader
        reader = ComReader()
        with pytest.raises((ValueError, Exception)):
            reader.extract("C:/dummy/file.docx")


class TestAutoReader:
    def test_auto_reader_docx_uses_plain(self, tmp_path: Path):
        """AutoReader.extract()로 .docx 경로를 주면 PlainReader 경로로 간다."""
        from knowmate.secure import AutoReader
        reader = AutoReader()
        fake_docx = tmp_path / "test.docx"
        with pytest.raises(Exception):
            reader.extract(str(fake_docx))

    def test_auto_reader_doc_uses_com(self):
        """AutoReader.extract()로 .doc 경로를 주면 COM 경로로 간다."""
        from knowmate.secure import AutoReader
        reader = AutoReader()
        try:
            import win32com.client
            with pytest.raises(Exception):
                reader.extract("C:/dummy/nonexistent.doc")
        except ImportError:
            from knowmate.secure.com_reader import ComUnavailableError
            with pytest.raises(ComUnavailableError):
                reader.extract("C:/dummy/nonexistent.doc")

    def test_get_extractor_auto_returns_auto_reader(self):
        """get_extractor(auto)가 AutoReader 인스턴스를 반환한다."""
        from knowmate.secure import get_extractor, AutoReader
        reader = get_extractor("auto")
        assert isinstance(reader, AutoReader)

    def test_get_extractor_plain_returns_plain_reader(self):
        """get_extractor(plain)이 PlainReader 인스턴스를 반환한다."""
        from knowmate.secure import get_extractor
        from knowmate.secure.plain_reader import PlainReader
        reader = get_extractor("plain")
        assert isinstance(reader, PlainReader)

    def test_get_extractor_fake_returns_fake_reader(self):
        """get_extractor(fake)가 FakeReader 인스턴스를 반환한다."""
        from knowmate.secure import get_extractor
        from knowmate.secure.fake_reader import FakeReader
        reader = get_extractor("fake")
        assert isinstance(reader, FakeReader)

    def test_get_extractor_unknown_raises(self):
        """알 수 없는 모드 문자열에서 ValueError."""
        from knowmate.secure import get_extractor
        with pytest.raises(ValueError):
            get_extractor("unknown_mode")


class TestPlainReaderTables:
    """표·도형 추출 검증 (docx 표 / pptx 표 + 그룹 재귀)."""

    def test_docx_extracts_table_in_order(self, tmp_path: Path):
        """docx 문단과 표가 문서 순서대로, 표는 ' | '로 추출된다."""
        import docx
        from knowmate.secure.plain_reader import PlainReader

        d = docx.Document()
        d.add_paragraph("머리말")
        t = d.add_table(rows=2, cols=2)
        t.cell(0, 0).text = "이름"; t.cell(0, 1).text = "부서"
        t.cell(1, 0).text = "홍길동"; t.cell(1, 1).text = "생산1팀"
        d.add_paragraph("꼬리말")
        p = tmp_path / "a.docx"
        d.save(str(p))

        text = PlainReader().extract(str(p))
        assert "머리말" in text
        assert "이름 | 부서" in text
        assert "홍길동 | 생산1팀" in text
        # 순서 보존: 머리말 → 표 → 꼬리말
        assert text.index("머리말") < text.index("홍길동") < text.index("꼬리말")

    def test_pptx_extracts_table_and_textbox(self, tmp_path: Path):
        """pptx 텍스트박스와 표 셀이 모두 추출된다."""
        from pptx import Presentation
        from pptx.util import Inches
        from knowmate.secure.plain_reader import PlainReader

        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        tb = slide.shapes.add_textbox(Inches(0), Inches(0), Inches(3), Inches(1))
        tb.text_frame.text = "조직도"
        table = slide.shapes.add_table(2, 2, Inches(0), Inches(2), Inches(4), Inches(1)).table
        table.cell(0, 0).text = "직급"; table.cell(0, 1).text = "이름"
        table.cell(1, 0).text = "팀장"; table.cell(1, 1).text = "김철수"
        p = tmp_path / "b.pptx"
        prs.save(str(p))

        text = PlainReader().extract(str(p))
        assert "조직도" in text
        assert "직급 | 이름" in text
        assert "팀장 | 김철수" in text
