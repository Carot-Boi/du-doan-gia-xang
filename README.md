# du-doan-gia-xang — dự đoán giá xăng dầu Việt Nam

Tự động, không dùng AI/LLM trong phần tính toán: dự đoán giá cơ sở cho chu
kỳ điều hành xăng dầu tiếp theo, dựa trên bản tin thật của MOIT (Bộ Công
Thương) và giá dầu thô thế giới thời gian thực.

**Live dashboard:** https://carot-boi.github.io/du-doan-gia-xang/

Bốn giai đoạn, mỗi giai đoạn build trên giai đoạn trước, không giai đoạn
nào thay thế giai đoạn cũ:

- **Phase 1** (dưới đây) — scraper + parser + SQLite: thu thập ground-truth
  từ moit.gov.vn. Không có logic dự đoán nào ở đây.
- **Phase 2** (`src/pricing/formula.py`, `src/pricing/calibrate.py`) —
  công thức "giá cơ sở" theo Nghị định 83/2014 + 95/2021 (Điều 38a), khớp
  lại chính xác các chu kỳ đã công bố (MAPE 1.57% trên dữ liệu sạch — xem
  `formula.py`'s module docstring).
- **Phase 3** (`scripts/predict_next_cycle.py`, `src/proxy/`,
  `src/pricing/bridge.py`) — dự đoán chu kỳ CHƯA công bố, dùng giá dầu thô
  Dubai thời gian thực (FRED) qua một cầu nối hồi quy đã khớp với lịch sử.
- **Phase 4** (`docs/`, `supabase/`, `scripts/sync_to_supabase.py`,
  `.github/workflows/`) — dashboard công khai + tự động hóa hàng tuần. Xem
  mục "Phase 4" ở cuối file này.

Phần B (phân tích tin tức bằng AI) được hoãn có chủ đích — xem lịch sử
trao đổi của dự án — không nằm trong phạm vi 4 phase này.

## What's here

```
src/
  scraper/
    http.py        rate-limited, robots.txt-respecting HTTP session
    discovery.py    Tier 1 (category listing API) + Tier 2 (brute-force Thursday prober) URL discovery
    fetch.py        fetch + archive raw HTML to data/raw_html/ before parsing
  parser/
    products.py     product code lookup tables (world-price / retail / BOG)
    bulletin_parser.py   parses one bulletin's HTML into structured records
  db/
    schema.py       SQLite schema
    store.py         insert/upsert helpers implementing the append-only rules
scripts/
  backfill.py       discover -> fetch -> parse -> store, with a summary report
tests/
  test_parser_20260709.py   regression test against a real, verified bulletin
  fixtures/bulletin_20260709.html
data/
  raw_html/          archived raw bulletin HTML + fetch metadata (gitignored)
  db/moit.sqlite3     the database (gitignored)
```

## How bulletin discovery works

**Tier 1 (primary):** MOIT's site runs a CMS (VHV) whose "Thị trường trong
nước" category listing page loads its content via an internal AJAX API
rather than real pagination — `?page=2` on the page itself returns HTTP 200
but silently ignores the parameter. The real endpoint, found by decoding a
base64-encoded widget config embedded in the page's HTML, is:

```
POST https://moit.gov.vn/api/Content/Article/selectAll
     categoryId=5238202&pageNo=<n>&itemsPerPage=100&orderBy=publishTime DESC&type=Article.News
```

This returns real, working pagination and lets us walk that category's
full ~985-item history quickly, filtering for URLs containing
`dieu-hanh-gia-xang-dau`.

**Tier 2 (fallback, `discover_via_brute_force`):** a clearly separate
function that generates candidate Thursday dates and probes both known
title-slug variants x zero-padded/non-padded day-month combinations,
rate-limited. Used only to gap-fill Thursdays Tier 1 didn't cover within
its own discovered date range. Kept intentionally separate so a report can
say plainly which tier found what — see the run summary printed by
`backfill.py`.

## Running the backfill

```bash
cd du-doan-gia-xang
source .venv/Scripts/activate   # Windows Git Bash; use .venv\Scripts\activate.bat on cmd
pip install -r requirements.txt  # first time only
python scripts/backfill.py
```

Options:
- `--limit N` — only process the N most recently discovered bulletins (useful for a quick smoke test)
- `--no-brute-force` — skip the Tier-2 gap fill and only use the category-listing API
- `--db PATH` — use a different SQLite file

The same discover -> fetch -> parse -> store pipeline is written to be
safely re-run (bulletins are keyed by URL) — this is meant to double as the
future "check for this week's new bulletin" job once that's scheduled in a
later phase, not a one-off script.

## Running the tests

```bash
python -m pytest tests/ -v
```

## Known data-quality issues the parser defends against

- **Mixed Unicode normalization within the same page.** Some paragraphs
  use precomposed Vietnamese accents (NFC), others use decomposed
  combining-mark sequences (NFD) — literal Vietnamese strings in Python
  source are NFC, so matching against un-normalized page text silently
  misses NFD passages. All extracted text is normalized to NFC before any
  matching.
- **Mixed decimal/thousands separators in the daily price table.** Some
  rows use `.` where the rest of the table uses `,` (and vice versa for
  the VCB exchange-rate columns). Both conventions place exactly 3 digits
  after the separator, so cell parsing strips the separator character and
  reconstructs the value from digit count alone, rather than assuming
  which convention a given row used.
- **Duplicate `TT` (row number) values.** Confirmed in real data (e.g. the
  same number appears for two different dates). `quote_date`, never `TT`,
  is the key.
- **Blank weekend/holiday rows** are normal (no Singapore market trading)
  and are simply skipped, not treated as errors.
- **Discontinued product columns** (e.g. "Dầu hỏa"/kerosene) going blank
  partway through a table are handled the same way.
- **Some bulletins embed a completely different table** (a BOG
  trích-lập/chi-sử-dụng *history* table, not the daily world-price table)
  as the first/only `<table>` on the page. The parser checks the header's
  shape (column count + "TT"/"Ngày" lead columns) before attempting
  positional parsing, and skips world-price extraction entirely rather
  than mis-mapping unrelated columns into wrong product codes.
- **The published cycle-average is reproducible, but not with the naive
  "strictly between both dates" rule.** Empirically verified against real
  data: averaging the non-blank daily quotes for
  `[prev_cycle_date, this_cycle_date)` — i.e. the *previous* cycle's own
  date is INCLUDED, the current cycle's date is excluded — reproduces
  MOIT's published average exactly (to 3 decimals) for every product
  checked. `validate_cycle_averages()` in `bulletin_parser.py` implements
  and cross-checks this; a mismatch is logged as a warning and stored with
  `validation_ok=0`, never raised as an error.

See `scripts/backfill.py`'s printed summary and this repo's actual
`data/db/moit.sqlite3` for real, current backfill results.

## Phase 4: public dashboard + weekly automation

```
supabase/
  schema.sql              Postgres schema (mirror of the SQLite tables + prediction_runs/predictions)
scripts/
  sync_to_supabase.py      mirrors the local SQLite DB + appends a new prediction snapshot
  send_alert_email.py      optional weekly email summary (Gmail SMTP + App Password)
docs/
  index.html               the public dashboard itself (no build step, plain HTML/CSS/JS)
.github/workflows/
  weekly-update.yml         Friday 09:00 VN time: backfill -> sync -> (optional) email
```

The local SQLite DB stays the pipeline's real source of truth (nothing
about Phases 1-3 changed). `sync_to_supabase.py` pushes a read-only mirror
of it to a public Supabase project after every backfill run, which
`docs/index.html` reads directly via the Supabase JS client using the
public anon key — safe to embed client-side because Row Level Security
(see `supabase/schema.sql`) restricts that key to SELECT-only on every
table. All writes go through the DB connection string, kept only as the
`SUPABASE_DB_URL` GitHub Actions secret.

Setup for a fresh clone (only needed once, already done for this repo):

```bash
# 1. Run supabase/schema.sql once against a new Supabase project (SQL Editor -> paste -> Run)
# 2. Set the DB connection string locally and as a repo secret:
echo "SUPABASE_DB_URL=postgresql://...pooler.supabase.com:5432/postgres" > .env
gh secret set SUPABASE_DB_URL --body "postgresql://..."
# 3. First sync:
python scripts/sync_to_supabase.py
# 4. Point docs/index.html's SUPABASE_URL / SUPABASE_ANON_KEY constants at the same project
#    (Project Settings -> API), then enable GitHub Pages from /docs.
```

If your network can't resolve Supabase's direct-connection host (IPv6
only, common on IPv4-only ISPs/CI runners), use the **Session pooler**
connection string instead (Connect -> Direct Connection string ->
Connection Method -> Session pooler) — confirmed necessary and working
for this project's own setup.

Email alerts are optional and no-op cleanly if `ALERT_EMAIL_FROM` /
`ALERT_EMAIL_APP_PASSWORD` / `ALERT_EMAIL_TO` aren't set as repo secrets
(see `.env.example`).
