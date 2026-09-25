"""处置核验接口：台账登记、更正、核验状态和结论留档的HTTP路由。

由 src/http_api.py 在主路由之前委托调用；返回 (状态码, 响应体)，
路径不匹配时返回 None，交由既有路由处理。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

_ITEM_RESOURCE = re.compile(
    r"^/api/items/(\d+)/(disposal-records|disposal-verification|disposal-conclusions)$")
_RECORD_CORRECT = re.compile(r"^/api/disposal-records/(\d+)/correct$")


def handle(service, method: str, path: str, body: Dict[str, Any],
           actor: str, role: str) -> Optional[Tuple[int, Any]]:
    item_route = _ITEM_RESOURCE.match(path)
    if item_route is not None:
        item_id = int(item_route.group(1))
        resource = item_route.group(2)
        if resource == "disposal-records":
            if method == "GET":
                return 200, {"records": service.list_disposal_records(item_id, role)}
            if method == "POST":
                return 201, service.add_disposal_record(item_id, body, actor, role)
        elif resource == "disposal-verification":
            if method == "GET":
                return 200, service.disposal_verification(item_id, role)
        elif resource == "disposal-conclusions":
            if method == "GET":
                return 200, {"conclusions": service.list_disposal_conclusions(item_id, role)}
        return None
    correct_route = _RECORD_CORRECT.match(path)
    if correct_route is not None and method == "POST":
        return 200, service.correct_disposal_record(
            int(correct_route.group(1)), body, actor, role)
    return None
