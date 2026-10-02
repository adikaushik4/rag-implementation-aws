-- RAG portfolio project — database schema
-- Run against a PostgreSQL 15.2+ instance with pgvector available.
-- Intended to be run once, via psql, against the RDS instance (see docs/setup-guide.md).

-- 1. Enable the pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- 2. Table storing each chunk's text and embedding
CREATE TABLE IF NOT EXISTS document_chunks (
    id            SERIAL PRIMARY KEY,
    document_key  TEXT NOT NULL,          -- S3 object key the chunk came from
    chunk_index   INT NOT NULL,           -- position of this chunk within the document
    chunk_text    TEXT NOT NULL,          -- the chunk's raw text, used as context at query time
    embedding     vector(1024),           -- must match the embedding model's output dimension
    created_at    TIMESTAMPTZ DEFAULT now(),

    -- Prevents duplicate rows if ingestion is re-run for the same document;
    -- paired with an ON CONFLICT upsert in the ingestion Lambda.
    UNIQUE (document_key, chunk_index)
);

-- 3. Index for fast approximate nearest-neighbor search
-- HNSW chosen over IVFFlat: faster queries, no need to pre-populate the table
-- before building the index — a better fit for incremental ingestion.
CREATE INDEX IF NOT EXISTS document_chunks_embedding_hnsw_idx
ON document_chunks
USING hnsw (embedding vector_cosine_ops);

-- Verification:
--   \dx              -- confirm the vector extension is listed
--   \d document_chunks   -- confirm the table and index exist