-- Directory: milli-agent/sql/init_supabase.sql
-- Lifted unchanged from launch-agentic-rag (github.com/ShenSeanChen/launch-agentic-rag)
-- so the vector upgrade path reuses the exact schema those videos teach.
--
-- Complete Supabase Database Setup for RAG Application
-- Run this script on a FRESH Supabase project
-- 
-- Instructions:
-- 1. Create a new Supabase project at https://supabase.com
-- 2. Wait for project initialization to complete
-- 3. Go to SQL Editor in your Supabase dashboard
-- 4. Create a new query
-- 5. Copy and paste this ENTIRE script
-- 6. Click "Run" to execute everything at once
--
-- This script creates everything from scratch

-- =============================================================================
-- STEP 1: Enable pgvector extension for vector similarity search
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- =============================================================================
-- STEP 2: Create the main RAG chunks table
-- =============================================================================

CREATE TABLE rag_chunks (
    id BIGSERIAL PRIMARY KEY,
    chunk_id TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    text TEXT NOT NULL,
    embedding VECTOR(1536),  -- OpenAI text-embedding-3-small dimension (1536)
    created_at TIMESTAMPTZ DEFAULT now()
);

-- =============================================================================
-- STEP 3: Create performance indexes for fast vector search
-- =============================================================================

-- Vector similarity index using IVFFlat algorithm (works with 1536 dimensions)
-- Using text-embedding-3-small (1536 dimensions) for compatibility
CREATE INDEX rag_chunks_vec_idx
    ON rag_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Regular B-tree indexes for filtering and sorting
CREATE INDEX rag_chunks_src_idx ON rag_chunks (source);
CREATE INDEX rag_chunks_chunk_id_idx ON rag_chunks (chunk_id);
CREATE INDEX rag_chunks_created_at_idx ON rag_chunks (created_at DESC);

-- =============================================================================
-- STEP 4: Create vector similarity search function
-- =============================================================================

CREATE OR REPLACE FUNCTION match_chunks (
  query_embedding vector(1536),
  match_count int DEFAULT 6,
  min_similarity float DEFAULT 0.0
)
RETURNS TABLE (
  chunk_id text,
  source text,
  text text,
  similarity float,
  created_at timestamptz
)
LANGUAGE plpgsql
AS $$
BEGIN
  RETURN QUERY
  SELECT
    rag_chunks.chunk_id,
    rag_chunks.source,
    rag_chunks.text,
    1 - (rag_chunks.embedding <=> query_embedding) as similarity,
    rag_chunks.created_at
  FROM rag_chunks
  WHERE 1 - (rag_chunks.embedding <=> query_embedding) >= min_similarity
  ORDER BY rag_chunks.embedding <=> query_embedding
  LIMIT match_count;
END;
$$;

-- =============================================================================
-- STEP 5: Create helper function to get database statistics
-- =============================================================================

CREATE OR REPLACE FUNCTION get_chunk_stats()
RETURNS TABLE (
  total_chunks bigint,
  unique_sources bigint,
  latest_chunk timestamptz
)
LANGUAGE plpgsql
AS $$
BEGIN
  RETURN QUERY
  SELECT
    COUNT(*) as total_chunks,
    COUNT(DISTINCT source) as unique_sources,
    MAX(created_at) as latest_chunk
  FROM rag_chunks;
END;
$$;

-- =============================================================================
-- STEP 6: Create Row Level Security (RLS) policies
-- =============================================================================

-- Enable RLS on the table
ALTER TABLE rag_chunks ENABLE ROW LEVEL SECURITY;

-- Allow all operations for service role (your backend)
CREATE POLICY "Allow service role full access" ON rag_chunks
  FOR ALL USING (auth.jwt() ->> 'role' = 'service_role');

-- Allow read access for authenticated users (future frontend)
CREATE POLICY "Allow authenticated read access" ON rag_chunks
  FOR SELECT USING (auth.role() = 'authenticated');

-- Allow anonymous read access for development (remove in production)
CREATE POLICY "Allow anonymous read access" ON rag_chunks
  FOR SELECT USING (true);

-- =============================================================================
-- STEP 7: Verification - Test that everything was created correctly
-- =============================================================================

-- Test 1: Check pgvector extension
SELECT 'pgvector extension installed' as test_result 
WHERE EXISTS (
  SELECT 1 FROM pg_extension WHERE extname = 'vector'
);

-- Test 2: Check table creation
SELECT 'rag_chunks table created' as test_result 
WHERE EXISTS (
  SELECT 1 FROM information_schema.tables 
  WHERE table_schema = 'public' AND table_name = 'rag_chunks'
);

-- Test 3: Check vector column dimensions
SELECT 
  'Vector column configured for text-embedding-3-small' as test_result,
  'VECTOR(1536) dimensions' as details
WHERE EXISTS (
  SELECT 1 FROM information_schema.columns 
  WHERE table_name = 'rag_chunks' 
  AND column_name = 'embedding'
);

-- Test 4: Check functions
SELECT 'match_chunks function created' as test_result 
WHERE EXISTS (
  SELECT 1 FROM information_schema.routines 
  WHERE routine_schema = 'public' AND routine_name = 'match_chunks'
);

SELECT 'get_chunk_stats function created' as test_result 
WHERE EXISTS (
  SELECT 1 FROM information_schema.routines 
  WHERE routine_schema = 'public' AND routine_name = 'get_chunk_stats'
);

-- Test 5: Check indexes
SELECT 'Vector index created' as test_result
WHERE EXISTS (
  SELECT 1 FROM pg_indexes 
  WHERE tablename = 'rag_chunks' AND indexname = 'rag_chunks_vec_idx'
);

-- Test 6: Show initial database stats (should be empty)
SELECT 
  'Database ready - ' || total_chunks::text || ' chunks' as test_result
FROM get_chunk_stats();

-- =============================================================================
-- SUCCESS MESSAGE
-- =============================================================================

SELECT '🎉 SUCCESS! Your Supabase database is ready for RAG!' as final_result;

-- =============================================================================
-- WHAT WAS CREATED:
-- =============================================================================
-- 
-- ✅ Extensions:
--    - pgvector (for vector operations)
-- 
-- ✅ Tables:
--    - rag_chunks (with VECTOR(1536) for text-embedding-3-small)
-- 
-- ✅ Indexes:
--    - IVFFlat vector index (optimized for 1536 dimensions)
--    - B-tree indexes for fast filtering
-- 
-- ✅ Functions:
--    - match_chunks() - vector similarity search
--    - get_chunk_stats() - database statistics
-- 
-- ✅ Security:
--    - Row Level Security enabled
--    - Policies for service role, authenticated users, and anonymous access
-- 
-- =============================================================================
-- NEXT STEPS:
-- =============================================================================
-- 
-- 1. Update your .env file with Supabase credentials:
--    SUPABASE_URL=https://your-project.supabase.co
--    SUPABASE_ANON_KEY=your_anon_key
--    SUPABASE_SERVICE_ROLE_KEY=your_service_key
--    OPENAI_API_KEY=your_openai_key
-- 
-- 2. Start your FastAPI backend:
--    uvicorn main:app --reload --port 8000
-- 
-- 3. Test the health check:
--    curl http://localhost:8000/healthz
-- 
-- 4. Seed your knowledge base:
--    curl -X POST http://localhost:8000/seed
-- 
-- 5. Ask your first question:
--    curl -X POST http://localhost:8000/answer \
--      -H "Content-Type: application/json" \
--      -d '{"query": "What is your return policy?"}'
-- 
-- 6. Visit interactive docs:
--    http://localhost:8000/docs
-- 
-- =============================================================================