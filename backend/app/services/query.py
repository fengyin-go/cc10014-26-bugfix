"""数据查询业务规则：状态流转、字段校验与筛选口径都收在这里。

查询入口是缓存链路的唯一读写口：先按查询定义选出当前生效的缓存策略，再把缓存键模板、
查询参数和转换模板版本一起算成完整缓存键，之后才读或写查询结果数据集。
"""
from __future__ import annotations

from typing import Any

from app.services.cache_contract import (
    STATE_MESSAGES,
    binding_watermark,
    contract_status,
    empty_state,
    lookup,
    write_result,
)
from app.services.cache_key import (
    compose_cache_key,
    normalize_params,
    policy_transform_version,
    resolve_policy,
)
from app.store import store

MODULE = "query"
REQUIRED_FIELDS = ["定义编号", "关联数据源", "查询模板"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用查询": "已启用", "停用查询": "已停用"}
NEGATIVE_ACTIONS = ["停用查询"]
SOURCE_MODULE = "_query_source"


class QueryService:
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
            rows = [row for row in rows if keyword in str(row.get("定义编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": store.next_id(MODULE)}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in ["参数占位", "结果字段", "排序规则", "分页大小", "超时秒数"]:
            if values.get(field) not in (None, ""):
                entry[field] = values.get(field)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"查询定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据查询可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"查询定义已{action}"

    # ------------------------------------------------------------------
    # 查询入口：缓存键 -> 读/写查询结果数据集
    # ------------------------------------------------------------------
    def resolve_context(
        self,
        entry: dict[str, Any],
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        """把查询定义 + 查询参数 + 转换版本解析成可执行的缓存上下文。"""
        if entry.get("status") != "已启用":
            return None, "disabled"
        requested = str(values.get("transform_version") or values.get("转换版本") or "").strip() or None
        policy, state = resolve_policy(
            entry,
            store.rows("cache"),
            store.rows("transform"),
            requested,
            current_version=binding_watermark(str(entry.get("定义编号") or "")),
        )
        if policy is None:
            return None, state
        version = policy_transform_version(policy, store.rows("transform"))
        params, missing = normalize_params(values.get("params") or {}, entry)
        if missing:
            return None, f"missing_params:{','.join(missing)}"
        query_no = str(entry.get("定义编号") or entry.get("id") or "")
        try:
            cache_key = compose_cache_key(
                str(policy.get("缓存键模板") or ""),
                policy_no=str(policy.get("策略编号") or policy.get("id") or ""),
                query_no=query_no,
                params=params,
                transform_version=version,
            )
        except ValueError as error:
            return None, str(error)
        ttl = float(policy.get("过期秒数") or 0)
        limit = int(float(policy.get("最大条目") or 0))
        return {
            "policy": dict(policy),
            "cache_key": cache_key,
            "params": params,
            "transform_version": version,
            "ttl": ttl,
            "max_entries": limit,
            "query_no": query_no,
        }, "ok"

    def execute(self, entry_id: int, values: dict[str, Any]) -> dict[str, Any]:
        """按完整缓存键读取或写入查询结果；所有异常分支都返回可读的空态。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return empty_state(f"查询定义 {entry_id} 不存在或已归档", state="not_found")
        operation = str(values.get("operation") or values.get("action") or "read").strip().lower()
        context, state = self.resolve_context(entry, values)
        if context is None:
            if state == "disabled":
                return empty_state(STATE_MESSAGES["disabled"], state="disabled")
            if state == "version_unbound":
                return empty_state(STATE_MESSAGES["version_unbound"], state="version_unbound")
            if state.startswith("missing_params:"):
                return empty_state(f"查询参数缺少占位：{state.split(':', 1)[1]}", state="invalid_params")
            return empty_state(STATE_MESSAGES["no_policy"], state=state if state in STATE_MESSAGES else "no_policy")

        if operation == "write":
            return self._write(entry, context, values)
        return self._read(entry, context, values)

    def _read(self, entry: dict[str, Any], context: dict[str, Any], values: dict[str, Any] | None = None) -> dict[str, Any]:
        timeout = bool((values or {}).get("timeout"))
        result = lookup(
            cache_key=context["cache_key"],
            params=context["params"],
            transform_version=context["transform_version"],
            ttl=context["ttl"],
            query_no=context["query_no"],
            timeout=timeout,
        )
        result.update(
            {
                "定义编号": entry.get("定义编号"),
                "策略编号": context["policy"].get("策略编号"),
                "ttl": context["ttl"],
                "max_entries": context["max_entries"],
            }
        )
        return result

    def _write(self, entry: dict[str, Any], context: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        rows = values.get("rows")
        if rows is None:
            rows = self.source_rows(entry, context["params"])
        if not isinstance(rows, list):
            return empty_state("写入内容必须是查询结果行数组", cache_key=context["cache_key"], state="invalid_rows")
        if not rows:
            return empty_state(
                STATE_MESSAGES["no_result"],
                cache_key=context["cache_key"],
                state="no_result",
                params=context["params"],
                transform_version=context["transform_version"],
            )
        result = write_result(
            cache_key=context["cache_key"],
            policy_no=str(context["policy"].get("策略编号") or context["policy"].get("id") or ""),
            query_no=context["query_no"],
            transform_version=context["transform_version"],
            params=context["params"],
            rows=rows,
            ttl=context["ttl"],
            max_entries=context["max_entries"],
            batch_size=int(values.get("batch_size") or 2),
            interrupt_after=(
                int(values["interrupt_after"])
                if values.get("interrupt_after") not in (None, "")
                else None
            ),
            resume=bool(values.get("resume")),
        )
        result.update(
            {
                "定义编号": entry.get("定义编号"),
                "策略编号": context["policy"].get("策略编号"),
                "查询参数": context["params"],
                "转换版本": context["transform_version"],
            }
        )
        return result

    def source_rows(self, entry: dict[str, Any], params: dict[str, str]) -> list[dict[str, Any]]:
        """从已有查询结果原始数据里取出当前参数对应的行。"""
        rows = store.rows(SOURCE_MODULE)
        query_no = str(entry.get("定义编号") or "")
        matched = [
            dict(row)
            for row in rows
            if str(row.get("定义编号") or "") == query_no
            and all(str(row.get(name)) == value for name, value in params.items() if name in row)
        ]
        return matched

    def detail_view(self, entry: dict[str, Any]) -> dict[str, Any]:
        """查询详情页的契约视图：当前有效缓存键、命中率与绑定关系。"""
        from app.services.cache_contract import bindings

        query_no = str(entry.get("定义编号") or "")
        status = contract_status()
        keys = [item for item in status["valid_keys"] if str(item.get("定义编号") or "") == query_no]
        return {
            **entry,
            "valid_keys": keys,
            "hit_rate": round(sum(float(item["命中率"]) for item in keys) / len(keys), 4) if keys else 0.0,
            "events": [item for item in status["events"] if str(item.get("缓存键") or "").find(query_no) >= 0],
            "bindings": bindings(entry, store.rows("cache"), store.rows("transform")),
        }
