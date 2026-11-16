<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as api from '../api/client'
import type { ChunkDetail, DocumentChunk, DocumentSubgraph, DocumentVersion, Folder, KnowledgeDocument, Tag } from '../types'
import { documentAccept, documentUploadError, pageLabel, supportedDocumentFormats } from '../documentPresentation'
import { usePermissions } from '../composables/usePermissions'
import OrganizationManager from './OrganizationManager.vue'
import { documentStages, stageState, stageName, statusName } from '../jobPresentation'
import { navigate, navigationState } from '../composables/useNavigation'
const { canEdit } = usePermissions()

const props = defineProps<{ knowledgeBaseId: string }>()
const documents = ref<KnowledgeDocument[]>([])
const tags = ref<Tag[]>([])
const folders = ref<Folder[]>([])
const folderFilter = ref('')
const tagFilter = ref('')
const showOrganization = ref(false)
const selectedFolder = ref('')
const selectedTags = ref<string[]>([])
const batchFolder = ref('')
const batchTags = ref<string[]>([])
let detailRequest = 0
let detailChunkId = ''
let loadRequest = 0
const selectedIds = ref<string[]>([])
const current = ref<KnowledgeDocument | null>(null)
const chunks = ref<DocumentChunk[]>([])
const chunkOffset = ref(0)
const chunkPageSize = 50
const chunkPage = ref(1)
const linkedChunk = ref<ChunkDetail | null>(null)
const versions = ref<DocumentVersion[]>([])
const subgraph = ref<DocumentSubgraph | null>(null)
const query = ref('')
const statusFilter = ref('')
const offset = ref(0)
const pageSize = ref(50)
const total = ref(0)
let filterTimer:number|undefined
let disposed=false
const detailTab = ref<'chunks' | 'versions' | 'graph'>('chunks')
const loading = ref(false)
const detailLoading = ref(false)
const error = ref('')
const notice = ref('')
const reindexing = ref(false)
const rebuildingIndex = ref(false)
const rebuildJobId = ref('')
let rebuildRequest = 0
const parseSummary = computed(() => {
  const value=current.value?.metadata_json?.parse_summary
  return value&&typeof value==='object'&&!Array.isArray(value)?value as {page_count?:number;ocr_recommended_pages?:number[];ocr_pages?:number[];table_pages?:number[];preview?:string;native_text_characters?:number}:null
})
const recommendedOcrPages = computed(() => (parseSummary.value?.ocr_recommended_pages||[]).filter(page=>Number.isInteger(page)&&page>=0))
const retryOcrPages = computed(() => [...new Set([...(parseSummary.value?.ocr_pages||[]),...recommendedOcrPages.value])].filter(page=>Number.isInteger(page)&&page>=0).sort((a,b)=>a-b))
function pageList(pages:number[]|undefined):string{return (pages||[]).filter(page=>Number.isInteger(page)&&page>=0).map(page=>page+1).join('、')}
let refreshTimer: number | null = null

const filtered = computed(() => documents.value)
const hasProcessing = computed(() => [...documents.value,...(current.value?[current.value]:[])].some(item=>['pending','processing'].includes(item.status)||['pending','running','queued'].includes(item.processing?.status||'')))

const statusNames: Record<string,string> = { pending:'等待处理', processing:'处理中', ready:'已就绪', completed:'已完成', failed:'处理失败' }
function statusLabel(status:string):string { return statusNames[status] || status }
async function load(silent=false,metadata=false): Promise<void> {
  const requested = ++loadRequest
  const kb=props.knowledgeBaseId
  if(!silent)loading.value = true
  try {
    const [page, catalog]=await Promise.all([
      api.listDocumentPage(kb,{offset:offset.value,limit:pageSize.value,query:query.value,status:statusFilter.value,folder_id:folderFilter.value,tag_id:tagFilter.value}),
      metadata?Promise.all([api.listTags(kb),api.listFolders(kb)]):null,
    ])
    if (disposed||kb!==props.knowledgeBaseId||requested !== loadRequest) return
    documents.value=page.items;total.value=page.total
    if(offset.value>0&&!page.items.length&&page.total<=offset.value){offset.value=Math.max(0,Math.floor(Math.max(0,page.total-1)/pageSize.value)*pageSize.value);await load(true);return}
    if(catalog){tags.value=catalog[0];folders.value=catalog[1]}
    error.value=''
    if(current.value){
      const previous=current.value,id=previous.id
      const fresh=page.items.find(item=>item.id===id)??await api.getDocument(kb,id)
      if(!disposed&&kb===props.knowledgeBaseId&&requested===loadRequest&&current.value?.id===id){
        current.value=fresh
        if(previous.status!==fresh.status||previous.chunk_count!==fresh.chunk_count||previous.current_version_no!==fresh.current_version_no||previous.processing?.updated_at!==fresh.processing?.updated_at)void refreshDetail(fresh,false)
      }
    }
    selectedIds.value = selectedIds.value.filter((id) => documents.value.some((item) => item.id === id))
  } catch (e) {
    if (requested === loadRequest && (e as Error).name !== 'AbortError') error.value = (e as Error).message
  } finally {
    if (requested === loadRequest) loading.value = false
  }
}

async function openDocument(document: KnowledgeDocument, chunkId = ''): Promise<void> {
  const requested=++detailRequest,kb=props.knowledgeBaseId
  current.value = document
  detailChunkId=chunkId
  chunkOffset.value=0;chunkPage.value=1;linkedChunk.value=null;detailTab.value='chunks'
  selectedFolder.value = document.folder_id || ''
  selectedTags.value = []
  if(navigationState.value.page==='documents'&&navigationState.value.kb===props.knowledgeBaseId)navigate({document:document.id,chunk:chunkId})
  chunks.value = []; versions.value = []; subgraph.value = null
  if(chunkId){
    detailLoading.value=true
    try{
      const target=await api.getChunkDetail(kb,chunkId,1)
      if(disposed||requested!==detailRequest||kb!==props.knowledgeBaseId)return
      if(target.document_id!==document.id)throw new Error('引用片段不属于此文档。')
      linkedChunk.value=target;chunkOffset.value=Math.floor(target.chunk_index/chunkPageSize)*chunkPageSize;chunkPage.value=Math.floor(chunkOffset.value/chunkPageSize)+1
    }catch(e){if(requested===detailRequest)error.value=(e as Error).message}
    if(disposed||requested!==detailRequest||kb!==props.knowledgeBaseId)return
  }
  await refreshDetail(document,true)
  if(chunkId){await nextTick();window.document.querySelector('.linked-chunk')?.scrollIntoView({block:'nearest'})}
}
async function refreshDetail(document:KnowledgeDocument,includeTags:boolean):Promise<void>{
  const requested=++detailRequest,kb=props.knowledgeBaseId
  if(includeTags)detailLoading.value = true
  try {
    const loaded = await Promise.all([
      api.listDocumentChunks(kb, document.id, {offset:chunkOffset.value,limit:chunkPageSize}),
      api.listDocumentVersions(kb, document.id),
      api.getDocumentSubgraph(kb, document.id).catch(() => null),
      includeTags?api.getDocumentTags(kb,document.id):null,
    ])
    if (disposed||kb!==props.knowledgeBaseId||current.value?.id!==document.id||requested !== detailRequest) return
    ;[chunks.value, versions.value, subgraph.value] = loaded
    if(loaded[3])selectedTags.value=loaded[3]
  } catch (e) {
    if (requested === detailRequest && (e as Error).name !== 'AbortError') error.value = (e as Error).message
  } finally {
    if (requested === detailRequest) detailLoading.value = false
  }
}

async function openLinkedDocument():Promise<void>{
  const id=navigationState.value.document,kb=props.knowledgeBaseId
  const chunkId=navigationState.value.chunk
  if(!id||(current.value?.id===id&&chunkId===detailChunkId))return
  try{const document=documents.value.find(item=>item.id===id)??await api.getDocument(kb,id);if(!disposed&&kb===props.knowledgeBaseId&&id===navigationState.value.document)await openDocument(document,chunkId)}catch(e){if(!disposed&&kb===props.knowledgeBaseId)error.value=(e as Error).message}
}
function changePage(delta:number):void{offset.value=Math.max(0,offset.value+delta*pageSize.value);void load()}
function changeChunkPage(page:number):void{
  if(!current.value||detailLoading.value)return
  const maxPage=Math.max(1,Math.ceil(current.value.chunk_count/chunkPageSize))
  chunkPage.value=Math.max(1,Math.min(maxPage,Math.trunc(page)||1));chunkOffset.value=(chunkPage.value-1)*chunkPageSize
  void refreshDetail(current.value,true)
}

async function upload(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  if (!files.length) return
  await uploadFiles(files)
  input.value = ''
}

// ---------- 带进度的批量上传 + 拖拽/粘贴 ----------
type UploadItem = { file:File; name: string; size: number; status: 'uploading' | 'done' | 'failed'|'cancelled'; error?: string }
const uploading = ref(false)
const uploadItems = ref<UploadItem[]>([])
const uploadPercent = ref(0)
const dragOver = ref(false)
let uploadController:AbortController|null=null
const retryableUploads=computed(()=>uploadItems.value.filter(item=>['failed','cancelled'].includes(item.status)))

function humanSize(size: number): string { return formatBytes(size) }

async function uploadFiles(files: File[]): Promise<void> {
  if (!files.length || uploading.value || !canEdit.value) return
  uploading.value = true
  error.value='';notice.value=''
  const kb=props.knowledgeBaseId
  uploadController=new AbortController()
  uploadPercent.value = 0
  uploadItems.value = files.map((file) => ({ file,name: file.name, size: file.size, status: documentUploadError(file)?'failed':'uploading',error:documentUploadError(file) }))
  const validItems=uploadItems.value.filter(item=>item.status==='uploading')
  try {
    if(!validItems.length){notice.value='没有可提交的文件，请查看下方原因。';return}
    const result = await api.uploadDocumentsWithProgress(props.knowledgeBaseId, validItems.map(item=>item.file), ({ loaded, total }) => {
      if(disposed||kb!==props.knowledgeBaseId)return
      uploadPercent.value = total ? Math.round((loaded / total) * 100) : 0
    },uploadController.signal)
    if(disposed||kb!==props.knowledgeBaseId)return
    const rejections=[...(result.rejected||[])]
    validItems.forEach((item,index) => {
      const rejectedIndex=rejections.findIndex(rejection=>rejection.index===index||(rejection.index===undefined&&rejection.filename===item.name))
      const error = rejectedIndex>=0?rejections.splice(rejectedIndex,1)[0]?.error:undefined
      if (error) { item.status = 'failed'; item.error = error }
      else item.status = 'done'
    })
    const acceptedCount = result.accepted ?? 0
    const rejectedCount = uploadItems.value.filter(item=>item.status==='failed').length
    notice.value = acceptedCount && rejectedCount
      ? `已提交 ${acceptedCount} 个文档，${rejectedCount} 个被拒绝（见下方列表）。系统正在自动解析并建立索引。`
      : acceptedCount?`已提交 ${acceptedCount} 个文档，系统正在自动解析、切片并建立索引。`:'没有文档被接收，请查看下方逐文件原因。'
    await load()
  } catch (e) {
    if(disposed||kb!==props.knowledgeBaseId)return
    const cancelled=(e as Error).name==='AbortError'
    if(cancelled)notice.value='已停止本次传输。已被服务器接收的文件可能继续处理，请以文档列表为准。'
    else error.value = (e as Error).message
    uploadItems.value.forEach((item) => { if (item.status === 'uploading') { item.status = cancelled?'cancelled':'failed'; item.error = cancelled?'传输已取消':(e as Error).message } })
    if(cancelled)await load(true)
  } finally {
    if(!disposed&&kb===props.knowledgeBaseId){uploading.value = false;uploadController=null}
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
  const validation=documentUploadError(file)
  if(validation){error.value=validation;input.value='';return}
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

async function categorize(batchMode = false, action: 'move'|'tag' = 'move'): Promise<void> {
  const ids = batchMode ? selectedIds.value : current.value ? [current.value.id] : []
  if (!canEdit.value || !ids.length) return
  if (batchMode && action==='tag' && !window.confirm(`替换所选 ${ids.length} 个文档的标签？未选标签会解除关联。`)) return
  try {
    if (batchMode) await api.batchDocuments(props.knowledgeBaseId, ids, action, action==='move' ? {folder_id:batchFolder.value||null} : {tag_ids:batchTags.value})
    else {
      await api.batchDocuments(props.knowledgeBaseId, ids, 'move', {folder_id:selectedFolder.value||null})
      await api.setDocumentTags(props.knowledgeBaseId, ids[0], selectedTags.value)
    }
    await load()
    if (current.value) { const updated = documents.value.find(item=>item.id===current.value?.id); if(updated) current.value = updated }
    notice.value = '文档分类已保存'
  } catch (e) { error.value = (e as Error).message }
}

async function reindexCurrent(mode:'default'|'recommended'|'force' = 'default'): Promise<void> {
  if (!current.value||reindexing.value) return
  if(mode==='recommended'&&!recommendedOcrPages.value.length)return
  if(mode==='recommended'&&retryOcrPages.value.length>40){error.value='需要保留及重新 OCR 的页数超过每文档 40 页上限，请拆分 PDF 后重试。';return}
  if(mode==='force'&&(parseSummary.value?.page_count||0)>40){error.value='本地 OCR 每文档最多处理 40 页，请拆分 PDF 后重试。';return}
  reindexing.value=true;error.value=''
  try {
    const options=mode==='recommended'?{force_ocr:false,ocr_pages:retryOcrPages.value}:mode==='force'?{force_ocr:true,ocr_pages:null}:undefined
    await api.reindexDocument(props.knowledgeBaseId,current.value.id,options)
    notice.value=mode==='default'?'已提交重新解析和索引任务。':'已提交本地 OCR 重解析任务，可在任务中心查看进度。'
    await load()
  }
  catch(e) { error.value=(e as Error).message }
  finally{reindexing.value=false}
}

async function rebuildIndex(): Promise<void> {
  if (!canEdit.value || rebuildingIndex.value) return
  if (!window.confirm('重建当前知识库的全部文档索引？\n系统将从已保存的原始文件重新解析，在后台构建影子索引，全部成功后再切换。\n重建会消耗解析、嵌入模型和额外存储资源；可在任务中心查看进度。')) return
  const kb = props.knowledgeBaseId, request = ++rebuildRequest
  rebuildingIndex.value = true
  error.value = ''; notice.value = ''; rebuildJobId.value = ''
  try {
    const result = await api.rebuildKnowledgeBaseIndex(kb)
    if (disposed || kb !== props.knowledgeBaseId || request !== rebuildRequest) return
    rebuildJobId.value = result.job_id
  } catch (e) {
    if (!disposed && kb === props.knowledgeBaseId && request === rebuildRequest) error.value = (e as Error).message
  } finally {
    if (!disposed && kb === props.knowledgeBaseId && request === rebuildRequest) rebuildingIndex.value = false
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

watch(() => props.knowledgeBaseId, () => { uploadController?.abort();current.value = null; selectedIds.value = []; notice.value='';rebuildRequest++;rebuildingIndex.value=false;rebuildJobId.value='';offset.value=0; void load(false,true) })
watch([query,statusFilter,folderFilter,tagFilter,pageSize],()=>{offset.value=0;window.clearTimeout(filterTimer);filterTimer=window.setTimeout(()=>void load(),200)})
watch(()=>[navigationState.value.document,navigationState.value.chunk],()=>void openLinkedDocument())
onMounted(() => { void load(false,true).then(openLinkedDocument); refreshTimer=window.setInterval(()=>{if(hasProcessing.value&&!document.hidden)void load(true)},3000); window.addEventListener('paste', onPaste) })
onBeforeUnmount(()=>{disposed=true;loadRequest++;detailRequest++;uploadController?.abort();window.clearTimeout(filterTimer);if(refreshTimer)window.clearInterval(refreshTimer);window.removeEventListener('paste', onPaste)})
</script>

<template>
  <div class="admin-page">
    <div class="toolbar">
      <div class="filters">
        <input v-model="query" placeholder="按文件名搜索" />
        <select v-model="statusFilter"><option value="">全部状态</option><option value="ready">已就绪</option><option value="processing">处理中</option><option value="failed">失败</option></select>
        <select v-model="folderFilter" aria-label="文件夹筛选"><option value="">全部文件夹</option><option v-for="folder in folders" :key="folder.id" :value="folder.id">{{folder.path}}</option></select>
        <select v-model="tagFilter" aria-label="标签筛选"><option value="">全部标签</option><option v-for="tag in tags" :key="tag.id" :value="tag.id">{{tag.name}}</option></select>
      </div>
      <div v-if="canEdit" class="actions">
        <button class="secondary" @click="showOrganization=true">文件夹与标签</button>
        <button class="secondary" :disabled="rebuildingIndex" @click="rebuildIndex">{{rebuildingIndex?'正在提交全库重建…':'重建全库索引'}}</button>
        <button class="secondary" :disabled="!selectedIds.length" @click="batch('reindex')">批量重建索引</button>
        <button class="danger" :disabled="!selectedIds.length" @click="batch('delete')">批量删除</button>
        <label class="primary">批量上传<input type="file" :accept="documentAccept" multiple hidden @change="upload" /></label>
      </div>
    </div>
    <p v-if="error" class="error-banner">{{ error }}</p>
    <p v-if="notice" class="notice-banner">{{ notice }}</p>
    <p v-if="rebuildJobId" class="notice-banner rebuild-task">全库索引重建任务已提交。系统将重新解析原件，构建影子索引并在成功后切换。<button @click="navigate({page:'jobs',kb:knowledgeBaseId,query:rebuildJobId})">查看重建任务</button><small>任务 ID：{{rebuildJobId}}</small></p>
    <div v-if="canEdit&&selectedIds.length" class="classification batch-classification"><b>所选 {{selectedIds.length}} 个文档</b><label>移动到<select v-model="batchFolder"><option value="">根目录</option><option v-for="folder in folders" :key="folder.id" :value="folder.id">{{folder.path}}</option></select></label><button @click="categorize(true,'move')">批量移动</button><label>替换标签<select v-model="batchTags" multiple aria-label="批量标签"><option v-for="tag in tags" :key="tag.id" :value="tag.id">{{tag.name}}</option></select></label><button @click="categorize(true,'tag')">应用标签</button></div>

    <div
      v-if="canEdit"
      class="drop-zone"
      :class="{ over: dragOver }"
      @dragover.prevent="dragOver = true"
      @dragleave.prevent="dragOver = false"
      @drop.prevent="onDrop"
    >
      <template v-if="uploading || uploadItems.length">
        <div class="upload-progress">
          <div class="upload-header"><strong>{{uploading?'本批文件总传输进度':'本批传输结果'}} · {{ uploadItems.length }} 个文件</strong><span>{{ uploadPercent }}%</span></div>
          <div class="progress-track"><div class="progress-fill" :style="{ width: uploadPercent + '%' }"></div></div>
          <small v-if="uploading">{{uploadPercent>=100?'传输完成，正在等待服务器接收结果…':'传输进度属于整批文件，后台解析和知识抽取会继续执行。'}}</small>
          <button v-if="uploading" type="button" @click="uploadController?.abort()">取消本次传输</button>
          <button v-else-if="retryableUploads.length" type="button" @click="uploadFiles(retryableUploads.map(item=>item.file))">重传失败或取消的文件（{{retryableUploads.length}}）</button>
          <button v-if="!uploading" type="button" @click="uploadItems=[]">收起本批结果</button>
        </div>
        <ul class="upload-list">
          <li v-for="(item,index) in uploadItems" :key="index" :class="item.status">
            <span class="upload-name" :title="item.name">{{ item.name }}</span>
            <span class="upload-size">{{ humanSize(item.size) }}</span>
            <span class="upload-status">{{ item.status === 'done' ? '已提交' : item.status === 'failed' ? '失败' : item.status==='cancelled'?'已取消':'等待整批确认' }}</span>
            <span v-if="item.error" class="upload-error" :title="item.error">{{ item.error }}</span>
          </li>
        </ul>
      </template>
      <template v-else>
        <span class="drop-icon">↑</span>
        <p>拖拽文件到此处上传，或 <label class="primary inline-label">点击选择文件<input type="file" :accept="documentAccept" multiple hidden @change="upload" /></label></p>
        <small>支持 {{supportedDocumentFormats}}，可粘贴这些格式的文件；截图请先转为 PDF · 批量上传自动建立索引</small>
      </template>
    </div>

    <div class="document-layout">
      <section class="table-card">
        <div class="table-summary"><strong>{{ total }}</strong> 个匹配文档 <span v-if="tags.length">· {{ tags.length }} 个标签</span></div>
        <div v-if="loading" class="empty">正在读取文档…</div>
        <div v-else-if="!filtered.length" class="empty">{{query||statusFilter||folderFilter||tagFilter?'没有匹配的文档，请调整搜索或筛选。':'暂无文档，可从右上角批量上传。'}}</div>
        <table v-else>
          <thead><tr><th v-if="canEdit"><input type="checkbox" :checked="selectedIds.length === filtered.length && filtered.length > 0" @change="toggleAll" /></th><th>文档</th><th>状态</th><th>Chunk</th><th>版本</th><th>大小</th><th>更新时间</th></tr></thead>
          <tbody>
            <tr v-for="document in filtered" :key="document.id" :class="{ active: current?.id === document.id }" @click="openDocument(document)">
              <td v-if="canEdit" @click.stop><input v-model="selectedIds" type="checkbox" :value="document.id" /></td>
              <td><strong>{{ document.original_filename }}</strong><small>{{ document.embedding_provider }} / {{ document.embedding_model }}</small></td>
              <td><span :class="['status', document.status]">{{ statusLabel(document.status) }}</span></td>
              <td>{{ document.chunk_count }}</td><td>v{{ document.current_version_no }}</td><td>{{ formatBytes(document.file_size) }}</td><td>{{ new Date(document.updated_at).toLocaleString() }}</td>
            </tr>
          </tbody>
        </table>
        <footer class="pagination"><span>第 {{Math.floor(offset/pageSize)+1}} / {{Math.max(1,Math.ceil(total/pageSize))}} 页</span><label>每页<select v-model.number="pageSize" aria-label="每页文档数"><option :value="25">25</option><option :value="50">50</option><option :value="100">100</option></select></label><button :disabled="loading||offset===0" @click="changePage(-1)">上一页</button><button :disabled="loading||offset+pageSize>=total" @click="changePage(1)">下一页</button></footer>
      </section>

      <aside v-if="current" class="detail-card">
        <div class="detail-head"><div><strong>{{ current.original_filename }}</strong><small>{{ current.source_path }}</small></div><button @click="current = null">×</button></div>
        <div class="quick-stats"><span><b>{{ current.chunk_count }}</b>Chunks</span><span><b>{{ versions.length }}</b>版本</span><span><b>{{ subgraph?.nodes.length ?? 0 }}</b>实体</span></div>
        <div class="pipeline">
          <div v-for="stage in documentStages" :key="stage.id" :class="stageState(current.processing?.stages,stage.id)" :title="statusName(current.processing?.stages.find(item=>item.name===stage.id)?.status||'unknown')"><i></i><span>{{stage.name}}</span></div>
        </div>
        <p v-if="current.processing" class="processing-hint">{{stageName(current.processing.stage)}} · {{statusName(current.processing.status)}}<span v-if="current.processing.message">：{{current.processing.message}}</span><br v-if="current.processing.waiting_reason"/><span v-if="current.processing.waiting_reason">{{current.processing.waiting_reason}}</span></p>
        <p v-else class="processing-hint">尚无详细阶段记录；文档状态以列表为准。</p>
        <p v-if="current.error_message" class="document-error">{{current.error_message}}</p>
        <p v-else-if="current.status==='processing'" class="processing-hint">系统正在后台处理，可离开本页；完成后会自动更新。</p>
        <section v-if="parseSummary" class="parse-diagnostics"><b>解析预览与质量检查</b><p v-if="parseSummary.page_count!=null">共 {{parseSummary.page_count}} 页<span v-if="parseSummary.native_text_characters!=null"> · 原生文本 {{parseSummary.native_text_characters}} 字符</span></p><p v-if="recommendedOcrPages.length" class="ocr-recommendation">第 {{pageList(recommendedOcrPages)}} 页可能包含扫描正文，建议核对下方预览后使用 OCR。</p><p v-if="parseSummary.ocr_pages?.length">已 OCR：第 {{pageList(parseSummary.ocr_pages)}} 页</p><p v-if="parseSummary.table_pages?.length">检测到表格：第 {{pageList(parseSummary.table_pages)}} 页，请核对行列关系。</p><p class="parse-preview">{{parseSummary.preview||'尚无可显示的解析文本。'}}</p><div v-if="canEdit&&current.original_filename.toLowerCase().endsWith('.pdf')" class="ocr-actions"><button v-if="recommendedOcrPages.length" :disabled="reindexing||retryOcrPages.length>40" @click="reindexCurrent('recommended')">按建议页面 OCR 重解析</button><button :disabled="reindexing||(parseSummary.page_count||0)>40" @click="reindexCurrent('force')">全部页面强制 OCR 重解析</button><small>使用服务器本地 OCR，每文档最多处理 40 页；超过上限请拆分 PDF。进度可在任务中心查看。</small></div></section>
        <div v-if="canEdit" class="detail-actions"><button :disabled="reindexing" @click="reindexCurrent()">重建索引</button><label>上传新版本<input type="file" :accept="documentAccept" hidden @change="uploadVersion" /></label></div>
        <div v-if="canEdit" class="classification"><label>文件夹<select v-model="selectedFolder" aria-label="文档文件夹"><option value="">根目录</option><option v-for="folder in folders" :key="folder.id" :value="folder.id">{{folder.path}}</option></select></label><label>标签（可多选）<select v-model="selectedTags" multiple aria-label="文档标签"><option v-for="tag in tags" :key="tag.id" :value="tag.id">{{tag.name}}</option></select></label><button @click="categorize()">保存分类</button></div>
        <div class="tabs"><button :class="{ active: detailTab === 'chunks' }" @click="detailTab = 'chunks'">Chunk</button><button :class="{ active: detailTab === 'versions' }" @click="detailTab = 'versions'">版本历史</button><button :class="{ active: detailTab === 'graph' }" @click="detailTab = 'graph'">文档子图</button></div>
        <div v-if="detailLoading" class="empty">加载详情…</div>
        <div v-else-if="detailTab === 'chunks'" class="detail-list"><article v-if="linkedChunk" class="linked-chunk"><header>引用定位 · #{{linkedChunk.chunk_index}} <span>{{pageLabel(linkedChunk.page)}}</span></header><p>{{linkedChunk.content}}</p></article><div class="chunk-pagination"><span>共 {{current.chunk_count}} 个片段 · 当前 {{chunks.length?chunkOffset+1:0}}–{{chunkOffset+chunks.length}}</span><button :disabled="!chunkOffset" @click="changeChunkPage(chunkPage-1)">上一页片段</button><label>第<input v-model.number="chunkPage" type="number" min="1" :max="Math.max(1,Math.ceil(current.chunk_count/chunkPageSize))" aria-label="片段页码" @keydown.enter="changeChunkPage(chunkPage)" />页</label><button @click="changeChunkPage(chunkPage)">跳转</button><button :disabled="chunkOffset+chunkPageSize>=current.chunk_count" @click="changeChunkPage(chunkPage+1)">下一页片段</button></div><article v-for="chunk in chunks" :key="chunk.id" :class="{referenced:chunk.id===linkedChunk?.chunk_id}"><header>#{{ chunk.chunk_index }} <span>{{pageLabel(chunk.page)}} · {{ chunk.token_count }} tokens · 图谱 {{ chunk.graph_status }}</span></header><p>{{ chunk.content }}</p></article><p v-if="!chunks.length">当前页暂无片段。</p></div>
        <div v-else-if="detailTab === 'versions'" class="detail-list"><article v-for="version in versions" :key="version.id"><header>版本 {{ version.version_no }} <span>{{ version.status }}</span></header><p>{{ formatBytes(version.file_size) }} · {{ new Date(version.created_at).toLocaleString() }}</p><button v-if="canEdit&&version.version_no !== current.current_version_no" class="restore" @click="restoreVersion(version)">恢复为新版本</button></article></div>
        <div v-else class="detail-list"><article><header>{{ subgraph?.nodes.length ?? 0 }} 个实体 · {{ subgraph?.edges.length ?? 0 }} 条关系</header><p v-if="!subgraph?.edges.length">该文档暂未生成有证据支持的关系。</p><p v-for="edge in subgraph?.edges" :key="edge.id">{{ subgraph?.nodes.find(n => n.id === edge.source)?.label }} —{{ edge.predicate }}→ {{ subgraph?.nodes.find(n => n.id === edge.target)?.label }}</p></article></div>
      </aside>
    </div>
    <OrganizationManager v-if="showOrganization&&canEdit" :knowledge-base-id="knowledgeBaseId" :folders="folders" :tags="tags" @close="showOrganization=false" @changed="load(false,true)" />
  </div>
</template>

<style scoped>
.rebuild-task button{margin-left:10px;padding:5px 8px;border:1px solid #b7d8c4;border-radius:6px;color:#276749;background:white;cursor:pointer}.rebuild-task small{display:block;margin-top:6px;overflow-wrap:anywhere}
.parse-diagnostics{margin:12px 0;padding:12px;background:#f5f8fc;border:1px solid #dae4f0;border-radius:8px;font-size:11px;color:#65748a}.parse-diagnostics b{color:#344660}.parse-preview{white-space:pre-wrap;max-height:180px;overflow:auto;line-height:1.7}.ocr-recommendation{color:#9c5a12}.ocr-actions{display:flex;gap:6px;flex-wrap:wrap}.ocr-actions button{padding:6px;border:1px solid #cad7ea;background:white;border-radius:6px;color:#355b97}.ocr-actions small{display:block;width:100%}
.chunk-pagination{display:flex;align-items:center;gap:6px;flex-wrap:wrap;font-size:11px;color:#69788c;padding:10px 0}.chunk-pagination input{width:45px;padding:4px}.chunk-pagination button{padding:5px;border:1px solid #d5deed;border-radius:5px;background:white}.chunk-pagination button:disabled{opacity:.4}.linked-chunk,.referenced{background:#edf5ff!important;border:1px solid #a9caf4!important}
.pagination{display:flex;justify-content:flex-end;align-items:center;gap:10px;padding:12px;color:#69788c;font-size:11px}.pagination button,.pagination select,.upload-progress button{padding:6px 9px;border:1px solid #dce3ed;border-radius:7px;background:white;color:#425b93}.upload-progress button{margin-top:8px;margin-right:8px}.pagination button:disabled{opacity:.4}.toolbar{flex-wrap:wrap}.filters{flex-wrap:wrap}
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
.classification{display:flex;gap:8px;align-items:end;flex-wrap:wrap;padding:12px;border-bottom:1px solid #edf1f6}.classification label{display:grid;gap:4px;font-size:11px;color:#66758a}.classification select,.classification button{padding:7px;border:1px solid #dce3ed;border-radius:7px;background:#fff;font-size:11px}.classification button{color:#4561d8;cursor:pointer}.batch-classification{margin-bottom:10px;border:1px solid #dce3ed;border-radius:9px;background:#fff}.filters{flex-wrap:wrap}
</style>
