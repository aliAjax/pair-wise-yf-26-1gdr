"""副本退役交接规则：登记申请、复核确认/退回、版本状态重算。

数据表见 retirement_schema.py，页面见 web/retirement.html。
复核确认条件（全部满足才确认，否则退回并列出阻塞项）：
1. 至少一份其他健康副本（未退役且 state='healthy'）完成内容比对，与版本清单一致；
2. 退役副本的最近核验时间晚于版本生成时间。
确认后旧副本标记退役并留档，版本状态按剩余健康副本重算，全程写审计日志。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

ROLES_WRITE = {"owner", "archivist"}
ROLES_READ = {"owner", "archivist", "auditor"}


class RuleError(Exception):
    """规则校验失败；由 app.PreservationStore 统一转换为 BusinessError。"""

    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get_request(conn: sqlite3.Connection, request_id: int) -> sqlite3.Row:
    request = conn.execute("SELECT * FROM retirement_requests WHERE id=?", (request_id,)).fetchone()
    if not request:
        raise RuleError("退役申请不存在", 404, "not_found")
    return request


def _serialize(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["blockers"] = json.loads(data["blockers"] or "[]")
    return data


def _compare_copy(conn: sqlite3.Connection, manifest: dict, copy: sqlite3.Row) -> dict:
    """把一份健康副本的实际内容与版本清单逐文件比对。"""
    files = conn.execute(
        "SELECT path,sha256,size,content FROM copy_files WHERE copy_id=?", (copy["id"],)
    ).fetchall()
    by_path = {row["path"]: row for row in files}
    mismatches = []
    if set(by_path) != set(manifest):
        mismatches.append("文件清单与版本不一致")
    for path, expected in manifest.items():
        got = by_path.get(path)
        if got is None:
            continue
        if (
            got["sha256"] != expected["sha256"]
            or got["size"] != expected["size"]
            or hashlib.sha256(got["content"]).hexdigest() != expected["sha256"]
        ):
            mismatches.append(path)
    return {"copy_id": copy["id"], "location": copy["location"], "matched": not mismatches, "mismatches": mismatches}


def evaluate(conn: sqlite3.Connection, request: sqlite3.Row) -> tuple[list[str], list[dict]]:
    """复核评估：返回 (阻塞项, 比对明细)，全部阻塞项为空才允许确认。"""
    copy = conn.execute("SELECT * FROM copies WHERE id=?", (request["copy_id"],)).fetchone()
    version = conn.execute("SELECT * FROM archive_versions WHERE id=?", (request["version_id"],)).fetchone()
    blockers: list[str] = []
    last_verified = copy["last_verified_at"]
    if not last_verified or last_verified <= version["created_at"]:
        blockers.append("退役副本最近核验未晚于版本生成时间，请先重新核验")
    manifest = {
        row["path"]: row
        for row in conn.execute(
            "SELECT path,sha256,size FROM archive_files WHERE version_id=?", (request["version_id"],)
        ).fetchall()
    }
    others = conn.execute(
        """SELECT * FROM copies
           WHERE version_id=? AND id<>? AND retired_at IS NULL AND state='healthy' ORDER BY id""",
        (request["version_id"], copy["id"]),
    ).fetchall()
    comparisons = [_compare_copy(conn, manifest, other) for other in others]
    if not others:
        blockers.append("没有其他健康副本可供内容比对")
    elif not any(item["matched"] for item in comparisons):
        blockers.append("其他健康副本内容比对均未通过")
    return blockers, comparisons


def request_retirement(store, actor_id: str, copy_id: int, successor_location: str, custodian: str) -> dict:
    """登记退役申请：选定待退役副本，记录接手位置与保管人，进入待复核。"""
    successor_location = (successor_location or "").strip()
    custodian = (custodian or "").strip()
    if len(successor_location) < 2:
        raise RuleError("接手位置不能为空", 422, "invalid_location")
    if not custodian:
        raise RuleError("保管人不能为空", 422, "invalid_custodian")
    with store.connect() as conn:
        actor = store._user(conn, actor_id, ROLES_WRITE)
        copy = conn.execute("SELECT * FROM copies WHERE id=?", (copy_id,)).fetchone()
        if not copy:
            raise RuleError("副本不存在", 404, "not_found")
        if copy["retired_at"]:
            raise RuleError("副本已退役", 409, "copy_retired")
        version = conn.execute("SELECT * FROM archive_versions WHERE id=?", (copy["version_id"],)).fetchone()
        store._access(conn, version["archive_id"], actor, require_write=True)
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """INSERT INTO retirement_requests(copy_id,version_id,archive_id,successor_location,custodian,requested_by,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (copy_id, copy["version_id"], version["archive_id"], successor_location, custodian, actor_id, _now()),
            )
            request_id = cur.lastrowid
            store._audit(conn, version["archive_id"], actor_id, "retirement.request", {
                "request_id": request_id, "copy_id": copy_id, "version_id": copy["version_id"],
                "successor_location": successor_location, "custodian": custodian,
            })
            return {
                "id": request_id, "copy_id": copy_id, "version_id": copy["version_id"],
                "successor_location": successor_location, "custodian": custodian, "state": "pending",
            }
        except sqlite3.IntegrityError:
            conn.rollback()
            raise RuleError("该副本已有待复核的退役申请", 409, "retirement_pending")
        except Exception:
            conn.rollback()
            raise


def review_retirement(store, actor_id: str, request_id: int) -> dict:
    """复核退役申请：条件全部满足则确认退役，否则退回并列出阻塞项。"""
    with store.connect() as conn:
        actor = store._user(conn, actor_id, ROLES_WRITE)
        request = _get_request(conn, request_id)
        store._access(conn, request["archive_id"], actor, require_write=True)
        try:
            conn.execute("BEGIN IMMEDIATE")
            request = _get_request(conn, request_id)  # 事务内重取，保证状态判断准确
            if request["state"] != "pending":
                raise RuleError("该退役申请已复核", 409, "retirement_closed")
            blockers, comparisons = evaluate(conn, request)
            if blockers:
                conn.execute(
                    "UPDATE retirement_requests SET state='returned',reviewed_by=?,reviewed_at=?,blockers=? WHERE id=?",
                    (actor_id, _now(), json.dumps(blockers, ensure_ascii=False), request_id),
                )
                store._audit(conn, request["archive_id"], actor_id, "retirement.review", {
                    "request_id": request_id, "copy_id": request["copy_id"],
                    "result": "returned", "blockers": blockers, "comparisons": comparisons,
                })
                return {"id": request_id, "state": "returned", "blockers": blockers, "comparisons": comparisons}
            conn.execute("UPDATE copies SET retired_at=? WHERE id=?", (_now(), request["copy_id"]))
            remaining = conn.execute(
                "SELECT COUNT(*) FROM copies WHERE version_id=? AND retired_at IS NULL AND state='healthy'",
                (request["version_id"],),
            ).fetchone()[0]
            version_state = "verified" if remaining else "degraded"
            conn.execute("UPDATE archive_versions SET state=? WHERE id=?", (version_state, request["version_id"]))
            conn.execute(
                "UPDATE retirement_requests SET state='confirmed',reviewed_by=?,reviewed_at=?,blockers='[]' WHERE id=?",
                (actor_id, _now(), request_id),
            )
            store._audit(conn, request["archive_id"], actor_id, "retirement.review", {
                "request_id": request_id, "copy_id": request["copy_id"], "result": "confirmed",
                "comparisons": comparisons, "remaining_healthy_copies": remaining, "version_state": version_state,
            })
            return {
                "id": request_id, "state": "confirmed", "blockers": [], "comparisons": comparisons,
                "remaining_healthy_copies": remaining, "version_state": version_state,
            }
        except Exception:
            conn.rollback()
            raise


def get_request(store, user_id: str, request_id: int) -> dict:
    """申请详情；待复核申请附带实时阻塞项与比对明细，便于复核人判断。"""
    with store.connect() as conn:
        user = store._user(conn, user_id, ROLES_READ)
        request = _get_request(conn, request_id)
        store._access(conn, request["archive_id"], user)
        result = _serialize(request)
        if request["state"] == "pending":
            blockers, comparisons = evaluate(conn, request)
            result["pending_blockers"] = blockers
            result["comparisons"] = comparisons
        return result


def list_for_version(store, user_id: str, version_id: int) -> list[dict]:
    """版本的全部退役申请，供版本详情追溯。"""
    with store.connect() as conn:
        user = store._user(conn, user_id, ROLES_READ)
        version = conn.execute("SELECT * FROM archive_versions WHERE id=?", (version_id,)).fetchone()
        if not version:
            raise RuleError("档案版本不存在", 404, "not_found")
        store._access(conn, version["archive_id"], user)
        rows = conn.execute(
            "SELECT * FROM retirement_requests WHERE version_id=? ORDER BY id", (version_id,)
        ).fetchall()
        return [_serialize(row) for row in rows]
