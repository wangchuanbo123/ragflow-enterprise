import { ref, watch, type Ref } from 'vue'
import { useAuth } from './useAuth'

/** Browser-tab drafts are isolated by account and logical page scope. */
export function useScopedPageState<T>(scope: () => string[], initial: () => T, decode: (value: unknown) => T | null): Ref<T> {
  const { user } = useAuth()
  const state = ref(initial()) as Ref<T>
  let activeKey = '', restoring = false
  watch(() => JSON.stringify([user.value?.id || '', ...scope()]), identity => {
    restoring = true
    activeKey = user.value?.id ? `rag-page-state:v1:${identity}` : ''
    let restored: T | null = null
    try {
      const raw = activeKey && window.sessionStorage.getItem(activeKey)
      if (raw) restored = decode(JSON.parse(raw))
    } catch { /* Storage may be disabled or contain an older draft. */ }
    state.value = restored ?? initial()
    restoring = false
  }, { immediate: true, flush: 'sync' })
  watch(state, value => {
    if (restoring || !activeKey) return
    try { window.sessionStorage.setItem(activeKey, JSON.stringify(value)) } catch { /* Keep editing when storage is full. */ }
  }, { deep: true, flush: 'sync' })
  return state
}
