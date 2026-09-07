"""
Phase 4: send a plain-text email summarizing the latest prediction snapshot
in Supabase (written by scripts/sync_to_supabase.py, which must run first).

Deliberately reads the LATEST prediction_runs row back OUT of Postgres
rather than being handed the run object in-process by sync_to_supabase.py:
keeps this script runnable/testable on its own, and means the email always
reflects exactly what the dashboard is showing, not a possibly-different
in-memory value.

Uses Gmail SMTP with an App Password (see .env.example for how to generate
one) -- no email-provider account signup beyond the Gmail account the user
already has. All three ALERT_EMAIL_* variables must be set or this script
exits 0 without sending anything (email alerts are an optional add-on, not
a hard requirement for the rest of Phase 4 to work).

Usage:
    python scripts/send_alert_email.py
"""

from __future__ import annotations

import os
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2
import psycopg2.extras

# Same bias list as sync_to_supabase.py -- see formula.py's module
# docstring for why these two products are called out.
KNOWN_BIAS_PRODUCTS = {"E5RON92", "E10RON95III"}


def fetch_latest_run(pg_conn) -> dict | None:
    with pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM prediction_runs ORDER BY run_at DESC LIMIT 1"
        )
        run = cur.fetchone()
        if run is None:
            return None
        cur.execute(
            "SELECT * FROM predictions WHERE run_id = %s ORDER BY retail_product_code",
            (run["id"],),
        )
        run = dict(run)
        run["predictions"] = [dict(r) for r in cur.fetchall()]
        return run


def format_email_body(run: dict) -> str:
    lines = [
        f"Du doan gia co so cho chu ky ke tiep (tinh den {run['today']})",
        f"Chu ky thuc gan nhat: {run['last_cycle_date']}",
        f"Gia dinh ngay ket thuc chu ky moi: {run['cycle_end_assumed']}",
        f"Ty gia gia dinh: {run['fx_rate']:,.1f} VND/USD ({run['fx_source']})",
        "",
    ]
    for p in run["predictions"]:
        lines.append(f"{p['retail_product_code']} ({p['unit']})")
        lines.append(
            f"  du doan: {p['predicted_vnd']:,.0f}   khoang: "
            f"[{p['low_vnd']:,.0f} .. {p['high_vnd']:,.0f}]"
        )
        pct_known = 100.0 * p["known_days"] / p["window_days_total"] if p["window_days_total"] else 0.0
        lines.append(f"  % ngay da biet thuc te: {pct_known:.0f}%")
        if p["retail_product_code"] in KNOWN_BIAS_PRODUCTS:
            lines.append(
                "  CANH BAO: san pham nay thuong du doan THAP hon gia that ~4-5%"
                " (xem README/formula.py) -- coi day la muc san, khong phai muc du kien."
            )
        lines.append("")
    lines.append("Xem chi tiet va lich su tren dashboard.")
    return "\n".join(lines)


def main() -> None:
    email_from = os.environ.get("ALERT_EMAIL_FROM")
    app_password = os.environ.get("ALERT_EMAIL_APP_PASSWORD")
    email_to = os.environ.get("ALERT_EMAIL_TO")
    db_url = os.environ.get("SUPABASE_DB_URL")

    if not (email_from and app_password and email_to):
        print("Email alert not configured (ALERT_EMAIL_* not set) -- skipping, this is optional.")
        return
    if not db_url:
        print("FATAL: SUPABASE_DB_URL is not set.")
        sys.exit(1)

    pg_conn = psycopg2.connect(db_url)
    try:
        run = fetch_latest_run(pg_conn)
    finally:
        pg_conn.close()

    if run is None:
        print("No prediction_runs rows in Supabase yet -- nothing to email.")
        return

    body = format_email_body(run)
    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = f"[Du doan gia xang] Cap nhat den {run['today']}"
    msg["From"] = email_from
    msg["To"] = email_to

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(email_from, app_password)
        smtp.sendmail(email_from, [email_to], msg.as_string())
    print(f"Email sent to {email_to}.")


if __name__ == "__main__":
    main()
