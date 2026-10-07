"""废弃物回运责任项目：法规版本、混装限制与期限规则（纯函数）。

法规版本在封箱时被整体快照冻结，后续登记新版本不能改变旧箱已经冻结的
混装限制、期限和责任人资格要求，因此本模块只提供对快照内容的校验与派生
计算，从不保存或回写任何状态。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .audit import digest

# 允许登记的废弃物类别。
WASTE_TYPES = frozenset({
    "waste_oil",                 # 废油
    "spent_battery",             # 废电池
    "contaminated_packaging",    # 受污染包装
})

# 责任主体类别。
CUSTODIAN_KINDS = frozenset({
    "camp",               # 野外营地（产生地）
    "staging_yard",       # 暂存场
    "carrier",            # 承运方
    "disposal_facility",  # 目的地接收/处置设施
})

# 移交种类。
HANDOVER_KINDS = frozenset({"staging", "cross_camp", "carrier", "destination", "return"})

# 移交种类对应的责任人资格要求键（取自封箱快照）。
REQUIREMENT_KEY = {
    "staging": "staging",
    "cross_camp": "cross_camp",
    "carrier": "carrier",
    "destination": "destination",
    "return": "cross_camp",
}

# 义务种类对应的期限小时数键（取自封箱快照）。
DEADLINE_KEY = {
    "handover_confirm": "handover_confirm_hours",
    "staging": "staging_max_hours",
    "transit": "transit_max_hours",
    "disposal": "disposal_max_hours",
    "shipment_return": "return_max_hours",
}

_DEADLINE_KEYS = frozenset(DEADLINE_KEY.values())

# 确认移交后容器进入的业务阶段。
STAGE_AFTER_CONFIRM = {
    "staging": "staged",
    "cross_camp": "staged",
    "carrier": "in_transit",
    "destination": "delivered",
    "return": "staged",
}

# 各类移交允许的容器当前阶段。
STAGE_BEFORE_HANDOVER = {
    "staging": frozenset({"sealed", "staged"}),
    "cross_camp": frozenset({"sealed", "staged"}),
    "carrier": frozenset({"sealed", "staged"}),
    "destination": frozenset({"in_transit"}),
    "return": frozenset({"in_transit"}),
}

JUMP_ACTIONS = {
    "staging": "staged",
    "cross_camp": "transfer",
    "carrier": "carrier",
    "destination": "delivered",
    "return": "returned",
}


def normalize_regulation(content: Any) -> dict[str, Any]:
    """校验并规范化一份法规版本内容。

    结构：
    {
      "mixing_rules": {
        "incompatibilities": [["waste_oil", "spent_battery"], ...],
        "max_total_kg": 500
      },
      "deadlines": {"staging_max_hours": 168, ...},
      "custodian_requirements": {"carrier": ["dangerous_goods_cert"], ...}
    }
    """

    if not isinstance(content, dict) or not content:
        raise ValueError("法规内容必须是非空对象")
    mixing = content.get("mixing_rules")
    if not isinstance(mixing, dict):
        raise ValueError("mixing_rules 必须是对象")
    incompatibilities = mixing.get("incompatibilities", [])
    if not isinstance(incompatibilities, list) or not incompatibilities:
        raise ValueError("mixing_rules.incompatibilities 必须是非空列表")
    pairs: list[list[str]] = []
    for pair in incompatibilities:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("每一条混装限制必须是两个废弃物类别")
        a, b = pair
        if a not in WASTE_TYPES or b not in WASTE_TYPES or a == b:
            raise ValueError("混装限制引用了无效的废弃物类别")
        pairs.append(sorted([a, b]))
    # 去重并排序，保证同内容同哈希。
    unique_pairs = sorted({tuple(p) for p in pairs})
    rules: dict[str, Any] = {"incompatibilities": [list(p) for p in unique_pairs]}
    if "max_total_kg" in mixing:
        max_total = mixing["max_total_kg"]
        if not isinstance(max_total, (int, float)) or isinstance(max_total, bool) or max_total <= 0:
            raise ValueError("max_total_kg 必须是正数")
        rules["max_total_kg"] = float(max_total)

    deadlines = content.get("deadlines", {})
    if not isinstance(deadlines, dict):
        raise ValueError("deadlines 必须是对象")
    normalized_deadlines: dict[str, float] = {}
    for key, value in deadlines.items():
        if key not in _DEADLINE_KEYS:
            raise ValueError(f"未知期限字段 {key}")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"期限 {key} 必须是正数小时")
        normalized_deadlines[key] = float(value)

    requirements = content.get("custodian_requirements", {})
    if not isinstance(requirements, dict):
        raise ValueError("custodian_requirements 必须是对象")
    normalized_requirements: dict[str, list[str]] = {}
    for key, value in requirements.items():
        if key not in set(REQUIREMENT_KEY.values()):
            raise ValueError(f"未知责任人要求 {key}")
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise ValueError(f"责任人要求 {key} 必须是非空字符串列表")
        normalized_requirements[key] = sorted(value)

    return {
        "mixing_rules": rules,
        "deadlines": normalized_deadlines,
        "custodian_requirements": normalized_requirements,
    }


def regulation_hash(snapshot: dict[str, Any]) -> str:
    """计算法规快照的稳定摘要。"""

    return digest(snapshot)


def find_incompatible_pairs(waste_types: list[str], snapshot: dict[str, Any]) -> list[list[str]]:
    """返回一组待封箱类别中违反混装限制的类别对。"""

    present = set(waste_types)
    violations: list[list[str]] = []
    for pair in snapshot["mixing_rules"]["incompatibilities"]:
        if set(pair) <= present:
            violations.append(pair)
    return violations


def max_total_kg(snapshot: dict[str, Any]) -> float | None:
    return snapshot["mixing_rules"].get("max_total_kg")


def missing_qualifications(kind: str, snapshot: dict[str, Any],
                           qualifications: list[str]) -> list[str]:
    """按封箱快照检查接收方缺少的资格。"""

    key = REQUIREMENT_KEY[kind]
    required = set(snapshot.get("custodian_requirements", {}).get(key, []))
    return sorted(required - set(qualifications))


def deadline_for(kind: str, snapshot: dict[str, Any], occurred_at: datetime) -> datetime | None:
    """根据快照中的期限计算义务截止时间；快照未规定该期限则返回 None。"""

    hours = snapshot.get("deadlines", {}).get(DEADLINE_KEY[kind])
    if hours is None:
        return None
    return occurred_at + timedelta(hours=hours)
