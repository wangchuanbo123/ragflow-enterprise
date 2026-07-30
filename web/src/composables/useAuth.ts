import { reactive, ref } from 'vue'
import * as api from '../api/client'
import type { User } from '../types'

const user = ref<User | null>(null)
const ready = ref(false)
const state = reactive({ loading: false, error: '' })

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
  user.value = null
}

export function useAuth() {
  return { user, ready, state, restore, login, logout }
}
