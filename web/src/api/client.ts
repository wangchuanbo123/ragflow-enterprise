import type {
  ApiError,
  Conversation,
  ConversationDetail,
  Message,
  User,
} from '../types'

const BASE = '/api/v1'

async function parseError(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as ApiError | { detail?: string }
    if ('error' in body) return body.error.message
    if ('detail' in body) return String(body.detail)
  } catch {
    // 非 JSON 响应
  }
  return `请求失败（${res.status}）`
}

export async function login(
  username: string,
  password: string,
): Promise<User> {
  const res = await fetch(`${BASE}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ username, password }),
  })
  if (!res.ok) throw new Error(await parseError(res))
  const body = (await res.json()) as { user: User }
  return body.user
}

export async function logout(): Promise<void> {
  await fetch(`${BASE}/auth/logout`, {
    method: 'POST',
    credentials: 'include',
  })
}

export async function fetchMe(): Promise<User | null> {
  const res = await fetch(`${BASE}/auth/me`, { credentials: 'include' })
  if (res.status === 401) return null
  if (!res.ok) throw new Error(await parseError(res))
  return (await res.json()) as User
}

export async function listConversations(): Promise<Conversation[]> {
  const res = await fetch(`${BASE}/conversations`, { credentials: 'include' })
  if (!res.ok) throw new Error(await parseError(res))
  return (await res.json()) as Conversation[]
}

export async function createConversation(
  title = '新会话',
): Promise<Conversation> {
  const res = await fetch(`${BASE}/conversations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ title }),
  })
  if (!res.ok) throw new Error(await parseError(res))
  return (await res.json()) as Conversation
}

export async function getConversation(
  id: string,
): Promise<ConversationDetail> {
  const res = await fetch(`${BASE}/conversations/${id}`, {
    credentials: 'include',
  })
  if (!res.ok) throw new Error(await parseError(res))
  return (await res.json()) as ConversationDetail
}

export async function renameConversation(
  id: string,
  title: string,
): Promise<Conversation> {
  const res = await fetch(`${BASE}/conversations/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ title }),
  })
  if (!res.ok) throw new Error(await parseError(res))
  return (await res.json()) as Conversation
}

export async function deleteConversation(id: string): Promise<void> {
  const res = await fetch(`${BASE}/conversations/${id}`, {
    method: 'DELETE',
    credentials: 'include',
  })
  if (!res.ok) throw new Error(await parseError(res))
}

// 同步问答（用于异常重试或非流式场景）
export async function sendMessage(
  conversationId: string,
  content: string,
): Promise<{ user_message: Message; assistant_message: Message }> {
  const res = await fetch(`${BASE}/conversations/${conversationId}/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ content }),
  })
  if (!res.ok) throw new Error(await parseError(res))
  return (await res.json()) as {
    user_message: Message
    assistant_message: Message
  }
}

export interface StreamHandlers {
  onMessageId?: (id: string) => void
  onDelta: (chunk: string) => void
  onSources?: (sources: { source: string; preview: string }[]) => void
  onDone?: (status: string) => void
  onError?: (code: string, message: string) => void
}

/**
 * 通过 POST 读取 SSE 流。
 * 使用 fetch + ReadableStream 手工解析 text/event-stream。
 */
export async function streamMessage(
  conversationId: string,
  content: string,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${BASE}/conversations/${conversationId}/messages/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ content }),
    signal,
  })

  if (!res.ok || !res.body) {
    throw new Error(await parseError(res))
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    // 按空行分割事件块
    let sep: number
    while ((sep = buffer.indexOf('\n\n')) !== -1) {
      const block = buffer.slice(0, sep)
      buffer = buffer.slice(sep + 2)
      parseEvent(block, handlers)
    }
  }
  // 处理剩余
  if (buffer.trim()) parseEvent(buffer, handlers)
}

function parseEvent(block: string, handlers: StreamHandlers): void {
  let event = ''
  let data = ''
  for (const line of block.split('\n')) {
    if (line.startsWith('event: ')) event = line.slice(7).trim()
    else if (line.startsWith('data: ')) data += line.slice(6)
  }
  if (!event || !data) return

  let payload: any = {}
  try {
    payload = JSON.parse(data)
  } catch {
    return
  }

  switch (event) {
    case 'message':
      handlers.onMessageId?.(payload.message_id)
      break
    case 'delta':
      handlers.onDelta(payload.content ?? '')
      break
    case 'sources':
      handlers.onSources?.(payload.sources ?? [])
      break
    case 'done':
      handlers.onDone?.(payload.status)
      break
    case 'error':
      handlers.onError?.(payload.code ?? 'UNKNOWN', payload.message ?? '未知错误')
      break
  }
}
