"""D1 (Recovery Release A): the complete, expected trigger set on the evidence and booking-commitment tables.

The AURORA gate used to require exactly two triggers on deployment_evidence (0013/0014). Migrations 0031 and 0032 add
two more there, and 0026/0030/0031 put guards on four other tables. This module is the one place that lists every
trigger those tables must have: its function, its timing/level/events (pg_trigger.tgtype) and that it is enabled.
`trigger_problems` reports a missing, disabled, unexpected or misconfigured trigger, each on its own line.
tests/test_recovery_release_a.py keeps this list equal to a freshly migrated database, so a new migration that adds or
changes one of these triggers must update it here (and the AURORA gate then checks it live)."""
from __future__ import annotations

# tgtype bits: 1 ROW, 2 BEFORE, 4 INSERT, 8 DELETE, 16 UPDATE, 32 TRUNCATE (64 INSTEAD)
EXPECTED: dict[str, dict[str, tuple[str, int]]] = {
    "deployment_evidence": {
        "aa_evidence_requires_guard": ("trg_evidence_requires_guard", 7),          # 0031: BEFORE INSERT ROW
        "ab_provisional_consumption": ("trg_provisional_consumption", 7),          # 0032: BEFORE INSERT ROW
        "deployment_evidence_append_only": ("trg_deployment_evidence_append_only", 58),  # 0013: BEFORE UPD/DEL/TRUNC
        "deployment_evidence_fresh": ("trg_deployment_evidence_fresh", 7),         # 0014: BEFORE INSERT ROW
    },
    "deployment_manifests": {
        "aa_evidence_requires_guard": ("trg_evidence_requires_guard", 7),
        "deployment_manifests_append_only": ("trg_activation_append_only", 58),
        "deployment_manifests_lock": ("trg_manifest_waits_for_commitments", 7),
    },
    "deployment_exceptions": {
        "aa_evidence_requires_guard": ("trg_evidence_requires_guard", 7),
        "deployment_exceptions_append_only": ("trg_activation_append_only", 58),
        "deployment_exceptions_clock": ("trg_deployment_exception_clock", 7),
    },
    "dock_occupancy": {
        "aa_booking_kill_switch": ("trg_refuse_booking_when_killed", 23),         # 0026: BEFORE INS/UPD ROW, first
        "ab_booking_evidence_guard": ("trg_require_booking_evidence", 23),        # 0030: second
        "claim_approval_consistent": ("trg_check_claim_approval", 17),
        "no_show_blocks_hold": ("trg_no_show_blocks_booking", 7),                 # 0048 (D3): BEFORE INSERT ROW
        "occupancy_compat": ("trg_occupancy_compat", 23),
        "occupancy_version": ("trg_bump_schedule_version", 29),
    },
    "appointments": {
        "aa_booking_kill_switch": ("trg_refuse_booking_when_killed", 23),
        "ab_booking_evidence_guard": ("trg_require_booking_evidence", 23),
        "appointment_approval_consistent": ("trg_check_appointment_approval", 21),
        "supersede_on_new_booking": ("trg_supersede_on_new_booking", 21),
    },
}

# Triggers a migration of THIS release introduces, by migration number. The AURORA gate runs before the release's
# provisional activation, and the activation is what applies the release's migrations (never by hand), so the gate
# must judge such a trigger against the migration ledger (setuhaul_meta.schema_migrations):
#   * migration recorded as applied  -> the trigger is required exactly as listed above (missing is a problem);
#   * migration not yet applied      -> the pre-migration state is required: the trigger must be ABSENT (a trigger
#                                       present before its migration is recorded means the schema was changed by hand).
# Every other trigger is required unconditionally. Without a ledger (applied=None) everything is required.
INTRODUCED_BY: dict[tuple[str, str], str] = {
    ("dock_occupancy", "no_show_blocks_hold"): "0048",                    # 27 Nov fix plan (D3)
}


def expected_for(applied: set[str] | None) -> dict[str, dict[str, tuple[str, int]]]:
    """The trigger set required now: EXPECTED minus the triggers of migrations not yet recorded as applied."""
    if applied is None:
        return EXPECTED
    return {t: {n: v for n, v in want.items() if INTRODUCED_BY.get((t, n), None) in (None, *applied)}
            for t, want in EXPECTED.items()}


def pending_triggers(applied: set[str] | None) -> list[str]:
    """The triggers the gate expects to be absent because their migration is not applied yet (reported as evidence)."""
    if applied is None:
        return []
    return sorted(f"{t}.{n} (migration {m} not yet applied)" for (t, n), m in INTRODUCED_BY.items() if m not in applied)


# The SQL the AURORA gate runs (as the owner): every non-internal trigger on these tables.
QUERY = """SELECT c.relname::text, t.tgname::text, t.tgenabled::text, p.proname::text, t.tgtype::int
             FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
             JOIN pg_proc p ON p.oid = t.tgfoid
            WHERE n.nspname = 'setuhaul' AND NOT t.tgisinternal
              AND c.relname IN ('deployment_evidence', 'deployment_manifests', 'deployment_exceptions',
                                'dock_occupancy', 'appointments')
            ORDER BY 1, 2"""


def trigger_problems(rows, applied: set[str] | None = None) -> list[str]:
    """rows: (table, trigger, tgenabled, function, tgtype). Empty when every table has exactly its expected triggers,
    each enabled ('O'), on its expected function, timing, level and events. `applied`: the migration numbers recorded
    in the ledger ("0047", "0048", ...); a trigger of a migration not in it must be absent (see INTRODUCED_BY)."""
    seen: dict[str, dict[str, tuple[str, str, int]]] = {t: {} for t in EXPECTED}
    problems = []
    for table, name, enabled, fn, tgtype in rows:
        if table not in EXPECTED:
            continue
        seen[table][name] = (enabled, fn, int(tgtype))
    required = expected_for(applied)
    for (table, name), m in sorted(INTRODUCED_BY.items()):
        if applied is not None and m not in applied and name in seen[table]:
            problems.append(f"{table}.{name}: present, but migration {m} is not recorded as applied")
            del seen[table][name]                     # reported once, not again as "unexpected"
    for table, want in required.items():
        got = seen[table]
        for name, (fn, tgtype) in want.items():
            if name not in got:
                problems.append(f"{table}.{name}: missing")
                continue
            enabled, gfn, gtype = got[name]
            if enabled != "O":
                problems.append(f"{table}.{name}: disabled (tgenabled {enabled!r})")
            if gfn != fn:
                problems.append(f"{table}.{name}: runs {gfn}, expected {fn}")
            if gtype != tgtype:
                problems.append(f"{table}.{name}: timing/events {gtype}, expected {tgtype}")
        for name in sorted(set(got) - set(want)):
            problems.append(f"{table}.{name}: unexpected trigger ({got[name][1]})")
    return problems
