"""Provision reproducible demo accounts and cases using synthetic deal figures."""
from __future__ import annotations

import os
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User

from covenant_ai.app.admin import AdminIdentityService
from covenant_ai.app.cases import CaseService
from covenant_ai.app.runs import ensure_local_db_url
from covenant_ai.providers.postgres_repo import PostgresCaseRepository
from deals.auth_service import load_request_actor, provision_web_user


DEMO_ACCOUNTS = (
    ("analyst.alice@covenant.local", "CovenantDemo2026!", "analyst", False),
    ("analyst.bob@covenant.local", "CovenantDemo2026!", "analyst", False),
    ("reviewer.carol@covenant.local", "CovenantDemo2026!", "reviewer", False),
    ("admin.dave@covenant.local", "CovenantDemo2026!", "admin", True),
)


class Command(BaseCommand):
    help = "Seed local demo Analyst, Reviewer, Release Admin users and 2 isolated demo cases."

    def handle(self, *args, **options) -> None:
        db_url = ensure_local_db_url(os.getenv("DATABASE_URL"))
        repo = PostgresCaseRepository(db_url=db_url)
        admin_svc = AdminIdentityService(db_url)
        cases = CaseService(repo)

        user_map = {}
        for email, pw, role, can_rel in DEMO_ACCOUNTS:
            existing = User.objects.filter(username=email).first()
            if existing is None:
                dj_user, uid = provision_web_user(
                    email=email,
                    password=pw,
                    role=role,
                    can_release_report=can_rel,
                    is_staff_admin=(role == "admin"),
                    db_url=db_url,
                )
            else:
                actor = load_request_actor(existing, db_url)
                uid = actor.user_id
                dj_user = existing
            user_map[email] = (dj_user, uid)

        alice_actor = load_request_actor(user_map["analyst.alice@covenant.local"][0], db_url)
        bob_actor = load_request_actor(user_map["analyst.bob@covenant.local"][0], db_url)
        carol_uid = user_map["reviewer.carol@covenant.local"][1]
        dave_uid = user_map["admin.dave@covenant.local"][1]

        alice_cases = cases.list_cases(alice_actor, search="Demo Controls")
        if not alice_cases:
            c1 = cases.create_case(
                "Demo Controls Manufacturing",
                intake_answers={
                    "company_name": "Demo Controls Manufacturing",
                    "business_description": "Synthetic industrial machinery controls business.",
                    "industry": "industrial_machinery",
                    "geography": "US",
                    "ebitda_period": "FY",
                    "revenue": "32000000",
                    "ebitda": "4800000",
                    "total_debt": "6000000",
                    "cash": "1500000",
                },
                management_mode="web_managed",
                actor=alice_actor,
            )
            admin_svc.assign_case_member(case_id=c1.case_id, user_id=carol_uid, role="reviewer", actor_id=dave_uid)
            admin_svc.assign_case_member(case_id=c1.case_id, user_id=dave_uid, role="release_admin", actor_id=dave_uid)
            self.stdout.write(self.style.SUCCESS(f"Seeded Alice demo case {c1.case_id}"))

        bob_cases = cases.list_cases(bob_actor, search="Demo Retail Snacks")
        if not bob_cases:
            c2 = cases.create_case(
                "Demo Retail Snacks",
                intake_answers={
                    "company_name": "Demo Retail Snacks",
                    "business_description": "Synthetic regional packaged snacks producer.",
                    "industry": "packaged_foods",
                    "geography": "US",
                    "ebitda_period": "FY",
                    "revenue": "25000000",
                    "ebitda": "3500000",
                },
                management_mode="web_managed",
                actor=bob_actor,
            )
            admin_svc.assign_case_member(case_id=c2.case_id, user_id=carol_uid, role="reviewer", actor_id=dave_uid)
            self.stdout.write(self.style.SUCCESS(f"Seeded Bob isolated case {c2.case_id}"))

        self.stdout.write(self.style.SUCCESS("Demo accounts and cases are ready."))
