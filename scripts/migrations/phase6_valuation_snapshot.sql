-- Phase 6 — Numerical integrity hard mode.
-- Run in the Supabase SQL Editor (never the pooler) for both T212 projects.
--
-- Adds two JSON columns on `theses` that persist the snapshot of authoritative
-- ratios (from get_fundamentals) and the peer comparison (from get_peer_metrics)
-- at thesis-creation time.  These are rendered by the dashboard as the primary
-- numerical signal; the prose fields become qualitative interpretation only.
--
-- Also adds a `macro_driver` string tag on `themes` (e.g. "ai_capex", "glp1",
-- "nuclear_revival", "cybersecurity") used by the conviction hard cap that
-- prevents > 3 active conviction-4+ theses sharing the same macro driver.

ALTER TABLE theses
    ADD COLUMN IF NOT EXISTS valuation_snapshot JSON,
    ADD COLUMN IF NOT EXISTS peer_snapshot      JSON;

ALTER TABLE themes
    ADD COLUMN IF NOT EXISTS macro_driver       VARCHAR(64);
