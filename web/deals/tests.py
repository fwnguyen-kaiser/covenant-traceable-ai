"""Comprehensive P2-P6 verification tests against isolated PostgreSQL test database."""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from io import BytesIO
from uuid import UUID, uuid4

import openpyxl
import psycopg
from django.contrib.auth.models import User
from django.test import Client, SimpleTestCase

from covenant_ai.app.admin import AdminIdentityService
from covenant_ai.app.cases import CaseService
from covenant_ai.app.errors import ForbiddenActionError
from covenant_ai.app.reviews import ReviewService
from covenant_ai.app.runs import ValuationRunnerService
from covenant_ai.providers import postgres as pg_mod
from covenant_ai.providers.postgres_repo import PostgresCaseRepository

from deals.auth_service import (
    activate_legacy_domain_user,
    provision_web_user,
    set_user_release_capability,
)


class SwePlanEndToEndTests(SimpleTestCase):
    """Covers P2 (Identity/RBAC), P3 (CRUD/Locks), P4 (Chat/Recovery), P5 (Review/Release), P6 (XLSX)."""

    databases = {"default"}

    def setUp(self) -> None:
        super().setUp()
        self.db_url = os.getenv("DATABASE_URL") or (
            "postgresql://covenant_user:covenant_secure_pass_2026@localhost:5432/covenant_ai_test"
        )
        os.environ["DATABASE_URL"] = self.db_url
        self.repo = PostgresCaseRepository(db_url=self.db_url)
        self.admin_svc = AdminIdentityService(self.db_url)
        self.cases = CaseService(self.repo)
        self.reviews = ReviewService(self.repo)

        self.tag = uuid4().hex[:8]
        self.pw = "CovenantPass2026!"
        self.admin_u, self.admin_id = provision_web_user(
            email=f"admin.{self.tag}@test.local",
            password=self.pw,
            role="admin",
            can_release_report=True,
            is_staff_admin=True,
            db_url=self.db_url,
        )
        self.alice_u, self.alice_id = provision_web_user(
            email=f"alice.{self.tag}@test.local",
            password=self.pw,
            role="analyst",
            actor_id=self.admin_id,
            db_url=self.db_url,
        )
        self.bob_u, self.bob_id = provision_web_user(
            email=f"bob.{self.tag}@test.local",
            password=self.pw,
            role="analyst",
            actor_id=self.admin_id,
            db_url=self.db_url,
        )
        self.carol_u, self.carol_id = provision_web_user(
            email=f"carol.{self.tag}@test.local",
            password=self.pw,
            role="reviewer",
            actor_id=self.admin_id,
            db_url=self.db_url,
        )
        self.created_case_ids: list[str] = []
        self.created_threads: list[str] = []

    def tearDown(self) -> None:
        with psycopg.connect(self.db_url) as conn:
            for tid in self.created_threads:
                conn.execute("DELETE FROM checkpoint_writes WHERE thread_id=%s;", (tid,))
                conn.execute("DELETE FROM checkpoint_blobs WHERE thread_id=%s;", (tid,))
                conn.execute("DELETE FROM checkpoints WHERE thread_id=%s;", (tid,))
            for cid in self.created_case_ids:
                conn.execute("DELETE FROM cases WHERE id=%s;", (cid,))
            for uid in (self.alice_id, self.bob_id, self.carol_id, self.admin_id):
                conn.execute("DELETE FROM audit_logs WHERE actor_id=%s OR entity_id=%s;", (str(uid), str(uid)))
                conn.execute("DELETE FROM users WHERE id=%s;", (str(uid),))
            conn.commit()
        User.objects.filter(username__endswith=f".{self.tag}@test.local").delete()

    def _login_client(self, email: str) -> Client:
        c = Client(enforce_csrf_checks=True)
        # Prime CSRF cookie cleanly
        c.get("/login/")
        csrf = c.cookies["csrftoken"].value
        res = c.post(
            "/api/login",
            data=json.dumps({"email": email, "password": self.pw}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf,
        )
        self.assertEqual(res.status_code, 200)
        return c

    def _post_json(self, c: Client, url: str, payload: dict):
        csrf = c.cookies["csrftoken"].value
        return c.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf,
        )

    def test_p2_csrf_session_revocation_legacy_activation_and_rbac_isolation(self):
        # 1. Legacy unverified domain user cannot login until explicit activation preserving UUID
        legacy_uid = uuid4()
        legacy_email = f"legacy.{self.tag}@test.local"
        with psycopg.connect(self.db_url) as conn:
            conn.execute(
                "INSERT INTO users (id, email, password_hash, role) VALUES (%s, %s, 'oldhash', 'analyst');",
                (str(legacy_uid), legacy_email),
            )
            conn.commit()
        try:
            anon = Client(enforce_csrf_checks=True)
            anon.get("/login/")
            csrf = anon.cookies["csrftoken"].value
            bad_login = anon.post(
                "/api/login",
                data=json.dumps({"email": legacy_email, "password": self.pw}),
                content_type="application/json",
                HTTP_X_CSRFTOKEN=csrf,
            )
            self.assertEqual(bad_login.status_code, 401)

            activate_legacy_domain_user(
                domain_user_id=legacy_uid,
                email=legacy_email,
                new_password=self.pw,
                actor_id=self.admin_id,
                db_url=self.db_url,
            )
            legacy_client = self._login_client(legacy_email)
            me = legacy_client.get("/api/me").json()
            self.assertEqual(me["user_id"], str(legacy_uid))
        finally:
            with psycopg.connect(self.db_url) as conn:
                conn.execute("DELETE FROM audit_logs WHERE entity_id=%s OR actor_id=%s;", (str(legacy_uid), str(legacy_uid)))
                conn.execute("DELETE FROM users WHERE id=%s;", (str(legacy_uid),))
                conn.commit()

        # 2. Missing CSRF token rejected (403)
        alice = self._login_client(self.alice_u.email)
        no_csrf = alice.post("/api/cases", data=json.dumps({"company_name": "Blocked"}), content_type="application/json")
        self.assertEqual(no_csrf.status_code, 403)

        # 3. Alice creates case; Bob gets 404 everywhere; unassigned Admin gets 403 on content but sees metadata
        c_res = self._post_json(alice, "/api/cases", {"company_name": "Alpha Private Co"})
        self.assertEqual(c_res.status_code, 201)
        cid = c_res.json()["case_id"]
        self.created_case_ids.append(cid)

        bob = self._login_client(self.bob_u.email)
        self.assertEqual(bob.get("/api/cases").json()["cases"], [])
        self.assertEqual(bob.get(f"/api/cases/{cid}").status_code, 404)
        self.assertEqual(bob.get(f"/api/cases/{cid}/profiles").status_code, 404)

        adm = self._login_client(self.admin_u.email)
        admin_cases = adm.get("/api/admin/cases").json()["cases"]
        self.assertTrue(any(row["case_id"] == cid for row in admin_cases))
        self.assertEqual(adm.get(f"/api/cases/{cid}").status_code, 403)

        # General user edit form / endpoint cannot grant report.release
        patch_bob = self._post_json(
            adm, f"/api/admin/users/{self.bob_id}", {"role": "admin", "can_release_report": True}
        )
        self.assertEqual(patch_bob.status_code, 200)
        self.assertFalse(patch_bob.json()["can_release_report"])

        # Locking active account immediately invalidates logged-in session on next request
        self._post_json(adm, f"/api/admin/users/{self.bob_id}", {"is_active": False})
        self.assertEqual(bob.get("/api/me").status_code, 401)
        self.assertEqual(bob.get(f"/api/cases/{cid}").status_code, 401)

        # Revoking release permission immediately invalidates admin's own release capability on active session
        from deals.auth_service import load_request_actor

        adm_actor = load_request_actor(self.admin_u, self.db_url)
        set_user_release_capability(
            target_user_id=self.admin_id, can_release=False, actor=adm_actor, db_url=self.db_url
        )
        self.assertFalse(adm.get("/api/me").json()["can_release_report"])

    def test_p3_crud_optimistic_versioning_null_vs_zero_and_concurrent_delete(self):
        alice = self._login_client(self.alice_u.email)
        c_res = self._post_json(alice, "/api/cases", {"company_name": "Draft Versioning Co"})
        self.assertEqual(c_res.status_code, 201)
        cid = c_res.json()["case_id"]
        self.created_case_ids.append(cid)

        # Float value rejected cleanly
        bad_float = self._post_json(
            alice,
            f"/api/cases/{cid}/profiles",
            {
                "intake_answers": {
                    "company_name": "Draft Versioning Co",
                    "industry": "industrial_machinery",
                    "geography": "US",
                    "ebitda_period": "FY",
                    "ebitda": 4800000.5,
                    "business_description": "Machinery",
                }
            },
        )
        self.assertEqual(bad_float.status_code, 400)

        # Save v1 with total_debt=None, cash="0"
        v1 = self._post_json(
            alice,
            f"/api/cases/{cid}/profiles",
            {
                "intake_answers": {
                    "company_name": "Draft Versioning Co",
                    "industry": "industrial_machinery",
                    "geography": "US",
                    "ebitda_period": "FY",
                    "ebitda": "4800000",
                    "revenue": "32000000",
                    "total_debt": None,
                    "cash": "0",
                    "business_description": "Machinery",
                }
            },
        )
        self.assertEqual(v1.status_code, 201)
        self.assertIsNone(v1.json()["total_debt"])
        self.assertEqual(v1.json()["cash"], "0")

        # Simulate two tabs saving against base_version=1
        v2 = self._post_json(
            alice,
            f"/api/cases/{cid}/profiles",
            {"base_version": 1, "partial": True, "intake_answers": {"ebitda": "5000000"}},
        )
        self.assertEqual(v2.status_code, 201)
        self.assertEqual(v2.json()["version"], 2)

        stale = self._post_json(
            alice,
            f"/api/cases/{cid}/profiles",
            {"base_version": 1, "partial": True, "intake_answers": {"ebitda": "5100000"}},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["latest_version"], 2)

        # Hard delete draft with no runs preserves audit_logs record
        del_res = alice.delete(f"/api/cases/{cid}", HTTP_X_CSRFTOKEN=alice.cookies["csrftoken"].value)
        self.assertEqual(del_res.status_code, 204)
        self.assertEqual(alice.get(f"/api/cases/{cid}").status_code, 404)
        del_log = psycopg.connect(self.db_url).execute(
            "SELECT count(*) FROM audit_logs WHERE action='case_hard_deleted' AND entity_id=%s;",
            (cid,),
        ).fetchone()[0]
        self.assertEqual(del_log, 1)

        # Concurrent start_run vs hard_delete on a draft case never leaves orphan runs/checkpoints
        c2 = self._post_json(
            alice,
            "/api/cases",
            {
                "company_name": "Concurrent Delete Race Co",
                "intake_answers": {
                    "industry": "industrial_machinery",
                    "geography": "US",
                    "ebitda_period": "FY",
                    "revenue": "20000000",
                    "ebitda": "4800000",
                    "business_description": "Pumps",
                },
            },
        )
        self.assertEqual(c2.status_code, 201)
        cid2 = c2.json()["case_id"]
        self.created_case_ids.append(cid2)

        def _do_start():
            c = self._login_client(self.alice_u.email)
            return self._post_json(c, f"/api/cases/{cid2}/runs", {}).status_code

        def _do_delete():
            c = self._login_client(self.alice_u.email)
            return c.delete(f"/api/cases/{cid2}", HTTP_X_CSRFTOKEN=c.cookies["csrftoken"].value).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            s_code = pool.submit(_do_start)
            d_code = pool.submit(_do_delete)
            codes = (s_code.result(), d_code.result())
        # Either start won (201 + 409) OR delete won (404 + 204) cleanly!
        self.assertIn(codes, {(201, 409), (404, 204), (409, 204)})

    def test_p4_p5_p6_full_chat_recovery_review_release_xlsx_and_cli_isolation(self):
        alice = self._login_client(self.alice_u.email)
        carol = self._login_client(self.carol_u.email)
        adm = self._login_client(self.admin_u.email)
        bob = self._login_client(self.bob_u.email)

        # Case name exercises Excel formula injection safety; cash=0, total_debt=None preserves missing != 0
        c_res = self._post_json(
            alice,
            "/api/cases",
            {
                "company_name": "=1+1 Formula Safe Corp",
                "intake_answers": {
                    "industry": "industrial_machinery",
                    "geography": "US",
                    "ebitda_period": "FY",
                    "revenue": "32000000",
                    "ebitda": "4800000",
                    "cash": "0",
                    "business_description": "Industrial machinery manufacturer",
                },
            },
        )
        cid = c_res.json()["case_id"]
        self.created_case_ids.append(cid)

        # Assign Carol as reviewer and Admin as release_admin
        self._post_json(adm, f"/api/admin/cases/{cid}/members", {"user_id": str(self.carol_id), "role": "reviewer"})
        self._post_json(adm, f"/api/admin/cases/{cid}/members", {"user_id": str(self.admin_id), "role": "release_admin"})

        # Start run -> hits confirm_industry interrupt
        r1 = self._post_json(alice, f"/api/cases/{cid}/runs", {})
        self.assertEqual(r1.status_code, 201)
        run1 = r1.json()
        run_id = run1["run_id"]
        tid1 = run1["thread_id"]
        self.created_threads.append(tid1)
        p1 = run1["pending_action"]
        self.assertEqual(p1["action"], "confirm_industry")

        # Duplicate client_message_id with different payload -> 409; same payload -> idempotent replay
        cmid1 = f"turn-{uuid4().hex}"
        missing_prompt = self._post_json(
            alice, f"/api/runs/{run_id}/messages",
            {"client_message_id": cmid1, "answer": "industrial_machinery"},
        )
        self.assertEqual(missing_prompt.status_code, 400)
        # Simulate a worker stopping after the user message was committed but
        # before LangGraph received it. A refreshed browser reuses the receipt.
        from covenant_ai.app.chat import _command_hash

        reply = {"client_message_id": cmid1, "prompt_id": p1["prompt_id"], "answer": "industrial_machinery"}
        with psycopg.connect(self.db_url) as conn:
            conn.execute(
                "INSERT INTO run_commands (run_id,thread_id,client_message_id,prompt_id,command_hash,status,actor_id) "
                "VALUES (%s,%s,%s,%s,%s,'pending',%s)",
                (run_id, tid1, cmid1, p1["prompt_id"], _command_hash(p1["prompt_id"], reply["answer"]), str(self.alice_id)),
            )
            conn.execute(
                "INSERT INTO chat_messages (run_id,seq_no,sender_type,sender_id,message_type,prompt_id,client_message_id,body,payload) "
                "SELECT %s,COALESCE(MAX(seq_no),0)+1,'user',%s,'answer',%s,%s,%s,%s::jsonb FROM chat_messages WHERE run_id=%s",
                (run_id, str(self.alice_id), p1["prompt_id"], cmid1, reply["answer"], json.dumps({"answer": reply["answer"]}), run_id),
            )
        resume = alice.get(f"/api/runs/{run_id}/messages").json()["pending_action"]["pending_reply"]
        self.assertEqual(resume, reply)
        competing = self._post_json(alice, f"/api/runs/{run_id}/messages", {
            **reply, "client_message_id": str(uuid4()), "answer": "application_software",
        })
        self.assertEqual(competing.status_code, 409)
        t1 = self._post_json(alice, f"/api/runs/{run_id}/messages", resume)
        self.assertEqual(t1.status_code, 200)
        self.assertFalse(t1.json()["idempotent_replay"])

        t1_dup = self._post_json(
            alice,
            f"/api/runs/{run_id}/messages",
            {"client_message_id": cmid1, "prompt_id": p1["prompt_id"], "answer": "industrial_machinery"},
        )
        self.assertEqual(t1_dup.status_code, 200)
        self.assertTrue(t1_dup.json()["idempotent_replay"])
        self.assertEqual(len(t1_dup.json()["messages"]), len(t1.json()["messages"]))

        t1_mismatch = self._post_json(
            alice,
            f"/api/runs/{run_id}/messages",
            {"client_message_id": cmid1, "prompt_id": p1["prompt_id"], "answer": "restaurants"},
        )
        self.assertEqual(t1_mismatch.status_code, 409)

        # Stale prompt_id -> 409
        t2_stale = self._post_json(
            alice,
            f"/api/runs/{run_id}/messages",
            {"client_message_id": f"stale-{uuid4().hex}", "prompt_id": p1["prompt_id"], "answer": ["GRC", "KAI", "TNC"]},
        )
        self.assertEqual(t2_stale.status_code, 409)

        # Answer peer_review (simulating web crash right after pending command was logged -> test replay recovery)
        p2 = t1.json()["pending_action"]
        self.assertEqual(p2["action"], "peer_review")
        cmid2 = f"turn-{uuid4().hex}"
        t2 = self._post_json(
            alice,
            f"/api/runs/{run_id}/messages",
            {"client_message_id": cmid2, "prompt_id": p2["prompt_id"], "answer": p2["payload"]["selected_peers"]},
        )
        self.assertEqual(t2.status_code, 200)
        self.assertEqual(t2.json()["status"], "completed")
        self.assertTrue(t2.json()["has_packet"])

        # Unauthenticated CLI is blocked on web_managed case
        with self.assertRaises(ForbiddenActionError):
            self.reviews.approve_legacy_windows_sid(tid1, "abc")
        with self.assertRaises(ForbiddenActionError):
            ValuationRunnerService(db_url=self.db_url, repo=self.repo).inspect_or_recover_run(tid1, actor_id=None)

        # Submit -> Reviewer requests changes (blank comment rejected 400; valid reason transitions) -> submitted -> reviewed -> released
        pkt_draft = self._post_json(alice, f"/api/runs/{run_id}/submit", {}).json()
        self.assertEqual(pkt_draft["review_status"], "submitted")
        digest = pkt_draft["digest"]

        # Alice cannot self-review or release
        self.assertEqual(self._post_json(alice, f"/api/runs/{run_id}/review", {"digest": digest}).status_code, 403)
        self.assertEqual(self._post_json(alice, f"/api/runs/{run_id}/release", {"digest": digest}).status_code, 403)
        # Carol cannot release
        self.assertEqual(self._post_json(carol, f"/api/runs/{run_id}/release", {"digest": digest}).status_code, 403)
        # Bad digest rejected 409
        self.assertEqual(
            self._post_json(carol, f"/api/runs/{run_id}/request-changes", {"digest": "bad", "comment": "fix"}).status_code,
            409,
        )
        # Missing comment rejected 400
        self.assertEqual(
            self._post_json(carol, f"/api/runs/{run_id}/request-changes", {"digest": digest, "comment": ""}).status_code,
            400,
        )
        # Carol requests changes cleanly, then Alice resubmits & Carol marks reviewed
        rc_pkt = self._post_json(
            carol, f"/api/runs/{run_id}/request-changes", {"digest": digest, "comment": "Confirm cash"}
        ).json()
        self.assertEqual(rc_pkt["review_status"], "changes_requested")
        self._post_json(alice, f"/api/runs/{run_id}/submit", {})
        rev_pkt = self._post_json(carol, f"/api/runs/{run_id}/review", {"digest": digest, "comment": "Verified"}).json()
        self.assertEqual(rev_pkt["review_status"], "reviewed")

        # Release Admin releases (idempotent double call + check report_approvals web_user)
        rel_pkt = self._post_json(
            adm, f"/api/runs/{run_id}/release", {"digest": digest, "acknowledge_warnings": True}
        )
        self.assertEqual(rel_pkt.status_code, 200)
        self.assertEqual(rel_pkt.json()["review_status"], "released")
        rel_dup = self._post_json(
            adm, f"/api/runs/{run_id}/release", {"digest": digest, "acknowledge_warnings": True}
        )
        self.assertEqual(rel_dup.status_code, 200)

        # P6 Export tests: Bob blocked 404; XLSX/JSON/MD generated strictly from frozen packet without market provider calls
        self.assertEqual(bob.get(f"/api/runs/{run_id}/exports/xlsx").status_code, 404)
        orig_get_peer = pg_mod.PostgresMarketDataProvider.get_peer_financials
        pg_mod.PostgresMarketDataProvider.get_peer_financials = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("Live market provider must never be called during frozen export!")
        )
        try:
            xlsx_resp = alice.get(f"/api/runs/{run_id}/exports/xlsx")
            md_resp = alice.get(f"/api/runs/{run_id}/exports/md")
            json_resp = alice.get(f"/api/runs/{run_id}/exports/json")
        finally:
            pg_mod.PostgresMarketDataProvider.get_peer_financials = orig_get_peer

        self.assertEqual(xlsx_resp.status_code, 200)
        self.assertEqual(md_resp.status_code, 200)
        self.assertEqual(json_resp.status_code, 200)
        wb = openpyxl.load_workbook(BytesIO(xlsx_resp.content), data_only=False)
        self.assertEqual(wb.sheetnames, ["Results", "Comparable Companies", "Sources"])

        ws_res = wb["Results"]
        res_map = {ws_res.cell(r, 1).value: ws_res.cell(r, 2) for r in range(2, ws_res.max_row + 1)}
        self.assertEqual(res_map["Company Name"].value, "=1+1 Formula Safe Corp")
        self.assertTrue(res_map["Company Name"].quotePrefix)
        self.assertIsNone(res_map["Target Total Debt"].value)  # missing != 0
        self.assertEqual(Decimal(str(res_map["Target Cash"].value)), Decimal(0))
        self.assertEqual(str(res_map["Report Digest (SHA-256)"].value), digest)
        self.assertEqual(
            Decimal(str(res_map["Median-Based EV"].value)),
            Decimal(pkt_draft["valuation"]["ev_mid"]),
        )
        self.assertEqual(
            Decimal(str(res_map["EV Arithmetic Midpoint ((Low+High)/2)"].value)),
            (Decimal(pkt_draft["valuation"]["ev_low"]) + Decimal(pkt_draft["valuation"]["ev_high"])) / Decimal(2),
        )
        ws_src = wb["Sources"]
        self.assertGreaterEqual(ws_src.max_row, 50)
        # Verify Sources column 5 keeps exact Decimal string matching snapshot value
        src_val = ws_src.cell(2, 5).value
        self.assertIsInstance(src_val, str)
        self.assertEqual(Decimal(src_val), Decimal(pkt_draft["sources"][0]["value"]))
        json_pkt = json.loads(json_resp.content.decode("utf-8"))
        self.assertEqual(json_pkt["valuation"]["ev_mid"], pkt_draft["valuation"]["ev_mid"])

        # Mid-flight crash recovery on chat turn + baseline immutability check:
        # 1. Start Run 2 on an LTM-missing partial profile -> intake_clarify -> simulate pending command after checkpoint advanced
        v2 = self._post_json(
            alice,
            f"/api/cases/{cid}/profiles",
            {
                "base_version": 1,
                "partial": True,
                "intake_answers": {"ebitda": "5200000", "ebitda_period": "FY"},
            },
        )
        self.assertEqual(v2.status_code, 201)
        bad_override = self._post_json(
            alice,
            f"/api/cases/{cid}/runs",
            {"intake_answers": {"company_name": "=1+1 Formula Safe Corp", "ebitda": "5200000"}},
        )
        self.assertEqual(bad_override.status_code, 400)
        self.assertIn("intake_answers", bad_override.json().get("field_errors", {}))
        runs_before_r2 = len(alice.get(f"/api/cases/{cid}/runs").json()["runs"])
        r2 = self._post_json(
            alice,
            f"/api/cases/{cid}/runs",
            {
                "intake_answers": {
                    "company_name": "=1+1 Formula Safe Corp",
                    "industry": "industrial_machinery",
                    "geography": "US",
                    "ebitda_period": "FY",
                    "revenue": "32000000",
                    "ebitda": "5200000",
                    "business_description": "Industrial machinery manufacturer",
                }
            },
        ).json()
        run2_id = r2["run_id"]
        self.created_threads.append(r2["thread_id"])
        p2_a = r2["pending_action"]
        cmid_crash = f"crash-{uuid4().hex}"
        # Execute turn directly inside runner (checkpoint advances), then insert pending run_commands row as if HTTP crashed before applying response!
        step_after = ValuationRunnerService(db_url=self.db_url, repo=self.repo).submit_hitl_decision(
            r2["thread_id"], "industrial_machinery", actor_id=self.alice_id
        )
        self.assertFalse(step_after.terminal)
        import hashlib as _hl

        cmd_hash = _hl.sha256(
            json.dumps({"prompt_id": p2_a["prompt_id"], "answer": "industrial_machinery"}, sort_keys=True).encode("utf-8")
        ).hexdigest()
        with psycopg.connect(self.db_url) as conn:
            conn.execute(
                "INSERT INTO run_commands (run_id, thread_id, client_message_id, prompt_id, command_hash, status, actor_id) "
                "VALUES (%s, %s, %s, %s, %s, 'pending', %s);",
                (run2_id, r2["thread_id"], cmid_crash, p2_a["prompt_id"], cmd_hash, str(self.alice_id)),
            )
            conn.commit()
        recov = self._post_json(
            alice,
            f"/api/runs/{run2_id}/messages",
            {"client_message_id": cmid_crash, "prompt_id": p2_a["prompt_id"], "answer": "industrial_machinery"},
        )
        self.assertEqual(recov.status_code, 200)
        self.assertTrue(recov.json()["idempotent_replay"])
        self.assertEqual(recov.json()["pending_action"]["action"], "peer_review")
        # Run 1 released packet still matches original ev_mid even after Run 2 ran with higher EBITDA
        self.assertEqual(
            alice.get(f"/api/runs/{run_id}/report").json()["valuation"]["ev_mid"],
            pkt_draft["valuation"]["ev_mid"],
        )
        self.assertEqual(len(alice.get(f"/api/cases/{cid}/runs").json()["runs"]), runs_before_r2 + 1)
