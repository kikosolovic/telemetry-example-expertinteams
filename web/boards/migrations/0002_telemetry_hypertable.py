"""Telemetry hypertable (SCRUM-154/187), owned by Django migrations but written
only by `realtime` via psycopg.

Design notes:
  * `ts` is the *board's* timestamp exactly as it was sent (t_us). It must be
    deterministic per sample: if the board resends a sample after a Wi-Fi
    drop, the row must be byte-identical so the unique index catches it. A
    clock-corrected time would change whenever the offset estimate changes,
    and the duplicate would slip through. The correction applied is stored
    alongside in `clock_offset_us` (corrected time = ts - offset).
  * TimescaleDB requires every unique index on a hypertable to include the
    time column, hence (device_id, boot_id, seq, ts) rather than just
    (device_id, boot_id, seq). Inserts use ON CONFLICT DO NOTHING -> resends
    are harmless, which is what makes "resend everything after the last ACK"
    safe.
  * No names or emails anywhere -- just session/device ids (SCRUM-154).
"""

from django.db import migrations

CREATE = """
CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE telemetry_sample (
    ts              timestamptz NOT NULL,   -- board time (t_us), see above
    clock_offset_us bigint      NOT NULL,   -- board minus server, at ingest
    session_id      bigint      NOT NULL,
    device_id       integer     NOT NULL,
    boot_id         bigint      NOT NULL,   -- u32 doesn't fit a signed int4
    seq             bigint      NOT NULL,
    lc_fl real NOT NULL, lc_fr real NOT NULL, lc_bl real NOT NULL, lc_br real NOT NULL,
    roll            real        NOT NULL,   -- degrees
    pitch           real        NOT NULL,
    actuator        smallint    NOT NULL,
    mode            smallint    NOT NULL,
    flags           integer     NOT NULL
);

SELECT create_hypertable('telemetry_sample', by_range('ts', INTERVAL '1 day'));

CREATE UNIQUE INDEX telemetry_sample_dedup
    ON telemetry_sample (device_id, boot_id, seq, ts);
CREATE INDEX telemetry_sample_session
    ON telemetry_sample (session_id, ts DESC);
"""

DROP = "DROP TABLE IF EXISTS telemetry_sample;"


class Migration(migrations.Migration):
    dependencies = [("boards", "0001_initial")]
    operations = [migrations.RunSQL(CREATE, DROP)]
