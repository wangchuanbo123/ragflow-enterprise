export interface User {
  id: string
  username: string
  display_name: string
  role: string
  is_active: boolean
}

export interface KnowledgeBase {
  id: string
  name: string
  slug: string
  description: string | null
  is_default: boolean
  is_active: boolean
}

export interface KnowledgeBaseMember {
  id: string
  user_id: string
  username: string
  display_name: string
  role: string
  created_at: string
}

export interface DataSource {
  id: string
  knowledge_base_id: string
  source_type: 'upload' | 'local_directory' | 'git' | 's3'
  name: string
  connection_ref: string | null
  config: Record<string, unknown> | null
  status: string
  health_status: string | null
  health_detail: string | null
  last_checked_at: string | null
  last_sync_status: string | null
  last_sync_detail: string | null
  last_sync_job_id: string | null
  last_synced_at: string | null
  sync_interval_minutes: number | null
  next_sync_at: string | null
  deletion_policy: 'ignore' | 'delete'
  sync_cursor_json: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

export interface DataSourceSyncResult {
  source_id: string
  job_id: string
  status: string
  accepted: number
  skipped: number
  rejected: Array<{ path?: string; filename?: string; error: string }>
}

export interface KnowledgeBackup {
  id: string
  knowledge_base_id: string
  backup_type: 'knowledge_base' | 'document_graph'
  scope_key: string
  document_id: string | null
  version_no: number
  label: string | null
  file_size: number
  status: string
  trigger: 'manual' | 'monthly' | 'pre_restore'
  manifest_json: { table_counts?: Record<string, number>; object_count?: number } | null
  created_at: string
  restored_at: string | null
}

export interface BackupSettings {
  monthly_enabled: boolean
  interval_days: number
  retention_versions: number
}

export interface Conversation {
  id: string
  knowledge_base_id: string
  title: string
  created_at: string
  updated_at: string
  last_message_at: string | null
}

export interface RetrievalScope {
  knowledge_base_ids: string[]
  document_ids: string[]
  tag_ids: string[]
  folder_id: string | null
  metadata: Record<string, string>
}

export type RetrievalStrategy = 'fast' | 'standard' | 'deep'

export interface RouteDecision {
  method: string
  selected_channels: Array<'text' | 'graph' | 'code'>
  requested_channels: Array<'text' | 'graph' | 'code'>
  unavailable_channels: Array<'text' | 'graph' | 'code'>
  confidence: number
  reasons: string[]
  scope: RetrievalScope
  scope_match_count?: number | null
  channel_results?: Record<string, { status: string; count: number; elapsed_ms: number }>
  fused_count?: number
  strategy: RetrievalStrategy
  rewrite?: { applied?: boolean; requested?: boolean; reason?: string }
  query_embedding?: { status?: string; shared?: boolean; dimensions?: number; elapsed_ms?: number }
  knowledge_bases?: Array<{ id: string; name: string; status?: string }>
}

export interface Message {
  id: string
  knowledge_base_id?: string
  conversation_id?: string
  role: string
  content: string
  status: string
  sources?: SourceItem[]
  images?: SourceImage[]
  code_snippets?: CodeSnippet[]
  route?: RouteDecision | null
  error_message?: string | null
  feedback?: 'helpful' | 'unhelpful' | null
  created_at: string
}

export interface SourceImage {
  marker: string
  url: string
  caption: string
  mime_type: string
  page: number | string | null
  width: number | null
  height: number | null
  filename: string
  knowledge_base_id?: string
  knowledge_base_name?: string
  document_id?: string
  chunk_id?: string
  citation_id?: string
}

export interface CodeSnippet {
  content: string
  language: string
  path: string
  start_line: number | null
  end_line: number | null
  line_scope: 'file' | 'chunk'
  revision: string
  origin: 'document' | 'repository' | 'generated'
  is_verbatim: boolean
  symbol_id?: string
  qualified_name?: string
  chunk_index?: number | null
  knowledge_base_id?: string
  knowledge_base_name?: string
  document_id?: string
  chunk_id?: string
  citation_id?: string
}

export interface SourceItem {
  source: string
  preview: string
  citation_id?: string
  document_id?: string
  chunk_id?: string
  page?: number | string | null
  section?: string | null
  retrieval_channels?: string[]
  graph_facts?: string[]
  fusion_score?: number
  relevance_score?: number | null
  knowledge_base_id?: string
  knowledge_base_name?: string
  images?: SourceImage[]
  code_snippets?: CodeSnippet[]
  contains_code?: boolean
}

export interface SearchHit {
  chunk_id: string
  document_id: string
  document_name: string
  chunk_index: number
  page: number | null
  section: string | null
  score: number
  snippet: string
  content: string
  images: SourceImage[]
}

export interface ChunkNeighbor {
  chunk_id: string
  chunk_index: number
  content: string
  page: number | null
  section: string | null
}

export interface ChunkDetail {
  chunk_id: string
  document_id: string
  document_name: string
  chunk_index: number
  content: string
  page: number | null
  section: string | null
  images: SourceImage[]
  previous: ChunkNeighbor[]
  next: ChunkNeighbor[]
}

export interface ConversationDetail extends Conversation {
  messages: Message[]
}

export interface KnowledgeDocument {
  id: string
  knowledge_base_id: string
  source_path: string
  folder_id: string | null
  original_filename: string
  file_hash: string
  file_size: number
  status: string
  chunk_count: number
  current_version_no: number
  metadata_json: Record<string, unknown> | null
  embedding_provider: string
  embedding_model: string
  error_message: string | null
  created_at: string
  updated_at: string
  indexed_at: string | null
}

export interface DocumentChunk {
  id: string
  chunk_index: number
  content: string
  token_count: number
  page: number | null
  section: string | null
  metadata_json?: Record<string, unknown> | null
  graph_status: string
  graph_schema_version: number
}

export interface DocumentVersion {
  id: string
  version_no: number
  object_key: string
  file_hash: string
  file_size: number
  mime_type: string | null
  status: string
  created_at: string
}

export interface Folder {
  id: string
  parent_id: string | null
  name: string
  path: string
}

export interface Tag {
  id: string
  name: string
  color: string | null
}

export interface IndexJob {
  id: string
  job_type: string
  status: string
  total_items: number
  processed_items: number
  succeeded_items: number
  failed_items: number
  error_message: string | null
  attempt_count: number
  max_attempts: number
  cancel_requested: boolean
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface GraphStats {
  entities: number
  aliases: number
  mentions: number
  relations: number
  evidence: number
  active_corrections: number
  conflict_corrections: number
}

export interface GraphEntity {
  id: string
  entity_type: string
  canonical_name: string
  description: string | null
  aliases: string[]
}

export interface SubgraphNode {
  id: string
  label: string
  entity_type: string
  description: string | null
  mention_count: number
}

export interface SubgraphEdge {
  id: string
  source: string
  target: string
  predicate: string
  confidence: number
  evidence: Array<{ chunk_id: string; quote: string; confidence: number }>
  manual?: boolean
  correction_id?: string | null
}

export interface DocumentSubgraph {
  document_id: string
  graph_kind?: GraphKind | null
  nodes: SubgraphNode[]
  edges: SubgraphEdge[]
}

export interface GraphCorrection {
  id: string
  knowledge_base_id: string
  action: 'add_entity' | 'add_relation' | 'hide_relation' | 'update_relation' | 'update_entity' | 'merge_relation'
  target_type: string
  target_id: string | null
  payload: Record<string, any>
  reason: string
  status: string
  conflict_reason: string | null
  created_by: string | null
  reverted_by: string | null
  created_at: string
  reverted_at: string | null
  checked_at: string | null
}

export interface EntitySubgraph {
  seed_entity_ids: string[]
  graph_kind?: GraphKind | null
  nodes: SubgraphNode[]
  edges: SubgraphEdge[]
  offset: number
  next_offset: number | null
}

export interface RetrievalDebugResult {
  original_query: string
  retrieval_queries: string[]
  route: RouteDecision
  sources: SourceItem[]
  context_preview: string
}

export interface Readiness {
  status: string
  checked_at: string
  components: Record<string, { status: string; detail?: string; [key: string]: unknown }>
}

export interface CodeGraphStats {
  active_snapshot_id: string | null
  revision?: string | null
  files: number
  symbols: number
  relations: number
  resolved_relations: number
}

export interface CodeSnapshot {
  id: string
  revision: string
  parser_version: string
  status: string
  language_stats_json: Record<string, number> | null
  error_message: string | null
  created_at: string
  activated_at: string | null
}

export interface CodeSnapshotDiff {
  base_snapshot_id: string
  target_snapshot_id: string
  added_files: string[]
  modified_files: string[]
  deleted_files: string[]
  unchanged_files: number
}

export interface CodeFileIssue {
  path: string
  language: string
  status: string
  errors: string[]
}

export interface CodeSymbol {
  id: string
  snapshot_id: string
  file_id: string
  path: string
  language: string
  kind: string
  name: string
  qualified_name: string
  signature: string | null
  start_line: number
  end_line: number
  source_excerpt: string | null
  metadata: Record<string, unknown>
}

export interface CodeRelation {
  id: string
  source: string
  source_name: string
  target: string | null
  target_name: string
  relation_type: string
  confidence: number
  resolution_status: string
  path: string
  start_line: number
  end_line: number
}

export interface CodeSubgraph { nodes: CodeSymbol[]; edges: CodeRelation[] }

export interface GraphSchema {
  id: string
  graph_kind: GraphKind
  version: number
  status: string
  entity_types_json: string[]
  relation_types_json: string[]
  graph_profiles_json: Record<string, GraphProfile> | null
  normalization_rules_json: Record<string, unknown> | null
  extraction_prompt: string | null
  generation_method: 'manual' | 'document_llm' | string
  source_document_ids_json: string[] | null
  generation_summary_json: Record<string, any> | null
  created_at: string
}

export type GraphKind = 'general' | 'concept' | 'functional' | 'configuration' | 'standard'

export interface GraphProfileEntityType {
  name: string
  label: string
  color: string
}

export interface GraphProfileRelationType {
  name: string
  label: string
  source_types: string[]
  target_types: string[]
}

export interface GraphProfile {
  key: GraphKind
  label: string
  short_label: string
  purpose: string
  positioning: string
  recommended_layout: 'cose' | 'concentric' | 'breadthfirst'
  color: string
  document_hints: string[]
  extraction_rules: string[]
  entity_types: GraphProfileEntityType[]
  relation_types: GraphProfileRelationType[]
  entity_count: number
  relation_count: number
  schema_entity_coverage: number
  schema_relation_coverage: number
}

export interface GraphSchemaImpact {
  change_kind: string
  affected_documents: number
  affected_chunks: number
  estimated_llm_requests: number
  estimated_minutes: number
  removed_entity_types: string[]
  removed_relation_types: string[]
}

export interface GraphAutomationSettings {
  auto_build_enabled: boolean
  auto_schema_generation_enabled: boolean
}

export interface GraphQuality {
  id: string
  graph_version: string
  status: string
  metrics_json: Record<string, any>
  gate_results_json: Record<string, { passed: boolean; minimum?: number; maximum?: number }>
  created_at: string
}

export interface GraphReviewItem {
  id: string
  item_type: string
  target_id: string | null
  payload_json: Record<string, any>
  reason: string
  confidence: number
  status: string
  created_at: string
}

export interface EntityMerge {
  id: string
  canonical_entity_id: string
  merged_entity_id: string
  reason: string
  status: string
  created_at: string
}

export interface GraphCommunity {
  id: string
  parent_id: string | null
  level: number
  title: string
  summary: string | null
  evidence_json: string[] | null
  members: Array<{ id: string; name: string; entity_type: string }>
}

export interface EntityVectorState {
  index_name: string | null
  embedding_fingerprint: string
  graph_fingerprint: string
  entity_count: number
  status: string
  updated_at: string | null
}

export interface RelationVectorState {
  index_name: string | null
  embedding_fingerprint: string
  graph_fingerprint: string
  relation_count: number
  status: string
  updated_at: string | null
}

export interface GraphCommunityState {
  status: 'empty' | 'stale' | 'ready' | string
  communities: number
  entities: number
  built_at: string | null
  updated_at: string | null
}

export interface DocumentGraphCoverageItem {
  document_id: string
  filename: string
  document_status: string
  coverage_status: 'covered' | 'no_facts' | 'processing' | 'attention' | 'no_chunks' | string
  total_chunks: number
  completed_chunks: number
  failed_chunks: number
  pending_chunks: number
  fact_chunks: number
  entity_mentions: number
  unique_entities: number
  relation_evidence: number
  unique_relations: number
  processing_rate: number
  fact_coverage_rate: number
  schema_versions: number[]
  errors: string[]
}

export interface DocumentGraphCoverage {
  summary: {
    documents: number
    chunks: number
    processed_chunks: number
    fact_chunks: number
    processing_rate: number
    fact_coverage_rate: number
  }
  documents: DocumentGraphCoverageItem[]
}

export interface ApiError {
  error: { code: string; message: string }
}
