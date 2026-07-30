import { reactive, ref } from 'vue'
import * as api from '../api/client'
import type { Conversation, ConversationDetail, Message, SourceItem } from '../types'

const conversations = ref<Conversation[]>([])
const activeId = ref<string | null>(null)
const messages = ref<Message[]>([])
const sending = ref(false)
const errorMsg = ref('')
const loadingList = ref(false)
const loadingDetail = ref(false)

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

async function create(): Promise<void> {
  const conv = await api.createConversation('新会话')
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

async function send(content: string): Promise<void> {
  if (!activeId.value || sending.value) return
  const convId = activeId.value
  sending.value = true
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
    role: 'assistant',
    content: '',
    status: 'streaming',
    sources: [] as SourceItem[],
    created_at: new Date().toISOString(),
  })
  messages.value.push(userMsg, assistantMsg)

  try {
    await api.streamMessage(
      convId,
      content,
      {
        onMessageId: (id) => {
          assistantMsg.id = id
        },
        onDelta: (chunk) => {
          assistantMsg.content += chunk
        },
        onSources: (sources) => {
          assistantMsg.sources = sources
        },
        onDone: (status) => {
          assistantMsg.status = status
        },
        onError: (code, message) => {
          void code
          assistantMsg.status = 'failed'
          assistantMsg.error_message = message
          errorMsg.value = message
        },
      },
    )
  } catch (e) {
    assistantMsg.status = 'failed'
    assistantMsg.error_message = (e as Error).message
    errorMsg.value = (e as Error).message
  } finally {
    sending.value = false
  }
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
    resetDetail,
  }
}
