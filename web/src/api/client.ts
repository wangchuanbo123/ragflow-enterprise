import type {
  ApiError,
  Conversation,
  ConversationDetail,
  CodeGraphStats,
  CodeSnapshot,
  CodeSubgraph,
  CodeSymbol,
  DataSource,
  DocumentChunk,
  DocumentSubgraph,
  DocumentVersion,
  EntitySubgraph,
  EntityMerge,
  EntityVectorState,
  Folder,
  GraphEntity,
  GraphCorrection,
  GraphStats,
  GraphCommunity,
  GraphQuality,
  GraphReviewItem,
  GraphSchema,
  IndexJob,
  KnowledgeBase,
  KnowledgeBaseMember,
  KnowledgeDocument,
  Message,
  Readiness,
  RetrievalDebugResult,
  RouteDecision,
  Tag,
  User,
} from '../types'

const BASE = '/api/v1'

async function parseError(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as ApiError | { detail?: string }
    if ('error' in body) return body.error.message
    if ('detail' in body) return String(body.detail)
  } catch {
    // Non-JSON error response.
  }
  return `请求失败（${res.status}）`
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers,
    credentials: 'include',
  })
  if (!res.ok) throw new Error(await parseError(res))
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export async function login(username: string, password: string): Promise<User> {
  const body = await request<{ user: User }>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  })
  return body.user
}

export function logout(): Promise<void> {
  return request<void>('/auth/logout', { method: 'POST' })
}

export async function fetchMe(): Promise<User | null> {
  const res = await fetch(`${BASE}/auth/me`, { credentials: 'include' })
  if (res.status === 401) return null
  if (!res.ok) throw new Error(await parseError(res))
  return (await res.json()) as User
}

export function listKnowledgeBases(): Promise<KnowledgeBase[]> {
  return request('/knowledge-bases')
}

export function listKnowledgeBaseMembers(knowledgeBaseId: string): Promise<KnowledgeBaseMember[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/members`)
}

export function upsertKnowledgeBaseMember(
  knowledgeBaseId: string,
  username: string,
  role: 'viewer' | 'editor',
): Promise<KnowledgeBaseMember> {
  return request(`/knowledge-bases/${knowledgeBaseId}/members`, {
    method: 'PUT',
    body: JSON.stringify({ username, role }),
  })
}

export function removeKnowledgeBaseMember(knowledgeBaseId: string, userId: string): Promise<void> {
  return request(`/knowledge-bases/${knowledgeBaseId}/members/${userId}`, { method: 'DELETE' })
}

export function listDataSources(knowledgeBaseId: string): Promise<DataSource[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/sources`)
}

export function createDataSource(
  knowledgeBaseId: string,
  payload: {
    source_type: DataSource['source_type']
    name: string
    connection_ref?: string | null
    config?: Record<string, unknown>
    sync_interval_minutes?: number | null
    deletion_policy?: 'ignore' | 'delete'
  },
): Promise<DataSource> {
  return request(`/knowledge-bases/${knowledgeBaseId}/sources`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function updateDataSource(
  knowledgeBaseId: string,
  sourceId: string,
  payload: Partial<Pick<DataSource, 'name' | 'connection_ref' | 'config' | 'status' | 'sync_interval_minutes' | 'deletion_policy'>>,
): Promise<DataSource> {
  return request(`/knowledge-bases/${knowledgeBaseId}/sources/${sourceId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export function deleteDataSource(knowledgeBaseId: string, sourceId: string): Promise<void> {
  return request(`/knowledge-bases/${knowledgeBaseId}/sources/${sourceId}`, { method: 'DELETE' })
}

export function checkDataSource(knowledgeBaseId: string, sourceId: string): Promise<DataSource> {
  return request(`/knowledge-bases/${knowledgeBaseId}/sources/${sourceId}/check`, { method: 'POST' })
}

export function syncDataSource(knowledgeBaseId: string, sourceId: string): Promise<import('../types').DataSourceSyncResult> {
  return request(`/knowledge-bases/${knowledgeBaseId}/sources/${sourceId}/sync`, { method: 'POST' })
}

export function listConversations(): Promise<Conversation[]> {
  return request('/conversations')
}

export function createConversation(
  title = '新会话',
  knowledgeBaseId?: string,
): Promise<Conversation> {
  return request('/conversations', {
    method: 'POST',
    body: JSON.stringify({ title, ...(knowledgeBaseId ? { knowledge_base_id: knowledgeBaseId } : {}) }),
  })
}

export function getConversation(id: string): Promise<ConversationDetail> {
  return request(`/conversations/${id}`)
}

export function renameConversation(id: string, title: string): Promise<Conversation> {
  return request(`/conversations/${id}`, {
    method: 'PATCH',
    body: JSON.stringify({ title }),
  })
}

export function deleteConversation(id: string): Promise<void> {
  return request(`/conversations/${id}`, { method: 'DELETE' })
}

export function sendMessage(
  conversationId: string,
  content: string,
  strategy: import('../types').RetrievalStrategy = 'standard',
): Promise<{ user_message: Message; assistant_message: Message }> {
  return request(`/conversations/${conversationId}/messages`, {
    method: 'POST',
    body: JSON.stringify({ content, retrieval: { strategy } }),
  })
}

export function listDocuments(knowledgeBaseId: string): Promise<KnowledgeDocument[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/documents`)
}

export async function uploadDocuments(knowledgeBaseId: string, files: File[]): Promise<void> {
  const form = new FormData()
  files.forEach((file) => form.append('files', file))
  await request(`/knowledge-bases/${knowledgeBaseId}/documents/batch-upload`, {
    method: 'POST',
    body: form,
  })
}

export async function reindexDocument(knowledgeBaseId: string, documentId: string): Promise<void> {
  await request(`/knowledge-bases/${knowledgeBaseId}/documents/${documentId}/reindex`, { method: 'POST' })
}

export async function deleteDocument(knowledgeBaseId: string, documentId: string): Promise<void> {
  await request(`/knowledge-bases/${knowledgeBaseId}/documents/${documentId}`, { method: 'DELETE' })
}

export async function batchDocuments(
  knowledgeBaseId: string,
  documentIds: string[],
  action: 'delete' | 'reindex' | 'tag' | 'move',
  extra: { tag_ids?: string[]; folder_id?: string | null } = {},
): Promise<void> {
  await request(`/knowledge-bases/${knowledgeBaseId}/documents/batch`, {
    method: 'POST',
    body: JSON.stringify({ document_ids: documentIds, action, ...extra }),
  })
}

export function listDocumentChunks(knowledgeBaseId: string, documentId: string): Promise<DocumentChunk[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/documents/${documentId}/chunks?limit=200`)
}

export function listDocumentVersions(knowledgeBaseId: string, documentId: string): Promise<DocumentVersion[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/documents/${documentId}/versions`)
}

export async function uploadDocumentVersion(
  knowledgeBaseId: string,
  documentId: string,
  file: File,
): Promise<void> {
  const form = new FormData()
  form.append('file', file)
  await request(`/knowledge-bases/${knowledgeBaseId}/documents/${documentId}/versions`, {
    method: 'POST',
    body: form,
  })
}

export async function restoreDocumentVersion(
  knowledgeBaseId: string,
  documentId: string,
  versionId: string,
): Promise<void> {
  await request(`/knowledge-bases/${knowledgeBaseId}/documents/${documentId}/versions/${versionId}/restore`, {
    method: 'POST',
  })
}

export function getDocumentSubgraph(knowledgeBaseId: string, documentId: string): Promise<DocumentSubgraph> {
  return request(`/knowledge-bases/${knowledgeBaseId}/documents/${documentId}/subgraph`)
}

export function listFolders(knowledgeBaseId: string): Promise<Folder[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/folders`)
}

export function listTags(knowledgeBaseId: string): Promise<Tag[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/tags`)
}

export function listIndexJobs(knowledgeBaseId: string): Promise<IndexJob[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/index-jobs`)
}

export async function startIndexSync(knowledgeBaseId: string): Promise<void> {
  await request(`/knowledge-bases/${knowledgeBaseId}/index-jobs/sync`, { method: 'POST' })
}

export function cancelIndexJob(knowledgeBaseId: string, jobId: string): Promise<IndexJob> {
  return request(`/knowledge-bases/${knowledgeBaseId}/index-jobs/${jobId}/cancel`, { method: 'POST' })
}

export function retryIndexJob(knowledgeBaseId: string, jobId: string): Promise<IndexJob> {
  return request(`/knowledge-bases/${knowledgeBaseId}/index-jobs/${jobId}/retry`, { method: 'POST' })
}

export function getGraphStats(knowledgeBaseId: string): Promise<GraphStats> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/stats`)
}

export function getEntitySubgraph(
  knowledgeBaseId: string,
  entityIds: string[],
  offset = 0,
  limit = 100,
): Promise<EntitySubgraph> {
  const params = new URLSearchParams({ offset: String(offset), limit: String(limit), hops: '2' })
  entityIds.forEach((id) => params.append('entity_ids', id))
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/subgraph?${params}`)
}

export function listGraphCorrections(knowledgeBaseId: string): Promise<GraphCorrection[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/corrections`)
}

export function createGraphCorrection(
  knowledgeBaseId: string,
  payload: { action: GraphCorrection['action']; target_id?: string | null; payload?: Record<string, any>; reason: string },
): Promise<GraphCorrection> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/corrections`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function revertGraphCorrection(knowledgeBaseId: string, correctionId: string): Promise<GraphCorrection> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/corrections/${correctionId}/revert`, { method: 'POST' })
}

export function reconcileGraphCorrections(
  knowledgeBaseId: string,
): Promise<{ checked: number; active: number; conflicts: number }> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/corrections/reconcile`, { method: 'POST' })
}

export function debugRetrieval(
  knowledgeBaseId: string,
  query: string,
  retrieval: { strategy?: import('../types').RetrievalStrategy; channels: string[]; scope: { document_ids: string[]; tag_ids: string[]; folder_id: string | null; metadata: Record<string, string> } },
): Promise<RetrievalDebugResult> {
  return request(`/knowledge-bases/${knowledgeBaseId}/retrieval/debug`, {
    method: 'POST',
    body: JSON.stringify({ query, retrieval }),
  })
}

export function searchGraphEntities(knowledgeBaseId: string, query: string): Promise<GraphEntity[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/entities?query=${encodeURIComponent(query)}`)
}

export function getReadiness(): Promise<Readiness> {
  return request('/ready')
}

export function getCodeGraphStats(knowledgeBaseId: string): Promise<CodeGraphStats> {
  return request(`/knowledge-bases/${knowledgeBaseId}/code-graph/stats`)
}

export function listCodeSnapshots(knowledgeBaseId: string): Promise<CodeSnapshot[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/code-graph/snapshots`)
}

export function requestCodeIndex(knowledgeBaseId: string, dataSourceId: string): Promise<IndexJob> {
  return request(`/knowledge-bases/${knowledgeBaseId}/code-graph/index`, {
    method: 'POST', body: JSON.stringify({ data_source_id: dataSourceId }),
  })
}

export function searchCodeSymbols(knowledgeBaseId: string, query: string): Promise<CodeSymbol[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/code-graph/symbols?query=${encodeURIComponent(query)}`)
}

export function getCodeSubgraph(knowledgeBaseId: string, symbolIds: string[]): Promise<CodeSubgraph> {
  const params = new URLSearchParams()
  symbolIds.forEach((id) => params.append('symbol_ids', id))
  return request(`/knowledge-bases/${knowledgeBaseId}/code-graph/subgraph?${params}`)
}

export function listGraphSchemas(knowledgeBaseId: string): Promise<GraphSchema[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/schemas`)
}

export function createGraphSchema(
  knowledgeBaseId: string,
  payload: { entity_types: string[]; relation_types: string[]; normalization_rules?: Record<string, unknown>; extraction_prompt?: string | null },
): Promise<GraphSchema> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/schemas`, {
    method: 'POST', body: JSON.stringify(payload),
  })
}

export function activateGraphSchema(knowledgeBaseId: string, schemaId: string): Promise<GraphSchema> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/schemas/${schemaId}/activate`, { method: 'POST' })
}

export function updateGraphSchema(
  knowledgeBaseId: string,
  schemaId: string,
  payload: { entity_types: string[]; relation_types: string[]; normalization_rules?: Record<string, unknown>; extraction_prompt?: string | null },
): Promise<GraphSchema> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/schemas/${schemaId}`, {
    method: 'PUT', body: JSON.stringify(payload),
  })
}

export function getGraphAutomation(knowledgeBaseId: string): Promise<import('../types').GraphAutomationSettings> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/automation`)
}

export function updateGraphAutomation(knowledgeBaseId: string, autoBuildEnabled: boolean): Promise<import('../types').GraphAutomationSettings> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/automation`, {
    method: 'PUT', body: JSON.stringify({ auto_build_enabled: autoBuildEnabled }),
  })
}

export function requestGraphBuild(knowledgeBaseId: string, mode: 'pending' | 'retry_failed' | 'full'): Promise<IndexJob> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/build`, {
    method: 'POST', body: JSON.stringify({ mode, rebuild_entity_vectors: true, rebuild_communities: true }),
  })
}

export function getGraphQuality(knowledgeBaseId: string): Promise<GraphQuality | null> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/quality`)
}

export function assessGraphQuality(knowledgeBaseId: string): Promise<GraphQuality> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/quality`, { method: 'POST' })
}

export function getEntityVectorState(knowledgeBaseId: string): Promise<EntityVectorState> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/entity-vector`)
}

export function rebuildEntityVectors(knowledgeBaseId: string): Promise<IndexJob> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/entity-vector/rebuild`, { method: 'POST' })
}

export function listGraphReviews(knowledgeBaseId: string): Promise<GraphReviewItem[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/reviews`)
}

export function resolveGraphReview(knowledgeBaseId: string, reviewId: string, decision: 'accepted' | 'rejected'): Promise<GraphReviewItem> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/reviews/${reviewId}/resolve`, {
    method: 'POST', body: JSON.stringify({ decision }),
  })
}

export function listEntityMerges(knowledgeBaseId: string): Promise<EntityMerge[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/merges`)
}

export function scanEntityMergeCandidates(knowledgeBaseId: string): Promise<{ scanned: number; candidates: number; threshold: number }> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/merge-candidates/scan`, { method: 'POST' })
}

export function createEntityMerge(knowledgeBaseId: string, canonicalEntityId: string, mergedEntityId: string, reason: string): Promise<EntityMerge> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/merges`, {
    method: 'POST', body: JSON.stringify({ canonical_entity_id: canonicalEntityId, merged_entity_id: mergedEntityId, reason }),
  })
}

export function revertEntityMerge(knowledgeBaseId: string, mergeId: string): Promise<EntityMerge> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/merges/${mergeId}/revert`, { method: 'POST' })
}

export function listRelationMerges(knowledgeBaseId: string): Promise<import('../types').GraphCorrection[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/relation-merges`)
}

export function scanRelationMergeCandidates(knowledgeBaseId: string): Promise<{ scanned: number; candidates: number }> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/relation-merge-candidates/scan`, { method: 'POST' })
}

export function createRelationMerge(knowledgeBaseId: string, canonicalRelationId: string, mergedRelationId: string, reason: string): Promise<import('../types').GraphCorrection> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/relation-merges`, {
    method: 'POST', body: JSON.stringify({ canonical_relation_id: canonicalRelationId, merged_relation_id: mergedRelationId, reason }),
  })
}

export function revertRelationMerge(knowledgeBaseId: string, mergeId: string): Promise<import('../types').GraphCorrection> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/relation-merges/${mergeId}/revert`, { method: 'POST' })
}

export function listGraphCommunities(knowledgeBaseId: string): Promise<GraphCommunity[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/communities`)
}

export function rebuildGraphCommunities(knowledgeBaseId: string): Promise<{ communities: number; entities: number }> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/communities/rebuild`, { method: 'POST' })
}

export interface StreamHandlers {
  onMessageId?: (id: string) => void
  onRoute?: (route: RouteDecision) => void
  onDelta: (chunk: string) => void
  onSources?: (sources: { source: string; preview: string }[]) => void
  onDone?: (status: string) => void
  onError?: (code: string, message: string) => void
}

export async function streamMessage(
  conversationId: string,
  content: string,
  handlers: StreamHandlers,
  strategy: import('../types').RetrievalStrategy = 'standard',
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${BASE}/conversations/${conversationId}/messages/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ content, retrieval: { strategy } }),
    signal,
  })
  if (!res.ok || !res.body) throw new Error(await parseError(res))

  const reader = res.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let separator: number
    while ((separator = buffer.indexOf('\n\n')) !== -1) {
      parseEvent(buffer.slice(0, separator), handlers)
      buffer = buffer.slice(separator + 2)
    }
  }
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
  let payload: any
  try {
    payload = JSON.parse(data)
  } catch {
    return
  }
  if (event === 'message') handlers.onMessageId?.(payload.message_id)
  else if (event === 'route') handlers.onRoute?.(payload.route)
  else if (event === 'delta') handlers.onDelta(payload.content ?? '')
  else if (event === 'sources') handlers.onSources?.(payload.sources ?? [])
  else if (event === 'done') handlers.onDone?.(payload.status)
  else if (event === 'error') handlers.onError?.(payload.code ?? 'UNKNOWN', payload.message ?? '未知错误')
}
