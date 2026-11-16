import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/cache'
const columns = [
  "策略编号",
  "关联服务",
  "缓存键模板",
  "过期秒数",
  "淘汰算法",
  "预热策略",
  "命中率阈值",
  "最大条目"
]
const actions = [
  "提交校验",
  "启用缓存",
  "停用缓存"
]
const statuses = [
  "草稿",
  "待校验",
  "已启用",
  "已停用"
]
const stats = [
  {
    "label": "缓存策略总数",
    "value": 0
  },
  {
    "label": "已启用策略",
    "value": 0
  },
  {
    "label": "待校验策略",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

type ContractKey = {
  "缓存键": string
  "策略编号"?: string
  "转换版本"?: string
  "状态"?: string
  "命中率"?: number
  "结果条数"?: number
}

type ContractStatus = {
  valid_keys: ContractKey[]
  invalid_keys: { 缓存键: string; 原因: string }[]
  events: { id: number; 事件: string; 缓存键?: string; 原因?: string }[]
  hit_rate: number
}

export default function CacheView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [contract, setContract] = useState<ContractStatus | null>(null)
  const [preview, setPreview] = useState<{ 缓存键: string; 转换版本: string; 查询参数: Record<string, string> } | null>(null)
  const [previewParams, setPreviewParams] = useState('{"order_id":"A1001"}')
  const [selectedPolicy, setSelectedPolicy] = useState<Row | null>(null)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('缓存策略列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      const summary = await request(`${ENDPOINT}/contract`)
      if (summary.ok) {
        setContract((await summary.json()) as ContractStatus)
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '结果缓存列表读取失败')
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
        // 启用条件不满足时（空模板、缺过期时间或最大条目）直接展示原因。
        throw new Error(payload.message ?? '结果缓存动作未生效，请稍后重试')
      }
      // 停用缓存时失效结果已同步给缓存策略契约，重新拉取即可读到新的有效键；
      // 启用成功后同样刷新，避免列表与契约状态对不上。
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '结果缓存操作失败')
    }
  }

  async function loadContract(row: Row) {
    setErrorMessage('')
    setSelectedPolicy(row)
    try {
      const response = await request(`${ENDPOINT}/contract/${row.id}`)
      if (!response.ok) {
        throw new Error('缓存策略契约读取失败')
      }
      const payload = await response.json()
      setContract((prev) => ({
        valid_keys: payload.valid_keys ?? [],
        invalid_keys: prev?.invalid_keys ?? [],
        events: payload.events ?? [],
        hit_rate: payload.hit_rate ?? 0,
      }))
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '缓存策略契约读取失败')
    }
  }

  async function previewKey() {
    if (!selectedPolicy) {
      setErrorMessage('请先在列表中选择一条缓存策略')
      return
    }
    setErrorMessage('')
    try {
      const params = JSON.parse(previewParams || '{}')
      const response = await request(`${ENDPOINT}/${selectedPolicy.id}/key`, {
        method: 'POST',
        body: JSON.stringify({ params }),
      })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '完整缓存键生成失败')
      }
      setPreview(payload.preview)
    } catch (error) {
      setPreview(null)
      setErrorMessage(error instanceof Error ? error.message : '完整缓存键生成失败')
    }
  }

  return (
    <section className="page" data-module="cache">
      <header className="page-head">
        <div>
          <h2>结果缓存管理</h2>
          <p className="page-desc">维护结果缓存策略的输入、执行约束与结果记录，为结果缓存提供可验证的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('缓存策略登记入口尚未接入审批流')}>
            登记缓存策略
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出结果缓存清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => {
          const value = item.label === '缓存策略总数'
            ? total
            : item.label === '已启用策略'
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
                <button className="link" type="button" onClick={() => void loadContract(row)}>契约</button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无结果缓存数据，可先登记缓存策略</td>
            </tr>
          )}
        </tbody>
      </table>

      {contract && (
        <section className="stat-row" style={{ marginTop: 12 }}>
          <article className="stat-card">
            <span className="stat-label">契约命中率</span>
            <strong className="stat-value">{Math.round((contract.hit_rate ?? 0) * 100)}%</strong>
            <span className="stat-label">当前有效缓存键 {contract.valid_keys.length} 个</span>
          </article>
          <article className="stat-card" style={{ flex: 2 }}>
            <span className="stat-label">当前有效缓存键（按完整缓存键读取）</span>
            {contract.valid_keys.length === 0 ? (
              <p className="empty-state">暂无有效缓存键，旧值已排除在当前查询结果之外</p>
            ) : (
              <ul>
                {contract.valid_keys.map((item) => (
                  <li key={item["缓存键"]}>
                    <code>{item["缓存键"]}</code>
                    <span> · 策略 {item["策略编号"]} · 转换 {item["转换版本"]} · 结果 {item["结果条数"]} 条 · 命中率 {Math.round((item["命中率"] ?? 0) * 100)}%</span>
                  </li>
                ))}
              </ul>
            )}
            {contract.invalid_keys.length > 0 && (
              <p className="error-text">已失效 {contract.invalid_keys.length} 个缓存键：{contract.invalid_keys[0]?.原因}</p>
            )}
          </article>
        </section>
      )}

      <section className="filter-bar" style={{ marginTop: 12, alignItems: 'flex-end' }}>
        <label className="filter-item">
          <span>复核查询参数（JSON）</span>
          <input value={previewParams} onChange={(event) => setPreviewParams(event.target.value)} />
        </label>
        <button className="btn" type="button" onClick={() => void previewKey()}>生成完整缓存键</button>
        {preview && (
          <span className="page-desc">
            <code>{preview["缓存键"]}</code> · 转换版本 {preview["转换版本"]}
          </span>
        )}
      </section>

      <footer className="page-foot">
        <span>共 {total} 条结果缓存记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
