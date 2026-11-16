import { computed, onScopeDispose, ref, watch, type Ref } from 'vue'
import { getOperationsOverview } from '../api/operations'
import type { User } from '../types'
import type { OperationsOverview } from '../types/operations'

export function useOperationsOverview(user: Ref<User | null>) {
  const isAdmin = computed(() => user.value?.role === 'admin')
  const overview = ref<OperationsOverview | null>(null)
  const loading = ref(false)
  const error = ref('')
  let controller: AbortController | undefined

  async function refresh(): Promise<void> {
    controller?.abort()
    if (!isAdmin.value) {
      controller = undefined
      overview.value = null
      loading.value = false
      error.value = ''
      return
    }
    const request = new AbortController()
    controller = request
    loading.value = true
    error.value = ''
    try {
      const result = await getOperationsOverview(request.signal)
      if (controller === request && !request.signal.aborted) overview.value = result
    } catch (failure) {
      if (controller === request && !request.signal.aborted && (failure as Error).name !== 'AbortError') {
        error.value = (failure as Error).message || '请稍后重试'
      }
    } finally {
      if (controller === request) loading.value = false
    }
  }

  watch(() => [user.value?.id, user.value?.role], () => {
    overview.value = null
    void refresh()
  }, { immediate: true })
  onScopeDispose(() => controller?.abort())

  return { isAdmin, overview, loading, error, refresh }
}
