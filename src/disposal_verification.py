"""处置核验：关闭事件前的三类条件核验。

关闭条件（缺一不可，不够时给出缺哪一类）：
- 回收数量：有效回收记录合计达到估算油量的八成；
- 岸线复查：最近一次复查结果为正常；
- 废弃物去向：已登记且全部完成交接。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

from .disposal_ledger import KIND_LABELS

RECOVERY_RATIO = 0.8
CATEGORY_KEYS = ("recovery", "shoreline", "waste")
_EPSILON = 1e-9


def verify_disposal(estimated_quantity: float,
                    records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """对当前有效台账记录执行核验，返回分类明细与缺失类别。"""
    records = list(records)
    recovery = [r for r in records if r["kind"] == "recovery"]
    shoreline = [r for r in records if r["kind"] == "shoreline"]
    waste = [r for r in records if r["kind"] == "waste"]

    estimated = float(estimated_quantity or 0)
    required = round(RECOVERY_RATIO * estimated, 6)
    recovery_total = round(sum(float(r["quantity"] or 0) for r in recovery), 6)
    recovery_ok = bool(recovery) and recovery_total + _EPSILON >= required

    latest = shoreline[-1] if shoreline else None
    shoreline_ok = latest is not None and latest["result"] == "normal"

    waste_pending = sum(1 for r in waste if r["transfer_status"] != "completed")
    waste_ok = bool(waste) and waste_pending == 0

    satisfied = {"recovery": recovery_ok, "shoreline": shoreline_ok, "waste": waste_ok}
    missing = [key for key in CATEGORY_KEYS if not satisfied[key]]
    return {
        "ok": not missing,
        "missing": missing,
        "missing_labels": [KIND_LABELS[key] for key in missing],
        "estimated_quantity": estimated,
        "required_quantity": required,
        "recovery_ratio": RECOVERY_RATIO,
        "recovery": {"records": len(recovery), "total": recovery_total,
                     "satisfied": recovery_ok},
        "shoreline": {"records": len(shoreline),
                      "latest_result": latest["result"] if latest else None,
                      "satisfied": shoreline_ok},
        "waste": {"records": len(waste), "pending": waste_pending,
                  "satisfied": waste_ok},
    }


def closure_blockers(estimated_quantity: float,
                     records: Iterable[Dict[str, Any]]) -> List[str]:
    """返回未满足类别的中文标签，供关闭事件时拼装错误信息。"""
    return verify_disposal(estimated_quantity, records)["missing_labels"]
