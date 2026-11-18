import { describe, expect, it, vi } from 'vitest'
import { getCodeVectorState, getIndexJob, rebuildCodeVectors } from '../../src/api/client'
import { resetSessionRequests } from '../../src/api/session'
import type { CodeVectorState } from '../../src/types'

describe('code graph vector governance requests', () => {
  it('reads a tracked job directly when it is outside the latest jobs list', async () => {
    const job = { id: 'older-job', job_type: 'code_index', status: 'completed' }
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(job)))
    vi.stubGlobal('fetch', fetcher)

    expect(await getIndexJob('kb', 'older-job')).toEqual(job)
    expect(fetcher).toHaveBeenCalledWith('/api/v1/knowledge-bases/kb/index-jobs/older-job', expect.objectContaining({ credentials: 'include', signal: expect.any(AbortSignal) }))
  })

  it('reads the vector state with the current authenticated session', async () => {
    const state: CodeVectorState = {
      snapshot_id: 'snapshot', index_name: 'code-index', embedding_fingerprint: 'embedding',
      symbol_fingerprint: 'symbols', symbol_count: 12, status: 'ready', updated_at: null,
    }
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(state)))
    vi.stubGlobal('fetch', fetcher)

    expect(await getCodeVectorState('kb')).toEqual(state)
    expect(fetcher).toHaveBeenCalledWith('/api/v1/knowledge-bases/kb/code-graph/vector', expect.objectContaining({ credentials: 'include', signal: expect.any(AbortSignal) }))
  })

  it('creates a rebuild job through the dedicated endpoint', async () => {
    const job = { id: 'vector-job', job_type: 'code_vector_rebuild', status: 'pending' }
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(job), { status: 202 }))
    vi.stubGlobal('fetch', fetcher)

    expect(await rebuildCodeVectors('kb')).toEqual(job)
    expect(fetcher).toHaveBeenCalledWith('/api/v1/knowledge-bases/kb/code-graph/vector/rebuild', expect.objectContaining({ method: 'POST', credentials: 'include' }))
    expect(fetcher.mock.calls[0][1].body).toBeUndefined()
  })

  it('preserves edit permission failures for the governance page', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { code: 'FORBIDDEN', message: '没有该知识库的编辑权限' } }), { status: 403 })))
    await expect(rebuildCodeVectors('kb')).rejects.toMatchObject({ status: 403, code: 'FORBIDDEN', message: '没有该知识库的编辑权限' })
  })

  it('discards vector state returned after the session changes', async () => {
    let finish!: (value: Response) => void
    vi.stubGlobal('fetch', vi.fn(() => new Promise(resolve => { finish = resolve })))
    const request = getCodeVectorState('kb')
    resetSessionRequests()
    finish(new Response('{"status":"ready"}'))
    await expect(request).rejects.toMatchObject({ name: 'AbortError' })
  })
})
