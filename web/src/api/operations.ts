import { ApiRequestError } from './client'
import { notifyUnauthorized, sessionRequest } from './session'
import type { OperationsOverview } from '../types/operations'

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}
function nonnegative(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
}
function timestamp(value: unknown): value is string {
  return typeof value === 'string' && Number.isFinite(Date.parse(value))
}
function optionalFields(value: Record<string, unknown>, fields: string[], valid: (field: unknown) => boolean): boolean {
  return fields.every(key => value[key] === undefined || valid(value[key]))
}
function isOperationsOverview(value: unknown): value is OperationsOverview {
  if (!object(value) || !timestamp(value.checked_at)
    || typeof value.source !== 'string' || !['shared_redis', 'process', 'process_fallback'].includes(value.source)
    || !nonnegative(value.retention_days) || value.retention_days < 1 || !Number.isInteger(value.retention_days)
    || !object(value.tasks) || !Object.values(value.tasks).every(item => nonnegative(item) && Number.isInteger(item))
    || !nonnegative(value.oldest_pending_seconds) || !object(value.metrics)) return false
  if (!Object.values(value.metrics).every(metric => object(metric)
    && nonnegative(metric.observations) && nonnegative(metric.total) && nonnegative(metric.mean))) return false
  if (!object(value.database_pool) || !optionalFields(value.database_pool,
    ['size', 'checkedin', 'checkedout', 'overflow'], item => typeof item === 'number' && Number.isFinite(item) && Number.isInteger(item))) return false
  if (!object(value.process) || !nonnegative(value.process.rss_bytes)
    || !object(value.disk) || !nonnegative(value.disk.total_bytes) || !nonnegative(value.disk.free_bytes)
    || !object(value.memory) || !nonnegative(value.memory.percent) || typeof value.memory.scope !== 'string' || !['host', 'container'].includes(value.memory.scope)) return false
  if (!object(value.backup) || typeof value.backup.status !== 'string'
    || !optionalFields(value.backup, ['last_success_at', 'last_attempt_at'], timestamp)
    || !optionalFields(value.backup, ['last_error', 'mirror_status', 'mirror_error'], item => typeof item === 'string')
    || !optionalFields(value.backup, ['rpo_target_hours', 'rto_target_minutes'], nonnegative)
    || !optionalFields(value.backup, ['rto_verified'], item => typeof item === 'boolean')) return false
  return Array.isArray(value.alerts) && value.alerts.every(alert => object(alert)
    && typeof alert.code === 'string' && typeof alert.message === 'string'
    && optionalFields(alert, ['seconds', 'count'], nonnegative))
}

export async function getOperationsOverview(signal?: AbortSignal): Promise<OperationsOverview> {
  const pending = sessionRequest(signal)
  try {
    let response: Response
    try {
      response = await fetch('/api/v1/operations/metrics', { credentials: 'include', signal: pending.signal })
    } catch (error) {
      if ((error as Error).name === 'AbortError') throw error
      throw new Error('无法连接到服务，请稍后重试')
    }
    pending.assertCurrent()
    if (response.status === 401) notifyUnauthorized()
    if (!response.ok) {
      let message = `请求失败（${response.status}）`
      let code: string | undefined
      let details: unknown
      try {
        const body = await response.json()
        if (typeof body?.error?.message === 'string') {
          message = body.error.message
          code = body.error.code
          details = body.error.details
        } else if (typeof body?.detail === 'string') message = body.detail
      } catch { /* Preserve the HTTP status for non-JSON responses. */ }
      throw new ApiRequestError(message, response.status, code, details)
    }
    let body: unknown
    try { body = await response.json() }
    catch {
      pending.assertCurrent()
      throw new ApiRequestError('运行指标响应格式无效，请稍后重试', response.status, 'INVALID_OPERATIONS_RESPONSE')
    }
    pending.assertCurrent()
    if (!isOperationsOverview(body)) throw new ApiRequestError('运行指标响应格式无效，请稍后重试', response.status, 'INVALID_OPERATIONS_RESPONSE')
    return body
  } finally {
    pending.release()
  }
}
