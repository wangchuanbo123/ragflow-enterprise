export interface User {
  id: string
  username: string
  display_name: string
  role: string
  is_active: boolean
}

export interface Conversation {
  id: string
  title: string
  created_at: string
  updated_at: string
  last_message_at: string | null
}

export interface Message {
  id: string
  role: string
  content: string
  status: string
  sources?: SourceItem[]
  error_message?: string | null
  created_at: string
}

export interface SourceItem {
  source: string
  preview: string
}

export interface ConversationDetail extends Conversation {
  messages: Message[]
}

export interface ApiError {
  error: { code: string; message: string }
}
