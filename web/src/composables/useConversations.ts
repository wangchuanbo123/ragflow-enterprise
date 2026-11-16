import { reactive, ref } from 'vue'
import * as api from '../api/client'
import type { CodeSnippet, Conversation, ConversationDetail, Message, RetrievalScope, RetrievalStrategy, SourceImage, SourceItem } from '../types'

const conversations = ref<Conversation[]>([])
const activeId = ref<string | null>(null)
const messages = ref<Message[]>([])
const sending = ref(false)
const errorMsg = ref('')
const loadingList = ref(false)
const loadingDetail = ref(false)
let activeController: AbortController | null = null
let detailController: AbortController | null = null
let generation = 0
let selection = 0

function resetDetail(): void {
  selection += 1
  detailController?.abort()
  detailController = null
  messages.value = []
  errorMsg.value = ''
  loadingDetail.value = false
}

export function resetConversations(): void {
  generation += 1
  activeController?.abort()
  activeController = null
  conversations.value = []
  activeId.value = null
  sending.value = false
  loadingList.value = false
  resetDetail()
}

async function loadList(): Promise<void> {
  const started = generation
  loadingList.value = true
  try {
    const loaded = await api.listConversations()
    if (started !== generation) return
    conversations.value = loaded
    if (activeId.value && !loaded.some(item => item.id === activeId.value)) {
      activeId.value = null
      resetDetail()
    }
  } catch (e) {
    if (started === generation && (e as Error).name !== 'AbortError') errorMsg.value = (e as Error).message
  } finally {
    if (started === generation) loadingList.value = false
  }
}

async function select(id: string): Promise<void> {
  activeId.value = id
  resetDetail()
  const selected = selection
  detailController = new AbortController()
  loadingDetail.value = true
  try {
    const detail: ConversationDetail = await api.getConversation(id, detailController.signal)
    if (selected !== selection || activeId.value !== id) return
    messages.value = detail.messages
  } catch (e) {
    if (selected === selection && (e as Error).name !== 'AbortError') errorMsg.value = (e as Error).message
  } finally {
    if (selected === selection) loadingDetail.value = false
  }
}

async function create(knowledgeBaseId?: string): Promise<void> {
  const started = generation
  try {
    const conv = await api.createConversation('新会话', knowledgeBaseId)
    if (started !== generation) return
    conversations.value.unshift(conv)
    activeId.value = conv.id
    resetDetail()
  } catch (e) {
    if (started === generation && (e as Error).name !== 'AbortError') errorMsg.value = (e as Error).message
    throw e
  }
}

async function rename(id: string, title: string): Promise<void> {
  const started = generation
  try {
    const conv = await api.renameConversation(id, title)
    if (started !== generation) return
    const idx = conversations.value.findIndex((c) => c.id === id)
    if (idx !== -1) conversations.value[idx] = { ...conversations.value[idx], ...conv }
  } catch(e) { if(started===generation&&(e as Error).name!=='AbortError')errorMsg.value=(e as Error).message }
}

async function remove(id: string): Promise<void> {
  const started = generation
  try {
    await api.deleteConversation(id)
    if (started !== generation) return
    conversations.value = conversations.value.filter((c) => c.id !== id)
    if (activeId.value === id) { activeId.value = null; resetDetail() }
  } catch(e) { if(started===generation&&(e as Error).name!=='AbortError')errorMsg.value=(e as Error).message }
}

async function send(content: string, strategy: RetrievalStrategy = 'standard', scope?: Partial<RetrievalScope>, knowledgeBaseId?: string): Promise<boolean> {
  if (!content.trim() || sending.value) return false
  const started = generation
  sending.value = true
  if (!activeId.value) {
    try { await create(knowledgeBaseId) }
    catch { if (started === generation) sending.value = false; return false }
  }
  if (started !== generation || !activeId.value) return false
  const convId = activeId.value
  activeController = new AbortController()
  errorMsg.value = ''

  // 立即显示用户消息
  const userMsg: Message = {
    id: 'pending-' + Date.now(),
    role: 'user',
    content,
    status: 'completed',
    created_at: new Date().toISOString(),
  }
  const assistantMsg: Message = reactive({
    id: '',
    conversation_id: convId,
    role: 'assistant',
    content: '',
    status: 'streaming',
    sources: [] as SourceItem[],
    images: [] as SourceImage[],
    code_snippets: [] as CodeSnippet[],
    created_at: new Date().toISOString(),
  })
  messages.value.push(userMsg, assistantMsg)
  let deltaBuffer = ''
  let deltaFrame: number | null = null
  const flushDelta = (): void => {
    if (!deltaBuffer) return
    assistantMsg.content += deltaBuffer
    deltaBuffer = ''
  }
  const scheduleDelta = (): void => {
    if (deltaFrame != null) return
    deltaFrame = window.requestAnimationFrame(() => {
      deltaFrame = null
      flushDelta()
    })
  }

  try {
    await api.streamMessage(
      convId,
      content,
      {
        onMessageId: (id) => {
          assistantMsg.id = id
        },
        onDelta: (chunk) => {
          deltaBuffer += chunk
          scheduleDelta()
        },
        onAnswer: (content) => {
          if (started !== generation) return
          if (deltaFrame != null) window.cancelAnimationFrame(deltaFrame)
          deltaFrame = null
          deltaBuffer = ''
          assistantMsg.content = content
        },
        onRoute: (route) => {
          assistantMsg.route = route
        },
        onSources: (sources, images, codeSnippets) => {
          assistantMsg.sources = sources
          assistantMsg.images = images
          assistantMsg.code_snippets = codeSnippets
        },
        onDone: (status) => {
          if (deltaFrame != null) window.cancelAnimationFrame(deltaFrame)
          deltaFrame = null
          flushDelta()
          assistantMsg.status = status
        },
        onError: (code, message) => {
          if (started !== generation) return
          void code
          assistantMsg.status = 'failed'
          assistantMsg.error_message = message
          if(activeId.value===convId)errorMsg.value = message
        },
      },
      { strategy, scope },
      activeController.signal,
    )
  } catch (e) {
    if (deltaFrame != null) window.cancelAnimationFrame(deltaFrame)
    deltaFrame = null
    flushDelta()
    if (started !== generation) return false
    if ((e as Error).name === 'AbortError') {
      assistantMsg.status = 'interrupted'
      assistantMsg.error_message = null
    } else {
      assistantMsg.status = 'failed'
      assistantMsg.error_message = (e as Error).message
      if(activeId.value===convId)errorMsg.value = (e as Error).message
    }
  } finally {
    flushDelta()
    if (started === generation) {
      sending.value = false
      activeController = null
    }
  }
  return started === generation && assistantMsg.status !== 'failed'
}

function stop(): void {
  activeController?.abort()
}

async function feedback(messageId:string, rating:'helpful'|'unhelpful', comment?:string):Promise<void> {
  const message = messages.value.find(item=>item.id===messageId)
  if (!message?.conversation_id || !message.id) return
  try {
    await api.saveMessageFeedback(message.conversation_id, message.id, rating, comment)
    message.feedback = rating
  } catch(e) { if((e as Error).name!=='AbortError')errorMsg.value=(e as Error).message }
}

export function useConversations() {
  return {
    conversations,
    activeId,
    messages,
    sending,
    errorMsg,
    loadingList,
    loadingDetail,
    loadList,
    select,
    create,
    rename,
    remove,
    send,
    stop,
    feedback,
    resetDetail,
  }
}
