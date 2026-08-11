<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import * as api from '../api/client'
import type { DocumentChunk, DocumentSubgraph, DocumentVersion, KnowledgeDocument, Tag } from '../types'

const props = defineProps<{ knowledgeBaseId: string }>()
const documents = ref<KnowledgeDocument[]>([])
const tags = ref<Tag[]>([])
const selectedIds = ref<string[]>([])
const current = ref<KnowledgeDocument | null>(null)
const chunks = ref<DocumentChunk[]>([])
const versions = ref<DocumentVersion[]>([])
const subgraph = ref<DocumentSubgraph | null>(null)
const query = ref('')
const statusFilter = ref('')
const detailTab = ref<'chunks' | 'versions' | 'graph'>('chunks')
const loading = ref(false)
const detailLoading = ref(false)
const error = ref('')

const filtered = computed(() => documents.value.filter((document) =>
  (!query.value || document.original_filename.toLowerCase().includes(query.value.toLowerCase())) &&
  (!statusFilter.value || document.status === statusFilter.value),
))

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    ;[documents.value, tags.value] = await Promise.all([
      api.listDocuments(props.knowledgeBaseId),
      api.listTags(props.knowledgeBaseId),
    ])
    selectedIds.value = selectedIds.value.filter((id) => documents.value.some((item) => item.id === id))
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

async function openDocument(document: KnowledgeDocument): Promise<void> {
  current.value = document
  detailLoading.value = true
  try {
    ;[chunks.value, versions.value, subgraph.value] = await Promise.all([
      api.listDocumentChunks(props.knowledgeBaseId, document.id),
      api.listDocumentVersions(props.knowledgeBaseId, document.id),
      api.getDocumentSubgraph(props.knowledgeBaseId, document.id),
    ])
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    detailLoading.value = false
  }
}

async function upload(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  if (!files.length) return
  try {
    await api.uploadDocuments(props.knowledgeBaseId, files)
    input.value = ''
    await load()
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function uploadVersion(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file || !current.value) return
  try {
    await api.uploadDocumentVersion(props.knowledgeBaseId, current.value.id, file)
    input.value = ''
    await load()
    await openDocument(current.value)
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function restoreVersion(version: DocumentVersion): Promise<void> {
  if (!current.value || !window.confirm(`将版本 ${version.version_no} 的内容恢复为一个新版本？`)) return
  try {
    const documentId = current.value.id
    await api.restoreDocumentVersion(props.knowledgeBaseId, documentId, version.id)
    await load()
    const refreshed = documents.value.find((item) => item.id === documentId)
    if (refreshed) await openDocument(refreshed)
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function batch(action: 'delete' | 'reindex'): Promise<void> {
  if (!selectedIds.value.length) return
  if (action === 'delete' && !window.confirm(`确认删除所选 ${selectedIds.value.length} 个文档？`)) return
  try {
    await api.batchDocuments(props.knowledgeBaseId, selectedIds.value, action)
    selectedIds.value = []
    current.value = null
    await load()
  } catch (e) {
    error.value = (e as Error).message
  }
}

function toggleAll(event: Event): void {
  selectedIds.value = (event.target as HTMLInputElement).checked ? filtered.value.map((item) => item.id) : []
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}

watch(() => props.knowledgeBaseId, () => { current.value = null; selectedIds.value = []; void load() })
onMounted(load)
</script>

<template>
  <div class="admin-page">
    <div class="toolbar">
      <div class="filters">
        <input v-model="query" placeholder="按文件名搜索" />
        <select v-model="statusFilter"><option value="">全部状态</option><option value="ready">已就绪</option><option value="processing">处理中</option><option value="failed">失败</option></select>
      </div>
      <div class="actions">
        <button class="secondary" :disabled="!selectedIds.length" @click="batch('reindex')">批量重建索引</button>
        <button class="danger" :disabled="!selectedIds.length" @click="batch('delete')">批量删除</button>
        <label class="primary">批量上传<input type="file" multiple hidden @change="upload" /></label>
      </div>
    </div>
    <p v-if="error" class="error-banner">{{ error }}</p>

    <div class="document-layout">
      <section class="table-card">
        <div class="table-summary"><strong>{{ filtered.length }}</strong> 个文档 <span v-if="tags.length">· {{ tags.length }} 个标签</span></div>
        <div v-if="loading" class="empty">正在读取文档…</div>
        <div v-else-if="!filtered.length" class="empty">暂无文档，可从右上角批量上传。</div>
        <table v-else>
          <thead><tr><th><input type="checkbox" :checked="selectedIds.length === filtered.length && filtered.length > 0" @change="toggleAll" /></th><th>文档</th><th>状态</th><th>Chunk</th><th>版本</th><th>大小</th><th>更新时间</th></tr></thead>
          <tbody>
            <tr v-for="document in filtered" :key="document.id" :class="{ active: current?.id === document.id }" @click="openDocument(document)">
              <td @click.stop><input v-model="selectedIds" type="checkbox" :value="document.id" /></td>
              <td><strong>{{ document.original_filename }}</strong><small>{{ document.embedding_provider }} / {{ document.embedding_model }}</small></td>
              <td><span :class="['status', document.status]">{{ document.status }}</span></td>
              <td>{{ document.chunk_count }}</td><td>v{{ document.current_version_no }}</td><td>{{ formatBytes(document.file_size) }}</td><td>{{ new Date(document.updated_at).toLocaleString() }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <aside v-if="current" class="detail-card">
        <div class="detail-head"><div><strong>{{ current.original_filename }}</strong><small>{{ current.source_path }}</small></div><button @click="current = null">×</button></div>
        <div class="quick-stats"><span><b>{{ current.chunk_count }}</b>Chunks</span><span><b>{{ versions.length }}</b>版本</span><span><b>{{ subgraph?.nodes.length ?? 0 }}</b>实体</span></div>
        <div class="detail-actions"><button @click="api.reindexDocument(knowledgeBaseId, current.id).then(load)">重建索引</button><label>上传新版本<input type="file" hidden @change="uploadVersion" /></label></div>
        <div class="tabs"><button :class="{ active: detailTab === 'chunks' }" @click="detailTab = 'chunks'">Chunk</button><button :class="{ active: detailTab === 'versions' }" @click="detailTab = 'versions'">版本历史</button><button :class="{ active: detailTab === 'graph' }" @click="detailTab = 'graph'">文档子图</button></div>
        <div v-if="detailLoading" class="empty">加载详情…</div>
        <div v-else-if="detailTab === 'chunks'" class="detail-list"><article v-for="chunk in chunks" :key="chunk.id"><header>#{{ chunk.chunk_index }} <span>{{ chunk.token_count }} tokens · 图谱 {{ chunk.graph_status }}</span></header><p>{{ chunk.content }}</p></article></div>
        <div v-else-if="detailTab === 'versions'" class="detail-list"><article v-for="version in versions" :key="version.id"><header>版本 {{ version.version_no }} <span>{{ version.status }}</span></header><p>{{ formatBytes(version.file_size) }} · {{ new Date(version.created_at).toLocaleString() }}</p><button v-if="version.version_no !== current.current_version_no" class="restore" @click="restoreVersion(version)">恢复为新版本</button></article></div>
        <div v-else class="detail-list"><article><header>{{ subgraph?.nodes.length ?? 0 }} 个实体 · {{ subgraph?.edges.length ?? 0 }} 条关系</header><p v-if="!subgraph?.edges.length">该文档暂未生成有证据支持的关系。</p><p v-for="edge in subgraph?.edges" :key="edge.id">{{ subgraph?.nodes.find(n => n.id === edge.source)?.label }} —{{ edge.predicate }}→ {{ subgraph?.nodes.find(n => n.id === edge.target)?.label }}</p></article></div>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.admin-page { height: 100%; padding: 18px; overflow: auto; }.toolbar { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 14px; }.filters,.actions { display: flex; gap: 8px; }.toolbar input,.toolbar select { padding: 9px 11px; border: 1px solid #dce3ed; border-radius: 8px; background: white; }.toolbar button,.toolbar label { border: 1px solid #d8e0eb; border-radius: 8px; padding: 8px 12px; background: white; color: #42506a; font-size: 12px; cursor: pointer; }.toolbar .primary { background: #4f6ef7; border-color: #4f6ef7; color: white; }.toolbar .danger { color: #bd2c2c; }.toolbar button:disabled { opacity: .45; cursor: default; }.error-banner { color: #b42318; background: #fff1f0; padding: 9px 12px; border-radius: 8px; font-size: 12px; }.document-layout { display: flex; gap: 14px; align-items: flex-start; }.table-card,.detail-card { background: white; border: 1px solid #e2e8f1; border-radius: 12px; box-shadow: 0 2px 8px rgba(27,39,60,.03); }.table-card { flex: 1; min-width: 0; overflow: auto; }.table-summary { padding: 14px 16px; border-bottom: 1px solid #edf1f6; color: #8290a4; font-size: 12px; }.table-summary strong { color: #243047; font-size: 16px; } table { width: 100%; border-collapse: collapse; min-width: 760px; } th { padding: 10px 12px; background: #fafbfd; color: #8491a3; font-size: 10px; text-align: left; text-transform: uppercase; } td { padding: 11px 12px; border-top: 1px solid #edf1f6; color: #526177; font-size: 12px; } tbody tr { cursor: pointer; } tbody tr:hover,tbody tr.active { background: #f5f7ff; } td strong,td small { display: block; }td strong { max-width: 230px; overflow: hidden; text-overflow: ellipsis; color: #263249; }td small { margin-top: 3px; color: #9aa5b4; }.status { padding: 3px 7px; border-radius: 10px; background: #eef2f7; }.status.ready,.status.completed { color: #18794e; background: #e9f8ef; }.status.failed { color: #b42318; background: #fff0ee; }.status.processing { color: #986800; background: #fff6d8; }.empty { padding: 40px; text-align: center; color: #98a4b4; font-size: 13px; }.detail-card { width: 390px; flex: 0 0 390px; overflow: hidden; }.detail-head { display: flex; justify-content: space-between; padding: 15px; border-bottom: 1px solid #edf1f6; }.detail-head strong,.detail-head small { display: block; }.detail-head strong { max-width: 310px; overflow: hidden; text-overflow: ellipsis; }.detail-head small { margin-top: 4px; color: #98a4b4; font-size: 10px; }.detail-head button { border: 0; background: transparent; font-size: 20px; color: #8a96a8; cursor: pointer; }.quick-stats { display: flex; padding: 12px 15px; gap: 10px; }.quick-stats span { flex: 1; color: #909bad; font-size: 10px; }.quick-stats b { display: block; color: #243047; font-size: 17px; }.detail-actions { display: flex; gap: 8px; padding: 0 15px 12px; }.detail-actions button,.detail-actions label { flex: 1; padding: 7px; text-align: center; border: 1px solid #dce3ed; border-radius: 7px; background: white; font-size: 11px; cursor: pointer; }.tabs { display: flex; border-top: 1px solid #edf1f6; border-bottom: 1px solid #edf1f6; }.tabs button { flex: 1; padding: 9px; border: 0; background: white; color: #7c889a; cursor: pointer; }.tabs button.active { color: #4f6ef7; box-shadow: inset 0 -2px #4f6ef7; }.detail-list { max-height: 430px; overflow: auto; padding: 10px; }.detail-list article { padding: 10px; margin-bottom: 8px; border: 1px solid #e7ecf3; border-radius: 8px; }.detail-list header { color: #344159; font-size: 11px; font-weight: 600; }.detail-list header span { float: right; color: #97a2b2; font-weight: 400; }.detail-list p { margin: 7px 0 0; color: #6d798b; font-size: 11px; line-height: 1.5; max-height: 100px; overflow: auto; }
.detail-list button.restore { margin-top: 8px; padding: 5px 8px; border: 1px solid #d5dfff; border-radius: 6px; color: #3f5ee6; background: #f3f6ff; cursor: pointer; font-size: 10px; }
@media (max-width: 1100px) { .document-layout { display: block; }.detail-card { width: 100%; margin-top: 12px; }.toolbar { align-items: flex-start; }.filters,.actions { flex-wrap: wrap; } }
</style>
