import base64
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import BusinessError, PreservationStore
from retirement_rules import RetirementRules


class RetirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.store = PreservationStore(self.db)
        self.store.seed()
        self.archive = self.store.create_archive("owner", "城市测绘档案", (date.today() + timedelta(days=3650)).isoformat())
        self.raw = b"<record><id>1</id></record>"
        self.version = self.store.ingest_version("owner", self.archive["id"], [
            {"path": "records/one.xml", "content_b64": base64.b64encode(self.raw).decode()},
            {"path": "README.txt", "content_b64": base64.b64encode(b"archive readme").decode()},
        ])
        self.copy1 = self.store.add_copy("owner", self.version["id"], "offline-disk-a")["id"]
        self.copy2 = self.store.add_copy("owner", self.version["id"], "offline-disk-b")["id"]
        # 复核须由另一名具备写权限的管理员办理；审计员具备只读权限
        self.store.grant("owner", self.archive["id"], "archivist", "write")
        self.store.grant("owner", self.archive["id"], "auditor", "read")
        self.rules = RetirementRules(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def _request(self, actor="owner", copy=None, loc="offline-disk-c", custodian="异地介质库-赵保管", note=""):
        return self.rules.request_retirement(actor, copy or self.copy1, loc, custodian, note)

    def _make_retiring_verification_stale(self, copy_id=None):
        """版本生成晚于待退役副本的最近核验，制造“核验不晚于版本生成”。"""
        copy_id = copy_id or self.copy1
        with self.store.connect() as conn:
            conn.execute("UPDATE copies SET last_verified_at='2001-01-01T00:00:00+00:00' WHERE id=?", (copy_id,))
            conn.execute("UPDATE archive_versions SET created_at='2001-06-01T00:00:00+00:00' WHERE id=?", (self.version["id"],))

    def _tamper_other_copy(self, copy_id, path="records/one.xml"):
        with self.store.connect() as conn:
            row = conn.execute("SELECT content FROM copy_files WHERE copy_id=? AND path=?", (copy_id, path)).fetchone()
            damaged = bytes([row["content"][0] ^ 0xFF]) + row["content"][1:]
            conn.execute("UPDATE copy_files SET content=? WHERE copy_id=? AND path=?", (damaged, copy_id, path))
            # 保持 healthy 但记录已“核验”，用于检验内容比对阻塞项

    def test_full_handover_confirm_and_traceability(self):
        req = self._request(custodian="异地介质库-赵保管")
        self.assertEqual(req["status"], "pending")
        self.assertEqual(req["new_location"], "offline-disk-c")
        # 申请人不能自行复核
        with self.assertRaises(BusinessError) as ctx:
            self.rules.review_retirement("owner", req["id"], "confirm")
        self.assertEqual(ctx.exception.status, 403)
        # auditor 不能复核
        with self.assertRaises(BusinessError):
            self.rules.review_retirement("auditor", req["id"], "confirm")
        done = self.rules.review_retirement("archivist", req["id"], "confirm")
        self.assertEqual(done["status"], "confirmed")
        self.assertEqual(done["blockers"], [])
        self.assertEqual(done["reviewed_by"], "archivist")
        # 旧副本标记退役并留档
        detail = self.store.get_version("owner", self.version["id"])
        states = {c["id"]: c["state"] for c in detail["copies"]}
        self.assertEqual(states[self.copy1], "retired")
        self.assertEqual(states[self.copy2], "healthy")
        self.assertEqual(detail["version"]["state"], "verified")
        # 退役后内容仍在，且不能再核验
        with self.store.connect() as conn:
            kept = conn.execute("SELECT COUNT(*) FROM copy_files WHERE copy_id=?", (self.copy1,)).fetchone()[0]
        self.assertEqual(kept, 2)
        with self.assertRaises(BusinessError) as ctx:
            self.store.verify_copy("owner", self.copy1)
        self.assertEqual(ctx.exception.code, "copy_retired")
        # 版本详情可追溯
        copy_info = next(c for c in detail["copies"] if c["id"] == self.copy1)
        self.assertTrue(copy_info["retired"])
        self.assertEqual(copy_info["retirement"]["status"], "confirmed")
        self.assertEqual(copy_info["retirement"]["new_location"], "offline-disk-c")
        self.assertEqual(len(detail["retirements"]), 1)
        # 审计历史可追溯
        status = self.store.archive_status("owner", self.archive["id"])
        actions = {a["action"] for a in status["audit"]}
        self.assertIn("copy.retire_request", actions)
        self.assertIn("copy.retire_confirmed", actions)
        confirmed = next(a for a in status["audit"] if a["action"] == "copy.retire_confirmed")
        self.assertEqual(confirmed["detail"]["remaining_healthy_copies"], 1)
        self.assertEqual(confirmed["detail"]["version_state"], "verified")
        # 已处理的交接单不能重复复核
        with self.assertRaises(BusinessError) as ctx:
            self.rules.review_retirement("archivist", req["id"], "confirm")
        self.assertEqual(ctx.exception.code, "retirement_closed")

    def test_blocked_when_only_copy(self):
        req = self.rules.request_retirement("owner", self.copy2, "disk-c", "保管人钱七")
        # 退役 copy1，使 copy2 成为唯一剩余副本，再申请退役 copy2
        first = self.rules.request_retirement("owner", self.copy1, "disk-d", "保管人钱七")
        self.rules.review_retirement("archivist", first["id"], "confirm")
        result = self.rules.review_retirement("archivist", req["id"], "confirm")
        self.assertEqual(result["status"], "rejected")
        codes = {b["code"] for b in result["blockers"]}
        self.assertIn("no_other_healthy_copy", codes)
        self.assertEqual(next(b for b in result["blockers"] if b["code"] == "no_other_healthy_copy")["message"],
                         "退役后将没有任何健康副本，不能停掉唯一副本")
        # 退回后副本未退役
        detail = self.store.get_version("owner", self.version["id"])
        self.assertEqual(next(c for c in detail["copies"] if c["id"] == self.copy2)["state"], "healthy")

    def test_blocked_on_mismatch_then_pass_after_reverify(self):
        self._tamper_other_copy(self.copy2)
        req = self._request()
        result = self.rules.review_retirement("archivist", req["id"], "confirm")
        self.assertEqual(result["status"], "rejected")
        blocker = next(b for b in result["blockers"] if b["code"] == "copy_content_mismatch")
        self.assertEqual(blocker["copy_id"], self.copy2)
        self.assertEqual(blocker["mismatched"], ["records/one.xml"])
        # 修复并重新核验（从健康源 copy1 修复）
        self.store.verify_copy("owner", self.copy2)
        second = self._request(custodian="异地介质库-孙保管")
        done = self.rules.review_retirement("archivist", second["id"], "confirm")
        self.assertEqual(done["status"], "confirmed")

    def test_blocked_when_verification_not_after_version(self):
        self._make_retiring_verification_stale()
        req = self._request()
        result = self.rules.review_retirement("archivist", req["id"], "confirm")
        self.assertEqual(result["status"], "rejected")
        codes = {b["code"] for b in result["blockers"]}
        self.assertIn("retiring_verification_stale", codes)
        # 重新核验后通过（版本时间早于核验时间）
        self.store.verify_copy("owner", self.copy1)
        second = self._request(custodian="异地介质库-周保管")
        done = self.rules.review_retirement("archivist", second["id"], "confirm")
        self.assertEqual(done["status"], "confirmed")

    def test_explicit_reject_and_request_rules(self):
        # 不能对同一副本重复申请
        first = self._request()
        with self.assertRaises(BusinessError) as ctx:
            self._request()
        self.assertEqual(ctx.exception.code, "retirement_pending")
        # 显式退回
        rejected = self.rules.review_retirement("archivist", first["id"], "reject", note="保管人信息待补")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["blockers"][0]["code"], "manually_rejected")
        # 退回后可重新申请并确认
        second = self._request(custodian="异地介质库-吴保管")
        done = self.rules.review_retirement("archivist", second["id"], "confirm")
        self.assertEqual(done["status"], "confirmed")
        # 已退役副本不能再申请
        with self.assertRaises(BusinessError) as ctx:
            self._request()
        self.assertEqual(ctx.exception.code, "copy_already_retired")
        # 输入校验
        with self.assertRaises(BusinessError) as ctx:
            self.rules.request_retirement("owner", self.copy2, "x", "保管人钱七")
        self.assertEqual(ctx.exception.code, "invalid_location")
        with self.assertRaises(BusinessError) as ctx:
            self.rules.request_retirement("owner", self.copy2, "disk-c", "x")
        self.assertEqual(ctx.exception.code, "invalid_custodian")
        # 非管理员不能申请；未授权用户不能查询
        with self.assertRaises(BusinessError):
            self.rules.request_retirement("auditor", self.copy2, "disk-c", "保管人钱七")
        with self.assertRaises(BusinessError):
            self.rules.get_retirement("outsider", second["id"])
        # 读权限可查
        self.assertEqual(self.rules.get_retirement("auditor", second["id"])["status"], "confirmed")

    def test_retiring_last_copy_stays_blocked_and_version_verified(self):
        # copy1 退役后只剩 copy2 一个健康副本；再退役 copy2 必须被持续阻塞
        first = self._request(copy=self.copy1, loc="disk-c", custodian="保管人钱七")
        self.rules.review_retirement("archivist", first["id"], "confirm")
        second = self.rules.request_retirement("owner", self.copy2, "disk-d", "保管人钱七")
        result = self.rules.review_retirement("archivist", second["id"], "confirm")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("no_other_healthy_copy", {b["code"] for b in result["blockers"]})
        detail = self.store.get_version("owner", self.version["id"])
        self.assertEqual(detail["version"]["state"], "verified")  # 退回不改变版本状态
        self.assertEqual(next(c for c in detail["copies"] if c["id"] == self.copy2)["state"], "healthy")

    def test_schema_migration_from_legacy_copies_table(self):
        # 用“旧定义”（不含 retired）建库再跑 init_schema，验证幂等迁移
        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory()
        legacy_db = Path(self.tmp.name) / "legacy.db"
        conn = sqlite3.connect(legacy_db)
        conn.executescript(
            """
            CREATE TABLE users(id TEXT PRIMARY KEY, name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('owner','archivist','auditor')));
            INSERT INTO users VALUES('owner','负责人','owner'),('archivist','管理员','archivist');
            CREATE TABLE archives(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
                owner_id TEXT NOT NULL REFERENCES users(id), retention_until TEXT NOT NULL,
                restricted INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
            INSERT INTO archives(name,owner_id,retention_until,restricted,created_at)
                VALUES('旧档','owner','2099-01-01',1,'2020-01-01T00:00:00+00:00');
            CREATE TABLE archive_versions(id INTEGER PRIMARY KEY AUTOINCREMENT, archive_id INTEGER NOT NULL,
                version INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'verified', created_by TEXT NOT NULL,
                created_at TEXT NOT NULL);
            INSERT INTO archive_versions(archive_id,version,created_by,created_at)
                VALUES(1,1,'owner','2020-01-02T00:00:00+00:00');
            CREATE TABLE copies(id INTEGER PRIMARY KEY AUTOINCREMENT, version_id INTEGER NOT NULL,
                location TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'healthy'
                CHECK(state IN ('healthy','corrupt','degraded')),
                created_at TEXT NOT NULL, last_verified_at TEXT, UNIQUE(version_id,location));
            INSERT INTO copies(version_id,location,created_at) VALUES(1,'old-location','2020-01-03T00:00:00+00:00');
            """
        )
        conn.commit()
        conn.close()
        # 迁移后旧 CHECK 必须允许 retired，且数据保留
        migrated = PreservationStore(legacy_db)
        migrated.init_schema()
        with migrated.connect() as conn:
            row = conn.execute("SELECT sql FROM sqlite_master WHERE name='copies'").fetchone()
            self.assertIn("retired", row[0])
            self.assertEqual(tuple(conn.execute("SELECT location,state FROM copies").fetchone()),
                             ("old-location", "healthy"))
            conn.execute("SELECT id FROM copy_retirements")  # 交接表存在
        # 再次初始化幂等
        migrated.init_schema()


if __name__ == "__main__":
    unittest.main()
