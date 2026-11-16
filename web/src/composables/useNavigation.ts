import { shallowRef } from 'vue'

export const pages = ['chat', 'agents', 'search', 'documents', 'sources', 'jobs', 'graph', 'graph-management', 'code-graph', 'backups', 'debug', 'readiness', 'users', 'account'] as const
export type Page = typeof pages[number]
export interface NavigationState { page: Page; kb: string; conversation: string; query: string; agentRun: string; document: string; chunk: string }

export function parseNavigation(hash: string): NavigationState {
  const params = new URLSearchParams(hash.replace(/^#\/?/, ''))
  const page = params.get('page') as Page
  return { page: pages.includes(page) ? page : 'chat', kb: params.get('kb') || '', conversation: params.get('conversation') || '', query: params.get('query') || '', agentRun:params.get('run')||'',document:params.get('document')||'',chunk:params.get('chunk')||'' }
}
export const navigationState = shallowRef(parseNavigation(window.location.hash))
const readLocation = () => { navigationState.value = parseNavigation(window.location.hash) }
window.addEventListener('popstate', readLocation)
window.addEventListener('hashchange', readLocation)

export function navigate(update: Partial<NavigationState>, replace = false): void {
  const next = { ...navigationState.value, ...update }
  const params = new URLSearchParams({ page: next.page })
  if (next.kb) params.set('kb', next.kb)
  if (next.page === 'chat' && next.conversation) params.set('conversation', next.conversation)
  if ((next.page === 'search' || next.page === 'jobs') && next.query) params.set('query', next.query)
  if (next.page === 'agents' && next.agentRun) params.set('run',next.agentRun)
  if (next.page === 'documents' && next.document) params.set('document',next.document)
  if (next.page === 'documents' && next.document && next.chunk) params.set('chunk',next.chunk)
  const hash = `#${params}`
  if (window.location.hash !== hash) window.history[replace ? 'replaceState' : 'pushState'](null, '', hash)
  navigationState.value = parseNavigation(hash)
}
