import csv
import asyncio
import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from portal.app import create_app
from portal.models import Application, User
from portal.security import verify_password
from portal.web import fetch_application_health, health_snapshot


ADMIN_EMAIL = "admin@example.local"
ADMIN_PASSWORD = "Admin-password-2026!"
INTERN_PASSWORD = "Intern-password-2026!"
READER_PASSWORD = "Reader-password-2026!"


class PortalIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "portal-test.db"
        self.app = create_app(
            database_url=f"sqlite:///{database_path}",
            session_secret_key="test-session-secret-key-with-more-than-32-bytes",
            initial_admin={
                "name": "Admin de test",
                "email": ADMIN_EMAIL,
                "password": ADMIN_PASSWORD,
            },
        )
        self.client = TestClient(self.app)
        self.client.__enter__()
        health_patcher = patch(
            "portal.web.fetch_application_health",
            new=AsyncMock(
                side_effect=lambda service_names, prometheus_url: {
                    name: health_snapshot("no-data")
                    for name in service_names
                }
            ),
        )
        health_patcher.start()
        self.addCleanup(health_patcher.stop)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp_dir.cleanup()

    @staticmethod
    def csrf(response) -> str:
        match = re.search(
            r'name="csrf_token"\s+value="([^"]+)"',
            response.text,
        )
        if match is None:
            raise AssertionError("Page does not contain a CSRF token.")
        return match.group(1)

    def login(self, client=None, email=ADMIN_EMAIL, password=ADMIN_PASSWORD):
        client = client or self.client
        page = client.get("/login")
        response = client.post(
            "/login",
            data={
                "csrf_token": self.csrf(page),
                "email": email,
                "password": password,
            },
        )
        return response

    def create_user(self, name, email, role, password):
        page = self.client.get("/users/new")
        return self.client.post(
            "/users/new",
            data={
                "csrf_token": self.csrf(page),
                "name": name,
                "email": email,
                "role": role,
                "password": password,
            },
        )

    def create_application(
        self,
        service_name="test-api",
        name="API de test",
        owner_id=None,
    ):
        page = self.client.get("/applications/new")
        if owner_id is None:
            with self.app.state.session_factory() as session:
                owner_id = session.query(User.id).filter(User.role == "stagiaire").scalar()
        return self.client.post(
            "/applications/new",
            data={
                "csrf_token": self.csrf(page),
                "name": name,
                "service_name": service_name,
                "description": "Application de test fonctionnel.",
                "language": "Python",
                "repository_url": "https://git.example.local/team/test-api",
                "version": "1.2.3",
                "internship_start": "2026-01-01",
                "internship_end": "2026-12-31",
                "owner_id": str(owner_id),
            },
        )

    def prepare_accounts_and_application(self):
        self.assertEqual(self.login().status_code, 200)
        intern = self.create_user(
            "Stagiaire A",
            "stagiaire-a@example.local",
            "stagiaire",
            INTERN_PASSWORD,
        )
        self.assertEqual(intern.status_code, 200)
        reader = self.create_user(
            "Lecteur",
            "lecteur@example.local",
            "lecteur",
            READER_PASSWORD,
        )
        self.assertEqual(reader.status_code, 200)
        with self.app.state.session_factory() as session:
            owner_id = (
                session.query(User.id)
                .filter(User.email == "stagiaire-a@example.local")
                .scalar()
            )
        created = self.create_application(owner_id=owner_id)
        self.assertEqual(created.status_code, 200)
        with self.app.state.session_factory() as session:
            application_id = (
                session.query(Application.id)
                .filter(Application.service_name == "test-api")
                .scalar()
            )
        return owner_id, application_id

    def test_login_csrf_and_secure_session(self):
        page = self.client.get("/login")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Connexion", page.text)
        self.assertEqual(self.client.get("/applications/export.csv").status_code, 401)
        self.assertEqual(
            self.client.post(
                "/login",
                data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.login(password="incorrect-password").status_code,
            400,
        )
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Applications suivies", response.text)
        cookie = self.client.cookies.get("stagiaires_session")
        self.assertIsNotNone(cookie)

    def test_admin_creates_application_and_history_records_changes(self):
        self.assertEqual(self.login().status_code, 200)
        self.create_user(
            "Stagiaire A",
            "stagiaire-a@example.local",
            "stagiaire",
            INTERN_PASSWORD,
        )
        with self.app.state.session_factory() as session:
            owner_id = (
                session.query(User.id)
                .filter(User.email == "stagiaire-a@example.local")
                .scalar()
            )
        create_response = self.create_application(owner_id=owner_id)
        self.assertEqual(create_response.status_code, 200)
        self.assertIn("Application enregistrée", create_response.text)
        detail = self.client.get("/applications/1")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("test-api", detail.text)
        self.assertIn("Fiche créée", detail.text)
        self.assertIn('data-health-status="no-data"', detail.text)
        self.assertIn("/grafana/d/stagiaires-applications/applications-stagiaires", detail.text)
        health = self.client.get("/applications/1/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "no-data")

        edit_page = self.client.get("/applications/1/edit")
        edit = self.client.post(
            "/applications/1/edit",
            data={
                "csrf_token": self.csrf(edit_page),
                "name": "API de test mise à jour",
                "service_name": "test-api",
                "description": "Description mise à jour.",
                "language": "Python",
                "repository_url": "https://git.example.local/team/test-api",
                "version": "2.0.0",
                "internship_start": "2026-01-01",
                "internship_end": "2026-12-31",
                "owner_id": str(owner_id),
            },
        )
        self.assertEqual(edit.status_code, 200)
        updated = self.client.get("/applications/1")
        self.assertIn("API de test mise à jour", updated.text)
        self.assertIn("Fiche modifiée", updated.text)

    def test_intern_can_only_view_and_edit_owned_applications(self):
        owner_a, application_a = self.prepare_accounts_and_application()
        self.create_user(
            "Stagiaire B",
            "stagiaire-b@example.local",
            "stagiaire",
            "Intern-B-password-2026!",
        )
        with self.app.state.session_factory() as session:
            owner_b = (
                session.query(User.id)
                .filter(User.email == "stagiaire-b@example.local")
                .scalar()
            )
        second = self.create_application(
            service_name="other-api",
            name="Autre API",
            owner_id=owner_b,
        )
        self.assertEqual(second.status_code, 200)

        intern_client = TestClient(self.app)
        self.addCleanup(intern_client.close)
        self.assertEqual(
            self.login(
                intern_client,
                "stagiaire-a@example.local",
                INTERN_PASSWORD,
            ).status_code,
            200,
        )
        visible = intern_client.get("/applications")
        self.assertIn("test-api", visible.text)
        self.assertNotIn("other-api", visible.text)
        self.assertIn("/applications/export.csv?", visible.text)
        self.assertNotIn("owner_id=", visible.text)
        self.assertEqual(
            intern_client.get("/applications/export.csv?status=all").status_code,
            200,
        )
        self.assertEqual(intern_client.get(f"/applications/{application_a}").status_code, 200)
        self.assertEqual(intern_client.get("/applications/2").status_code, 404)
        self.assertEqual(intern_client.get("/applications/2/health").status_code, 404)
        self.assertEqual(intern_client.get("/applications/2/edit").status_code, 404)
        self.assertEqual(owner_a, 2)

    def test_reader_is_read_only_and_admin_routes_are_protected(self):
        self.prepare_accounts_and_application()
        reader = TestClient(self.app)
        self.addCleanup(reader.close)
        self.assertEqual(
            self.login(reader, "lecteur@example.local", READER_PASSWORD).status_code,
            200,
        )
        self.assertEqual(reader.get("/applications").status_code, 200)
        self.assertEqual(reader.get("/applications/new").status_code, 403)
        self.assertEqual(reader.get("/applications/export.csv").status_code, 200)
        detail = reader.get("/applications/1")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(reader.get("/users").status_code, 403)
        self.assertEqual(reader.get("/users/new").status_code, 403)

    def test_health_filter_and_csv_export_respect_scope_and_escape_formulas(self):
        owner_id, _ = self.prepare_accounts_and_application()
        self.create_user(
            "Stagiaire B",
            "stagiaire-b@example.local",
            "stagiaire",
            "Intern-B-password-2026!",
        )
        with self.app.state.session_factory() as session:
            owner_b = (
                session.query(User.id)
                .filter(User.email == "stagiaire-b@example.local")
                .scalar()
            )
            application = session.query(Application).filter_by(service_name="test-api").one()
            application.name = '=HYPERLINK("https://example.invalid")'
            session.commit()
        self.assertEqual(
            self.create_application(
                service_name="healthy-api",
                name="API opérationnelle",
                owner_id=owner_b,
            ).status_code,
            200,
        )
        with patch(
            "portal.web.fetch_application_health",
            new=AsyncMock(
                return_value={
                    "test-api": health_snapshot("no-data"),
                    "healthy-api": health_snapshot("healthy", 12),
                }
            ),
        ):
            filtered = self.client.get("/applications?health=healthy")
            self.assertIn("healthy-api", filtered.text)
            self.assertNotIn("test-api", filtered.text)
            self.assertIn("health=healthy", filtered.text)

            export = self.client.get("/applications/export.csv?health=healthy")
            self.assertEqual(export.status_code, 200)
            self.assertIn("text/csv", export.headers["content-type"])
            self.assertIn("attachment;", export.headers["content-disposition"])
            rows = list(
                csv.reader(
                    io.StringIO(export.content.decode("utf-8-sig")),
                    delimiter=";",
                )
            )
            self.assertEqual(rows[0][0], "Application")
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1][1], "healthy-api")

            all_export = self.client.get("/applications/export.csv")
            all_rows = list(
                csv.reader(
                    io.StringIO(all_export.content.decode("utf-8-sig")),
                    delimiter=";",
                )
            )
            risky_row = next(row for row in all_rows[1:] if row[1] == "test-api")
            self.assertTrue(risky_row[0].startswith("'=HYPERLINK("))
            self.assertEqual(len(all_rows), 3)

            intern = TestClient(self.app)
            self.addCleanup(intern.close)
            self.assertEqual(
                self.login(
                    intern,
                    "stagiaire-a@example.local",
                    INTERN_PASSWORD,
                ).status_code,
                200,
            )
            scoped_export = intern.get("/applications/export.csv?status=all")
            scoped_rows = list(
                csv.reader(
                    io.StringIO(scoped_export.content.decode("utf-8-sig")),
                    delimiter=";",
                )
            )
            self.assertEqual(len(scoped_rows), 2)
            self.assertEqual(scoped_rows[1][1], "test-api")
            self.assertNotIn("healthy-api", scoped_export.text)

    def test_csrf_validation_duplicate_service_and_archival(self):
        self.prepare_accounts_and_application()
        page = self.client.get("/applications/new")
        duplicate = self.client.post(
            "/applications/new",
            data={
                "csrf_token": self.csrf(page),
                "name": "Copie",
                "service_name": "test-api",
                "owner_id": "2",
            },
        )
        self.assertEqual(duplicate.status_code, 409)
        self.assertIn("déjà utilisé", duplicate.text)

        detail = self.client.get("/applications/1")
        self.assertEqual(
            self.client.post(
                "/applications/1/archive",
                data={"csrf_token": "invalid"},
            ).status_code,
            403,
        )
        archived = self.client.post(
            "/applications/1/archive",
            data={"csrf_token": self.csrf(detail)},
        )
        self.assertEqual(archived.status_code, 200)
        self.assertNotIn("test-api", self.client.get("/applications").text)
        self.assertIn("test-api", self.client.get("/applications?status=archived").text)
        self.assertIn("Application archivée", self.client.get("/applications/1").text)

    def test_password_change_and_account_deactivation(self):
        self.assertEqual(self.login().status_code, 200)
        self.create_user(
            "Stagiaire à désactiver",
            "inactive@example.local",
            "stagiaire",
            "Inactive-password-2026!",
        )
        password_page = self.client.get("/account/password")
        changed = self.client.post(
            "/account/password",
            data={
                "csrf_token": self.csrf(password_page),
                "current_password": ADMIN_PASSWORD,
                "new_password": "Admin-password-updated-2026!",
                "confirm_password": "Admin-password-updated-2026!",
            },
        )
        self.assertEqual(changed.status_code, 200)
        with self.app.state.session_factory() as session:
            admin = session.query(User).filter(User.email == ADMIN_EMAIL).one()
            self.assertTrue(verify_password(admin.password_hash, "Admin-password-updated-2026!"))
            inactive_id = session.query(User.id).filter(User.email == "inactive@example.local").scalar()
        csrf = self.csrf(self.client.get("/users"))
        self.client.post(
            f"/users/{inactive_id}/toggle",
            data={"csrf_token": csrf},
        )
        with self.app.state.session_factory() as session:
            inactive = session.get(User, inactive_id)
            self.assertFalse(inactive.is_active)

        deactivated_client = TestClient(self.app)
        self.addCleanup(deactivated_client.close)
        response = self.login(
            deactivated_client,
            "inactive@example.local",
            "Inactive-password-2026!",
        )
        self.assertEqual(response.status_code, 400)

    def test_prometheus_heartbeat_age_maps_to_health_states(self):
        samples = [
            {"metric": {"service_name": "healthy-api"}, "value": [0, "60"]},
            {"metric": {"service_name": "warning-api"}, "value": [0, "61"]},
            {"metric": {"service_name": "threshold-api"}, "value": [0, "300"]},
            {"metric": {"service_name": "down-api"}, "value": [0, "301"]},
        ]
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"status": "success", "data": {"resultType": "vector", "result": samples}},
            )
        )
        async_client = httpx.AsyncClient
        with patch(
            "portal.web.httpx.AsyncClient",
            side_effect=lambda **kwargs: async_client(
                transport=transport,
                **kwargs,
            ),
        ):
            health = asyncio.run(
                fetch_application_health(
                    [
                        "healthy-api",
                        "warning-api",
                        "threshold-api",
                        "down-api",
                        "new-api",
                    ],
                    "http://prometheus",
                )
            )
        self.assertEqual(health["healthy-api"]["status"], "healthy")
        self.assertEqual(health["warning-api"]["status"], "warning")
        self.assertEqual(health["threshold-api"]["status"], "warning")
        self.assertEqual(health["down-api"]["status"], "unavailable")
        self.assertEqual(health["new-api"]["status"], "no-data")

    def test_prometheus_failure_produces_explicit_unknown_health(self):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(503, text="temporarily unavailable")
        )
        async_client = httpx.AsyncClient
        with patch(
            "portal.web.httpx.AsyncClient",
            side_effect=lambda **kwargs: async_client(
                transport=transport,
                **kwargs,
            ),
        ):
            health = asyncio.run(
                fetch_application_health(["test-api"], "http://prometheus")
            )
        self.assertEqual(health["test-api"]["status"], "unknown")
        self.assertIn("Prometheus est indisponible", health["test-api"]["description"])


if __name__ == "__main__":
    unittest.main()
