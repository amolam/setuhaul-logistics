"""Build invariant: the agent is never a privileged database operator.

It understands and requests actions; the application and the database decide whether they are allowed.
These catalogue tests pin the agent role's powers to an explicit allowlist. Any new grant, function or
membership that widens them fails here and needs a deliberate, reviewed change to this file.
"""
AGENT = "setuhaul_agent"

# The complete set of tables the agent may write, and how (INSERT, or UPDATE of listed columns only).
# Since migration 0004 the agent writes almost nothing directly: options, signals, exceptions, escalations, audit,
# outbox, idempotency keys, threads, selections and attestations are written only by functions or trusted paths.
AGENT_INSERT = {"chat_messages", "tracking_consents", "thread_flow"}          # chat_messages: AGENT rows only (RLS)
AGENT_UPDATE_COLUMNS = {
    "chat_threads": {"clarification_turns"},             # shipment binding only via bind_thread_shipment()
    "tracking_consents": {"withdrawn_at"},
    "thread_flow": {"state", "selected_option", "yes_message_id", "hold_option", "appointment_id", "attestation_id",
                    "version", "updated_at"},
}
# The complete set of functions the agent may call. Each is a REQUEST that re-checks rules, evidence and RLS.
AGENT_FUNCTIONS = {"hold_option", "submit_booking_request", "release_hold", "record_read_back", "record_driver_eta",
                   "escalate_thread", "bind_thread_shipment", "scope_allows", "driver_or_staff",
                   "note_turn_facts",           # 0008: earlier-turn facts, own thread only, fixed keys (never a booking)
                   "resolve_thread_shipment",   # 0010: own thread + own shipment, resolved and bound under the lock
                   "assert_turn_fence",         # 0012: read-only check that this worker still holds the turn
                   # 0023 (T52): the agent asks for a model-budget reservation and reports the outcome; the database
                   # decides against the cap. Neither touches booking, approval or capacity state.
                   "reserve_model_call", "settle_model_call",
                   # 0026 (Option D): read-only snapshot for the booking switch's start-up check (gate rows, guard
                   # triggers, kill switch, recorded manifest); returns no facility, driver or booking data
                   "booking_gate_snapshot",
                   # 0032 (Option P): read-only snapshot of one deployment's provisional authorization (status and
                   # window only); no facility, driver or booking data
                   "booking_provisional_snapshot"}
# Never writable by the agent in any way.
PROTECTED = {"dock_occupancy", "appointments", "approval_requests", "notifications", "facilities", "facility_config",
             "docks", "shipments", "appointment_slots", "dock_events", "drivers", "vehicles", "carriers",
             "shown_options", "eta_signals", "driver_exceptions", "escalations", "booking_events", "outbox", "thread_facts",
             "idempotency_keys", "option_selections", "confirmation_attestations",
             "inbound_receipts", "deployment_evidence",
             "staff_context", "approval_role_rules", "booking_holds", "confirmation_rules", "outbox_consumptions",  # 0015
             "model_budget_day", "model_usage",                                                        # 0023
             "approval_callbacks",                                                                     # 0024
             "optimization_runs", "agent_turn_metrics", "evaluation_runs"}                             # 0027


def test_agent_role_attributes(conn):
    r = conn.execute("SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolcanlogin FROM pg_roles WHERE rolname=%s",
                     (AGENT,)).fetchone()
    assert r == (False, False, False, False, False)


def test_agent_belongs_to_no_other_role(conn):
    n = conn.execute("""SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member WHERE r.rolname = %s""",
                     (AGENT,)).fetchone()[0]
    assert n == 0


def test_agent_table_inserts_are_exactly_the_allowlist(conn):
    got = {r[0] for r in conn.execute("""SELECT table_name FROM information_schema.role_table_grants
                                         WHERE grantee=%s AND table_schema='setuhaul' AND privilege_type='INSERT'""", (AGENT,))}
    assert got == AGENT_INSERT


def test_agent_has_no_table_level_update_delete_truncate(conn):
    got = conn.execute("""SELECT table_name, privilege_type FROM information_schema.role_table_grants
                          WHERE grantee=%s AND table_schema='setuhaul' AND privilege_type IN ('UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER')""",
                       (AGENT,)).fetchall()
    assert got == []


def test_agent_column_updates_are_exactly_the_allowlist(conn):
    got: dict = {}
    for t, col in conn.execute("""SELECT table_name, column_name FROM information_schema.column_privileges
                                  WHERE grantee=%s AND table_schema='setuhaul' AND privilege_type='UPDATE'""", (AGENT,)):
        got.setdefault(t, set()).add(col)
    assert got == AGENT_UPDATE_COLUMNS


def test_agent_can_write_no_protected_table(conn):
    for t in PROTECTED:
        for priv in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            assert not conn.execute("SELECT has_table_privilege(%s, %s, %s)", (AGENT, f"setuhaul.{t}", priv)).fetchone()[0], (t, priv)
        assert not conn.execute("SELECT has_any_column_privilege(%s, %s, 'UPDATE')", (AGENT, f"setuhaul.{t}")).fetchone()[0], t


def test_agent_functions_are_exactly_the_allowlist(conn):
    got = {r[0] for r in conn.execute("""SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                                         WHERE n.nspname='setuhaul' AND has_function_privilege(%s, p.oid, 'EXECUTE')""", (AGENT,))}
    assert got == AGENT_FUNCTIONS


def test_agent_cannot_read_approval_task_tokens(conn):
    assert not conn.execute("SELECT has_column_privilege(%s, 'setuhaul.approval_requests', 'task_token', 'SELECT')",
                            (AGENT,)).fetchone()[0]


def test_every_definer_function_is_safe(conn):
    """SECURITY DEFINER = runs with the owner's rights, so the owner must be the NOBYPASSRLS function role and
    search_path must be pinned."""
    bad = conn.execute("""SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                          WHERE n.nspname='setuhaul' AND p.prosecdef
                            AND (pg_get_userbyid(p.proowner) <> 'setuhaul_rpc'
                                 OR NOT coalesce(array_to_string(p.proconfig, ',') LIKE '%search_path=setuhaul, pg_catalog, pg_temp%', false))""").fetchall()
    assert bad == []
    owner = conn.execute("SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname='setuhaul_rpc'").fetchone()
    assert owner == (False, False, False)


def test_nothing_is_executable_by_public(conn):
    n = conn.execute("""SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                        WHERE n.nspname='setuhaul' AND has_function_privilege('public', p.oid, 'EXECUTE')""").fetchone()[0]
    assert n == 0


def test_every_table_is_rls_forced_or_reasoned(conn):
    """Coverage over the migrated schema (0001 + later migrations), same rule as the Phase 2 validator."""
    allow = {"carriers", "drivers", "vehicles", "facilities", "facility_config", "docks", "idempotency_keys",
             "deployment_evidence",   # 0013: append-only deployment evidence, no facility data; read by setuhaul_system
             # 0015: no application role has any privilege on these; only the definer functions (setuhaul_rpc) use them
             "staff_context",         # one row per console transaction, read only for that transaction (B-WF-2 §15)
             "approval_role_rules",   # role-per-kind configuration, no facility data
             "confirmation_rules",    # versioned policy configuration, append-only
             "outbox_consumptions",   # consumer bookkeeping (consumer, event id), no facility data
             # 0023 (T52): model usage ledger; no facility or driver data, written only by the definer functions
             "model_budget_day", "model_usage",
             # 0024 (E4-01): Step Functions callback bookkeeping (approval id, token, attempts); no facility data,
             # no application role has any privilege on it, only the definer functions use it
             "approval_callbacks",
             # 0026 (Option D): append-only manifests and owner-approved exceptions (no facility data; written only by
             # the deploy-evidence login, read only through booking_gate_snapshot) and the single-row kill switch
             # (changed only by set_booking_kill_switch or the owner)
             "deployment_manifests", "deployment_exceptions", "booking_kill_switch",
             # 0027 (Phase G2): evaluation run summaries; synthetic evaluation totals only, no facility, driver or
             # booking data; written only by record_evaluation_run (system role), read by console and system
             "evaluation_runs",
             # 0030: the single-row evidence-guard setting (enforced or not), like the kill switch: no facility data,
             # no application role has any privilege on it, read only by the guard trigger, changed only one way
             "booking_evidence_guard",
             # 0032 (Option P): provisional authorizations and their append-only audit trail; deployment ids, release
             # SHAs and approval references only (no facility data); no application role has any privilege on either,
             # only the definer functions (issue, consume, revoke, expire, snapshot) use them
             "provisional_authorizations", "provisional_authorization_events",
             # 0033 (Recovery Release A): owner-attested decider kinds (approval id, kind, provenance; no facility
             # data); append-only, no application role has any privilege, read only by approval_execution_evidence
             "approval_decider_attestations"}
    bad = [r[0] for r in conn.execute("""SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                                         WHERE n.nspname='setuhaul' AND c.relkind='r'
                                           AND NOT (c.relrowsecurity AND c.relforcerowsecurity)""") if r[0] not in allow]
    assert bad == []



def _grants(conn, role):
    tables: dict = {}
    for t, priv in conn.execute("""SELECT table_name, privilege_type FROM information_schema.role_table_grants
                                   WHERE grantee=%s AND table_schema='setuhaul'""", (role,)):
        tables.setdefault(priv, set()).add(t)
    cols: dict = {}
    for t, c in conn.execute("""SELECT table_name, column_name FROM information_schema.column_privileges
                                WHERE grantee=%s AND table_schema='setuhaul' AND privilege_type='UPDATE'""", (role,)):
        cols.setdefault(t, set()).add(c)
    funcs = {r[0] for r in conn.execute("""SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                                           WHERE n.nspname='setuhaul' AND has_function_privilege(%s, p.oid, 'EXECUTE')""", (role,))}
    return tables, cols, funcs


def test_ingest_role_allowlist(conn):
    """The verified inbound path: find driver and thread, store DRIVER messages, attest, keep receipts. Nothing else."""
    tables, cols, funcs = _grants(conn, "setuhaul_ingest")
    assert tables == {"SELECT": {"drivers", "shipments", "chat_threads", "chat_messages", "inbound_receipts"},
                      "INSERT": {"chat_threads", "chat_messages", "inbound_receipts"}}
    assert cols == {"inbound_receipts": {"attestation_status", "attempts", "last_error", "enqueued", "enqueued_at", "updated_at",
                                         # 0011 (B-WF-1): the inbound outbox and processing lease
                                         "published_at", "generation", "processing_owner", "processing_until",
                                         "processed_at", "deliveries"}}
    assert funcs == {"attest_confirmation", "thread_lock", "scope_allows", "driver_or_staff",
                     "record_delivery_status",   # 0017 (B-WF-3): provider receipts from the signed webhook
                     "booking_gate_snapshot",    # 0026: read-only start-up check of the booking switch (the api)
                     "booking_provisional_snapshot",  # 0032: the same check reads an open Option P window
                     "booking_authorization_status",  # 0033 (D4): /health reads the database's booking verdict now
                     # 0035 (roadmap Phases 1/4/5): the verified inbound path answers dispatcher offers, records a
                     # shared location and links a Telegram account; every one checks the driver itself
                     "dispatch_offer_for_driver", "request_offer_alternative", "driver_location_context",
                     "record_driver_location", "redeem_driver_link_code", "driver_for_channel",
                     "open_offer_for_driver",   # 0036: a typed Telegram reply answers the driver's own open offer
                     "offer_telegram_message"}  # 0038: ...and clears that offer's buttons (read only, own offer)


def test_feasibility_role_allowlist(conn):
    """Reads the feasibility inputs; its only write is publishing computed options."""
    tables, cols, funcs = _grants(conn, "setuhaul_feasibility")
    assert tables == {"SELECT": {"shipments", "docks", "facilities", "facility_config", "appointment_slots", "appointments",
                                 "dock_events", "dock_occupancy", "eta_signals"},
                      "INSERT": {"shown_options"}}
    # 0012: fenced publishing. 0027 (Phase G1/G3): the service records the optimizer run summaries and per-turn agent
    # metrics it measured, through definer functions that validate every value and keep facility scope. Neither touches
    # booking, approval or capacity state, and the agent role gains nothing.
    assert cols == {} and funcs == {"scope_allows", "driver_or_staff", "assert_turn_fence",
                                    "record_optimization_run", "record_agent_turn"}


def test_system_role_lost_inbound_duties(conn):
    tables, _, funcs = _grants(conn, "setuhaul_system")
    assert not ({"chat_messages", "chat_threads", "shown_options"} & tables.get("INSERT", set()))
    assert "attest_confirmation" not in funcs


def test_new_roles_cannot_bypass_rls(conn):
    rows = conn.execute("""SELECT rolname, rolsuper, rolbypassrls, rolcanlogin FROM pg_roles
                           WHERE rolname IN ('setuhaul_ingest','setuhaul_feasibility') ORDER BY 1""").fetchall()
    assert rows == [("setuhaul_feasibility", False, False, False), ("setuhaul_ingest", False, False, False)]



def test_conversation_lock_uses_a_64_bit_key(conn):
    src = conn.execute("""SELECT pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                          WHERE n.nspname='setuhaul' AND p.proname='thread_lock'""").fetchone()[0]
    assert "hashtextextended" in src


def test_deploy_evidence_role_can_only_insert_evidence(conn):
    """R2.0 S4-R2-05: the pipeline's role writes evidence and nothing else; no application role writes evidence."""
    tables, cols, funcs = _grants(conn, "setuhaul_deploy_evidence")
    # 0026 (Option D): it also records the deployment manifest and the owner-approved exceptions, append-only
    gate_tables = {"deployment_evidence", "deployment_manifests", "deployment_exceptions"}
    assert tables == {"INSERT": gate_tables, "SELECT": gate_tables} and cols == {}
    assert funcs <= {"scope_allows", "driver_or_staff", "evidence_max_validity",   # 0014: per-gate validity window
                     "booking_gate_snapshot",                                      # 0026: the database fingerprint
                     "approval_execution_evidence",                                # 0029: renewal, read-only, scoped
                     # 0032 (Option P): issue (all conditions re-checked in the database), revoke on a failed run,
                     # mark expired windows, read one deployment's window; no table privilege
                     "issue_provisional_authorization", "revoke_provisional_authorization",
                     "expire_provisional_authorizations", "booking_provisional_snapshot",
                     # 0034: read the pilot's provisional rate limit (the readiness check), nothing else
                     "provisional_rate_limit"}
    r = conn.execute("SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname='setuhaul_deploy_evidence'").fetchone()
    assert r == (False, False, False)
    n = conn.execute("""SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid
                         WHERE r.rolname = 'setuhaul_deploy_evidence'""").fetchone()[0]
    assert n == 0                                                     # no application role is a member

