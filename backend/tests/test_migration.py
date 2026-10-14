import os
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from app.models import Agent, Organization, Trace, TracePolicy
from app.config import settings
from app.database import Base

BACKEND = Path(__file__).resolve().parents[1]
TEST_DB = "agentops_migration_test"
NEW_TABLES = {"trace_policies", "policy_violations"}


def _url(host: str, database: str) -> sa.engine.URL:
    return sa.engine.URL.create(
        "postgresql+psycopg2", username=settings.POSTGRES_USER, password=settings.POSTGRES_PASSWORD,
        host=host, port=settings.POSTGRES_PORT, database=database,
    )


@pytest.fixture
def pg(monkeypatch):
    host = os.environ.get("POSTGRES_HOST", "localhost")
    admin = sa.create_engine(_url(host, settings.POSTGRES_DB), isolation_level="AUTOCOMMIT",
                             connect_args={"connect_timeout": 2})
    try:
        with admin.connect() as c:
            c.execute(sa.text(f"DROP DATABASE IF EXISTS {TEST_DB}"))
            c.execute(sa.text(f"CREATE DATABASE {TEST_DB}"))
    except sa.exc.OperationalError as e:
        admin.dispose()
        pytest.skip(f"postgres not reachable at {host}: {e.orig}")
    monkeypatch.setattr(settings, "POSTGRES_HOST", host)
    monkeypatch.setattr(settings, "POSTGRES_DB", TEST_DB)
    eng = sa.create_engine(_url(host, TEST_DB))
    yield eng
    eng.dispose()
    with admin.connect() as c:
        c.execute(sa.text(f"DROP DATABASE IF EXISTS {TEST_DB}"))
    admin.dispose()


def _seqs(conn, trace_id):
    return conn.execute(sa.text(
        "SELECT name, seq FROM spans WHERE trace_id = :t ORDER BY seq"), {"t": trace_id}).all()


def test_migration_0002_roundtrip(pg):
    cfg = Config()
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))

    command.upgrade(cfg, "0001_init")
    with pg.begin() as c:
        c.execute(sa.text("INSERT INTO organizations (id, name, slug) VALUES ('o1', 'O', 'o')"))
        c.execute(sa.text("INSERT INTO agents (id, org_id, name) VALUES ('a1', 'o1', 'A')"))
        for t in ("t1", "t2"):
            c.execute(sa.text("INSERT INTO traces (id, org_id, agent_id, trace_uid) VALUES (:t, 'o1', 'a1', :t)"),
                      {"t": t})
        spans = [  # id order differs from started_at order on purpose
            ("s1", "t1", "third", datetime(2026, 1, 1, 0, 0, 3)),
            ("s2", "t1", "first", datetime(2026, 1, 1, 0, 0, 1)),
            ("s3", "t1", "second", datetime(2026, 1, 1, 0, 0, 2)),
            ("s5", "t2", "tie_b", datetime(2026, 1, 1)),
            ("s4", "t2", "tie_a", datetime(2026, 1, 1)),
        ]
        for sid, tid, name, ts in spans:
            c.execute(sa.text("INSERT INTO spans (id, trace_id, name, started_at) VALUES (:i, :t, :n, :s)"),
                      {"i": sid, "t": tid, "n": name, "s": ts})

    command.upgrade(cfg, "0002_trace_policies")
    with pg.begin() as c:  # rows written before 0003 may carry NULLs
        c.execute(sa.text("INSERT INTO trace_policies (id, org_id, name, source, severity, created_at)"
                          " VALUES ('p0', 'o1', 'P0', 'x', NULL, NULL)"))
        c.execute(sa.text(
            "INSERT INTO policy_violations (id, org_id, trace_id, agent_id, policy_id, policy_name, policy_version,"
            " severity, at_seq, mode, detected_at)"
            " VALUES ('v0', 'o1', 't2', 'a1', 'p0', 'P0', 1, 'low', 0, 'live', NULL)"))

    command.upgrade(cfg, "head")
    with pg.connect() as c:
        p0 = c.execute(sa.text("SELECT severity, action, created_at FROM trace_policies WHERE id = 'p0'")).one()
        v0 = c.execute(sa.text(
            "SELECT counterexample, message, detected_at, counterexample_status FROM policy_violations")).one()
        assert p0[:2] == ("medium", "log") and p0[2] and v0[:2] == ([], "") and v0[2] and v0[3] == "none"
        assert _seqs(c, "t1") == [("first", 0), ("second", 1), ("third", 2)]
        assert _seqs(c, "t2") == [("tie_a", 0), ("tie_b", 1)]
        assert c.execute(sa.text("SELECT version_num FROM alembic_version")).scalar() == "0006_counterexample_status"
    insp = sa.inspect(pg)
    assert NEW_TABLES <= set(insp.get_table_names())
    span_cols = {col["name"]: col for col in insp.get_columns("spans")}
    assert span_cols["seq"]["nullable"] and span_cols["attrs"]["nullable"]
    vcols = {col["name"]: col for col in insp.get_columns("policy_violations")}
    assert not any(vcols[c]["nullable"] for c in ("org_id", "trace_id", "agent_id", "policy_id", "policy_name"))
    idx = {i["name"]: i for t in ("spans", "trace_policies", "policy_violations") for i in insp.get_indexes(t)}
    assert idx["ix_span_trace_seq"]["column_names"] == ["trace_id", "seq"] and idx["ix_span_trace_seq"]["unique"]
    assert idx["ix_trace_policy_org_name"]["column_names"] == ["org_id", "name"]
    assert idx["ix_trace_policy_org_name"]["unique"]
    assert idx["ix_violation_org_detected"]["column_names"] == ["org_id", "detected_at"]
    assert idx["ix_violation_trace_policy_version"]["column_names"] == ["trace_id", "policy_id", "policy_version"]
    assert idx["ix_violation_trace_policy_version"]["unique"]

    with pg.connect() as c:
        diff = compare_metadata(MigrationContext.configure(c), Base.metadata)
    touched = NEW_TABLES | {"spans", "alert_rules"}
    structural = [d for d in diff if not isinstance(d, list) and _diff_table(d) in touched]
    assert structural == [], structural
    ours = lambda t, col: (t in NEW_TABLES or (t == "spans" and col in ("seq", "attrs"))
                           or (t, col) == ("alert_rules", "context"))
    nullable = [m[2:4] for d in diff if isinstance(d, list) for m in d if m[0] == "modify_nullable" and ours(*m[2:4])]
    assert nullable == [], nullable

    with pg.begin() as c, pytest.raises(sa.exc.IntegrityError):
        c.execute(sa.text("UPDATE spans SET seq = 0 WHERE id = 's3'"))

    with pg.begin() as c:
        c.execute(sa.text("INSERT INTO trace_policies (id, org_id, name, source) VALUES ('p1', 'o1', 'P', 'x')"))
        c.execute(sa.text(
            "INSERT INTO policy_violations (id, org_id, trace_id, agent_id, policy_id, policy_name, policy_version,"
            " severity, at_seq, mode) VALUES ('v1', 'o1', 't1', 'a1', 'p1', 'P', 1, 'high', 0, 'batch')"))
        p1 = c.execute(sa.text("SELECT severity, action FROM trace_policies WHERE id = 'p1'")).one()
        assert p1 == ("medium", "log")
        ce, msg, det, st = c.execute(sa.text("SELECT counterexample, message, detected_at, counterexample_status"
                                             " FROM policy_violations WHERE id = 'v1'")).one()
        assert (ce, msg, st) == ([], "", "minimal") and det is not None

    with pg.begin() as c:
        c.execute(sa.text("INSERT INTO alert_rules (id, org_id, name, metric, operator, threshold)"
                          " VALUES ('r1', 'o1', 'R', 'policy_violations', '>=', 1)"))
        assert c.execute(sa.text("SELECT context FROM alert_rules WHERE id = 'r1'")).scalar() == {}

    _check_archive(pg)

    command.downgrade(cfg, "0004_alert_rule_context")
    _check_unarchived(pg)
    with pg.connect() as c:
        assert c.execute(sa.text("SELECT id FROM policy_violations ORDER BY id")).scalars().all() == ["v0", "v1"]
    command.downgrade(cfg, "0003_policy_not_null")
    assert "context" not in {col["name"] for col in sa.inspect(pg).get_columns("alert_rules")}
    command.downgrade(cfg, "0001_init")
    insp = sa.inspect(pg)
    assert not NEW_TABLES & set(insp.get_table_names())
    assert {"seq", "attrs"}.isdisjoint(col["name"] for col in insp.get_columns("spans"))
    assert "ix_span_trace_seq" not in {i["name"] for i in insp.get_indexes("spans")}

    command.upgrade(cfg, "head")
    with pg.connect() as c:
        assert _seqs(c, "t1") == [("first", 0), ("second", 1), ("third", 2)]
    assert NEW_TABLES <= set(sa.inspect(pg).get_table_names())


def _check_archive(eng):
    """At 0005: archived policies may share a name with each other and with one live policy."""
    policy = lambda pid, archived_at=None: TracePolicy.__table__.insert().values(
        id=pid, org_id="o1", name="Q", source="x", archived_at=archived_at)
    with eng.begin() as c:
        c.execute(policy("p2", datetime(2026, 1, 1)))
        c.execute(policy("p3", datetime(2026, 1, 2)))
        c.execute(policy("p4"))
    with eng.begin() as c, pytest.raises(sa.exc.IntegrityError):
        c.execute(policy("p5"))


def _check_unarchived(eng):
    """After downgrading 0005: archived policies are kept, disabled and renamed to their ids."""
    with eng.connect() as c:
        rows = c.execute(sa.text("SELECT id, name, enabled FROM trace_policies WHERE name = 'Q' OR id IN ('p2', 'p3')"
                                 " ORDER BY id")).all()
    assert [tuple(r) for r in rows] == [("p2", "p2", False), ("p3", "p3", False), ("p4", "Q", True)]


def _diff_table(d):
    for part in d[1:]:
        if isinstance(part, sa.Table):
            return part.name
        if isinstance(part, (sa.Index, sa.UniqueConstraint, sa.ForeignKeyConstraint)):
            return part.table.name
        if isinstance(part, sa.Column):
            return part.table.name
    for part in d[1:]:
        if isinstance(part, str):
            return part
    return None


def test_migration_0005_on_sqlite(tmp_path, monkeypatch):
    """The app migrates Postgres only and 0001-0004 are Postgres-only; 0005 runs both ways on SQLite too."""
    url = f"sqlite:///{tmp_path}/m.db"
    monkeypatch.setattr(type(settings), "db_url_sync", property(lambda _: url))
    eng = sa.create_engine(url)
    Base.metadata.create_all(eng)
    cfg = Config()
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    command.stamp(cfg, "head")
    command.downgrade(cfg, "0004_alert_rule_context")
    assert "archived_at" not in {col["name"] for col in sa.inspect(eng).get_columns("trace_policies")}
    with eng.begin() as c:
        c.execute(Organization.__table__.insert().values(id="o1", name="O", slug="o"))
        c.execute(sa.text("INSERT INTO trace_policies (id, org_id, name, source, severity, action, enabled, version,"
                          " created_at, updated_at) VALUES ('p1', 'o1', 'P', 'x', 'low', 'log', 1, 1, :t, :t)"),
                  {"t": datetime(2026, 1, 1)})
    command.upgrade(cfg, "head")
    _check_archive(eng)
    command.downgrade(cfg, "0004_alert_rule_context")
    _check_unarchived(eng)
    eng.dispose()


def test_migration_0006_on_sqlite(tmp_path, monkeypatch):
    """Empty live counterexamples are pending on open streams and none on closed ones; the rest stay minimal."""
    url = f"sqlite:///{tmp_path}/m.db"
    monkeypatch.setattr(type(settings), "db_url_sync", property(lambda _: url))
    eng = sa.create_engine(url)
    Base.metadata.create_all(eng)
    cfg = Config()
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    command.stamp(cfg, "head")
    command.downgrade(cfg, "0005_policy_archive")
    assert "counterexample_status" not in {col["name"] for col in sa.inspect(eng).get_columns("policy_violations")}
    with eng.begin() as c:
        c.execute(Organization.__table__.insert().values(id="o1", name="O", slug="o"))
        c.execute(Agent.__table__.insert().values(id="a1", org_id="o1", name="A"))
        for tid, meta in (("t1", {"stream": "open"}), ("t2", {"stream": "final"}), ("t3", None)):
            c.execute(Trace.__table__.insert().values(id=tid, org_id="o1", agent_id="a1", trace_uid=tid, meta=meta))
        for vid, tid, cx, mode in (("v1", "t1", "[]", "live"), ("v2", "t1", "[0]", "live"), ("v3", "t1", "[]", "batch"),
                                   ("v4", "t2", "[]", "live"), ("v5", "t3", "[]", "live")):
            pid = "p" + vid
            c.execute(TracePolicy.__table__.insert().values(id=pid, org_id="o1", name=pid, source="x"))
            c.execute(sa.text(
                "INSERT INTO policy_violations (id, org_id, trace_id, agent_id, policy_id, policy_name, policy_version,"
                " severity, at_seq, counterexample, message, mode, detected_at)"
                " VALUES (:v, 'o1', :tr, 'a1', :p, 'P', 1, 'low', 0, :cx, '', :m, :t)"),
                {"v": vid, "tr": tid, "p": pid, "cx": cx, "m": mode, "t": datetime(2026, 1, 1)})
    command.upgrade(cfg, "head")
    with eng.connect() as c:
        rows = c.execute(sa.text("SELECT id, counterexample_status FROM policy_violations ORDER BY id")).all()
    assert [tuple(r) for r in rows] == [
        ("v1", "pending"), ("v2", "minimal"), ("v3", "minimal"), ("v4", "none"), ("v5", "none")]
    command.downgrade(cfg, "0005_policy_archive")
    assert "counterexample_status" not in {col["name"] for col in sa.inspect(eng).get_columns("policy_violations")}
    eng.dispose()
