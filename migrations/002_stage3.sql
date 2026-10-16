-- 可变维度只用于精确检索，各索引仍由契约及触发器强制维度隔离。
ALTER TABLE knowledge_chunks ALTER COLUMN embedding TYPE vector USING embedding::vector;
CREATE TABLE index_config_versions (
    id text NOT NULL,
    version text NOT NULL,
    fingerprint text NOT NULL,
    configuration jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(id,version)
);
CREATE FUNCTION validate_chunk_contract() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE dims integer; parent knowledge_chunks; document knowledge_documents;
BEGIN
    SELECT (embedding_contract->>'dimensions')::integer INTO dims
    FROM knowledge_indexes WHERE id=NEW.index_id;
    IF NEW.role='child' AND vector_dims(NEW.embedding) IS DISTINCT FROM dims THEN
        RAISE EXCEPTION 'embedding_dimension_incompatible';
    END IF;
    SELECT * INTO document FROM knowledge_documents WHERE document_key=NEW.document_key;
    IF NEW.source_start < 0 OR NEW.source_end <= NEW.source_start OR
       substring(document.source_text FROM NEW.source_start+1 FOR NEW.source_end-NEW.source_start) <> NEW.text THEN
        RAISE EXCEPTION 'source_range_incompatible';
    END IF;
    IF NEW.role='child' THEN
        SELECT * INTO parent FROM knowledge_chunks WHERE index_id=NEW.index_id AND id=NEW.parent_id;
        IF parent.id IS NULL OR parent.role<>'parent' OR parent.document_key<>NEW.document_key OR
           NEW.source_start < parent.source_start OR NEW.source_end > parent.source_end THEN
            RAISE EXCEPTION 'parent_relation_incompatible';
        END IF;
    ELSIF NEW.parent_id IS NOT NULL THEN
        RAISE EXCEPTION 'parent_relation_incompatible';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER chunk_contract BEFORE INSERT OR UPDATE ON knowledge_chunks
FOR EACH ROW EXECUTE FUNCTION validate_chunk_contract();
