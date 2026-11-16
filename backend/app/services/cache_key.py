"""缓存键与版本仲裁：缓存键模板、查询参数、转换模板版本必须一起参与计算。

这个模块只做纯函数，方便在查询入口、缓存策略契约和复测脚本里共用同一套口径：
同一个查询参数、同一个转换版本，无论从哪个页面读取，算出来的完整缓存键都必须一致；
任意一项变化，缓存键就必须变化，旧缓存值因此不可能被当前查询命中。
"""
from __future__ import annotations

import json
import re
from string import Formatter
from typing import Any, Iterable, Mapping

PARAM_SPLIT = re.compile(r"[,，;；/\s]+")
VERSION_DIGITS = re.compile(r"\d+")

STATE_MESSAGES: dict[str, str] = {
    "hit": "缓存命中：按完整缓存键返回查询结果",
    "miss": "缓存未命中：当前完整缓存键没有有效结果",
    "empty": "空缓存命中：缓存中的结果为空，本次返回明确空态，不返回旧值",
    "expired": "缓存已过期：旧值已清理，请重新执行查询",
    "stale": "旧值残留：缓存键、查询参数或转换版本不一致，已排除并清理",
    "invalidated": "缓存已失效：缓存策略契约已把当前缓存键置为无效，请重新执行查询",
    "timeout": "查询超时：本次返回明确空态，失败后重试不会复用过期结果",
    "interrupted": "批次执行已中断：仅保留最后完整批次，请从断点继续写入",
    "partial": "缓存仅有未完成批次：当前缓存键尚未生效，请继续写入",
    "no_policy": "无可用缓存策略：请先登记并启用带完整缓存键模板、过期秒数和最大条目的策略",
    "version_unbound": "转换版本未绑定到该查询的缓存策略：旧版本缓存已被排除",
    "no_result": "没有可写入的查询结果：本次保持明确空态，未覆盖已有缓存值",
    "disabled": "查询定义已停用：本次返回明确空态",
}

ARBITRATION_RULE = "当前转换版本水位 > 已启用策略 > 策略版本高者 > 登记 id 大者；当前版本无可用策略时不回落到旧版本"


def version_sort_key(value: Any) -> tuple[int, ...]:
    """把 v1、v2、v10 这类版本号转成可比较的数字序列。"""
    digits = VERSION_DIGITS.findall(str(value or ""))
    return tuple(int(item) for item in digits) if digits else (0,)


def split_params(value: Any) -> list[str]:
    """参数占位支持逗号、分号、斜杠或空白分隔。"""
    return [item for item in PARAM_SPLIT.split(str(value or "").strip()) if item]


def normalize_params(raw: Mapping[str, Any] | None, query_row: Mapping[str, Any] | None = None) -> tuple[dict[str, str], list[str]]:
    """统一参数口径：只保留字符串值、按键排序，并检查查询定义声明的占位是否齐全。"""
    params: dict[str, str] = {}
    for key, value in (raw or {}).items():
        name = str(key).strip()
        if not name:
            continue
        if isinstance(value, (dict, list)):
            params[name] = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        elif value is None:
            params[name] = ""
        else:
            params[name] = str(value)
    declared = split_params(query_row.get("参数占位")) if query_row else []
    missing = [name for name in declared if name not in params]
    return dict(sorted(params.items())), missing


def params_token(params: Mapping[str, str]) -> str:
    """参数的稳定序列化，用来直接写进缓存键。"""
    return json.dumps(dict(sorted(params.items())), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def template_fields(template: str) -> list[str]:
    try:
        return [field for _, field, _, _ in Formatter().parse(template) if field]
    except ValueError:
        return []


def compose_cache_key(
    template: str,
    *,
    policy_no: str,
    query_no: str,
    params: Mapping[str, str],
    transform_version: str,
) -> str:
    """渲染完整缓存键。

    模板负责业务可读前缀，参数签名和转换版本作为强制后缀追加，避免模板漏写占位时
    不同查询参数或不同转换版本落进同一个缓存键。
    """
    cleaned_template = str(template or "").strip()
    if not cleaned_template:
        raise ValueError("缓存键模板不能为空")
    version = str(transform_version or "").strip()
    if not version:
        raise ValueError("转换版本不能为空")
    token = params_token(params)
    context = {
        **dict(params),
        "定义编号": str(query_no or ""),
        "查询编号": str(query_no or ""),
        "策略编号": str(policy_no or ""),
        "转换版本": version,
        "版本": version,
        "参数": token,
        "查询参数": token,
    }

    class _SafeDict(dict[str, str]):
        def __missing__(self, key: str) -> str:  # pragma: no cover - 兜底分支
            return "{" + key + "}"

    try:
        rendered = cleaned_template.format_map(_SafeDict(context))
    except (IndexError, KeyError, ValueError) as error:
        raise ValueError(f"缓存键模板占位不合法：{error}") from error
    return f"{rendered}|params={token}|tpl={version}"


def policy_binds_query(policy: Mapping[str, Any], query_row: Mapping[str, Any]) -> bool:
    """缓存策略与查询定义的绑定关系，同时接受编号、id 两种写法。"""
    reference = str(
        policy.get("关联查询定义")
        or policy.get("定义编号")
        or policy.get("query_id")
        or ""
    ).strip()
    if not reference:
        return False
    return reference in {
        str(query_row.get("定义编号") or "").strip(),
        str(query_row.get("id") or "").strip(),
    }


def policy_transform_version(policy: Mapping[str, Any], transforms: Iterable[Mapping[str, Any]]) -> str:
    """策略自身声明的转换版本优先，否则回落到绑定转换模板的当前版本。"""
    declared = str(policy.get("转换版本") or policy.get("transform_version") or "").strip()
    if declared:
        return declared
    reference = str(policy.get("关联转换模板") or policy.get("模板编号") or "").strip()
    if not reference:
        return ""
    for row in transforms:
        if reference in {str(row.get("模板编号") or "").strip(), str(row.get("id") or "").strip()}:
            return str(row.get("版本") or row.get("文档版本") or "").strip()
    return ""


def policy_errors(policy: Mapping[str, Any], query_row: Mapping[str, Any] | None, transforms: Iterable[Mapping[str, Any]]) -> list[str]:
    """启用缓存前的完整性校验，过期时间、最大条目和缓存键模板缺一不可。"""
    errors: list[str] = []
    template = str(policy.get("缓存键模板") or "").strip()
    if not template:
        errors.append("缓存键模板不能为空")
    else:
        try:
            Formatter().parse(template)
        except ValueError as error:
            errors.append(f"缓存键模板占位不合法：{error}")
    ttl = policy.get("过期秒数")
    try:
        if ttl is None or str(ttl).strip() == "" or float(ttl) <= 0:
            errors.append("过期秒数缺失或不为正数")
    except (TypeError, ValueError):
        errors.append("过期秒数缺失或不为正数")
    limit = policy.get("最大条目")
    try:
        if limit is None or str(limit).strip() == "" or int(float(limit)) <= 0:
            errors.append("最大条目缺失或不为正数")
    except (TypeError, ValueError):
        errors.append("最大条目缺失或不为正数")
    if query_row is None:
        errors.append("关联查询定义不存在")
    if not policy_transform_version(policy, transforms):
        errors.append("转换版本未绑定：缓存键无法与转换模板版本绑定")
    return errors


def resolve_policy(
    query_row: Mapping[str, Any],
    policies: Iterable[Mapping[str, Any]],
    transforms: Iterable[Mapping[str, Any]],
    requested_version: str | None = None,
    current_version: str | None = None,
) -> tuple[Mapping[str, Any] | None, str]:
    """按固定优先级选出当前生效的缓存策略。

    版本仲裁分两步：
    1. 先确定当前转换版本：查询上已登记的版本水位优先，其次取绑定策略里的最高版本。
       水位只会随新版本登记/启用升高，不会因为停用高版本策略而回落，
       因此旧版本的缓存值不会重新变成「当前结果」。
    2. 再在同一版本的已启用策略里按 策略版本 > 登记 id 选出一条。
    当前版本没有已启用策略时直接返回 version_unbound，绝不回落到更低版本。
    """
    transforms = list(transforms)
    bound: list[Mapping[str, Any]] = []
    for policy in policies:
        if policy.get("status") == "已停用":
            continue
        if not policy_binds_query(policy, query_row):
            continue
        if not str(policy.get("缓存键模板") or "").strip():
            continue
        if not policy_transform_version(policy, transforms):
            continue
        bound.append(policy)

    version = str(requested_version or "").strip()
    if not version:
        version = str(current_version or "").strip()
    if not version:
        declared = [policy_transform_version(policy, transforms) for policy in bound]
        version = max(declared, key=version_sort_key) if declared else ""
    if not version:
        return None, "no_policy"

    eligible = [
        policy
        for policy in bound
        if policy.get("status") == "已启用"
        and policy_transform_version(policy, transforms) == version
        and not policy_errors(policy, query_row, transforms)
    ]
    if not eligible:
        # 该版本没有可用的已启用策略：宁可明确空态，也不回落到其它版本的旧缓存值。
        return None, "version_unbound" if bound else "no_policy"
    eligible.sort(
        key=lambda policy: (
            version_sort_key(policy.get("策略版本")),
            int(policy.get("id") or 0),
        ),
        reverse=True,
    )
    return eligible[0], "ok"


def key_matches(
    value: Mapping[str, Any],
    *,
    cache_key: str,
    params: Mapping[str, str],
    transform_version: str,
) -> bool:
    """缓存值必须与当前完整缓存键、参数签名和转换版本同时一致，旧值一律排除。"""
    if str(value.get("缓存键") or "") != cache_key:
        return False
    if str(value.get("转换版本") or "") != str(transform_version or ""):
        return False
    stored = value.get("查询参数")
    if isinstance(stored, Mapping):
        stored_token = params_token({str(k): str(v) for k, v in stored.items()})
    else:
        stored_token = str(stored or "")
    return stored_token == params_token(params)
