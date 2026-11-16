import { describe, expect, it, vi } from 'vitest'
import { getOperationsOverview } from '../../src/api/operations'
import { resetSessionRequests } from '../../src/api/session'

function validOverview() {
  return {
    checked_at: '2026-11-06T10:00:00Z', source: 'shared_redis', retention_days: 7,
    tasks: { pending: 1 }, oldest_pending_seconds: 0,
    metrics: { model_stream_seconds: { observations: 1, total: 2, mean: 2 } },
    database_pool: { size: 5, checkedin: 0, checkedout: 1, overflow: -4 },
    process: { rss_bytes: 100 }, disk: { total_bytes: 100, free_bytes: 50 },
    memory: { percent: 20, scope: 'container' },
    backup: { status: 'not_recorded' }, alerts: [],
  }
}

describe('operations request session contract', () => {
  it('uses the authenticated cookie and supports caller cancellation', async () => {
    const value = validOverview()
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(value)))
    vi.stubGlobal('fetch', fetcher)
    const controller = new AbortController()
    expect(await getOperationsOverview(controller.signal)).toEqual(value)
    expect(fetcher).toHaveBeenCalledWith('/api/v1/operations/metrics', expect.objectContaining({ credentials: 'include', signal: expect.any(AbortSignal) }))
  })

  it.each([
    ['empty object', {}], ['null', null], ['array', []],
    ['successful HTTP error envelope', { error: { message: 'upstream failed' } }],
    ['missing tasks', { ...validOverview(), tasks: undefined }],
    ['null tasks', { ...validOverview(), tasks: null }],
    ['non-numeric memory', { ...validOverview(), memory: { percent: '20', scope: 'container' } }],
    ['malformed metric', { ...validOverview(), metrics: { model_stream_seconds: { observations: 1, total: 2, mean: null } } }],
    ['malformed alerts', { ...validOverview(), alerts: [null] }],
    ['malformed backup', { ...validOverview(), backup: { status: 'backed_up', rto_verified: 'false' } }],
  ])('rejects %s as an independent metric contract error', async (_name, value) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(value))))
    await expect(getOperationsOverview()).rejects.toMatchObject({ code: 'INVALID_OPERATIONS_RESPONSE', message: '运行指标响应格式无效，请稍后重试' })
  })

  it('rejects a non-JSON successful response as a metric contract error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<html>unavailable</html>')))
    await expect(getOperationsOverview()).rejects.toMatchObject({ code: 'INVALID_OPERATIONS_RESPONSE' })
  })

  it('invalidates a response belonging to the previous authenticated session', async () => {
    let finish!: (value: Response) => void
    vi.stubGlobal('fetch', vi.fn(() => new Promise(resolve => { finish = resolve })))
    const request = getOperationsOverview(); resetSessionRequests()
    finish(new Response('{"tasks":{}}'))
    await expect(request).rejects.toMatchObject({ name: 'AbortError' })
  })

  it('uses the common unauthorized event and preserves the server error', async () => {
    const unauthorized = vi.fn()
    window.addEventListener('rag:unauthorized', unauthorized)
    try {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"error":{"message":"登录已失效","code":"UNAUTHORIZED"}}', { status: 401 })))
      await expect(getOperationsOverview()).rejects.toMatchObject({ status: 401, message: '登录已失效', code: 'UNAUTHORIZED' })
      expect(unauthorized).toHaveBeenCalledTimes(1)
    } finally { window.removeEventListener('rag:unauthorized', unauthorized) }
  })
})
