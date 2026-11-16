"""缓存策略契约：缓存键失效同步、命中率、批次写入与结果数据集。

主流程和跨模块页面都从这里读取同一份状态：
- 查询入口按完整缓存键读取查询结果数据集，命中/空态/超时/旧值残留都给出明确状态；
- 缓存策略页面读取当前有效缓存键和命中率；
- 查询结果保持可追溯：每条结果都带上缓存键、参数签名、转换版本与批次号。

批次写入遵守「最后完整批次」原则：执行断开时丢弃未写完的批次，恢复时从最后一个
完整批次的边界继续追加，绝不让半批数据覆盖已有缓存值。
"""
from __future__ import annotations

import time
from typing import Any, Iterable, Mapping

from app.services.cache_key import (
    STATE_MESSAGES,
    compose_cache_key,
    key_matches,
    params_token,
    policy_transform_version,
)

VALUES_MODULE = "_cache_values"
DATASETS_MODULE = "_cache_datasets"
BATCHES_MODULE = "_cache_batches"
EVENTS_MODULE = "_cache_events"
STATS_MODULE = "_cache_stats"

VALUE_VALID = "有效"
VALUE_INVALID = "无效"
VALUE_PARTIAL = "部分"

BATCH_WRITING = "写入中"
BATCH_COMPLETE = "已完成"
BATCH_INTERRUPTED = "已中断"


def _rows(module: str) -> list[dict[str, Any]]:
    from app.store import store

    return store.rows(module)


def _next_id(module: str) -> int:
    from app.store import store

    return store.next_id(module)


def _events() -> list[dict[str, Any]]:
    return _rows(EVENTS_MODULE)


def binding_watermark(query_no: str) -> str:
    """查询上的转换版本水位：只随绑定策略登记/启用升高，停用不会让旧版本回流。"""
    for row in _rows("_cache_watermarks"):
        if str(row.get("定义编号") or "") == str(query_no):
            return str(row.get("转换版本") or "")
    return ""


def _note_version(query_no: str, transform_version: str) -> None:
    from app.services.cache_key import version_sort_key
    from app.store import store

    if not query_no or not str(transform_version or "").strip():
        return
    rows = store.rows("_cache_watermarks")
    current = binding_watermark(query_no)
    if current and version_sort_key(transform_version) <= version_sort_key(current):
        return
    for row in rows:
        if str(row.get("定义编号") or "") == str(query_no):
            row["转换版本"] = str(transform_version)
            return
    rows.append({"id": store.next_id("_cache_watermarks"), "定义编号": str(query_no), "转换版本": str(transform_version)})


note_binding = _note_version


def record_event(event: str, **payload: Any) -> dict[str, Any]:
    entry = {"id": _next_id(EVENTS_MODULE), "事件": event, "时间": time.time()}
    entry.update(payload)
    _rows(EVENTS_MODULE).append(entry)
    return entry


def _stats_for(cache_key: str) -> dict[str, Any]:
    for row in _rows(STATS_MODULE):
        if row.get("缓存键") == cache_key:
            return row
    row = {"id": _next_id(STATS_MODULE), "缓存键": cache_key, "命中次数": 0, "未命中次数": 0, "读取次数": 0}
    _rows(STATS_MODULE).append(row)
    return row


def hit_rate(cache_key: str) -> float:
    """按完整缓存键统计命中率；没有读取记录时返回 0.0，避免除零。"""
    row = _stats_for(cache_key)
    reads = int(row.get("读取次数") or 0)
    return round(int(row.get("命中次数") or 0) / reads, 4) if reads else 0.0


def _record_read(cache_key: str, *, hit: bool) -> None:
    row = _stats_for(cache_key)
    row["读取次数"] = int(row.get("读取次数") or 0) + 1
    if hit:
        row["命中次数"] = int(row.get("命中次数") or 0) + 1
    else:
        row["未命中次数"] = int(row.get("未命中次数") or 0) + 1


def _expired(value: Mapping[str, Any], now: float | None = None) -> bool:
    expire_at = value.get("过期时间")
    if expire_at is None:
        return False
    return float(expire_at) <= (now if now is not None else time.time())


def _dataset_rows(cache_key: str) -> list[dict[str, Any]]:
    return [row for row in _rows(DATASETS_MODULE) if row.get("缓存键") == cache_key]


def _mark_invalid(cache_key: str, reason: str, *, keep_dataset: bool = False) -> int:
    """把某个完整缓存键下的缓存值和结果行一起置为无效，旧值不再参与命中。"""
    touched = 0
    for value in _rows(VALUES_MODULE):
        if value.get("缓存键") == cache_key and value.get("状态") != VALUE_INVALID:
            value["状态"] = VALUE_INVALID
            value["失效原因"] = reason
            touched += 1
    if not keep_dataset:
        for row in _rows(DATASETS_MODULE):
            if row.get("缓存键") == cache_key:
                row["状态"] = VALUE_INVALID
                row["失效原因"] = reason
    if touched:
        record_event("缓存键失效", 缓存键=cache_key, 原因=reason, 影响条数=touched)
    return touched


def invalidate_key(cache_key: str, reason: str = "缓存策略契约要求失效") -> dict[str, Any]:
    """给缓存策略契约用的失效入口：失效结果同步写入事件与结果数据集。"""
    touched = _mark_invalid(cache_key, reason)
    return {"cache_key": cache_key, "invalidated": touched, "reason": reason}


def invalidate_for_policy(policy: Mapping[str, Any]) -> list[dict[str, Any]]:
    """策略停用或参数变化时，把它名下所有完整缓存键一起失效。"""
    policy_no = str(policy.get("策略编号") or policy.get("id") or "")
    keys = {
        str(row.get("缓存键") or "")
        for module in (VALUES_MODULE, DATASETS_MODULE)
        for row in _rows(module)
        if str(row.get("策略编号") or "") == policy_no and row.get("缓存键")
    }
    return [invalidate_key(key, f"缓存策略 {policy_no} 已停用或变更") for key in sorted(keys)]


def lookup(
    *,
    cache_key: str,
    params: Mapping[str, str],
    transform_version: str,
    ttl: float | None = None,
    query_no: str | None = None,
    timeout: bool = False,
) -> dict[str, Any]:
    """按完整缓存键读取查询结果，任何不一致或过期都返回明确空态。"""
    values = [row for row in _rows(VALUES_MODULE) if row.get("缓存键") == cache_key]
    state = "miss"
    invalidated_current = False
    matched: Mapping[str, Any] | None = None
    for value in sorted(values, key=lambda row: int(row.get("id") or 0), reverse=True):
        if value.get("状态") == VALUE_INVALID:
            state = "invalidated"
            invalidated_current = True
            continue
        if not key_matches(value, cache_key=cache_key, params=params, transform_version=transform_version):
            state = "stale"
            _mark_invalid(str(value.get("缓存键") or cache_key), "缓存键或参数/转换版本不一致")
            continue
        if _expired(value):
            state = "expired"
            _mark_invalid(cache_key, "缓存已过期")
            continue
        if value.get("状态") == VALUE_PARTIAL:
            state = "partial"
            continue
        matched = value
        break

    if matched is not None and timeout:
        # 超时不返回任何旧值，同时把当前键置为无效，避免失败重试复用过期结果。
        _mark_invalid(cache_key, "查询超时，旧值不再参与命中")
        _record_read(cache_key, hit=False)
        return empty_state(
            STATE_MESSAGES["timeout"],
            cache_key=cache_key,
            state="timeout",
            params=dict(params),
            transform_version=transform_version,
        )

    if matched is not None:
        dataset = [row for row in _dataset_rows(cache_key) if row.get("状态", VALUE_VALID) != VALUE_INVALID]
        if not dataset:
            _record_read(cache_key, hit=False)
            return {
                "state": "empty",
                "hit": False,
                "cache_key": cache_key,
                "params": dict(params),
                "transform_version": transform_version,
                "items": [],
                "message": STATE_MESSAGES["empty"],
                "traceable": True,
            }
        _record_read(cache_key, hit=True)
        return {
            "state": "hit",
            "hit": True,
            "cache_key": cache_key,
            "params": dict(params),
            "transform_version": transform_version,
            "items": [dict(row) for row in dataset],
            "batch": matched.get("批次号"),
            "message": STATE_MESSAGES["hit"],
            "traceable": True,
        }

    # 旧值残留：同一查询、同一参数下还存在其它转换版本/缓存键的值。
    # 这里只做排除（不返回、不命中），旧键的清理交给写入新版本或超时/空态清理。
    stale_current = state == "stale"
    residual_keys: list[str] = []
    if query_no:
        for value in _rows(VALUES_MODULE):
            if value.get("缓存键") == cache_key or value.get("状态") == VALUE_INVALID:
                continue
            if str(value.get("定义编号") or "") != str(query_no):
                continue
            stored = value.get("查询参数")
            stored_token = params_token({str(k): str(v) for k, v in stored.items()}) if isinstance(stored, Mapping) else str(stored or "")
            if stored_token == params_token(params):
                residual_keys.append(str(value.get("缓存键") or ""))
                stale_current = True
    if invalidated_current or state == "expired":
        # 当前缓存键已被明确置为无效（失效同步、过期、超时或版本不一致），重试时保持该状态，
        # 不能因为旁边还有旧版本的值就「看起来没事」。
        pass
    elif state != "partial" and stale_current:
        # 未完成批次优先报 partial：此时缓存里虽然还有别的版本，但首要原因是写入没写完。
        state = "stale"

    if timeout:
        state = "timeout"
        _mark_invalid(cache_key, "查询超时，旧值不再参与命中")
        for key in residual_keys:
            _mark_invalid(key, "查询超时，旧值残留一并清理")


    _record_read(cache_key, hit=False)
    message = STATE_MESSAGES.get(state, STATE_MESSAGES["miss"])
    return {
        "state": state,
        "hit": False,
        "cache_key": cache_key,
        "params": dict(params),
        "transform_version": transform_version,
        "items": [],
        "message": message,
        "traceable": True,
    }


def empty_state(message: str, *, cache_key: str = "", state: str = "miss", **extra: Any) -> dict[str, Any]:
    """统一空态：始终带 items=[]、hit=False 和可读原因，便于页面直接渲染。"""
    payload = {
        "state": state,
        "hit": False,
        "cache_key": cache_key,
        "items": [],
        "message": message,
        "traceable": True,
    }
    payload.update(extra)
    return payload


def write_result(
    *,
    cache_key: str,
    policy_no: str,
    query_no: str,
    transform_version: str,
    params: Mapping[str, str],
    rows: Iterable[Mapping[str, Any]],
    ttl: float,
    max_entries: int,
    batch_size: int = 2,
    interrupt_after: int | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    """把查询结果按完整缓存键写入缓存，返回可复核的写入结果。

    interrupt_after 用来复现执行断开：写满指定行数后模拟断开，未写完的批次被丢弃，
    但已完成的批次保留。resume=True 时从最后完整批次之后继续写，不会重复或覆盖。
    """
    rows = [dict(row) for row in rows]
    size = max(int(batch_size or 1), 1)
    if not rows:
        # 空结果不覆盖已有缓存：保持明确空态，避免把旧值洗成「合法空值」。
        return empty_state(STATE_MESSAGES["no_result"], cache_key=cache_key, state="no_result")

    batches = [row for row in _rows(BATCHES_MODULE) if row.get("缓存键") == cache_key]
    # 已经写完整、但上一轮没有收尾的批次，统一按「已中断」归档；
    # 它的结束行号只会在完整写入一个分片后才推进，所以可以直接作为恢复边界。
    for row in batches:
        if row.get("状态") == BATCH_WRITING:
            row["状态"] = BATCH_INTERRUPTED

    # 每次完整执行是一个新的轮次；恢复只在最近一次中断的轮次里找边界，
    # 不能被更早那轮完整写入的结束行号带偏（否则会误判成「已全部写完」）。
    cycle = max((int(row.get("轮次") or 1) for row in batches), default=0)
    batch: dict[str, Any] | None = None
    if resume:
        cycle_batches = [row for row in batches if int(row.get("轮次") or 1) == cycle]
        start = max((int(row.get("结束行号") or 0) for row in cycle_batches), default=0)
        if start >= len(rows):
            return {
                "state": "resumed_complete",
                "cache_key": cache_key,
                "written": 0,
                "resumed_from": start,
                "message": "批次已全部完整写入，无需继续",
                "traceable": True,
            }
        # 续写沿用同一个批次号：恢复不是新批次，数据集里的批次来源必须前后一致。
        batch = next(
            (row for row in reversed(cycle_batches) if row.get("状态") == BATCH_INTERRUPTED),
            None,
        )
        if batch is None:
            batch = next((row for row in reversed(cycle_batches) if row.get("状态") == BATCH_COMPLETE), None)
        if batch is None:
            batch = cycle_batches[-1] if cycle_batches else None
        if batch is None:
            # 没有任何可续写的批次时退化成一次完整写入。
            cycle += 1
            start = 0
        else:
            batch["状态"] = BATCH_WRITING
            batch["结束行号"] = start
            batch["已写入行"] = start
    else:
        cycle += 1
        start = 0
        # 重新完整执行时，先让该缓存键下的旧结果行退出有效集合，再写入新结果；
        # 旧行保留在数据集里并标记失效原因，保证可追溯。
        for row in _rows(DATASETS_MODULE):
            if row.get("缓存键") == cache_key and row.get("状态") != VALUE_INVALID:
                row["状态"] = VALUE_INVALID
                row["失效原因"] = "按同一缓存键重新写入"
        record_event("重新写入", 缓存键=cache_key, 已作废旧行=1)

    if batch is None:
        batch_no = f"{cache_key}#b{cycle}"
        batch = {
            "id": _next_id(BATCHES_MODULE),
            "批次号": batch_no,
            "轮次": cycle,
            "缓存键": cache_key,
            "策略编号": policy_no,
            "开始行号": start,
            "结束行号": start,
            "总行数": len(rows),
            "已写入行": 0,
            "状态": BATCH_WRITING,
        }
        _rows(BATCHES_MODULE).append(batch)
    else:
        batch_no = str(batch.get("批次号"))
        batch["总行数"] = len(rows)

    written = 0
    interrupted = False
    for offset in range(start, len(rows), size):
        chunk = rows[offset:offset + size]
        if interrupt_after is not None and written + len(chunk) > interrupt_after:
            interrupted = True
            break
        # 一个分片要么全写、要么不写；断开时未开始的分片不留任何残行。
        for row in chunk:
            dataset_row = {
                "id": _next_id(DATASETS_MODULE),
                "缓存键": cache_key,
                "策略编号": policy_no,
                "定义编号": query_no,
                "转换版本": transform_version,
                "查询参数": params_token(params),
                "批次号": batch_no,
                "状态": VALUE_VALID,
            }
            dataset_row.update(row)
            _rows(DATASETS_MODULE).append(dataset_row)
        written += len(chunk)
        batch["结束行号"] = offset + len(chunk)
        batch["已写入行"] = written

    if interrupted:
        batch["状态"] = BATCH_INTERRUPTED
        value = _upsert_value(
            cache_key=cache_key,
            policy_no=policy_no,
            query_no=query_no,
            transform_version=transform_version,
            params=params,
            ttl=ttl,
            max_entries=max_entries,
            state=VALUE_PARTIAL,
            batch_no=batch_no,
        )
        record_event("批次中断", 缓存键=cache_key, 批次号=batch_no, 已写入行=written, 总行数=len(rows))
        return {
            "state": "interrupted",
            "cache_key": cache_key,
            "batch": batch,
            "written": written,
            "resumable_from": int(batch.get("结束行号") or start),
            "value": value,
            "message": STATE_MESSAGES["interrupted"],
            "traceable": True,
        }

    batch["状态"] = BATCH_COMPLETE
    batch["结束行号"] = len(rows)
    pruned = prune_dataset(cache_key, max_entries)
    value = _upsert_value(
        cache_key=cache_key,
        policy_no=policy_no,
        query_no=query_no,
        transform_version=transform_version,
        params=params,
        ttl=ttl,
        max_entries=max_entries,
        state=VALUE_VALID,
        batch_no=batch_no,
    )
    record_event("批次完成", 缓存键=cache_key, 批次号=batch_no, 已写入行=written, 总行数=len(rows), 淘汰条数=pruned)
    return {
        "state": "written",
        "cache_key": cache_key,
        "batch": batch,
        "written": written,
        "resumed_from": start,
        "pruned": pruned,
        "value": value,
        "message": "查询结果已按完整缓存键写入缓存",
        "traceable": True,
    }


def _upsert_value(
    *,
    cache_key: str,
    policy_no: str,
    query_no: str,
    transform_version: str,
    params: Mapping[str, str],
    ttl: float,
    max_entries: int,
    state: str,
    batch_no: str,
) -> dict[str, Any]:
    now = time.time()
    value = None
    for row in _rows(VALUES_MODULE):
        if row.get("缓存键") == cache_key and row.get("状态") != VALUE_INVALID:
            value = row
            break
    if value is None:
        value = {"id": _next_id(VALUES_MODULE), "缓存键": cache_key, "创建时间": now}
        _rows(VALUES_MODULE).append(value)
    value.update(
        {
            "策略编号": policy_no,
            "定义编号": query_no,
            "转换版本": transform_version,
            "查询参数": params_token(params),
            "批次号": batch_no,
            "状态": state,
            "过期时间": now + float(ttl),
            "最大条目": int(max_entries),
        }
    )
    value.pop("失效原因", None)
    return value


def prune_dataset(cache_key: str, max_entries: int) -> int:
    """按最大条目淘汰最旧的有效结果行；已失效的历史行不计入容量。"""
    valid = [
        row
        for row in _rows(DATASETS_MODULE)
        if row.get("缓存键") == cache_key and row.get("状态") != VALUE_INVALID
    ]
    overflow = len(valid) - int(max_entries)
    if overflow <= 0:
        return 0
    for row in sorted(valid, key=lambda item: int(item.get("id") or 0))[:overflow]:
        row["状态"] = VALUE_INVALID
        row["失效原因"] = "超过最大条目"
    return overflow


def contract_status() -> dict[str, Any]:
    """缓存策略契约的读取面：当前有效键、命中率、批次与失效事件。"""
    values = [row for row in _rows(VALUES_MODULE) if row.get("状态") != VALUE_INVALID]
    keys = []
    for value in sorted(values, key=lambda row: str(row.get("缓存键") or "")):
        key = str(value.get("缓存键") or "")
        keys.append(
            {
                "缓存键": key,
                "策略编号": value.get("策略编号"),
                "定义编号": value.get("定义编号"),
                "转换版本": value.get("转换版本"),
                "查询参数": value.get("查询参数"),
                "状态": value.get("状态"),
                "过期时间": value.get("过期时间"),
                "批次号": value.get("批次号"),
                "命中率": hit_rate(key),
                "结果条数": len([row for row in _dataset_rows(key) if row.get("状态") != VALUE_INVALID]),
            }
        )
    return {
        "valid_keys": keys,
        "invalid_keys": [
            {"缓存键": row.get("缓存键"), "原因": row.get("失效原因"), "状态": row.get("状态")}
            for row in _rows(VALUES_MODULE)
            if row.get("状态") == VALUE_INVALID
        ],
        "events": list(_events()),
        "hit_rate": round(
            sum(hit_rate(str(row.get("缓存键") or "")) for row in keys) / len(keys), 4
        ) if keys else 0.0,
    }


def dataset(cache_key: str | None = None) -> list[dict[str, Any]]:
    """查询结果数据集：可按完整缓存键复核每一行结果的来源。"""
    rows = _rows(DATASETS_MODULE)
    if cache_key:
        rows = [row for row in rows if row.get("缓存键") == cache_key]
    return [dict(row) for row in rows]


def bindings(query_row: Mapping[str, Any], policies: Iterable[Mapping[str, Any]], transforms: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """把某条查询定义下所有策略的绑定关系摊平，供页面和复测脚本复核。"""
    from app.services.cache_key import policy_binds_query

    transforms = list(transforms)
    result = []
    for policy in policies:
        if not policy_binds_query(policy, query_row):
            continue
        version = policy_transform_version(policy, transforms)
        result.append(
            {
                "策略编号": policy.get("策略编号"),
                "状态": policy.get("status"),
                "缓存键模板": policy.get("缓存键模板"),
                "转换版本": version,
                "过期秒数": policy.get("过期秒数"),
                "最大条目": policy.get("最大条目"),
                "策略版本": policy.get("策略版本"),
                "可求缓存键": bool(str(policy.get("缓存键模板") or "").strip() and version),
            }
        )
    return result


__all__ = [
    "STATE_MESSAGES",
    "compose_cache_key",
    "invalidate_key",
    "invalidate_for_policy",
    "lookup",
    "write_result",
    "prune_dataset",
    "contract_status",
    "dataset",
    "hit_rate",
    "bindings",
    "empty_state",
]
