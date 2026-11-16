import { reactive, ref } from 'vue'
import * as api from '../api/client'
import type { User } from '../types'
import { resetConversations } from './useConversations'
import { resetSessionRequests } from '../api/session'

const user = ref<User | null>(null)
const ready = ref(false)
const state = reactive({ loading: false, error: '' })

function clearSession(): void {
  resetSessionRequests()
  resetConversations()
  user.value = null
}

window.addEventListener('rag:unauthorized', clearSession)

async function restore(): Promise<void> {
  try {
    user.value = await api.fetchMe()
  } catch (e) {
    user.value = null
  } finally {
    ready.value = true
  }
}

async function login(username: string, password: string): Promise<void> {
  clearSession()
  state.loading = true
  state.error = ''
  try {
    user.value = await api.login(username, password)
  } catch (e) {
    state.error = (e as Error).message
    throw e
  } finally {
    state.loading = false
  }
}

async function logout(): Promise<void> {
  await api.logout()
  clearSession()
}

export function useAuth() {
  return { user, ready, state, restore, login, logout, clearSession }
}
