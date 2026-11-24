from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from coredb.schema import load_metadata


def test_embeddings_schema_loads_vector_column_and_hnsw_cosine_index() -> None:
    root = Path(__file__).resolve().parents[1]
    metadata = load_metadata([root / "postgres/schemas/news_embeddings.json"], engine_name="postgresql")
    table = metadata.tables["news_article_chunks"]

    assert table.c.embedding.type.compile(dialect=postgresql.dialect()) == "VECTOR(1024)"
    assert table.c.published_at.type.timezone is True
    assert any(index.unique and [column.name for column in index.columns] == ["article_uid", "chunk_index"] for index in table.indexes)

    vector_indexes = [index for index in table.indexes if "embedding" in [column.name for column in index.columns]]
    assert len(vector_indexes) == 1
    ddl = str(CreateIndex(vector_indexes[0]).compile(dialect=postgresql.dialect()))
    assert "USING hnsw" in ddl
    assert "vector_cosine_ops" in ddl
