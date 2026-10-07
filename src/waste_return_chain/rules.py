"""废弃物分类、混装限制以及封箱时冻结的法规规则。

法规版本是不可变资料：封箱时把整版规则复制进容器快照，之后发布的新版本
不会改变已经封箱的箱子所适用的限制、期限和责任人要求。
"""

from __future__ import annotations

import json
from typing import Any

from polar_station_foundation.audit import canonical_json, digest
from polar_station_foundation.errors import ValidationError


# 受监管的废弃物类别。
WASTE_CATEGORIES = frozenset({
    "waste_oil",            # 废油
    "waste_battery",        # 废电池
    "contaminated_packaging",  # 受污染包装
})

UNITS = frozenset({"kg", "L"})

# 混装矩阵：列出同一箱内禁止共存的类别对（双向）。
# 废油与废电池禁止混装；受污染包装不得与废油混装。
FORBIDDEN_PAIRS = frozenset({
    frozenset({"waste_oil", "waste_battery"}),
    frozenset({"waste_oil", "contaminated_packaging"}),
})

REQUIRED_RESPONSIBILITY_ROLES = ("origin_manager", "transporter", "receiver")
DEADLINE_FIELDS = ("storage_due_hours", "transfer_confirm_due_hours", "receipt_due_hours")
REQUIRED_PAYLOAD_FIELDS = ("mixing_rules", "deadlines", "responsibility_roles")


def validate_regulation_payload(payload: dict[str, Any]) -> None:
    """校验一版法规内容是否自洽，供发布与快照冻结使用。"""

    if not isinstance(payload, dict) or not payload:
        raise ValidationError("法规内容必须是非空对象")
    for name in REQUIRED_PAYLOAD_FIELDS:
        if name not in payload:
            raise ValidationError(f"法规内容缺少 {name}")
    rules = payload["mixing_rules"]
    if not isinstance(rules, dict):
        raise ValidationError("mixing_rules 必须是对象")
    deadlines = payload["deadlines"]
    if not isinstance(deadlines, dict):
        raise ValidationError("deadlines 必须是对象")
    for name in DEADLINE_FIELDS:
        value = deadlines.get(name)
        if not isinstance(value, (int, float)) or value <= 0:
            raise ValidationError(f"期限 {name} 必须是正数小时数")
    roles = payload["responsibility_roles"]
    if not isinstance(roles, dict) or any(role not in roles for role in REQUIRED_RESPONSIBILITY_ROLES):
        raise ValidationError("responsibility_roles 必须覆盖产生、运输、接收三类责任人")


def snapshot_regulation(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """返回规范化快照文本对应的对象与摘要，冻结到容器上。"""

    validate_regulation_payload(payload)
    canonical = canonical_json(payload)
    return json.loads(canonical), digest(payload)


def assert_mixing_allowed(categories: set[str], rules: dict[str, Any]) -> None:
    """依据某一版规则检查一组类别能否同箱。

    rules 缺省为禁止混装矩阵时回退到内置矩阵；显式规则以
    {"forbidden_pairs": [["a","b"], ...]} 表示。
    """

    forbidden = FORBIDDEN_PAIRS
    custom = rules.get("forbidden_pairs")
    if custom is not None:
        forbidden = frozenset(frozenset(pair) for pair in custom)
    for pair in forbidden:
        if pair <= categories:
            raise ValidationError(f"混装被法规禁止：{sorted(pair)}")
