import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { agentRunMarkdown, agentRunPresentation, type AgentRun } from '../../src/agents'
import { ApiRequestError, listAgentRuns, streamMessage } from '../../src/api/client'
import StructuredResult from '../../src/components/StructuredResult.vue'
import { codecodeSystemData, codecodeText } from '../../src/systemPresentation'

afterEach(() => vi.unstubAllGlobals())

describe('system branding presentation', () => {
  it('normalizes historical brand spelling while retaining configuration key case', () => {
    expect(codecodeText('OpenCode / opencode / OPENCODE_BASE_URL')).toBe('codecode / codecode / CODECODE_BASE_URL')
  })

  it('keeps operational references and original evidence intact in presentation copies', () => {
    const original = {
      backend: 'opencode', message: 'OpenCode 连接失败',
      session_id: 'opencode-session', model: 'opencode/model', base_url: 'https://opencode.example/v1',
      quote: '文档讨论 OpenCode', content: 'OpenCode 原文',
      nested: [{ message: 'OPENCODE_MODEL 未配置', document_ids: ['opencode-document'] }],
    }
    expect(codecodeSystemData(original)).toEqual({
      ...original, backend: 'codecode', message: 'codecode 连接失败',
      nested: [{ message: 'CODECODE_MODEL 未配置', document_ids: ['opencode-document'] }],
    })
    expect(original.backend).toBe('opencode')
  })

  it('brands diagnostics and retains source text in the shared result renderer', () => {
    const system = mount(StructuredResult, { props: { value: { backend: 'opencode', message: 'OpenCode 执行完成', session_id: 'opencode-session' }, system: true } })
    expect(system.text()).toContain('codecode 执行完成')
    expect(system.text()).toContain('opencode-session')
    const evidence = mount(StructuredResult, { props: { value: { quote: 'OpenCode 文档原文' } } })
    expect(evidence.text()).toContain('OpenCode 文档原文')
  })

  it('brands HTTP and streaming errors without changing error codes or message IDs', async () => {
    expect(new ApiRequestError('OpenCode 不可用', 503, 'OPENCODE_UNAVAILABLE')).toMatchObject({ message: 'codecode 不可用', code: 'OPENCODE_UNAVAILABLE' })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ error: { message: 'OPENCODE_MODEL 未配置', code: 'CONFIGURATION_ERROR' } }), { status: 503 })))
    await expect(listAgentRuns('kb')).rejects.toMatchObject({ message: 'CODECODE_MODEL 未配置', status: 503 })
    const onError = vi.fn(), onMessageId = vi.fn()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('event: message\ndata: {"message_id":"opencode-message"}\n\nevent: error\ndata: {"code":"OPENCODE_ERROR","message":"OpenCode 执行失败"}\n\n')))
    await streamMessage('conversation', '问题', { onDelta: vi.fn(), onError, onMessageId })
    expect(onError).toHaveBeenCalledWith('OPENCODE_ERROR', 'codecode 执行失败')
    expect(onMessageId).toHaveBeenCalledWith('opencode-message')
  })

  it('brands historical report metadata while retaining source evidence and references', () => {
    const run: AgentRun = {
      id: 'opencode-run', knowledge_base_id: 'kb', task: 'business_analysis', status: 'failed',
      instructions: '分析 OpenCode 文档', document_ids: ['opencode-document'], options: {}, created_at: '2026-11-18T00:00:00Z',
      backend: 'opencode', model: 'opencode/model', session_id: 'opencode-session', error_message: 'OpenCode 失败',
      output: { summary: 'OpenCode 业务文本', evidence: [{ document_id: 'opencode-document', quote: 'OpenCode 原文证据' }], diagnostics: [{ message: 'OpenCode 诊断' }] },
      events: [{ id: 'opencode-event', type: 'session', message: 'OpenCode 会话已创建' }],
      usage: { cost_note: '按 OpenCode 报告估算' },
    }
    const presentation = agentRunPresentation(run)
    expect(presentation).toMatchObject({ id: run.id, backend: 'codecode', model: run.model, session_id: run.session_id, error_message: 'codecode 失败' })
    expect(presentation.events?.[0]).toMatchObject({ id: 'opencode-event', message: 'codecode 会话已创建' })
    const markdown = agentRunMarkdown(run)
    expect(markdown).toContain('codecode 诊断')
    expect(markdown).toContain('codecode 会话已创建')
    expect(markdown).toContain('OpenCode 原文证据')
    expect(markdown).toContain('opencode-session')
    expect(run.backend).toBe('opencode')
  })
})
