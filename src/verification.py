from __future__ import annotations

from typing import Any, Dict, List

from .domain import ConflictError, ValidationError, require_number, require_text

# 处置核验三类记录：回收数量、岸线复查、废弃物去向
RECOVERY = "recovery"
SHORELINE = "shoreline"
WASTE = "waste"
KINDS = (RECOVERY, SHORELINE, WASTE)
KIND_LABELS = {
    RECOVERY: "回收数量",
    SHORELINE: "岸线复查",
    WASTE: "废弃物去向",
}

# 回收量达到估算油量的八成方可关闭
RECOVERY_RATIO = 0.8

_SHORELINE_RESULTS = {
    "normal": "normal", "正常": "normal",
    "abnormal": "abnormal", "异常": "abnormal",
}
_BOOL_TRUE = {"true", "1", "yes", "是", "已交接"}
_BOOL_FALSE = {"false", "0", "no", "否", "未交接"}
_FLOAT_EPSILON = 1e-9


class VerificationConflict(ConflictError):
    """处置核验未通过，missing列出缺少或不达标的记录类别。"""

    def __init__(self, missing: List[Dict[str, str]]):
        self.missing = missing
        labels = "、".join(item["label"] for item in missing) or "处置核验记录"
        super().__init__(f"处置核验未通过，缺少或不达标：{labels}")


def normalize_kind(value: Any) -> str:
    value = require_text(value, "kind", 50)
    if value not in KINDS:
        raise ValidationError("kind必须是recovery、shoreline或waste")
    return value


def _to_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _BOOL_TRUE:
            return True
        if text in _BOOL_FALSE:
            return False
    raise ValidationError(f"{field}必须是布尔值")


def build_fields(kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """按记录类别提取并校验登记字段。"""
    fields: Dict[str, Any] = {
        "recovery_quantity": None,
        "shoreline_result": None,
        "inspected_at": None,
        "waste_destination": None,
        "handed_over": False,
    }
    if kind == RECOVERY:
        fields["recovery_quantity"] = require_number(
            payload.get("recovery_quantity", 0), "recovery_quantity")
    elif kind == SHORELINE:
        result = require_text(payload.get("shoreline_result"), "shoreline_result", 20)
        canonical = _SHORELINE_RESULTS.get(result)
        if canonical is None:
            raise ValidationError("shoreline_result必须是normal/abnormal（正常/异常）")
        fields["shoreline_result"] = canonical
        inspected_at = payload.get("inspected_at")
        if inspected_at is not None:
            fields["inspected_at"] = require_text(inspected_at, "inspected_at", 40)
    else:
        fields["waste_destination"] = require_text(
            payload.get("waste_destination"), "waste_destination", 200)
        fields["handed_over"] = _to_bool(payload.get("handed_over", False), "handed_over")
    return fields


def merge_fields(kind: str, entry: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """更正时以原始记录为底，仅覆盖本次提交的字段。"""
    base = {
        "recovery_quantity": entry.get("recovery_quantity"),
        "shoreline_result": entry.get("shoreline_result"),
        "inspected_at": entry.get("inspected_at"),
        "waste_destination": entry.get("waste_destination"),
        "handed_over": entry.get("handed_over"),
    }
    for key in base:
        if key in payload and payload[key] is not None:
            base[key] = payload[key]
    return build_fields(kind, base)


def evaluate(entries: List[Dict[str, Any]], estimated_quantity: float) -> Dict[str, Any]:
    """对生效中的三类记录做关闭核验，返回核验结论和缺失类别。"""
    active = [e for e in entries if e["status"] == "active"]
    recovery_entries = [e for e in active if e["kind"] == RECOVERY]
    shoreline_entries = [e for e in active if e["kind"] == SHORELINE]
    waste_entries = [e for e in active if e["kind"] == WASTE]
    missing: List[Dict[str, str]] = []

    required = float(estimated_quantity) * RECOVERY_RATIO
    recovery_total = sum(float(e["recovery_quantity"] or 0.0) for e in recovery_entries)
    if not recovery_entries:
        missing.append({"code": RECOVERY, "label": KIND_LABELS[RECOVERY],
                        "reason": "未登记回收数量"})
    elif recovery_total + _FLOAT_EPSILON < required:
        missing.append({"code": RECOVERY, "label": KIND_LABELS[RECOVERY],
                        "reason": f"回收量{recovery_total:g}未达到估算油量{estimated_quantity:g}的八成（{required:g}）"})
    ratio = (recovery_total / float(estimated_quantity)
             if estimated_quantity and estimated_quantity > 0 else 1.0)

    latest_shoreline = max(shoreline_entries, key=lambda e: e["id"]) if shoreline_entries else None
    if latest_shoreline is None:
        missing.append({"code": SHORELINE, "label": KIND_LABELS[SHORELINE],
                        "reason": "未登记岸线复查结果"})
    elif latest_shoreline["shoreline_result"] != "normal":
        missing.append({"code": SHORELINE, "label": KIND_LABELS[SHORELINE],
                        "reason": "最近一次岸线复查结果异常"})

    waste_handed = all(bool(e["handed_over"]) for e in waste_entries) if waste_entries else False
    if not waste_entries:
        missing.append({"code": WASTE, "label": KIND_LABELS[WASTE],
                        "reason": "未登记废弃物交接去向"})
    elif not waste_handed:
        missing.append({"code": WASTE, "label": KIND_LABELS[WASTE],
                        "reason": "存在未完成交接的废弃物"})

    return {
        "ready": not missing,
        "estimated_quantity": float(estimated_quantity),
        "recovery": {
            "registered": recovery_total,
            "required": required,
            "ratio": ratio,
        },
        "shoreline": {
            "latest_result": latest_shoreline["shoreline_result"] if latest_shoreline else None,
            "inspected_at": latest_shoreline["inspected_at"] if latest_shoreline else None,
        },
        "waste": {
            "count": len(waste_entries),
            "all_handed_over": waste_handed,
        },
        "missing": missing,
    }


def ensure_closable(entries: List[Dict[str, Any]], estimated_quantity: float) -> Dict[str, Any]:
    verdict = evaluate(entries, estimated_quantity)
    if not verdict["ready"]:
        raise VerificationConflict(verdict["missing"])
    return verdict
