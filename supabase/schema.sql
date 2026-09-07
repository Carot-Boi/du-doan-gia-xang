-- Postgres schema for the Supabase project backing the public dashboard.
--
-- This is a DERIVED copy of src/db/schema.py's SQLite schema, not a
-- replacement for it: the scraper/parser/backfill pipeline (Phases 1-3)
-- keeps running against local SQLite as its own source of truth, exactly
-- as before. scripts/sync_to_supabase.py pushes the relevant tables here
-- AFTER a local backfill run, so this DB is a read replica for the public
-- dashboard + a home for forward-looking prediction snapshots (which have
-- no equivalent table in SQLite, since predict_next_cycle.py only used to
-- print to stdout).
--
-- Security model: the anon/public API key is meant to be embedded in the
-- static dashboard's client-side JS (docs/index.html) -- that is normal
-- Supabase usage, NOT a leaked secret, because Row Level Security below
-- restricts the anon role to read-only (SELECT) on every table. All
-- writes come from scripts/sync_to_supabase.py using the service_role key,
-- which is kept ONLY as a GitHub Actions secret and never shipped to the
-- browser.
--
-- Run this once against a fresh Supabase project (SQL Editor -> paste ->
-- Run). Safe to re-run: every statement is IF NOT EXISTS / OR REPLACE.

create table if not exists bulletins (
    id integer primary key,
    url text not null unique,
    bulletin_date date,
    effective_at timestamptz,
    fetch_timestamp timestamptz not null,
    http_status integer,
    title text,
    title_variant text,
    parse_status text not null,
    parse_warnings text,
    discovery_method text
);

create table if not exists world_price_daily (
    id bigint generated always as identity primary key,
    product_code text not null,
    quote_date date not null,
    price double precision not null,
    unit text not null,
    source_bulletin_id integer not null references bulletins(id),
    conflicts_with_prior boolean not null default false
);
create index if not exists idx_world_price_daily_product_date
    on world_price_daily(product_code, quote_date);

create table if not exists cycle_summary (
    id bigint generated always as identity primary key,
    bulletin_id integer not null references bulletins(id),
    product_code text not null,
    prev_cycle_date date,
    this_cycle_date date,
    avg_price_published double precision,
    delta_published double precision,
    pct_change_published double precision,
    computed_avg double precision,
    validation_ok boolean,
    validation_note text
);

create table if not exists retail_prices (
    id bigint generated always as identity primary key,
    bulletin_id integer not null references bulletins(id),
    product_code text not null,
    price_vnd integer,
    delta_vnd integer,
    unit text,
    effective_at timestamptz
);
create index if not exists idx_retail_prices_product_effective
    on retail_prices(product_code, effective_at);

create table if not exists bog_actions (
    id bigint generated always as identity primary key,
    bulletin_id integer not null references bulletins(id),
    product_code text not null,
    trich_lap_vnd integer,
    chi_su_dung_vnd integer,
    unit text
);

-- Forward-looking prediction snapshots. NOT present in the SQLite schema --
-- predict_next_cycle.py only printed to stdout before Phase 4. Every
-- weekly automation run inserts ONE new prediction_runs row + one
-- predictions row per product, so the dashboard can show how a given
-- cycle's prediction evolved as it got closer (nowcast % rising toward
-- 100%) instead of only ever showing the latest guess.
create table if not exists prediction_runs (
    id bigint generated always as identity primary key,
    run_at timestamptz not null default now(),
    today date not null,
    last_cycle_date date not null,
    cycle_end_assumed date not null,
    fx_rate double precision not null,
    fx_source text not null
);

create table if not exists predictions (
    id bigint generated always as identity primary key,
    run_id bigint not null references prediction_runs(id) on delete cascade,
    retail_product_code text not null,
    world_product_code text not null,
    predicted_vnd double precision not null,
    low_vnd double precision not null,
    high_vnd double precision not null,
    known_days integer not null,
    nowcast_days integer not null,
    forecast_days integer not null,
    window_days_total integer not null,
    bog_net_vnd double precision not null,
    lumped_residual_vnd double precision not null,
    world_price_avg double precision not null,
    unit text not null,
    known_bias_caveat boolean not null default false
);
create index if not exists idx_predictions_run on predictions(run_id);
create index if not exists idx_predictions_product on predictions(retail_product_code);

-- Row Level Security: anon (the public dashboard) may only ever SELECT.
-- All writes go through the service_role key (scripts/sync_to_supabase.py),
-- which bypasses RLS entirely, so no "insert/update" policy is needed for
-- any role.
alter table bulletins enable row level security;
alter table world_price_daily enable row level security;
alter table cycle_summary enable row level security;
alter table retail_prices enable row level security;
alter table bog_actions enable row level security;
alter table prediction_runs enable row level security;
alter table predictions enable row level security;

drop policy if exists "public read" on bulletins;
create policy "public read" on bulletins for select using (true);
drop policy if exists "public read" on world_price_daily;
create policy "public read" on world_price_daily for select using (true);
drop policy if exists "public read" on cycle_summary;
create policy "public read" on cycle_summary for select using (true);
drop policy if exists "public read" on retail_prices;
create policy "public read" on retail_prices for select using (true);
drop policy if exists "public read" on bog_actions;
create policy "public read" on bog_actions for select using (true);
drop policy if exists "public read" on prediction_runs;
create policy "public read" on prediction_runs for select using (true);
drop policy if exists "public read" on predictions;
create policy "public read" on predictions for select using (true);
