"""세 검색 범위의 독립 선택과 후보 제한 전 권한 필터를 검증한다."""
import getpass
import itertools
from types import SimpleNamespace

import lancedb
import pytest

from knowmate.agents.knowledge_agent import KnowledgeAgent
from knowmate.rag.email_indexer import EMAIL_SCHEMA
from knowmate.rag.embedding import VECTOR_DIM
from knowmate.rag.indexer import SCHEMA
from knowmate.rag.retriever import Retriever
from knowmate.tests.test_query_reader import row


SCOPES = [list(values) for size in range(4)
          for values in itertools.combinations(['local', 'shared', 'mail'], size)]


class FixedEmbedding:
    """거리 순서가 재현되는 fake 임베딩 클라이언트다."""

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        """질문마다 동일한 합성 벡터를 반환한다."""
        self.calls += 1
        return [[1.0] + [0.0] * (VECTOR_DIM - 1) for _ in texts]


def make_retriever(tmp_path, documents=None, mails=None, **options):
    """임시 실DB 테이블과 fake 검색기를 만든다."""
    db = lancedb.connect(str(tmp_path / 'db'))
    documents = documents if documents is not None else [
        row(SCHEMA, chunk_id='local', scope='local', file_path='C:/synthetic/local.txt'),
        row(SCHEMA, chunk_id='shared', scope='shared', file_path='Z:/synthetic/shared.txt'),
    ]
    mails = mails if mails is not None else [
        row(EMAIL_SCHEMA, chunk_id='knox', source_type='knox', source_file='C:/synthetic/knox.mysingle'),
        row(EMAIL_SCHEMA, chunk_id='eml', source_type='eml', source_file='C:/synthetic/mail.eml'),
    ]
    chunks = db.create_table('chunks', schema=SCHEMA)
    emails = db.create_table('emails', schema=EMAIL_SCHEMA)
    if documents:
        chunks.add(documents)
    if mails:
        emails.add(mails)
    embed = FixedEmbedding()
    retriever = Retriever(SimpleNamespace(table=chunks), embed, top_k=options.pop('top_k', 20),
                          email_indexer=SimpleNamespace(table=emails), score_threshold=-1,
                          config=options.pop('config', {}), **options)
    return retriever, embed


@pytest.mark.parametrize('scopes', SCOPES)
def test_real_database_scope_combinations(tmp_path, scopes):
    """저장상 local인 메일도 mail 선택만으로 독립 회수한다."""
    retriever, embed = make_retriever(tmp_path)
    expected = set(scopes) - {'mail'}
    if 'mail' in scopes:
        expected.update({'knox', 'eml'})
    assert {value['chunk_id'] for value in retriever.search('q', scopes)} == expected
    assert embed.calls == bool(scopes)


@pytest.mark.parametrize('invalid', ['local', ('local',), {}, 1, [None], [1], ['unknown'], ['local', 'unknown']])
def test_invalid_scope_never_embeds_or_queries(invalid):
    """잘못된 범위는 전체 검색으로 확대하지 않는다."""
    retriever = Retriever(None, SimpleNamespace(embed=lambda _: pytest.fail('embedding')), config={})
    with pytest.raises(ValueError, match='invalid scopes'):
        retriever.search('q', invalid)


def test_empty_selection_has_no_embedding_config_or_table_access(monkeypatch):
    """전부 해제하면 설정 조회와 임베딩도 생략한다."""
    monkeypatch.setattr('knowmate.config.get_config', lambda: pytest.fail('config'))
    retriever = Retriever(None, SimpleNamespace(embed=lambda _: pytest.fail('embedding')))
    assert retriever.search('q', []) == []


@pytest.mark.parametrize('scopes', [['local'], ['shared'], ['mail']])
def test_nonselected_table_never_searches(tmp_path, scopes):
    """비선택 테이블은 벡터·파일명 검색 모두 생략한다."""
    retriever, _ = make_retriever(tmp_path)
    forbidden = SimpleNamespace(search=lambda *_: pytest.fail('nonselected data query'))
    if scopes == ['mail']:
        retriever._table = forbidden
    else:
        retriever._email_indexer.table = forbidden
    assert retriever.search('synthetic', scopes)


@pytest.mark.parametrize('scopes', [['local'], ['shared'], ['local', 'shared'], None])
def test_scope_and_owner_filters_precede_candidate_limit(tmp_path, scopes):
    """많은 비선택·타인 후보 뒤의 유효 문서가 누락되지 않는다."""
    other_scope = 'local' if scopes == ['shared'] else 'shared'
    candidates = [row(SCHEMA, chunk_id=f'other-{i}', scope=other_scope,
                      owner='another-user' if other_scope == 'local' else getpass.getuser())
                  for i in range(12)] if scopes in (['local'], ['shared']) else []
    candidates += [row(SCHEMA, chunk_id=f'foreign-{i}', owner='another-user') for i in range(12)]
    weaker = [0.9, 0.1] + [0.0] * (VECTOR_DIM - 2)
    wanted = []
    for scope in ['local', 'shared']:
        if scopes is None or scope in scopes:
            candidates.append(row(SCHEMA, chunk_id=f'wanted-{scope}', scope=scope, vector=weaker))
            wanted.append(f'wanted-{scope}')
    retriever, _ = make_retriever(tmp_path, documents=candidates, mails=[], top_k=3)
    assert {value['chunk_id'] for value in retriever.search('q', scopes)} == set(wanted)


def test_none_and_duplicate_scopes_keep_owner_protection(tmp_path):
    """내부 전체 검색과 중복 선택도 타인의 로컬 문서를 반환하지 않는다."""
    retriever, _ = make_retriever(tmp_path, documents=[
        row(SCHEMA, chunk_id='own'), row(SCHEMA, chunk_id='foreign', owner='another-user')])
    assert {value['chunk_id'] for value in retriever.search('q')} == {'own', 'knox', 'eml'}
    assert {value['chunk_id'] for value in retriever.search('q', ['mail', 'mail'])} == {'knox', 'eml'}


def test_quoted_owner_does_not_expand_document_filter(tmp_path):
    """따옴표가 있는 사용자 이름도 SQL 조건을 확대하지 않는다."""
    user = "synthetic' OR 1=1 --"
    retriever, _ = make_retriever(tmp_path, documents=[
        row(SCHEMA, chunk_id='own', owner=user),
        row(SCHEMA, chunk_id='foreign', owner='another-user'),
        row(SCHEMA, chunk_id='shared', scope='shared'),
    ])
    retriever._current_user = user
    assert {value['chunk_id'] for value in retriever.search('q', ['local'])} == {'own'}


@pytest.mark.parametrize('scopes', [['local'], ['shared'], ['mail'], ['local', 'shared', 'mail']])
def test_keyword_date_and_exclusion_keep_scope_and_owner(tmp_path, monkeypatch, scopes):
    """날짜·파일명 보강·제외 경로에서도 비선택/타인/삭제행은 차단한다."""
    monkeypatch.setattr('knowmate.rag.retriever.parse_date_range_ko', lambda _: (900, 1100))
    documents = []
    for scope in ['local', 'shared']:
        for kind in ['allowed', 'excluded', 'deleted', 'foreign']:
            documents.append(row(SCHEMA, chunk_id=f'{scope}-{kind}', scope=scope,
                                 file_path=f'C:/synthetic/target-{scope}-{kind}.txt', mtime=1000,
                                 is_deleted=kind == 'deleted',
                                 owner='another-user' if kind == 'foreign' and scope == 'local' else getpass.getuser()))
    mails = [row(EMAIL_SCHEMA, chunk_id=f'mail-{kind}', source_type='knox', mail_date_ts=1000,
                 source_file=f'C:/synthetic/target-{kind}.mysingle', is_deleted=kind == 'deleted')
             for kind in ['allowed', 'excluded', 'deleted']]
    excluded = [value['file_path'] for value in documents if value['chunk_id'].endswith('excluded')]
    excluded.append('C:/synthetic/target-excluded.mysingle')
    retriever, _ = make_retriever(tmp_path, documents, mails,
                                config={'collector': {'exclude_files': excluded}})
    expected = set()
    if 'local' in scopes:
        expected.add('local-allowed')
    if 'shared' in scopes:
        expected.update({'shared-allowed', 'shared-foreign'})
    if 'mail' in scopes:
        expected.add('mail-allowed')
    assert {value['chunk_id'] for value in retriever.search('target', scopes)} == expected


def test_missing_mail_index_and_empty_tables(tmp_path):
    """메일 저장소가 없거나 비어 있어도 다른 범위를 대신 검색하지 않는다."""
    retriever, _ = make_retriever(tmp_path, documents=[], mails=[])
    assert retriever.search('q', ['mail']) == []
    retriever._email_indexer = None
    assert retriever.search('q', ['mail']) == []
    assert retriever.search('q', ['local', 'shared']) == []


@pytest.mark.parametrize('scopes', SCOPES[1:])
def test_knowledge_agent_forwards_exact_selected_scopes(scopes):
    """에이전트는 세 범위를 재해석하지 않고 Retriever에 전달한다."""
    calls = []
    retriever = SimpleNamespace(search=lambda query, **kwargs: calls.append((query, kwargs['scopes'])) or [])
    agent = KnowledgeAgent()
    agent._pipeline = {'retriever': retriever, 'llm': SimpleNamespace(answer=lambda *_: 'synthetic')}
    agent.handle('q', {'scopes': scopes})
    assert calls == [('q', scopes)]
