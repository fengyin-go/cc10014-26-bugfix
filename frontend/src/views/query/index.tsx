import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/query'
const columns = [
  "定义编号",
  "关联数据源",
  "查询模板",
  "参数占位",
  "结果字段",
  "排序规则",
  "分页大小",
  "超时秒数"
]
const actions = [
  "提交校验",
  "启用查询",
  "停用查询"
]
const statuses = [
  "草稿",
  "待校验",
  "已启用",
  "已停用"
]
const stats = [
  {
    "label": "查询定义总数",
    "value": 0
  },
  {
    "label": "已启用定义",
    "value": 0
  },
  {
    "label": "待校验定义",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

type ContractKey = {
  "缓存键": string
  "策略编号"?: string
  "转换版本"?: string
  "命中率"?: number
  "结果条数"?: number
}

type QueryDetail = Row & {
  valid_keys?: ContractKey[]
  hit_rate?: number
  events?: { id: number; 事件: string; 原因?: string }[]
}

type ExecuteResult = {
  state: string
  hit: boolean
  cache_key: string
  message: string
  items: Record<string, string | number | null>[]
  transform_version?: string
  params?: Record<string, string>
  resumable_from?: number
}

export default function QueryView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [detail, setDetail] = useState<QueryDetail | null>(null)
  const [orderId, setOrderId] = useState('A1001')
  const [transformVersion, setTransformVersion] = useState('')
  const [result, setResult] = useState<ExecuteResult | null>(null)
  const [busy, setBusy] = useState(false)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('查询定义列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据查询列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '数据查询动作未生效，请稍后重试')
      }
      await reload()
      if (detail && detail.id === row.id) {
        await openDetail(row)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据查询操作失败')
    }
  }

  async function openDetail(row: Row) {
    setErrorMessage('')
    setResult(null)
    try {
      const response = await request(`${ENDPOINT}/${row.id}`)
      if (!response.ok) {
        throw new Error('查询详情读取失败')
      }
      const payload = (await response.json()) as QueryDetail
      setDetail(payload)
      const version = payload.valid_keys?.[0]?.["转换版本"]
      if (version) {
        setTransformVersion(String(version))
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '查询详情读取失败')
    }
  }

  async function execute(operation: 'read' | 'write', extra: Record<string, unknown> = {}) {
    if (!detail) {
      setErrorMessage('请先打开一条查询定义')
      return
    }
    setBusy(true)
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${detail.id}/execute`, {
        method: 'POST',
        body: JSON.stringify({
          operation,
          params: { order_id: orderId },
          transform_version: transformVersion || undefined,
          ...extra,
        }),
      })
      if (!response.ok) {
        throw new Error('查询执行失败')
      }
      const payload = (await response.json()) as ExecuteResult
      setResult(payload)
      // 命中、空态、超时、旧值残留都会更新契约里的有效键与命中率。
      const refreshed = await request(`${ENDPOINT}/${detail.id}`)
      if (refreshed.ok) {
        setDetail((await refreshed.json()) as QueryDetail)
      }
    } catch (error) {
      setResult(null)
      setErrorMessage(error instanceof Error ? error.message : '查询执行失败')
    } finally {
      setBusy(false)
    }
  }

  const emptyState = result && !result.hit

  return (
    <section className="page" data-module="query">
      <header className="page-head">
        <div>
          <h2>数据查询管理</h2>
          <p className="page-desc">维护数据查询定义的输入、执行约束与结果记录，为数据查询提供可验证的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('查询定义登记入口尚未接入审批流')}>
            登记查询定义
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出数据查询清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => {
          const value = item.label === '查询定义总数'
            ? total
            : item.label === '已启用定义'
              ? rows.filter((row) => row.status === '已启用').length
              : rows.filter((row) => row.status === '待校验').length
          return (
            <article key={item.label} className="stat-card">
              <span className="stat-label">{item.label}</span>
              <strong className="stat-value">{value}</strong>
            </article>
          )
        })}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void openDetail(row)}>详情</button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无数据查询数据，可先登记查询定义</td>
            </tr>
          )}
        </tbody>
      </table>

      {detail && (
        <section style={{ marginTop: 16 }}>
          <header className="page-head">
            <div>
              <h3>查询详情：{detail["定义编号"]}</h3>
              <p className="page-desc">
                当前有效缓存键 {detail.valid_keys?.length ?? 0} 个 · 命中率 {Math.round((detail.hit_rate ?? 0) * 100)}%
              </p>
            </div>
          </header>

          <div className="filter-bar">
            <label className="filter-item">
              <span>order_id</span>
              <input value={orderId} onChange={(event) => setOrderId(event.target.value)} />
            </label>
            <label className="filter-item">
              <span>转换版本</span>
              <input
                value={transformVersion}
                placeholder="默认取当前策略绑定版本"
                onChange={(event) => setTransformVersion(event.target.value)}
              />
            </label>
            <button className="btn primary" type="button" disabled={busy} onClick={() => void execute('read')}>按完整缓存键读取</button>
            <button className="btn" type="button" disabled={busy} onClick={() => void execute('write')}>写入查询结果</button>
            <button className="btn" type="button" disabled={busy} onClick={() => void execute('write', { interrupt_after: 2 })}>模拟断开</button>
            <button className="btn" type="button" disabled={busy} onClick={() => void execute('write', { resume: true })}>从断点继续</button>
            <button className="btn" type="button" disabled={busy} onClick={() => void execute('read', { timeout: true })}>模拟超时</button>
          </div>

          {detail.valid_keys && detail.valid_keys.length > 0 && (
            <div className="stat-row">
              <article className="stat-card" style={{ flex: 2 }}>
                <span className="stat-label">当前有效缓存键</span>
                <ul>
                  {detail.valid_keys.map((item) => (
                    <li key={item["缓存键"]}>
                      <code>{item["缓存键"]}</code>
                      <span> · 策略 {item["策略编号"]} · 转换 {item["转换版本"]} · 命中率 {Math.round((item["命中率"] ?? 0) * 100)}%</span>
                    </li>
                  ))}
                </ul>
              </article>
            </div>
          )}

          {result && (
            <div>
              <p className={result.hit ? 'page-desc' : 'error-text'}>
                {result.hit ? '命中' : `空态：${result.state}`} · {result.message}
              </p>
              <p className="page-desc">
                <code>{result.cache_key}</code> · 参数 {JSON.stringify(result.params ?? {})} · 转换版本 {result.transform_version}
                {typeof result.resumable_from === 'number' && result.resumable_from > 0 && ` · 可继续写入位置 ${result.resumable_from}`}
              </p>
              {emptyState ? (
                <p className="empty-state">当前查询没有可返回的结果，未复用任何旧值</p>
              ) : (
                <table className="data-table">
                  <thead>
                    <tr>
                      {Object.keys(result.items[0] ?? {}).map((key) => <th key={key}>{key}</th>)}
                    </tr>
                  </thead>
                  <tbody>
                    {result.items.map((item, index) => (
                      <tr key={String(item.id ?? index)}>
                        {Object.keys(result.items[0] ?? {}).map((key) => <td key={key}>{item[key] ?? '—'}</td>)}
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条数据查询记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
