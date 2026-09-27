import base64
import hashlib
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import BusinessError, PreservationStore


class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PreservationStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.archive = self.store.create_archive("owner", "城市测绘档案", (date.today() + timedelta(days=3650)).isoformat())
        self.raw = b"<record><id>1</id></record>"
        self.version = self.store.ingest_version("owner", self.archive["id"], [
            {"path": "records/one.xml", "content_b64": base64.b64encode(self.raw).decode()},
            {"path": "README.txt", "content_b64": base64.b64encode(b"archive readme").decode()},
        ])
        self.copy1 = self.store.add_copy("owner", self.version["id"], "offline-disk-a")["id"]
        self.copy2 = self.store.add_copy("owner", self.version["id"], "offline-disk-b")["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_integrity_repair_and_format_migration(self):
        self.store.simulate_corruption("owner", self.copy1, "records/one.xml")
        result = self.store.verify_copy("owner", self.copy1)
        self.assertEqual(result["state"], "healthy")
        self.assertTrue(result["repaired"])
        self.assertEqual(result["corrupt_paths"], ["records/one.xml"])
        migrated = self.store.migrate(
            "owner", self.version["id"], "records/one.xml", "records/one.html", "html",
            base64.b64encode(b"<html><body><p>1</p></body></html>").decode(),
        )
        detail = self.store.get_version("owner", migrated["id"])
        self.assertEqual(detail["version"]["version"], 2)
        self.assertTrue(any(f["path"] == "records/one.html" for f in detail["files"]))
        status = self.store.archive_status("owner", self.archive["id"])
        self.assertGreater(status["days_remaining"], 3000)

    def test_restricted_access_and_invalid_manifest_are_rejected(self):
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_version("outsider", self.version["id"])
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.ingest_version("owner", self.archive["id"], [{"path": "../escape.txt", "content_b64": "eA=="}])
        self.assertEqual(ctx.exception.code, "unsafe_path")
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_copy("owner", self.version["id"], "offline-disk-a")
        self.assertEqual(ctx.exception.code, "copy_exists")


class RetirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PreservationStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.archive = self.store.create_archive("owner", "古籍扫描档案", (date.today() + timedelta(days=3650)).isoformat())
        self.version = self.store.ingest_version("owner", self.archive["id"], [
            {"path": "pages/one.txt", "content_b64": base64.b64encode(b"page one").decode()},
        ])
        self.copy1 = self.store.add_copy("owner", self.version["id"], "offline-disk-a")["id"]
        self.copy2 = self.store.add_copy("owner", self.version["id"], "offline-disk-b")["id"]
        self.store.grant("owner", self.archive["id"], "archivist", "write")
        self.store.grant("owner", self.archive["id"], "auditor", "read")
        # 固定版本生成时间，让“最近核验晚于版本生成”在测试中可控
        with self.store.connect() as conn:
            conn.execute("UPDATE archive_versions SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", (self.version["id"],))

    def tearDown(self):
        self.tmp.cleanup()

    def test_confirm_retirement_recalculates_version_state(self):
        req = self.store.request_retirement("owner", self.copy1, "offline-disk-c", "保管人甲")
        self.assertEqual(req["state"], "pending")
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_retirement("owner", self.copy1, "offline-disk-d", "保管人乙")
        self.assertEqual(ctx.exception.code, "retirement_pending")
        result = self.store.review_retirement("archivist", req["id"])
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual(result["remaining_healthy_copies"], 1)
        self.assertEqual(result["version_state"], "verified")
        self.assertTrue(result["comparisons"][0]["matched"])
        detail = self.store.get_version("owner", self.version["id"])
        copies = {c["id"]: c for c in detail["copies"]}
        self.assertIsNotNone(copies[self.copy1]["retired_at"])
        self.assertIsNone(copies[self.copy2]["retired_at"])
        self.assertEqual(detail["version"]["state"], "verified")
        record = self.store.get_retirement_request("auditor", req["id"])
        self.assertEqual(record["state"], "confirmed")
        self.assertEqual(record["custodian"], "保管人甲")
        self.assertEqual(record["successor_location"], "offline-disk-c")
        self.assertEqual(record["reviewed_by"], "archivist")
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_retirement("owner", self.copy1, "offline-disk-e", "保管人丙")
        self.assertEqual(ctx.exception.code, "copy_retired")
        with self.assertRaises(BusinessError) as ctx:
            self.store.review_retirement("owner", req["id"])
        self.assertEqual(ctx.exception.code, "retirement_closed")
        listed = self.store.list_retirements("owner", self.version["id"])
        self.assertEqual([r["id"] for r in listed], [req["id"]])
        status = self.store.archive_status("owner", self.archive["id"])
        actions = [a["action"] for a in status["audit"]]
        self.assertIn("retirement.request", actions)
        self.assertIn("retirement.review", actions)

    def test_review_returns_blockers_when_conditions_fail(self):
        with self.store.connect() as conn:
            # 条件一不满足：退役副本最近核验早于版本生成
            conn.execute("UPDATE copies SET last_verified_at='2019-01-01T00:00:00+00:00' WHERE id=?", (self.copy1,))
            # 条件二不满足：唯一其他副本内容被改坏但状态仍为 healthy
            conn.execute("UPDATE copy_files SET content=? WHERE copy_id=? AND path='pages/one.txt'", (b"tampered", self.copy2))
        req = self.store.request_retirement("owner", self.copy1, "offline-disk-c", "保管人甲")
        pending = self.store.get_retirement_request("owner", req["id"])
        self.assertEqual(len(pending["pending_blockers"]), 2)
        result = self.store.review_retirement("archivist", req["id"])
        self.assertEqual(result["state"], "returned")
        self.assertEqual(len(result["blockers"]), 2)
        self.assertTrue(any("最近核验" in b for b in result["blockers"]))
        self.assertTrue(any("比对" in b for b in result["blockers"]))
        detail = self.store.get_version("owner", self.version["id"])
        self.assertTrue(all(c["retired_at"] is None for c in detail["copies"]))
        # 退回后阻塞项留档，且允许重新登记
        record = self.store.get_retirement_request("owner", req["id"])
        self.assertEqual(record["state"], "returned")
        self.assertEqual(len(record["blockers"]), 2)
        req2 = self.store.request_retirement("owner", self.copy1, "offline-disk-c", "保管人甲")
        self.assertEqual(req2["state"], "pending")

    def test_single_copy_retirement_is_blocked(self):
        self.store.simulate_corruption("owner", self.copy2, "pages/one.txt")
        req = self.store.request_retirement("owner", self.copy1, "offline-disk-c", "保管人甲")
        result = self.store.review_retirement("owner", req["id"])
        self.assertEqual(result["state"], "returned")
        self.assertTrue(any("没有其他健康副本" in b for b in result["blockers"]))

    def test_retirement_requires_write_access(self):
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_retirement("outsider", self.copy1, "offline-disk-c", "保管人甲")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_retirement("owner", self.copy1, "", "保管人甲")
        self.assertEqual(ctx.exception.code, "invalid_location")
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_retirement("owner", self.copy1, "offline-disk-c", " ")
        self.assertEqual(ctx.exception.code, "invalid_custodian")


if __name__ == "__main__":
    unittest.main()
