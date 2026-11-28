"""P7 real Chromium browser E2E verification across Analyst, Reviewer, and Release Admin sessions."""
from __future__ import annotations

import os
import threading
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from wsgiref.simple_server import WSGIRequestHandler, make_server

import openpyxl
import psycopg
import pytest
from django.contrib.auth.models import User
from django.contrib.staticfiles.handlers import StaticFilesHandler
from django.core.wsgi import get_wsgi_application

from covenant_ai.app.admin import AdminIdentityService
from covenant_ai.providers.postgres_repo import PostgresCaseRepository
from deals.auth_service import provision_web_user


CHROME_CANDIDATES = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
)


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        return


def _find_browser_executable() -> str | None:
    for p in CHROME_CANDIDATES:
        if p.is_file():
            return str(p)
    return None


def test_p7_real_browser_e2e_chat_review_release_and_xlsx_download(tmp_path):
    playwright_mod = pytest.importorskip("playwright.sync_api")
    browser_exe = _find_browser_executable()
    if browser_exe is None:
        pytest.skip("No local Chrome or Edge binary found for headless browser E2E.")

    db_url = os.getenv("DATABASE_URL") or (
        "postgresql://covenant_user:covenant_secure_pass_2026@localhost:5432/covenant_ai_test"
    )
    os.environ["DATABASE_URL"] = db_url
    repo = PostgresCaseRepository(db_url=db_url)
    admin_svc = AdminIdentityService(db_url)

    tag = uuid4().hex[:8]
    pw = "BrowserE2E2026!"
    _, admin_id = provision_web_user(
        email=f"dave.{tag}@e2e.local",
        password=pw,
        role="admin",
        can_release_report=True,
        is_staff_admin=True,
        db_url=db_url,
    )
    _, alice_id = provision_web_user(
        email=f"alice.{tag}@e2e.local", password=pw, role="analyst", actor_id=admin_id, db_url=db_url
    )
    _, bob_id = provision_web_user(
        email=f"bob.{tag}@e2e.local", password=pw, role="analyst", actor_id=admin_id, db_url=db_url
    )
    _, carol_id = provision_web_user(
        email=f"carol.{tag}@e2e.local", password=pw, role="reviewer", actor_id=admin_id, db_url=db_url
    )

    wsgi_app = StaticFilesHandler(get_wsgi_application())
    httpd = make_server("127.0.0.1", 0, wsgi_app, handler_class=_QuietHandler)
    port = httpd.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    created_case_ids: list[str] = []
    try:
        with playwright_mod.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=browser_exe, headless=True)
            try:
                # 1. Analyst Alice creates a case, saves v1, starts chat, and closes browser midway
                ctx_a1 = browser.new_context(accept_downloads=True)
                page_a1 = ctx_a1.new_page()
                page_a1.goto(f"{base_url}/login/", wait_until="networkidle")
                page_a1.fill("#id_email", f"alice.{tag}@e2e.local")
                page_a1.fill("#id_password", pw)
                page_a1.click("button[type='submit']")
                page_a1.wait_for_url(f"{base_url}/")

                page_a1.fill("#new_company_name", f"Defense Controls {tag}")
                page_a1.click("#create-case-form button[type='submit']")
                page_a1.wait_for_url("**/cases/**/")
                case_url = page_a1.url
                case_id = case_url.rstrip("/").split("/")[-1]
                created_case_ids.append(case_id)

                # Assign Carol (reviewer) and Dave (release_admin)
                admin_svc.assign_case_member(
                    case_id=case_id, user_id=carol_id, role="reviewer", actor_id=admin_id
                )
                admin_svc.assign_case_member(
                    case_id=case_id, user_id=admin_id, role="release_admin", actor_id=admin_id
                )

                # Save v1 profile via browser form (debt left blank -> unknown, cash=0)
                page_a1.select_option("#prof_industry", "industrial_machinery")
                page_a1.fill("#prof_ebitda", "4800000")
                page_a1.fill("#prof_revenue", "32000000")
                page_a1.fill("#prof_debt", "")
                page_a1.fill("#prof_cash", "0")
                page_a1.fill("#prof_desc", "Precision industrial controls")
                with page_a1.expect_navigation(wait_until="networkidle"):
                    page_a1.click("#profile-version-form button[type='submit']")

                # Start run -> pauses at confirm_industry
                with page_a1.expect_navigation(wait_until="networkidle"):
                    page_a1.click("#btn-start-run")
                page_a1.wait_for_selector("#hitl-industry")
                active_run_url = page_a1.url
                ctx_a1.close()

                # Re-open in fresh browser context (mid-flow resume) -> still sees confirm_industry!
                ctx_a2 = browser.new_context(accept_downloads=True)
                page_a2 = ctx_a2.new_page()
                page_a2.goto(f"{base_url}/login/", wait_until="networkidle")
                page_a2.fill("#id_email", f"alice.{tag}@e2e.local")
                page_a2.fill("#id_password", pw)
                page_a2.click("button[type='submit']")
                page_a2.wait_for_url(f"{base_url}/")
                page_a2.goto(active_run_url, wait_until="networkidle")
                page_a2.wait_for_selector("#hitl-industry")

                # Confirm industry -> peer_review
                assert "Normalized EBITDA" in page_a2.locator("#chat-log").inner_text()
                page_a2.click("#chat-prompt-box button[type='submit']")
                page_a2.wait_for_selector("#hitl-peers")

                # Approve peers -> finishes draft & reveals Submit Draft button
                page_a2.click("#chat-prompt-box button[type='submit']")
                page_a2.wait_for_selector("#report-card", state="visible")
                assert "DRAFT" in page_a2.locator("#report-meta").inner_text().upper()
                page_a2.click("#report-actions button")
                page_a2.wait_for_function(
                    "() => document.querySelector('#report-meta').textContent.includes('SUBMITTED')"
                )

                # 2. Analyst Bob isolated from Alice's case in real browser
                ctx_b = browser.new_context()
                page_b = ctx_b.new_page()
                page_b.goto(f"{base_url}/login/", wait_until="networkidle")
                page_b.fill("#id_email", f"bob.{tag}@e2e.local")
                page_b.fill("#id_password", pw)
                page_b.click("button[type='submit']")
                page_b.wait_for_url(f"{base_url}/")
                assert f"Defense Controls {tag}" not in page_b.content()
                resp_bob = page_b.goto(f"{base_url}/cases/{case_id}/")
                assert resp_bob is not None and resp_bob.status == 404
                ctx_b.close()

                # 3. Reviewer Carol reviews & marks Reviewed on /reviews/
                ctx_c = browser.new_context()
                page_c = ctx_c.new_page()
                page_c.goto(f"{base_url}/login/", wait_until="networkidle")
                page_c.fill("#id_email", f"carol.{tag}@e2e.local")
                page_c.fill("#id_password", pw)
                page_c.click("button[type='submit']")
                page_c.wait_for_url(f"{base_url}/")
                page_c.goto(f"{base_url}/reviews/", wait_until="networkidle")
                with page_c.expect_navigation(wait_until="networkidle"):
                    page_c.locator("button:has-text('Mark Reviewed')").first.click()
                assert "reviewed" in page_c.content().lower()
                ctx_c.close()

                # 4. Admin Dave releases & downloads Excel, tracing values back to Sources
                ctx_d = browser.new_context(accept_downloads=True)
                page_d = ctx_d.new_page()
                page_d.goto(f"{base_url}/login/", wait_until="networkidle")
                page_d.fill("#id_email", f"dave.{tag}@e2e.local")
                page_d.fill("#id_password", pw)
                page_d.click("button[type='submit']")
                page_d.wait_for_url(f"{base_url}/")
                page_d.goto(f"{base_url}/reviews/", wait_until="networkidle")
                # Check ACK box if audit_verdict is WARN
                ack_boxes = page_d.locator("input[type='checkbox'][id^='ack-warn-']")
                if ack_boxes.count() > 0:
                    ack_boxes.first.check()
                with page_d.expect_navigation(wait_until="networkidle"):
                    page_d.locator("button:has-text('Release Report')").first.click()

                with page_d.expect_download() as dl_info:
                    page_d.locator("a:has-text('Download XLSX')").first.click()
                dl_path = tmp_path / "downloaded_report.xlsx"
                dl_info.value.save_as(str(dl_path))
                wb = openpyxl.load_workbook(str(dl_path), data_only=False)
                assert wb.sheetnames == ["Results", "Comparable Companies", "Sources"]
                ws_res = wb["Results"]
                res_map = {ws_res.cell(r, 1).value: ws_res.cell(r, 2) for r in range(2, ws_res.max_row + 1)}
                assert res_map["Target Total Debt"].value is None
                assert Decimal(str(res_map["Target Cash"].value)) == Decimal(0)
                assert Decimal(str(res_map["Median-Based EV"].value)) > 0
                assert Decimal(str(res_map["EV Arithmetic Midpoint ((Low+High)/2)"].value)) > 0
                assert wb["Sources"].max_row >= 50
                ctx_d.close()
                ctx_a2.close()
            finally:
                browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        with psycopg.connect(db_url) as conn:
            for cid in created_case_ids:
                tids = [
                    r[0]
                    for r in conn.execute(
                        "SELECT thread_id FROM analysis_runs WHERE case_id=%s AND thread_id IS NOT NULL;",
                        (cid,),
                    ).fetchall()
                ]
                for tid in tids:
                    conn.execute("DELETE FROM checkpoint_writes WHERE thread_id=%s;", (tid,))
                    conn.execute("DELETE FROM checkpoint_blobs WHERE thread_id=%s;", (tid,))
                    conn.execute("DELETE FROM checkpoints WHERE thread_id=%s;", (tid,))
                conn.execute("DELETE FROM cases WHERE id=%s;", (cid,))
            for uid in (alice_id, bob_id, carol_id, admin_id):
                conn.execute("DELETE FROM audit_logs WHERE actor_id=%s OR entity_id=%s;", (str(uid), str(uid)))
                conn.execute("DELETE FROM users WHERE id=%s;", (str(uid),))
            conn.commit()
        User.objects.filter(username__endswith=f".{tag}@e2e.local").delete()
