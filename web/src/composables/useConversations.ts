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

function resetDetail(): void {
  messages.value = []
  errorMsg.value = ''
}

async function loadList(): Promise<void> {
  loadingList.value = true
  try {
    conversations.value = await api.listConversations()
  } catch (e) {
    errorMsg.value = (e as Error).message
  } finally {
    loadingList.value = false
  }
}

async function select(id: string): Promise<void> {
  activeId.value = id
  resetDetail()
  loadingDetail.value = true
  try {
    const detail: ConversationDetail = await api.getConversation(id)
    messages.value = detail.messages
  } catch (e) {
    errorMsg.value = (e as Error).message
  } finally {
    loadingDetail.value = false
  }
}

async function create(knowledgeBaseId?: string): Promise<void> {
  const conv = await api.createConversation('新会话', knowledgeBaseId)
  conversations.value.unshift(conv)
  await select(conv.id)
}

async function rename(id: string, title: string): Promise<void> {
  const conv = await api.renameConversation(id, title)
  const idx = conversations.value.findIndex((c) => c.id === id)
  if (idx !== -1) conversations.value[idx] = { ...conversations.value[idx], ...conv }
}

async function remove(id: string): Promise<void> {
  await api.deleteConversation(id)
  conversations.value = conversations.value.filter((c) => c.id !== id)
  if (activeId.value === id) {
    activeId.value = null
    resetDetail()
  }
}

async function send(content: string, strategy: RetrievalStrategy = 'standard', scope?: Partial<RetrievalScope>): Promise<void> {
  if (!activeId.value || sending.value) return
  const convId = activeId.value
  sending.value = true
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
          void code
          assistantMsg.status = 'failed'
          assistantMsg.error_message = message
          errorMsg.value = message
        },
      },
      { strategy, scope },
      activeController.signal,
    )
  } catch (e) {
    if (deltaFrame != null) window.cancelAnimationFrame(deltaFrame)
    deltaFrame = null
    flushDelta()
    if ((e as Error).name === 'AbortError') {
      assistantMsg.status = 'stopped'
      assistantMsg.error_message = null
    } else {
      assistantMsg.status = 'failed'
      assistantMsg.error_message = (e as Error).message
      errorMsg.value = (e as Error).message
    }
  } finally {
    flushDelta()
    sending.value = false
    activeController = null
  }
}

function stop(): void {
  activeController?.abort()
}

async function feedback(messageId:string, rating:'helpful'|'unhelpful', comment?:string):Promise<void> {
  const message = messages.value.find(item=>item.id===messageId)
  if (!message?.conversation_id || !message.id) return
  await api.saveMessageFeedback(message.conversation_id, message.id, rating, comment)
  message.feedback = rating
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
