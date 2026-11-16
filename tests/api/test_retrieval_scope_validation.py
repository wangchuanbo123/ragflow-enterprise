import pytest
from pydantic import ValidationError

from app.schemas.rag import RetrievalScope


@pytest.mark.parametrize('kind', ['all', 'knowledge_base', 'document', 'folder', 'tag'])
def test_incomplete_explicit_scope_is_rejected(kind):
    with pytest.raises(ValidationError, match='空范围不会自动扩大'):
        RetrievalScope(selection_kind=kind)


@pytest.mark.parametrize('field,value', [('document_ids', [' ']), ('knowledge_base_ids', ['']), ('tag_ids', ['']), ('folder_id', '')])
def test_blank_scope_identifiers_are_rejected(field, value):
    with pytest.raises(ValidationError):
        RetrievalScope(**{field: value})


def test_default_scope_and_valid_explicit_scope_remain_compatible():
    assert RetrievalScope().document_ids == []
    assert RetrievalScope(selection_kind='document', document_ids=['doc']).document_ids == ['doc']
