import type {
  ApiError,
  ChunkDetail,
  Conversation,
  ConversationDetail,
  CodeGraphStats,
  CodeSnippet,
  CodeFileIssue,
  CodeSnapshot,
  CodeSnapshotDiff,
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
  PageResult,
  KnowledgeBase,
  KnowledgeBaseMember,
  KnowledgeBackup,
  BackupSettings,
  KnowledgeDocument,
  Message,
  Readiness,
  RetrievalDebugResult,
  RouteDecision,
  SearchHit,
  SourceItem,
  SourceImage,
  Tag,
  User,
} from '../types'
import { notifyUnauthorized, sessionRequest } from './session'
import type { AgentRun, AgentRunCreate, AgentTaskCatalog } from '../agents'

const BASE = '/api/v1'

export class ApiRequestError extends Error {
  constructor(message:string,readonly status:number,readonly code?:string,readonly details?:unknown){super(message);this.name='ApiRequestError'}
}

async function parseError(res: Response): Promise<ApiRequestError> {
  try {
    const body = (await res.json()) as ApiError | { detail?: string }
    if ('error' in body) return new ApiRequestError(body.error.message,res.status,body.error.code,body.error.details)
    if ('detail' in body) return new ApiRequestError(String(body.detail),res.status)
  } catch {
    // Non-JSON error response.
  }
  return new ApiRequestError(`请求失败（${res.status}）`,res.status)
}

async function request<T>(path: string, init: RequestInit = {}, acceptedStatuses: number[] = []): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const pending = sessionRequest(init.signal ?? undefined)
  try {
    let res: Response
    try {
      res = await fetch(`${BASE}${path}`, { ...init, headers, credentials: 'include', signal: pending.signal })
    } catch (error) {
      if ((error as Error).name === 'AbortError') throw error
      throw new Error('无法连接到服务，请检查服务是否已启动或稍后重试')
    }
    pending.assertCurrent()
    if (res.status === 401 && path !== '/auth/login') notifyUnauthorized()
    if (!res.ok && !acceptedStatuses.includes(res.status)) throw await parseError(res)
    if (res.status === 204) return undefined as T
    const body = await res.json() as T
    pending.assertCurrent()
    return body
  } finally {
    pending.release()
  }
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
  try { return await request<User>('/auth/me') }
  catch (error) { if ((error as Error).name === 'AbortError') return null; throw error }
}

export function listKnowledgeBases(): Promise<KnowledgeBase[]> {
  return request('/knowledge-bases')
}

export function createKnowledgeBase(payload: { name: string; slug?: string | null; description?: string | null }): Promise<KnowledgeBase> {
  return request('/knowledge-bases', { method: 'POST', body: JSON.stringify(payload) })
}

export function updateKnowledgeBase(knowledgeBaseId: string, payload: { name?: string; slug?: string; description?: string | null }): Promise<KnowledgeBase> {
  return request(`/knowledge-bases/${knowledgeBaseId}`, { method: 'PATCH', body: JSON.stringify(payload) })
}

export function archiveKnowledgeBase(knowledgeBaseId: string): Promise<void> {
  return request(`/knowledge-bases/${knowledgeBaseId}`, { method: 'DELETE' })
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

export function listBackups(knowledgeBaseId: string): Promise<KnowledgeBackup[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/backups`)
}

export function getBackupSettings(knowledgeBaseId: string): Promise<BackupSettings> {
  return request(`/knowledge-bases/${knowledgeBaseId}/backup-settings`)
}

export function createBackup(
  knowledgeBaseId: string,
  payload: { backup_type: 'knowledge_base' | 'document_graph'; document_id?: string | null; label?: string | null },
): Promise<KnowledgeBackup> {
  return request(`/knowledge-bases/${knowledgeBaseId}/backups`, {
    method: 'POST', body: JSON.stringify(payload),
  })
}

export function restoreBackup(knowledgeBaseId: string, backupId: string): Promise<KnowledgeBackup> {
  return request(`/knowledge-bases/${knowledgeBaseId}/backups/${backupId}/restore`, { method: 'POST' })
}

export function deleteBackup(knowledgeBaseId: string, backupId: string): Promise<void> {
  return request(`/knowledge-bases/${knowledgeBaseId}/backups/${backupId}`, { method: 'DELETE' })
}

export function listDataSources(knowledgeBaseId: string): Promise<DataSource[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/sources`)
}

export function listConnectorCredentials(knowledgeBaseId: string): Promise<import('../types').ConnectorCredential[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/connector-credentials`)
}

export function listAgentTaskCatalog(kb: string): Promise<AgentTaskCatalog[]> { return request(`/knowledge-bases/${kb}/agent-task-catalog`) }
export function listAgentRuns(kb: string, offset = 0, limit = 50): Promise<AgentRun[]> { return request(`/knowledge-bases/${kb}/agent-runs?offset=${offset}&limit=${limit}`) }
export function listAgentRunPage(kb: string, options: { offset?: number; limit?: number; task?: string; status?: string } = {}): Promise<import('../types').PageResult<AgentRun>> {
  const params = new URLSearchParams({ offset: String(options.offset ?? 0), limit: String(options.limit ?? 50) })
  if (options.task) params.set('task', options.task)
  if (options.status) params.set('status', options.status)
  return request(`/knowledge-bases/${kb}/agent-runs/page?${params}`)
}
export function createAgentRun(kb: string, payload: AgentRunCreate): Promise<AgentRun> { return request(`/knowledge-bases/${kb}/agent-runs`, { method:'POST',body:JSON.stringify(payload) }) }
export function getAgentRun(kb: string, id: string, signal?: AbortSignal): Promise<AgentRun> { return request(`/knowledge-bases/${kb}/agent-runs/${id}`, {signal}) }
export function getAgentRunArtifact(kb: string, id: string): Promise<AgentRun> { return request(`/knowledge-bases/${kb}/agent-runs/${id}/artifact`) }
export function cancelAgentRun(kb: string, id: string): Promise<AgentRun> { return request(`/knowledge-bases/${kb}/agent-runs/${id}/cancel`, {method:'POST'}) }
export function retryAgentRun(kb: string, id: string): Promise<AgentRun> { return request(`/knowledge-bases/${kb}/agent-runs/${id}/retry`, {method:'POST'}) }

export function listUsers(): Promise<User[]> { return request('/users') }
export function createUser(payload: { username: string; password: string; display_name?: string; role: 'admin' | 'user'; is_active: boolean }): Promise<User> {
  return request('/users', { method: 'POST', body: JSON.stringify(payload) })
}
export function updateUser(id: string, payload: Partial<Pick<User, 'display_name' | 'role' | 'is_active'>> & { password?: string }): Promise<User> {
  return request(`/users/${id}`, { method: 'PATCH', body: JSON.stringify(payload) })
}
export function changePassword(current_password: string, new_password: string): Promise<void> {
  return request('/auth/password', { method: 'POST', body: JSON.stringify({ current_password, new_password }) })
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

export function getConversation(id: string, signal?: AbortSignal): Promise<ConversationDetail> {
  return request(`/conversations/${id}`, { signal })
}

export function saveMessageFeedback(
  conversationId: string,
  messageId: string,
  rating: 'helpful' | 'unhelpful',
  comment?: string | null,
): Promise<{ message_id: string; rating: 'helpful' | 'unhelpful'; comment: string | null }> {
  return request(`/conversations/${conversationId}/messages/${messageId}/feedback`, {
    method: 'PUT', body: JSON.stringify({ rating, comment: comment || null }),
  })
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

export function listDocumentPage(kb: string, filters: {offset?:number;limit?:number;query?:string;status?:string;folder_id?:string;tag_id?:string} = {}): Promise<PageResult<KnowledgeDocument>> {
  const params = new URLSearchParams()
  Object.entries(filters).forEach(([key,value])=>{if(value!==undefined&&value!=='')params.set(key,String(value))})
  return request(`/knowledge-bases/${kb}/documents/page?${params}`)
}

export function getDocumentTagMap(knowledgeBaseId: string): Promise<Record<string, string[]>> {
  return request(`/knowledge-bases/${knowledgeBaseId}/documents/tag-map`)
}
export function getDocument(kb:string,id:string):Promise<KnowledgeDocument>{return request(`/knowledge-bases/${kb}/documents/${id}`)}
export function getDocumentTags(kb:string,id:string):Promise<string[]>{return request(`/knowledge-bases/${kb}/documents/${id}/tags`)}

export interface DocumentUploadResult { job_id: string | null; document_ids: string[]; accepted: number; rejected: Array<{ filename: string; error: string; index?: number }>; status: string }

export async function uploadDocuments(knowledgeBaseId: string, files: File[]): Promise<DocumentUploadResult> {
  const form = new FormData()
  files.forEach((file) => form.append('files', file))
  return request(`/knowledge-bases/${knowledgeBaseId}/documents/batch-upload`, {
    method: 'POST',
    body: form,
  })
}

export async function reindexDocument(knowledgeBaseId: string, documentId: string, options?: { force_ocr?: boolean; ocr_pages?: number[] | null }): Promise<void> {
  await request(`/knowledge-bases/${knowledgeBaseId}/documents/${documentId}/reindex`, { method: 'POST', ...(options?{body:JSON.stringify(options)}:{}) })
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

export function listDocumentChunks(knowledgeBaseId: string, documentId: string, options: { offset?: number; limit?: number } = {}): Promise<DocumentChunk[]> {
  const params = new URLSearchParams({ limit: String(options.limit ?? 200), offset: String(options.offset ?? 0) })
  return request(`/knowledge-bases/${knowledgeBaseId}/documents/${documentId}/chunks?${params}`)
}

export function searchKnowledgeBase(knowledgeBaseId: string, query: string, limit = 20): Promise<SearchHit[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/search?query=${encodeURIComponent(query)}&limit=${limit}`)
}

export function getChunkDetail(knowledgeBaseId: string, chunkId: string, context = 1): Promise<ChunkDetail> {
  return request(`/knowledge-bases/${knowledgeBaseId}/chunks/${chunkId}?context=${context}`)
}

export interface UploadProgress { loaded: number; total: number }

/** 带进度回调的批量上传（fetch 不支持上传进度，用 XHR）。 */
export function uploadDocumentsWithProgress(
  knowledgeBaseId: string,
  files: File[],
  onProgress?: (info: UploadProgress) => void,
  signal?: AbortSignal,
): Promise<DocumentUploadResult> {
  return new Promise((resolve, reject) => {
    if(signal?.aborted){reject(new DOMException('上传已取消','AbortError'));return}
    const form = new FormData()
    files.forEach((file) => form.append('files', file))
    const xhr = new XMLHttpRequest()
    const pending = sessionRequest()
    const abort = () => xhr.abort()
    pending.signal.addEventListener('abort', abort, { once: true })
    signal?.addEventListener('abort',abort,{once:true})
    const cleanup = () => { pending.signal.removeEventListener('abort', abort);signal?.removeEventListener('abort',abort); pending.release() }
    xhr.open('POST', `${BASE}/knowledge-bases/${knowledgeBaseId}/documents/batch-upload`)
    xhr.withCredentials = true
    xhr.responseType = 'json'
    xhr.upload.onprogress = (event) => {
      if (onProgress && event.lengthComputable) onProgress({ loaded: event.loaded, total: event.total })
    }
    xhr.onload = () => {
      try { pending.assertCurrent() } catch (error) { cleanup(); reject(error); return }
      const body = xhr.response ?? {}
      cleanup()
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body as DocumentUploadResult)
      } else {
        if (xhr.status === 401) notifyUnauthorized()
        reject(new Error((body as ApiError).error?.message || (body as { detail?: string }).detail || `请求失败（${xhr.status}）`))
      }
    }
    xhr.onerror = () => { cleanup(); reject(new Error('无法连接到服务，请检查服务是否已启动或稍后重试')) }
    xhr.onabort = () => { cleanup(); reject(new DOMException('上传已取消', 'AbortError')) }
    xhr.timeout = 300000
    xhr.ontimeout = () => { cleanup(); reject(new Error('上传超时，请重试')) }
    xhr.send(form)
  })
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

export function getDocumentSubgraph(knowledgeBaseId: string, documentId: string, limit = 200, graphKind?: import('../types').GraphKind): Promise<DocumentSubgraph> {
  const params = new URLSearchParams({ limit: String(limit) })
  if (graphKind) params.set('graph_kind', graphKind)
  return request(`/knowledge-bases/${knowledgeBaseId}/documents/${documentId}/subgraph?${params}`)
}

export function listFolders(knowledgeBaseId: string): Promise<Folder[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/folders`)
}

export function listTags(knowledgeBaseId: string): Promise<Tag[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/tags`)
}

export function createFolder(kb: string, payload: { name: string; parent_id?: string | null; description?: string }): Promise<Folder> {
  return request(`/knowledge-bases/${kb}/folders`, { method: 'POST', body: JSON.stringify(payload) })
}
export function updateFolder(kb: string, id: string, payload: { name: string; parent_id?: string | null; description?: string }): Promise<Folder> {
  return request(`/knowledge-bases/${kb}/folders/${id}`, { method: 'PATCH', body: JSON.stringify(payload) })
}
export function deleteFolder(kb: string, id: string): Promise<void> { return request(`/knowledge-bases/${kb}/folders/${id}`, { method: 'DELETE' }) }
export function createTag(kb: string, payload: { name: string; color?: string }): Promise<Tag> {
  return request(`/knowledge-bases/${kb}/tags`, { method: 'POST', body: JSON.stringify(payload) })
}
export function updateTag(kb: string, id: string, payload: { name: string; color?: string }): Promise<Tag> {
  return request(`/knowledge-bases/${kb}/tags/${id}`, { method: 'PATCH', body: JSON.stringify(payload) })
}
export function deleteTag(kb: string, id: string): Promise<void> { return request(`/knowledge-bases/${kb}/tags/${id}`, { method: 'DELETE' }) }
export function setDocumentTags(kb: string, id: string, tag_ids: string[]): Promise<string[]> {
  return request(`/knowledge-bases/${kb}/documents/${id}/tags`, { method: 'PUT', body: JSON.stringify({ tag_ids }) })
}

export function listIndexJobs(knowledgeBaseId: string): Promise<IndexJob[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/index-jobs`)
}

export function listIndexJobPage(kb:string,filters:{offset?:number;limit?:number;status?:string;query?:string}={}):Promise<PageResult<IndexJob>>{
  const params=new URLSearchParams()
  Object.entries(filters).forEach(([key,value])=>{if(value!==undefined&&value!=='')params.set(key,String(value))})
  return request(`/knowledge-bases/${kb}/index-jobs/page?${params}`)
}

export async function startIndexSync(knowledgeBaseId: string): Promise<void> {
  await request(`/knowledge-bases/${knowledgeBaseId}/index-jobs/sync`, { method: 'POST' })
}

export function rebuildKnowledgeBaseIndex(knowledgeBaseId: string): Promise<{ job_id: string; status: 'accepted' }> {
  return request(`/knowledge-bases/${knowledgeBaseId}/index-jobs/rebuild`, { method: 'POST' })
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
  hops: 1 | 2 = 1,
  graphKind?: import('../types').GraphKind,
): Promise<EntitySubgraph> {
  const params = new URLSearchParams({ offset: String(offset), limit: String(limit), hops: String(hops) })
  entityIds.forEach((id) => params.append('entity_ids', id))
  if (graphKind) params.set('graph_kind', graphKind)
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
  retrieval: { strategy?: import('../types').RetrievalStrategy; channels: string[]; scope: { knowledge_base_ids: string[]; document_ids: string[]; tag_ids: string[]; folder_id: string | null; metadata: Record<string, string> } },
): Promise<RetrievalDebugResult> {
  return request(`/knowledge-bases/${knowledgeBaseId}/retrieval/debug`, {
    method: 'POST',
    body: JSON.stringify({ query, retrieval }),
  })
}

export function searchGraphEntities(knowledgeBaseId: string, query: string, graphKind?: import('../types').GraphKind): Promise<GraphEntity[]> {
  const params = new URLSearchParams({ query })
  if (graphKind) params.set('graph_kind', graphKind)
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/entities?${params}`)
}

export function getReadiness(): Promise<Readiness> {
  return request('/ready', {}, [503])
}
export function probeModels(): Promise<import('../types').ModelProbe> {
  return request('/model-probe', { method: 'POST' }, [503])
}

export function getCodeGraphStats(knowledgeBaseId: string): Promise<CodeGraphStats> {
  return request(`/knowledge-bases/${knowledgeBaseId}/code-graph/stats`)
}

export function listCodeSnapshots(knowledgeBaseId: string): Promise<CodeSnapshot[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/code-graph/snapshots`)
}

export async function uploadCodeArchive(knowledgeBaseId: string, file: File): Promise<DataSource> {
  const form = new FormData()
  form.append('file', file)
  return request(`/knowledge-bases/${knowledgeBaseId}/code-graph/upload`, { method: 'POST', body: form })
}

export function getCodeSnapshotDiff(knowledgeBaseId: string, snapshotId: string, baseSnapshotId: string): Promise<CodeSnapshotDiff> {
  return request(`/knowledge-bases/${knowledgeBaseId}/code-graph/snapshots/${snapshotId}/diff?base_snapshot_id=${encodeURIComponent(baseSnapshotId)}`)
}

export function getCodeSnapshotIssues(knowledgeBaseId: string, snapshotId: string): Promise<CodeFileIssue[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/code-graph/snapshots/${snapshotId}/issues`)
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

export function listGraphProfiles(knowledgeBaseId: string): Promise<import('../types').GraphProfile[]> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/profiles`)
}

export function previewGraphFilter(kb:string,payload:{principles:string[];max_nodes:number;max_edges:number},signal?:AbortSignal):Promise<import('../types').GraphFilterPreview>{return request(`/knowledge-bases/${kb}/knowledge-graph/filter-preview`,{method:'POST',body:JSON.stringify(payload),signal})}
export function listGraphFilters(kb:string):Promise<import('../types').SavedGraphFilter[]>{return request(`/knowledge-bases/${kb}/knowledge-graph/filters`)}
export function saveGraphFilter(kb:string,payload:{name:string;principles:string[]}):Promise<import('../types').SavedGraphFilter>{return request(`/knowledge-bases/${kb}/knowledge-graph/filters`,{method:'POST',body:JSON.stringify(payload)})}
export function deleteGraphFilter(kb:string,id:string):Promise<void>{return request(`/knowledge-bases/${kb}/knowledge-graph/filters/${id}`,{method:'DELETE'})}
export function previewSavedGraphFilter(kb:string,id:string,payload:{max_nodes:number;max_edges:number},signal?:AbortSignal):Promise<import('../types').GraphFilterPreview>{return request(`/knowledge-bases/${kb}/knowledge-graph/filters/${id}/preview`,{method:'POST',body:JSON.stringify(payload),signal})}

export function createGraphSchema(
  knowledgeBaseId: string,
  payload: { graph_kind?: import('../types').GraphKind; entity_types: string[]; relation_types: string[]; normalization_rules?: Record<string, unknown>; extraction_prompt?: string | null; graph_profiles?: Record<string, import('../types').GraphProfile> },
): Promise<GraphSchema> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/schemas`, {
    method: 'POST', body: JSON.stringify(payload),
  })
}

export function activateGraphSchema(knowledgeBaseId: string, schemaId: string, reprocess: 'auto' | 'incremental' | 'full' = 'auto'): Promise<GraphSchema> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/schemas/${schemaId}/activate?reprocess=${reprocess}`, { method: 'POST' })
}

export function generateGraphSchema(
  knowledgeBaseId: string,
  instructions = '',
  documentIds: string[] = [],
  mode: 'new' | 'overwrite' = 'new',
  targetSchemaId: string | null = null,
  graphKind: import('../types').GraphKind = 'general',
): Promise<IndexJob> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/schemas/generate`, {
    method: 'POST', body: JSON.stringify({ instructions: instructions || null, document_ids: documentIds, mode, target_schema_id: targetSchemaId, graph_kind: graphKind }),
  })
}

export function checkGraphSchemaDrift(knowledgeBaseId: string, instructions = '', documentIds: string[] = [], graphKind: import('../types').GraphKind = 'general'): Promise<IndexJob> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/schemas/drift-check`, {
    method: 'POST', body: JSON.stringify({ instructions: instructions || null, document_ids: documentIds, graph_kind: graphKind }),
  })
}

export function getGraphSchemaImpact(knowledgeBaseId: string, schemaId: string): Promise<import('../types').GraphSchemaImpact> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/schemas/${schemaId}/impact`)
}

export function updateGraphSchema(
  knowledgeBaseId: string,
  schemaId: string,
  payload: { entity_types: string[]; relation_types: string[]; normalization_rules?: Record<string, unknown>; extraction_prompt?: string | null; graph_profiles?: Record<string, import('../types').GraphProfile> },
): Promise<GraphSchema> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/schemas/${schemaId}`, {
    method: 'PUT', body: JSON.stringify(payload),
  })
}

export function getGraphAutomation(knowledgeBaseId: string): Promise<import('../types').GraphAutomationSettings> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/automation`)
}

export function updateGraphAutomation(knowledgeBaseId: string, autoBuildEnabled: boolean, autoSchemaGenerationEnabled: boolean): Promise<import('../types').GraphAutomationSettings> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/automation`, {
    method: 'PUT', body: JSON.stringify({ auto_build_enabled: autoBuildEnabled, auto_schema_generation_enabled: autoSchemaGenerationEnabled }),
  })
}

export function requestGraphBuild(knowledgeBaseId: string, profile: 'fast' | 'standard' | 'deep', graphKind: import('../types').GraphKind = 'general'): Promise<IndexJob> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/build`, {
    method: 'POST', body: JSON.stringify({ profile, graph_kind: graphKind }),
  })
}

export function getDocumentGraphCoverage(knowledgeBaseId: string): Promise<import('../types').DocumentGraphCoverage> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/coverage`)
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

export function getRelationVectorState(knowledgeBaseId: string): Promise<import('../types').RelationVectorState> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/relation-vector`)
}

export function rebuildRelationVectors(knowledgeBaseId: string): Promise<IndexJob> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/relation-vector/rebuild`, { method: 'POST' })
}

export function getGraphCommunityState(knowledgeBaseId: string): Promise<import('../types').GraphCommunityState> {
  return request(`/knowledge-bases/${knowledgeBaseId}/knowledge-graph/community-status`)
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
  onAnswer?: (content: string) => void
  onSources?: (sources: SourceItem[], images: SourceImage[], codeSnippets: CodeSnippet[]) => void
  onDone?: (status: string) => void
  onError?: (code: string, message: string) => void
}

export async function streamMessage(
  conversationId: string,
  content: string,
  handlers: StreamHandlers,
  retrieval: {
    strategy?: import('../types').RetrievalStrategy
    scope?: Partial<import('../types').RetrievalScope>
  } = { strategy: 'standard' },
  signal?: AbortSignal,
): Promise<void> {
  const pending = sessionRequest(signal)
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
  let finished = false
  const guardedHandlers: StreamHandlers = {
    ...handlers,
    onDone: status => { finished = true; handlers.onDone?.(status) },
    onError: (code, message) => { finished = true; handlers.onError?.(code, message) },
  }
  try {
  const res = await fetch(`${BASE}/conversations/${conversationId}/messages/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ content, retrieval }),
    signal: pending.signal,
  })
  pending.assertCurrent()
  if (res.status === 401) notifyUnauthorized()
  if (!res.ok || !res.body) throw await parseError(res)

  reader = res.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  while (true) {
    const { value, done } = await reader.read()
    pending.assertCurrent()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let separator: number
    while ((separator = buffer.indexOf('\n\n')) !== -1) {
      parseEvent(buffer.slice(0, separator), guardedHandlers)
      buffer = buffer.slice(separator + 2)
    }
  }
  buffer += decoder.decode()
  if (buffer.trim()) parseEvent(buffer, guardedHandlers)
  if (!finished) throw new Error('回答连接提前中断，请重试')
  } finally {
    if (reader) { try { await reader.cancel() } catch { /* Network already closed. */ } reader.releaseLock() }
    pending.release()
  }
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
  else if (event === 'answer' && typeof payload.content === 'string') handlers.onAnswer?.(payload.content)
  else if (event === 'sources') handlers.onSources?.(
    payload.sources ?? [], payload.images ?? [], payload.code_snippets ?? [],
  )
  else if (event === 'done') handlers.onDone?.(payload.status)
  else if (event === 'error') handlers.onError?.(payload.code ?? 'UNKNOWN', payload.message ?? '未知错误')
}
