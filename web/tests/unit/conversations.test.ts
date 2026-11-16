import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../../src/api/client'
import { resetConversations, useConversations } from '../../src/composables/useConversations'
import type { ConversationDetail } from '../../src/types'
vi.mock('../../src/api/client', () => ({ getConversation: vi.fn(), createConversation: vi.fn(), listConversations: vi.fn(), streamMessage: vi.fn(), logout: vi.fn(), login: vi.fn() }))

function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done }); return { promise, resolve } }
beforeEach(() => resetConversations())

describe('conversation isolation', () => {
  it('replaces streamed draft text with the final validated answer without replaying buffered deltas', async () => {
    vi.mocked(api.createConversation).mockResolvedValue({id:'validated',knowledge_base_id:'kb'} as never)
    vi.mocked(api.streamMessage).mockImplementation(async(_id,_content,handlers)=>{
      handlers.onDelta('draft with [来源99]')
      handlers.onAnswer?.('校验后的最终回答')
      handlers.onDone?.('completed')
    })
    const conv=useConversations();await conv.send('question','standard',undefined,'kb')
    expect(conv.messages.value[1].content).toBe('校验后的最终回答')
  })
  it('ignores a late response from the previously selected conversation', async () => {
    const a = deferred<ConversationDetail>(); const b = deferred<ConversationDetail>()
    vi.mocked(api.getConversation).mockReturnValueOnce(a.promise).mockReturnValueOnce(b.promise)
    const conv = useConversations(); const first = conv.select('A'); const second = conv.select('B')
    b.resolve({ messages: [{ id: 'b', content: 'B' }] } as ConversationDetail); await second
    a.resolve({ messages: [{ id: 'a', content: 'A' }] } as ConversationDetail); await first
    expect(conv.activeId.value).toBe('B'); expect(conv.messages.value[0].content).toBe('B')
  })
  it('creates a conversation for the first question and streams the answer', async () => {
    vi.mocked(api.createConversation).mockResolvedValue({ id: 'new', knowledge_base_id: 'kb' } as never)
    vi.mocked(api.streamMessage).mockImplementation(async (_id, _content, handlers) => { handlers.onDelta('answer'); handlers.onDone?.('completed') })
    const conv = useConversations()
    expect(await conv.send('question', 'standard', undefined, 'kb')).toBe(true)
    expect(api.createConversation).toHaveBeenCalledWith('新会话', 'kb')
    expect(conv.messages.value[1].content).toBe('answer')
  })
  it('rejects failed creation without silently accepting the question', async () => {
    vi.mocked(api.createConversation).mockRejectedValueOnce(new Error('offline'))
    expect(await useConversations().send('question', 'standard', undefined, 'kb')).toBe(false)
    expect(useConversations().errorMsg.value).toBe('offline')
  })
  it('clears all private data and ignores in-flight selection after logout', async () => {
    const late = deferred<ConversationDetail>(); vi.mocked(api.getConversation).mockReturnValueOnce(late.promise)
    const conv = useConversations(); const pending = conv.select('private')
    resetConversations()
    late.resolve({ messages: [{ content: 'private-data' }] } as ConversationDetail); await pending
    expect(conv.activeId.value).toBeNull(); expect(conv.messages.value).toEqual([]); expect(conv.loadingDetail.value).toBe(false)
  })
})
