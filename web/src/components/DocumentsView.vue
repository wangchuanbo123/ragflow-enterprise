<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as api from '../api/client'
import type { DocumentChunk, DocumentGraphCoverage, DocumentSubgraph, DocumentVersion, KnowledgeDocument, Tag } from '../types'

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
const notice = ref('')
const coverage = ref<DocumentGraphCoverage | null>(null)
let refreshTimer: number | null = null

const filtered = computed(() => documents.value.filter((document) =>
  (!query.value || document.original_filename.toLowerCase().includes(query.value.toLowerCase())) &&
  (!statusFilter.value || document.status === statusFilter.value),
))
const coverageByDocument = computed(() => new Map((coverage.value?.documents || []).map(item => [item.document_id, item])))
const hasProcessing = computed(() => documents.value.some(item => ['pending','processing'].includes(item.status)) || (coverage.value?.documents || []).some(item => item.coverage_status === 'processing'))

const statusNames: Record<string,string> = { pending:'等待处理', processing:'处理中', ready:'已就绪', completed:'已完成', failed:'处理失败' }
function statusLabel(status:string):string { return statusNames[status] || status }
function stageState(document:KnowledgeDocument, stage:'parse'|'chunk'|'vector'|'schema'|'graph'):'done'|'active'|'waiting'|'failed' {
  if (document.status === 'failed') return 'failed'
  const item = coverageByDocument.value.get(document.id)
  if (stage === 'parse') return document.chunk_count > 0 || document.status === 'ready' ? 'done' : document.status === 'processing' ? 'active' : 'waiting'
  if (stage === 'chunk') return document.chunk_count > 0 ? 'done' : document.status === 'processing' ? 'active' : 'waiting'
  if (stage === 'vector') return document.status === 'ready' ? 'done' : document.chunk_count > 0 ? 'active' : 'waiting'
  if (stage === 'schema') return item?.schema_versions?.length ? 'done' : document.status === 'ready' ? 'active' : 'waiting'
  if (item && ['covered','no_facts'].includes(item.coverage_status)) return 'done'
  return item?.coverage_status === 'attention' ? 'failed' : item?.coverage_status === 'processing' ? 'active' : 'waiting'
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const [loadedDocuments, loadedTags, loadedCoverage] = await Promise.all([
      api.listDocuments(props.knowledgeBaseId),
      api.listTags(props.knowledgeBaseId),
      api.getDocumentGraphCoverage(props.knowledgeBaseId).catch(() => null),
    ])
    documents.value = loadedDocuments
    tags.value = loadedTags
    coverage.value = loadedCoverage
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
  await uploadFiles(files)
  input.value = ''
}

// ---------- 带进度的批量上传 + 拖拽/粘贴 ----------
type UploadItem = { name: string; size: number; progress: number; status: 'uploading' | 'done' | 'failed'; error?: string }
const uploading = ref(false)
const uploadItems = ref<UploadItem[]>([])
const uploadPercent = ref(0)
const dragOver = ref(false)

function humanSize(size: number): string { return formatBytes(size) }

async function uploadFiles(files: File[]): Promise<void> {
  if (!files.length) return
  uploading.value = true
  uploadPercent.value = 0
  uploadItems.value = files.map((file) => ({ name: file.name, size: file.size, progress: 0, status: 'uploading' }))
  try {
    const result = await api.uploadDocumentsWithProgress(props.knowledgeBaseId, files, ({ loaded, total }) => {
      uploadPercent.value = total ? Math.round((loaded / total) * 100) : 0
      uploadItems.value.forEach((item) => { if (item.status === 'uploading') item.progress = uploadPercent.value })
    })
    const rejectedNames = new Map((result.rejected || []).map((item) => [item.filename, item.error]))
    uploadItems.value.forEach((item) => {
      const error = rejectedNames.get(item.name)
      if (error) { item.status = 'failed'; item.error = error }
      else { item.status = 'done'; item.progress = 100 }
    })
    const acceptedCount = result.accepted ?? 0
    const rejectedCount = (result.rejected || []).length
    notice.value = acceptedCount && rejectedCount
      ? `已提交 ${acceptedCount} 个文档，${rejectedCount} 个被拒绝（见下方列表）。系统正在自动解析并建立索引。`
      : `已提交 ${acceptedCount} 个文档，系统正在自动解析、切片并建立索引。`
    await load()
    // 上传列表停留一段时间供查看，然后自动清理成功的项
    window.setTimeout(() => { uploadItems.value = uploadItems.value.filter((item) => item.status === 'failed') }, 6000)
  } catch (e) {
    error.value = (e as Error).message
    uploadItems.value.forEach((item) => { if (item.status === 'uploading') { item.status = 'failed'; item.error = (e as Error).message } })
  } finally {
    uploading.value = false
  }
}

function onDrop(event: DragEvent): void {
  dragOver.value = false
  const files = Array.from(event.dataTransfer?.files ?? [])
  if (files.length) void uploadFiles(files)
}

function onPaste(event: ClipboardEvent): void {
  const files = Array.from(event.clipboardData?.files ?? [])
  if (files.length) void uploadFiles(files)
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

watch(() => props.knowledgeBaseId, () => { current.value = null; selectedIds.value = []; notice.value=''; void load() })
onMounted(() => { void load(); refreshTimer=window.setInterval(()=>{if(hasProcessing.value)void load()},3000); window.addEventListener('paste', onPaste) })
onBeforeUnmount(()=>{if(refreshTimer)window.clearInterval(refreshTimer);window.removeEventListener('paste', onPaste)})
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
    <p v-if="notice" class="notice-banner">{{ notice }}</p>

    <div
      class="drop-zone"
      :class="{ over: dragOver }"
      @dragover.prevent="dragOver = true"
      @dragleave.prevent="dragOver = false"
      @drop.prevent="onDrop"
    >
      <template v-if="uploading || uploadItems.length">
        <div class="upload-progress">
          <div class="upload-header"><strong>正在上传 {{ uploadItems.filter(i => i.status === 'uploading').length || uploadItems.length }} 个文件</strong><span>{{ uploadPercent }}%</span></div>
          <div class="progress-track"><div class="progress-fill" :style="{ width: uploadPercent + '%' }"></div></div>
        </div>
        <ul class="upload-list">
          <li v-for="item in uploadItems" :key="item.name" :class="item.status">
            <span class="upload-name" :title="item.name">{{ item.name }}</span>
            <span class="upload-size">{{ humanSize(item.size) }}</span>
            <span class="upload-status">{{ item.status === 'done' ? '已提交' : item.status === 'failed' ? '失败' : item.progress + '%' }}</span>
            <span v-if="item.error" class="upload-error" :title="item.error">{{ item.error }}</span>
          </li>
        </ul>
      </template>
      <template v-else>
        <span class="drop-icon">↑</span>
        <p>拖拽文件到此处上传，或 <label class="primary inline-label">点击选择文件<input type="file" multiple hidden @change="upload" /></label></p>
        <small>支持直接在页面粘贴（Ctrl+V）截图与文档 · 批量上传自动解析、切片并建立索引</small>
      </template>
    </div>

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
              <td><span :class="['status', document.status]">{{ statusLabel(document.status) }}</span></td>
              <td>{{ document.chunk_count }}</td><td>v{{ document.current_version_no }}</td><td>{{ formatBytes(document.file_size) }}</td><td>{{ new Date(document.updated_at).toLocaleString() }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <aside v-if="current" class="detail-card">
        <div class="detail-head"><div><strong>{{ current.original_filename }}</strong><small>{{ current.source_path }}</small></div><button @click="current = null">×</button></div>
        <div class="quick-stats"><span><b>{{ current.chunk_count }}</b>Chunks</span><span><b>{{ versions.length }}</b>版本</span><span><b>{{ subgraph?.nodes.length ?? 0 }}</b>实体</span></div>
        <div class="pipeline">
          <div v-for="stage in [{id:'parse',name:'解析'},{id:'chunk',name:'切片'},{id:'vector',name:'向量'},{id:'schema',name:'Schema'},{id:'graph',name:'图谱'}]" :key="stage.id" :class="stageState(current, stage.id as 'parse'|'chunk'|'vector'|'schema'|'graph')"><i></i><span>{{stage.name}}</span></div>
        </div>
        <p v-if="current.error_message" class="document-error">{{current.error_message}}</p>
        <p v-else-if="current.status==='processing'" class="processing-hint">系统正在后台处理，可离开本页；完成后会自动更新。</p>
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
.admin-page { height: 100%; padding: 18px; overflow: auto; }
.drop-zone { display:flex; flex-direction:column; align-items:center; justify-content:center; gap:4px; margin-bottom:14px; padding:16px 20px; border:1.5px dashed #c9d5e8; border-radius:12px; background:#fbfcfe; color:#7d8ba0; transition:border-color .15s, background .15s; }
.drop-zone.over { border-color:#4f6ef7; background:#f0f4ff; }
.drop-zone .drop-icon { display:grid; place-items:center; width:30px; height:30px; border-radius:50%; color:#4f6ef7; background:#eef2ff; font-size:15px; }
.drop-zone p { margin:2px 0; font-size:12px; color:#5f6d84; }
.drop-zone small { font-size:10px; color:#9aa7bb; }
.inline-label { display:inline-block; margin:0 2px; padding:2px 8px; border-radius:6px; background:#4f6ef7; color:#fff; cursor:pointer; font-size:11px; }
.upload-progress { width:100%; max-width:640px; }
.upload-header { display:flex; justify-content:space-between; margin-bottom:6px; font-size:11px; color:#42506a; }
.progress-track { height:6px; border-radius:4px; background:#e6ebf4; overflow:hidden; }
.progress-fill { height:100%; border-radius:4px; background:linear-gradient(90deg,#4f6ef7,#6a83f2); transition:width .2s; }
.upload-list { width:100%; max-width:640px; margin:8px 0 0; padding:0; list-style:none; }
.upload-list li { display:flex; align-items:center; gap:10px; padding:6px 4px; border-bottom:1px solid #eef2f7; font-size:11px; }
.upload-list li.done .upload-status { color:#18794e; }
.upload-list li.failed .upload-status { color:#b42318; }
.upload-name { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:#3d4a60; }
.upload-size { color:#9aa7bb; }
.upload-status { min-width:44px; text-align:right; color:#5f7ac9; }
.upload-error { flex-basis:100%; margin-top:2px; color:#b42318; font-size:10px; }
.toolbar { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 14px; }.filters,.actions { display: flex; gap: 8px; }.toolbar input,.toolbar select { padding: 9px 11px; border: 1px solid #dce3ed; border-radius: 8px; background: white; }.toolbar button,.toolbar label { border: 1px solid #d8e0eb; border-radius: 8px; padding: 8px 12px; background: white; color: #42506a; font-size: 12px; cursor: pointer; }.toolbar .primary { background: #4f6ef7; border-color: #4f6ef7; color: white; }.toolbar .danger { color: #bd2c2c; }.toolbar button:disabled { opacity: .45; cursor: default; }.error-banner,.notice-banner { padding: 9px 12px; border-radius: 8px; font-size: 12px; }.error-banner{color:#b42318;background:#fff1f0}.notice-banner{color:#276749;background:#edf9f2}.document-layout { display: flex; gap: 14px; align-items: flex-start; }.table-card,.detail-card { background: white; border: 1px solid #e2e8f1; border-radius: 12px; box-shadow: 0 2px 8px rgba(27,39,60,.03); }.table-card { flex: 1; min-width: 0; overflow: auto; }.table-summary { padding: 14px 16px; border-bottom: 1px solid #edf1f6; color: #8290a4; font-size: 12px; }.table-summary strong { color: #243047; font-size: 16px; } table { width: 100%; border-collapse: collapse; min-width: 760px; } th { padding: 10px 12px; background: #fafbfd; color: #8491a3; font-size: 10px; text-align: left; text-transform: uppercase; } td { padding: 11px 12px; border-top: 1px solid #edf1f6; color: #526177; font-size: 12px; } tbody tr { cursor: pointer; } tbody tr:hover,tbody tr.active { background: #f5f7ff; } td strong,td small { display: block; }td strong { max-width: 230px; overflow: hidden; text-overflow: ellipsis; color: #263249; }td small { margin-top: 3px; color: #9aa5b4; }.status { padding: 3px 7px; border-radius: 10px; background: #eef2f7; }.status.ready,.status.completed { color: #18794e; background: #e9f8ef; }.status.failed { color: #b42318; background: #fff0ee; }.status.processing { color: #986800; background: #fff6d8; }.empty { padding: 40px; text-align: center; color: #98a4b4; font-size: 13px; }.detail-card { width: 390px; flex: 0 0 390px; overflow: hidden; }.detail-head { display: flex; justify-content: space-between; padding: 15px; border-bottom: 1px solid #edf1f6; }.detail-head strong,.detail-head small { display: block; }.detail-head strong { max-width: 310px; overflow: hidden; text-overflow: ellipsis; }.detail-head small { margin-top: 4px; color: #98a4b4; font-size: 10px; }.detail-head button { border: 0; background: transparent; font-size: 20px; color: #8a96a8; cursor: pointer; }.quick-stats { display: flex; padding: 12px 15px; gap: 10px; }.quick-stats span { flex: 1; color: #909bad; font-size: 10px; }.quick-stats b { display: block; color: #243047; font-size: 17px; }.pipeline{display:grid;grid-template-columns:repeat(5,1fr);gap:4px;padding:0 15px 12px}.pipeline div{position:relative;display:grid;justify-items:center;gap:4px;color:#9aa5b4;font-size:9px}.pipeline div:not(:last-child)::after{position:absolute;top:5px;left:58%;right:-45%;height:2px;background:#e4e9f1;content:''}.pipeline i{z-index:1;width:10px;height:10px;border:2px solid #d7dee9;border-radius:50%;background:#fff}.pipeline .done{color:#247651}.pipeline .done i,.pipeline .done::after{border-color:#44ad78;background:#44ad78}.pipeline .active{color:#3568df}.pipeline .active i{border-color:#4f79ef;box-shadow:0 0 0 3px #4f79ef22}.pipeline .failed{color:#b42318}.pipeline .failed i{border-color:#d74f45;background:#d74f45}.document-error,.processing-hint{margin:0 15px 12px;padding:8px;border-radius:7px;font-size:10px}.document-error{color:#b42318;background:#fff1f0}.processing-hint{color:#526a8a;background:#f3f7fd}.detail-actions { display: flex; gap: 8px; padding: 0 15px 12px; }.detail-actions button,.detail-actions label { flex: 1; padding: 7px; text-align: center; border: 1px solid #dce3ed; border-radius: 7px; background: white; font-size: 11px; cursor: pointer; }.tabs { display: flex; border-top: 1px solid #edf1f6; border-bottom: 1px solid #edf1f6; }.tabs button { flex: 1; padding: 9px; border: 0; background: white; color: #7c889a; cursor: pointer; }.tabs button.active { color: #4f6ef7; box-shadow: inset 0 -2px #4f6ef7; }.detail-list { max-height: 430px; overflow: auto; padding: 10px; }.detail-list article { padding: 10px; margin-bottom: 8px; border: 1px solid #e7ecf3; border-radius: 8px; }.detail-list header { color: #344159; font-size: 11px; font-weight: 600; }.detail-list header span { float: right; color: #97a2b2; font-weight: 400; }.detail-list p { margin: 7px 0 0; color: #6d798b; font-size: 11px; line-height: 1.5; max-height: 100px; overflow: auto; }
.detail-list button.restore { margin-top: 8px; padding: 5px 8px; border: 1px solid #d5dfff; border-radius: 6px; color: #3f5ee6; background: #f3f6ff; cursor: pointer; font-size: 10px; }
@media (max-width: 1100px) { .document-layout { display: block; }.detail-card { width: 100%; margin-top: 12px; }.toolbar { align-items: flex-start; }.filters,.actions { flex-wrap: wrap; } }
</style>
