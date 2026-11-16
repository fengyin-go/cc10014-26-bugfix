"""数据查询接口：维护查询定义，覆盖提交校验、启用查询、停用查询等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.query import QueryService

router = APIRouter(prefix="/api/query", tags=["数据查询"])

service = QueryService()

LIST_FIELDS = ["定义编号", "关联数据源", "查询模板", "参数占位", "结果字段", "排序规则", "分页大小", "超时秒数"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("/contract", response_model=dict)
def contract() -> dict:
    """缓存策略契约读取面：当前有效缓存键、命中率、批次与失效事件。"""
    from app.services.cache_contract import contract_status

    return contract_status()


@router.get("/results", response_model=dict)
def results(
    缓存键: str | None = Query(default=None, description="按完整缓存键复核查询结果数据集"),
    cache_key: str | None = Query(default=None, description="完整缓存键的英文别名"),
):
    """查询结果数据集：可按完整缓存键复核每一行的参数、转换版本和批次。"""
    from app.services.cache_contract import dataset

    key = 缓存键 or cache_key
    rows = dataset(key)
    return {"total": len(rows), "items": rows, "cache_key": key}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按定义编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按定义编号与状态过滤数据查询列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据查询清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "query", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条查询定义明细，并带上当前有效缓存键与命中率，便于详情页复核。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"查询定义 {entry_id} 不存在或已归档")
    return service.detail_view(entry)


@router.post("/{entry_id}/execute", response_model=dict)
def execute(entry_id: int, payload: EntryPayload) -> dict:
    """查询入口：按完整缓存键读取或写入查询结果，异常分支返回明确空态。"""
    return service.execute(entry_id, payload.values)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条查询定义，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="查询定义已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条查询定义执行提交校验、启用查询、停用查询；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
