// Requests are scoped to the authenticated browser session. A stale response may
// never populate a new user's UI, even if its network response beats abort().
let generation = 0
const controllers = new Set<AbortController>()

export function sessionRequest(signal?: AbortSignal) {
  const controller = new AbortController()
  const started = generation
  const abort = () => controller.abort()
  if (signal?.aborted) controller.abort()
  else signal?.addEventListener('abort', abort, { once: true })
  controllers.add(controller)
  return {
    signal: controller.signal,
    assertCurrent() {
      if (started !== generation || controller.signal.aborted) throw new DOMException('请求已取消', 'AbortError')
    },
    release() {
      controllers.delete(controller)
      signal?.removeEventListener('abort', abort)
    },
  }
}

export function resetSessionRequests(): void {
  generation += 1
  for (const controller of controllers) controller.abort()
  controllers.clear()
}

export function notifyUnauthorized(): void {
  window.dispatchEvent(new Event('rag:unauthorized'))
}
