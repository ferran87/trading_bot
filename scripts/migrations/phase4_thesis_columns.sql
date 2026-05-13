-- Phase 4 migration: theme linkage + analyst-frame fields on theses table.
--
-- Run this DDL in the Supabase SQL Editor (project: mfrngzrzwxuygfyjektg).
-- Per project memory: ALTER TABLE via the pooler DATABASE_URL times out
-- (QueryCanceled), so we never run ALTER via psycopg2. Always Supabase SQL Editor.
--
-- The new tables (themes, theme_stock_proposals) are created automatically by
-- Base.metadata.create_all() on next engine startup — no manual SQL needed
-- for those. Only the existing theses table needs ALTER.

ALTER TABLE theses ADD COLUMN IF NOT EXISTS theme_id INTEGER;
ALTER TABLE theses ADD COLUMN IF NOT EXISTS positioning_vs_theme TEXT;
ALTER TABLE theses ADD COLUMN IF NOT EXISTS execution_evidence TEXT;
ALTER TABLE theses ADD COLUMN IF NOT EXISTS valuation_assessment TEXT;

CREATE INDEX IF NOT EXISTS ix_theses_theme_id ON theses (theme_id);

-- The FK constraint targets themes.id which only exists after create_all()
-- has run at least once (i.e. after the app boots on the feature branch).
-- This statement is safe to re-run.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'themes')
       AND NOT EXISTS (
           SELECT 1 FROM information_schema.table_constraints
           WHERE table_name = 'theses' AND constraint_name = 'fk_theses_theme_id'
       )
    THEN
        ALTER TABLE theses
            ADD CONSTRAINT fk_theses_theme_id
            FOREIGN KEY (theme_id) REFERENCES themes(id);
    END IF;
END $$;
