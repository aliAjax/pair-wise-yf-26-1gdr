"""副本退役交接 —— 规则层。

业务流程（介质更换 / 副本退役）：

1. ``request_retirement``：选定待退役副本，登记接手位置与保管人，生成
   ``pending`` 交接单等待复核；同一副本同时只能有一张待复核单。
2. ``review_retirement`` 确认时必须同时满足：
   - 除退役副本外仍有至少一个健康副本，且这些健康副本最近都完成过与版本
     权威内容的逐文件比对（路径集合、SHA-256 全部一致）；
   - 退役副本的最近核验时间晚于该版本的生成时间，保证退役依据针对当前版本。
   任一不满足则交接单退回（``rejected``）并在 ``blockers`` 中列出阻塞项；
   复核人也可显式退回。
3. 确认后旧副本状态置为 ``retired`` 并保留全部内容留档，版本状态按剩余健康
   副本数重算（一个健康副本都没有则 ``degraded``，否则 ``verified``）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from app import BusinessError, now

MANAGER_ROLES = {"owner", "archivist"}
READER_ROLES = {"owner", "archivist", "auditor"}


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class RetirementRules:
    """以鸭子类型接入 PreservationStore：只用到其 ``connect`` 方法。"""

    def __init__(self, store):
        self.store = store

    # ---- 通用校验 -------------------------------------------------------

    def _manager(self, conn, user_id: str):
        if not user_id:
            raise BusinessError("缺少 X-User-Id", 401, "authentication_required")
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise BusinessError("用户不存在", 401, "unknown_user")
        if user["role"] not in MANAGER_ROLES:
            raise BusinessError("仅 owner 或 archivist 可办理退役交接", 403, "forbidden")
        return user

    def _reader(self, conn, user_id: str):
        if not user_id:
            raise BusinessError("缺少 X-User-Id", 401, "authentication_required")
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise BusinessError("用户不存在", 401, "unknown_user")
        if user["role"] not in READER_ROLES:
            raise BusinessError("当前角色无权查看交接信息", 403, "forbidden")
        return user

    @staticmethod
    def _audit(conn, archive_id: int, actor: str, action: str, detail: dict) -> None:
        conn.execute(
            "INSERT INTO audit_log(archive_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (archive_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), now()),
        )

    @staticmethod
    def _load_copy(conn, copy_id: int):
        copy = conn.execute("SELECT * FROM copies WHERE id=?", (copy_id,)).fetchone()
        if not copy:
            raise BusinessError("副本不存在", 404, "not_found")
        version = conn.execute("SELECT * FROM archive_versions WHERE id=?", (copy["version_id"],)).fetchone()
        return copy, version

    @staticmethod
    def _load_retirement(conn, retirement_id: int):
        row = conn.execute("SELECT * FROM copy_retirements WHERE id=?", (retirement_id,)).fetchone()
        if not row:
            raise BusinessError("交接单不存在", 404, "not_found")
        return row

    @staticmethod
    def _require_archive_access(conn, archive_id: int, user, require_write: bool):
        archive = conn.execute("SELECT * FROM archives WHERE id=?", (archive_id,)).fetchone()
        if not archive:
            raise BusinessError("档案不存在", 404, "not_found")
        if archive["owner_id"] == user["id"]:
            return
        row = conn.execute(
            "SELECT permission FROM archive_members WHERE archive_id=? AND user_id=?",
            (archive_id, user["id"]),
        ).fetchone()
        if not row or (require_write and row["permission"] != "write"):
            raise BusinessError("没有该受限档案的访问权限", 403, "forbidden")

    # ---- 内容比对 -------------------------------------------------------

    @staticmethod
    def _compare_copy_to_version(conn, version_id: int, copy_id: int) -> dict:
        """逐文件比对副本内容与版本权威清单。"""
        expected = {
            r["path"]: r
            for r in conn.execute(
                "SELECT path,sha256,size FROM archive_files WHERE version_id=?", (version_id,)
            ).fetchall()
        }
        stored = {
            r["path"]: r
            for r in conn.execute(
                "SELECT path,sha256,size,content FROM copy_files WHERE copy_id=?", (copy_id,)
            ).fetchall()
        }
        missing = sorted(set(expected) - set(stored))
        extra = sorted(set(stored) - set(expected))
        mismatched = sorted(
            p for p in set(expected) & set(stored)
            if hashlib.sha256(stored[p]["content"]).hexdigest() != expected[p]["sha256"]
            or len(stored[p]["content"]) != expected[p]["size"]
        )
        return {"match": not (missing or extra or mismatched),
                "missing": missing, "extra": extra, "mismatched": mismatched}

    # ---- 阻塞项评估 -----------------------------------------------------

    def _evaluate_blockers(self, conn, version, retiring_copy: sqlite3.Row) -> tuple[list[dict], list[dict]]:
        """返回 (阻塞项, 其他健康副本的比对结果)。"""
        blockers: list[dict] = []
        others = conn.execute(
            "SELECT id,location,state,last_verified_at FROM copies WHERE version_id=? AND id<>?",
            (version["id"], retiring_copy["id"]),
        ).fetchall()
        healthy = [c for c in others if c["state"] == "healthy"]
        comparisons: list[dict] = []
        if not healthy:
            blockers.append({"code": "no_other_healthy_copy",
                             "message": "退役后将没有任何健康副本，不能停掉唯一副本"})
        for copy in healthy:
            result = self._compare_copy_to_version(conn, version["id"], copy["id"])
            entry = {"copy_id": copy["id"], "location": copy["location"], **result}
            comparisons.append(entry)
            if not copy["last_verified_at"]:
                blockers.append({"code": "copy_never_verified",
                                 "message": f"健康副本 {copy['location']} 从未核验",
                                 "copy_id": copy["id"], "location": copy["location"]})
            elif not result["match"]:
                blockers.append({"code": "copy_content_mismatch",
                                 "message": f"健康副本 {copy['location']} 与版本内容比对不一致",
                                 "copy_id": copy["id"], "location": copy["location"],
                                 "missing": result["missing"], "extra": result["extra"],
                                 "mismatched": result["mismatched"]})
        verified_at = _parse_ts(retiring_copy["last_verified_at"])
        created_at = _parse_ts(version["created_at"])
        if not verified_at:
            blockers.append({"code": "retiring_copy_never_verified",
                             "message": "待退役副本缺少最近核验记录"})
        elif created_at and not verified_at > created_at:
            blockers.append({"code": "retiring_verification_stale",
                             "message": "待退役副本的最近核验早于或等于版本生成时间，需按当前版本重新核验",
                             "last_verified_at": retiring_copy["last_verified_at"],
                             "version_created_at": version["created_at"]})
        return blockers, comparisons

    # ---- 1) 申请交接 ----------------------------------------------------

    def request_retirement(self, user_id: str, copy_id: int, new_location: str, custodian: str, note: str = "") -> dict:
        new_location = new_location.strip()
        custodian = custodian.strip()
        if len(new_location) < 2:
            raise BusinessError("接手位置不能为空", 422, "invalid_location")
        if len(custodian) < 2:
            raise BusinessError("保管人不能为空", 422, "invalid_custodian")
        with self.store.connect() as conn:
            user = self._manager(conn, user_id)
            copy, version = self._load_copy(conn, copy_id)
            self._require_archive_access(conn, version["archive_id"], user, require_write=True)
            if copy["state"] == "retired":
                raise BusinessError("该副本已退役", 409, "copy_already_retired")
            existing = conn.execute(
                "SELECT id FROM copy_retirements WHERE copy_id=? AND status='pending'", (copy_id,)
            ).fetchone()
            if existing:
                raise BusinessError("该副本已有待复核的交接单", 409, "retirement_pending")
            cur = conn.execute(
                """INSERT INTO copy_retirements(copy_id,version_id,archive_id,retiring_location,
                      new_location,custodian,status,note,requested_by,requested_at)
                   VALUES(?,?,?,?,?,?, 'pending', ?,?,?)""",
                (copy_id, version["id"], version["archive_id"], copy["location"],
                 new_location, custodian, note.strip(), user_id, now()),
            )
            retirement_id = cur.lastrowid
            self._audit(conn, version["archive_id"], user_id, "copy.retire_request",
                        {"retirement_id": retirement_id, "copy_id": copy_id,
                         "version_id": version["id"], "retiring_location": copy["location"],
                         "new_location": new_location, "custodian": custodian, "note": note.strip()})
            return self._serialize(self._load_retirement(conn, retirement_id))

    # ---- 2) 复核确认 / 退回 ---------------------------------------------

    def review_retirement(self, user_id: str, retirement_id: int, decision: str, note: str = "") -> dict:
        decision = decision.strip().lower()
        if decision not in {"confirm", "reject"}:
            raise BusinessError("decision 必须是 confirm 或 reject", 422, "invalid_decision")
        with self.store.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                user = self._manager(conn, user_id)
                retirement = self._load_retirement(conn, retirement_id)
                self._require_archive_access(conn, retirement["archive_id"], user, require_write=True)
                if retirement["status"] != "pending":
                    raise BusinessError("交接单已处理，不能重复复核", 409, "retirement_closed")
                if retirement["requested_by"] == user_id:
                    raise BusinessError("交接单需由申请之外的管理员复核", 403, "reviewer_conflict")
                copy, version = self._load_copy(conn, retirement["copy_id"])
                if copy["state"] == "retired":
                    raise BusinessError("该副本已退役", 409, "copy_already_retired")

                blockers, comparisons = self._evaluate_blockers(conn, version, copy)
                if decision == "reject" or blockers:
                    if decision == "reject" and not blockers:
                        blockers = [{"code": "manually_rejected",
                                     "message": note.strip() or "复核人退回，需重新办理交接"}]
                    conn.execute(
                        """UPDATE copy_retirements SET status='rejected',blockers=?,note=?,
                               reviewed_by=?,reviewed_at=? WHERE id=?""",
                        (json.dumps(blockers, ensure_ascii=False), note.strip(), user_id, now(), retirement_id),
                    )
                    self._audit(conn, retirement["archive_id"], user_id, "copy.retire_rejected",
                                {"retirement_id": retirement_id, "copy_id": copy["id"],
                                 "decision": decision, "blockers": blockers})
                    conn.commit()
                    return self._serialize(self._load_retirement(conn, retirement_id))

                conn.execute("UPDATE copies SET state='retired' WHERE id=?", (copy["id"],))
                version_state = self._recompute_version_state(conn, version["id"])
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM copies WHERE version_id=? AND state='healthy'", (version["id"],)
                ).fetchone()[0]
                conn.execute(
                    """UPDATE copy_retirements SET status='confirmed',note=?,reviewed_by=?,reviewed_at=?
                       WHERE id=?""",
                    (note.strip(), user_id, now(), retirement_id),
                )
                self._audit(conn, retirement["archive_id"], user_id, "copy.retire_confirmed",
                            {"retirement_id": retirement_id, "copy_id": copy["id"],
                             "version_id": version["id"], "retiring_location": copy["location"],
                             "new_location": retirement["new_location"], "custodian": retirement["custodian"],
                             "remaining_healthy_copies": remaining, "version_state": version_state,
                             "healthy_copy_checks": comparisons})
                conn.commit()
                return self._serialize(self._load_retirement(conn, retirement_id))
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _recompute_version_state(conn, version_id: int) -> str:
        """按剩余健康副本重算版本状态：无健康副本 degraded，否则 verified。"""
        healthy = conn.execute(
            "SELECT COUNT(*) FROM copies WHERE version_id=? AND state='healthy'", (version_id,)
        ).fetchone()[0]
        state = "verified" if healthy > 0 else "degraded"
        conn.execute("UPDATE archive_versions SET state=? WHERE id=?", (state, version_id))
        return state

    # ---- 查询与追溯 -----------------------------------------------------

    def get_retirement(self, user_id: str, retirement_id: int) -> dict:
        with self.store.connect() as conn:
            user = self._reader(conn, user_id)
            retirement = self._load_retirement(conn, retirement_id)
            self._require_archive_access(conn, retirement["archive_id"], user, require_write=False)
            return self._serialize(retirement)

    @staticmethod
    def annotate_version_detail(conn, payload: dict) -> dict:
        """在 get_version 返回体上挂接退役追溯信息。"""
        version_id = payload["version"]["id"]
        for copy in payload.get("copies", []):
            copy["retired"] = copy.get("state") == "retired"
            latest = conn.execute(
                """SELECT id,new_location,custodian,status,requested_by,requested_at,
                          reviewed_by,reviewed_at FROM copy_retirements
                   WHERE copy_id=? ORDER BY id DESC LIMIT 1""",
                (copy["id"],),
            ).fetchone()
            copy["retirement"] = dict(latest) if latest else None
        payload["retirements"] = [
            dict(r) | {"blockers": json.loads(r["blockers"])}
            for r in conn.execute(
                """SELECT id,copy_id,retiring_location,new_location,custodian,status,blockers,note,
                          requested_by,requested_at,reviewed_by,reviewed_at
                   FROM copy_retirements WHERE version_id=? ORDER BY id""",
                (version_id,),
            ).fetchall()
        ]
        return payload

    @staticmethod
    def _serialize(row) -> dict:
        data = dict(row)
        data["blockers"] = json.loads(data.get("blockers") or "[]")
        return data
