import { describe, expect, it, vi } from 'vitest'
import { getReadiness, streamMessage, listKnowledgeBases, rebuildKnowledgeBaseIndex } from '../../src/api/client'
import { resetSessionRequests } from '../../src/api/session'

describe('API responses', () => {
  it('creates a full-library shadow rebuild using the dedicated endpoint',async()=>{
    const fetch=vi.fn().mockResolvedValue(new Response(JSON.stringify({job_id:'shadow-job',status:'accepted'}),{status:202}))
    vi.stubGlobal('fetch',fetch)
    expect(await rebuildKnowledgeBaseIndex('kb')).toEqual({job_id:'shadow-job',status:'accepted'})
    expect(fetch).toHaveBeenCalledWith('/api/v1/knowledge-bases/kb/index-jobs/rebuild',expect.objectContaining({method:'POST'}))
    expect(fetch.mock.calls[0][1].body).toBeUndefined()
  })
  it('passes final answer replacement and the final route through the SSE stream',async()=>{
    const body='event: delta\ndata: {"content":"draft"}\n\nevent: answer\ndata: {"content":"final"}\n\nevent: route\ndata: {"route":{"citation_validation":{"status":"valid"}}}\n\nevent: done\ndata: {"status":"completed"}\n\n'
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue(new Response(body)))
    const answer=vi.fn(),route=vi.fn()
    await streamMessage('id','q',{onDelta:vi.fn(),onAnswer:answer,onRoute:route})
    expect(answer).toHaveBeenCalledWith('final')
    expect(route).toHaveBeenCalledWith({citation_validation:{status:'valid'}})
  })
  it('preserves readiness diagnostics on HTTP 503', async () => {
    const body = { status: 'not_ready', components: { database: { status: 'error', detail: 'database down' } } }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status: 503 })))
    expect(await getReadiness()).toEqual(body)
  })
  it('discards a response from a previous authenticated session', async () => {
    let finish!: (value: Response) => void
    vi.stubGlobal('fetch', vi.fn(() => new Promise(resolve => { finish = resolve })))
    const pending = listKnowledgeBases(); resetSessionRequests(); finish(new Response('[]'))
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
  })
  it('reports a truncated stream instead of leaving a perpetual streaming answer', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('event: delta\ndata: {"content":"partial"}\n\n')))
    const delta = vi.fn()
    await expect(streamMessage('id', 'q', { onDelta: delta })).rejects.toThrow('提前中断')
    expect(delta).toHaveBeenCalledWith('partial')
  })
})
