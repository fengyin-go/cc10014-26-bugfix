"""结果缓存业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.cache_contract import bindings as binding_rows
from app.services.cache_contract import invalidate_for_policy, note_binding
from app.services.cache_key import (
    compose_cache_key,
    normalize_params,
    policy_errors,
    policy_transform_version,
    resolve_policy,
)
from app.store import store

MODULE = "cache"
REQUIRED_FIELDS = ["策略编号", "关联服务", "缓存键模板", "过期秒数", "最大条目"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用缓存": "已启用", "停用缓存": "已停用"}
NEGATIVE_ACTIONS = ["停用缓存"]
# 登记时允许一起提交的可选字段，缺省的运行参数按默认值补全。
OPTIONAL_FIELDS = [
    "关联查询定义",
    "关联转换模板",
    "转换版本",
    "策略版本",
    "淘汰算法",
    "预热策略",
    "命中率阈值",
]


class CacheService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def _query_row(self, values: dict[str, Any]) -> dict[str, Any] | None:
        reference = str(values.get("关联查询定义") or values.get("定义编号") or "").strip()
        if not reference:
            return None
        for row in store.rows("query"):
            if reference in {str(row.get("定义编号") or "").strip(), str(row.get("id") or "").strip()}:
                return row
        return None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记缓存策略：过期时间和最大条目必须一起给，空缓存键模板直接拒绝。"""
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        template = str(values.get("缓存键模板") or "").strip()
        if not template:
            return None, ["缓存键模板"]
        ttl = self._positive_number(values.get("过期秒数"))
        if ttl is None:
            return None, ["过期秒数"]
        limit = self._positive_int(values.get("最大条目"))
        if limit is None:
            return None, ["最大条目"]

        query_row = self._query_row(values)
        reference = str(values.get("关联查询定义") or values.get("定义编号") or "").strip()
        if reference and query_row is None:
            return None, ["关联查询定义"]

        transform_version = str(values.get("转换版本") or "").strip()
        transform_ref = str(values.get("关联转换模板") or "").strip()
        if transform_ref and not transform_version:
            for row in store.rows("transform"):
                if transform_ref in {str(row.get("模板编号") or "").strip(), str(row.get("id") or "").strip()}:
                    transform_version = str(row.get("版本") or "").strip()
                    break
            if not transform_version:
                return None, ["关联转换模板"]
        if not transform_version:
            return None, ["转换版本"]

        rows = store.rows(MODULE)
        entry = {"id": store.next_id(MODULE)}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["缓存键模板"] = template
        entry["过期秒数"] = ttl
        entry["最大条目"] = limit
        entry["关联查询定义"] = reference or (query_row or {}).get("定义编号")
        entry["关联转换模板"] = transform_ref or None
        entry["转换版本"] = transform_version
        entry["策略版本"] = str(values.get("策略版本") or "v1").strip() or "v1"
        entry["淘汰算法"] = values.get("淘汰算法") or "LRU"
        entry["预热策略"] = values.get("预热策略") or "不预热"
        entry["命中率阈值"] = values.get("命中率阈值") if values.get("命中率阈值") is not None else 0.5
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        note_binding(str(entry.get("关联查询定义") or ""), transform_version)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"缓存策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于结果缓存可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        if action == "启用缓存":
            query_row = self._query_row(entry)
            errors = policy_errors(entry, query_row, store.rows("transform"))
            if errors:
                entry["abnormal"] = True
                return None, "缓存策略不满足启用条件：" + "；".join(errors)

        if action == "停用缓存":
            # 失效结果同步给缓存策略契约：停用后旧键不能再被查询入口命中。
            invalidate_for_policy(entry)

        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        if action == "启用缓存":
            note_binding(
                str(entry.get("关联查询定义") or ""),
                policy_transform_version(entry, store.rows("transform")),
            )
        return entry, f"缓存策略已{action}"

    def contract_view(self, entry: dict[str, Any]) -> dict[str, Any]:
        """缓存策略页面的契约视图：当前有效键、命中率、批次和绑定关系。"""
        from app.services.cache_contract import contract_status

        policy_no = str(entry.get("策略编号") or entry.get("id") or "")
        status = contract_status()
        keys = [item for item in status["valid_keys"] if str(item.get("策略编号") or "") == policy_no]
        query_row = self._query_row(entry)
        return {
            "policy": entry,
            "valid_keys": keys,
            "hit_rate": round(sum(float(item["命中率"]) for item in keys) / len(keys), 4) if keys else 0.0,
            "events": [item for item in status["events"] if str(item.get("缓存键") or "").find(policy_no) >= 0
                       or str(item.get("缓存键") or "").split("|")[0].find(str(entry.get("关联查询定义") or "")) >= 0],
            "bindings": binding_rows(query_row, store.rows(MODULE), store.rows("transform")) if query_row else [],
        }

    def preview_key(
        self,
        entry_id: int,
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        """按当前策略与给定参数计算完整缓存键，供页面复核。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"缓存策略 {entry_id} 不存在或已归档"
        query_row = self._query_row(entry)
        if query_row is None:
            return None, "缓存策略未绑定有效的查询定义"
        params, missing = normalize_params(values.get("params") or {}, query_row)
        if missing:
            return None, f"查询参数缺少占位：{'、'.join(missing)}"
        version = policy_transform_version(entry, store.rows("transform"))
        try:
            key = compose_cache_key(
                str(entry.get("缓存键模板") or ""),
                policy_no=str(entry.get("策略编号") or ""),
                query_no=str(query_row.get("定义编号") or ""),
                params=params,
                transform_version=version,
            )
        except ValueError as error:
            return None, str(error)
        return {
            "缓存键": key,
            "查询参数": params,
            "转换版本": version,
            "策略编号": entry.get("策略编号"),
            "定义编号": query_row.get("定义编号"),
        }, "完整缓存键已生成"

    def resolve_effective_policy(
        self,
        query_row: dict[str, Any],
        requested_version: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        policy, state = resolve_policy(query_row, store.rows(MODULE), store.rows("transform"), requested_version)
        return dict(policy) if policy else None, state

    @staticmethod
    def _positive_number(value: Any) -> float | None:
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        try:
            number = int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None
