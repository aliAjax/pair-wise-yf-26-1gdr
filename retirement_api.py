"""副本退役交接 —— 页面接入层。

把规则层挂到 JSON API 与 Web 页面：

- ``POST /api/copies/{id}/retirement``：申请退役交接（登记接手位置、保管人）。
- ``POST /api/retirements/{id}/review``：复核，``{"decision":"confirm|reject"}``；
  条件不满足时服务端自动退回，响应 ``blockers`` 列出全部阻塞项。
- ``GET /api/retirements/{id}``：交接单留档查询。
- ``GET /retirement``：退役交接复核页面。

``dispatch`` 未命中返回 ``None``，交由应用继续匹配其余接口。
"""
from __future__ import annotations

from pathlib import Path

from retirement_rules import RetirementRules

PAGE_PATH = Path(__file__).resolve().parent / "web" / "retirement.html"


def rules_for(store) -> RetirementRules:
    return RetirementRules(store)


def page() -> bytes:
    return PAGE_PATH.read_bytes()


def dispatch(method: str, parts: list[str], body: dict, user: str, store):
    if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "copies"] and parts[3] == "retirement":
        result = rules_for(store).request_retirement(
            user, int(parts[2]), body.get("new_location", ""), body.get("custodian", ""), body.get("note", "")
        )
        return 201, result
    if len(parts) == 4 and parts[:2] == ["api", "retirements"] and parts[3] == "review" and method == "POST":
        result = rules_for(store).review_retirement(
            user, int(parts[2]), body.get("decision", ""), body.get("note", "")
        )
        return 200, result
    if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "retirements"]:
        return 200, rules_for(store).get_retirement(user, int(parts[2]))
    return None
