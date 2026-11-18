<script setup lang="ts">
import { computed, onActivated, onBeforeUnmount, onDeactivated, reactive, ref, watch } from 'vue'
import * as api from '../api/client'
import { usePermissions } from '../composables/usePermissions'
import type { CodeFileIssue, CodeGraphStats, CodeSnapshot, CodeSnapshotDiff, CodeVectorState, ConnectorCredential, DataSource, IndexJob } from '../types'

const props = defineProps<{ knowledgeBaseId: string }>()
const { canEdit } = usePermissions()
const stats = ref<CodeGraphStats | null>(null)
const snapshots = ref<CodeSnapshot[]>([])
const vector = ref<CodeVectorState | null>(null)
const sources = ref<DataSource[]>([])
const credentials = ref<ConnectorCredential[]>([])
const jobs = ref<IndexJob[]>([])
const sourceId = ref('')
const sourceHealth = ref<DataSource | null>(null)
const issueSnapshotId = ref('')
const targetSnapshotId = ref('')
const baseSnapshotId = ref('')
const fileIssues = ref<CodeFileIssue[]>([])
const issuesLoaded = ref(false)
const issuesLoading = ref(false)
const issuesError = ref('')
const snapshotDiff = ref<CodeSnapshotDiff | null>(null)
const comparing = ref(false)
const loading = ref(false)
const busy = ref(false)
const message = ref('')
const error = ref('')
const sourceError = ref('')
const sourceForm = reactive({ source_type: 'git' as 'git' | 'local_directory', name: '', location: '', branch: '', subdirectory: '', connection_ref: '' })
const statusNames: Record<string, string> = { empty: '尚未建立', pending: '等待中', running: '构建中', building: '构建中', completed: '已完成', partial: '部分完成', failed: '失败', cancelled: '已取消', active: '当前版本', superseded: '历史版本', ready: '已就绪', stale: '待更新', skipped: '已跳过', unsupported: '暂不支持' }
const stateName = (status?: string) => statusNames[status || 'empty'] || status || '尚未建立'
const isOpen = (job: IndexJob) => ['pending', 'running', 'queued', 'retry'].includes(job.status)
const codeSources = computed(() => sources.value.filter(source => ['git', 'local_directory'].includes(source.source_type)))
const relevantJobs = computed(() => jobs.value.filter(job => ['code_index', 'code_vector_rebuild'].includes(job.job_type)))
const codeJob = computed(() => relevantJobs.value.find(job => job.job_type === 'code_index' && isOpen(job)) || relevantJobs.value.find(job => job.job_type === 'code_index'))
const vectorJob = computed(() => relevantJobs.value.find(job => job.job_type === 'code_vector_rebuild' && isOpen(job)) || relevantJobs.value.find(job => job.job_type === 'code_vector_rebuild'))
const targetSnapshot = computed(() => snapshots.value.find(snapshot => snapshot.id === targetSnapshotId.value))
const baseOptions = computed(() => snapshots.value.filter(snapshot => snapshot.id !== targetSnapshotId.value && snapshot.data_source_id === targetSnapshot.value?.data_source_id))
const canCompare = computed(() => Boolean(targetSnapshot.value && baseOptions.value.some(snapshot => snapshot.id === baseSnapshotId.value)))
const unresolved = computed(() => Math.max(0, (stats.value?.relations || 0) - (stats.value?.resolved_relations || 0)))
const resolutionRate = computed(() => stats.value?.relations ? `${Math.round(stats.value.resolved_relations / stats.value.relations * 100)}%` : '—')
const vectorStale = computed(() => Boolean(stats.value?.active_snapshot_id && vector.value?.snapshot_id !== stats.value.active_snapshot_id))
const issueSnapshot = computed(() => snapshots.value.find(snapshot => snapshot.id === issueSnapshotId.value))
const sourceName = (snapshot: CodeSnapshot) => sources.value.find(source => source.id === snapshot.data_source_id)?.name || (snapshot.data_source_id ? '已移除的代码源' : '文档代码')
const snapshotLabel = (snapshot: CodeSnapshot) => `${sourceName(snapshot)} · ${snapshot.revision} · ${new Date(snapshot.created_at).toLocaleString()}`
const progress = (job: IndexJob) => job.total_items ? Math.min(100, Math.round(job.processed_items / job.total_items * 100)) : job.status === 'completed' ? 100 : 0

let disposed = false
let viewActive = true
let epoch = 0
let coreRequest = 0
let sourceRequest = 0
let issueRequest = 0
let diffRequest = 0
let timer: number | undefined
let coreInFlight: { generation: number; promise: Promise<void> } | undefined
let completionRefreshPending = false
const active = (generation: number) => !disposed && generation === epoch
const errorText = (cause: unknown) => cause instanceof Error ? cause.message : String(cause)

function stopPolling(): void {
  if (timer !== undefined) window.clearTimeout(timer)
  timer = undefined
}

function schedulePolling(): void {
  stopPolling()
  if (!disposed && viewActive && (completionRefreshPending || relevantJobs.value.some(isOpen) || ['pending', 'building', 'running'].includes(vector.value?.status || ''))) {
    timer = window.setTimeout(() => { timer = undefined; void refreshCore() }, 2500)
  }
}

async function loadIssues(): Promise<void> {
  const generation = epoch, request = ++issueRequest, snapshotId = issueSnapshotId.value
  fileIssues.value = []; issuesLoaded.value = false; issuesError.value = ''
  issuesLoading.value = Boolean(snapshotId)
  if (!snapshotId) return
  try {
    const result = await api.getCodeSnapshotIssues(props.knowledgeBaseId, snapshotId)
    if (!active(generation) || request !== issueRequest) return
    fileIssues.value = result; issuesLoaded.value = true
  } catch (cause) {
    if (active(generation) && request === issueRequest) issuesError.value = errorText(cause)
  } finally {
    if (active(generation) && request === issueRequest) issuesLoading.value = false
  }
}

function refreshCore(): Promise<void> {
  if (coreInFlight?.generation === epoch) return coreInFlight.promise
  const promise = readCore()
  const pending = { generation: epoch, promise }
  coreInFlight = pending
  void promise.finally(() => { if (coreInFlight === pending) coreInFlight = undefined })
  return promise
}

async function readCore(): Promise<void> {
  const generation = epoch, request = ++coreRequest, kb = props.knowledgeBaseId
  stopPolling(); loading.value = true
  const finishingRefresh = completionRefreshPending
  completionRefreshPending = false
  const previousSnapshotId = stats.value?.active_snapshot_id
  const previousOpen = new Set(relevantJobs.value.filter(isOpen).map(job => job.id))
  const results = await Promise.allSettled([
    api.getCodeGraphStats(kb), api.listCodeSnapshots(kb), api.getCodeVectorState(kb), api.listIndexJobs(kb),
  ])
  if (!active(generation) || request !== coreRequest) return
  const [statsResult, snapshotsResult, vectorResult, jobsResult] = results
  relevantJobs.value.filter(isOpen).forEach(job => previousOpen.add(job.id))
  const listedJobs = jobsResult.status === 'fulfilled' ? jobsResult.value : []
  const listedIds = new Set(listedJobs.map(job => job.id))
  const missingJobs = relevantJobs.value.filter(job => isOpen(job) && !listedIds.has(job.id))
  // The general jobs endpoint only returns the latest 50 jobs.
  const lookups = await Promise.allSettled(missingJobs.map(job => api.getIndexJob(kb, job.id)))
  if (!active(generation) || request !== coreRequest) return
  const deletedJobIds = new Set<string>()
  const jobFailures: string[] = []
  const receivedJobs = [...listedJobs]
  lookups.forEach((result, index) => {
    if (result.status === 'fulfilled') receivedJobs.push(result.value)
    else {
      const missingJob = missingJobs[index]
      if ((result.reason as { status?: number })?.status === 404) deletedJobIds.add(missingJob.id)
      jobFailures.push(`任务 ${missingJob.id.slice(0, 8)}：${errorText(result.reason)}`)
    }
  })
  if (statsResult.status === 'fulfilled') stats.value = statsResult.value
  if (snapshotsResult.status === 'fulfilled') {
    snapshots.value = snapshotsResult.value
    const currentSnapshotId = snapshots.value.find(snapshot => snapshot.id === stats.value?.active_snapshot_id)?.id || snapshots.value[0]?.id || ''
    if (!snapshots.value.some(snapshot => snapshot.id === targetSnapshotId.value) || (targetSnapshotId.value === previousSnapshotId && previousSnapshotId !== currentSnapshotId)) targetSnapshotId.value = currentSnapshotId
    if (!snapshots.value.some(snapshot => snapshot.id === issueSnapshotId.value) || (issueSnapshotId.value === previousSnapshotId && previousSnapshotId !== currentSnapshotId)) issueSnapshotId.value = currentSnapshotId
    if (!baseOptions.value.some(snapshot => snapshot.id === baseSnapshotId.value)) baseSnapshotId.value = baseOptions.value[0]?.id || ''
  }
  if (vectorResult.status === 'fulfilled') vector.value = vectorResult.value
  if (jobsResult.status === 'fulfilled' || receivedJobs.length || deletedJobIds.size) {
    const received = new Set(receivedJobs.map(job => job.id))
    jobs.value = [...receivedJobs, ...jobs.value.filter(job => isOpen(job) && !received.has(job.id) && !deletedJobIds.has(job.id))]
    if (receivedJobs.some(job => previousOpen.has(job.id) && !isOpen(job)) || deletedJobIds.size) {
      // Other parallel reads can have completed before the worker committed its results.
      completionRefreshPending = true
      snapshotDiff.value = null
      void loadIssues()
    }
  }
  const failures = [...results.flatMap((result, index) => result.status === 'rejected' ? [`${['图谱概览', '快照历史', '代码索引', '任务状态'][index]}：${errorText(result.reason)}`] : []), ...jobFailures]
  if (finishingRefresh && failures.length) completionRefreshPending = true
  error.value = failures.join('；')
  loading.value = false
  schedulePolling()
}

async function loadSources(): Promise<void> {
  const generation = epoch, request = ++sourceRequest
  const results = await Promise.allSettled([
    api.listDataSources(props.knowledgeBaseId),
    canEdit.value ? api.listConnectorCredentials(props.knowledgeBaseId) : Promise.resolve([]),
  ])
  if (!active(generation) || request !== sourceRequest) return
  if (results[0].status === 'fulfilled') {
    sources.value = results[0].value
    if (!codeSources.value.some(source => source.id === sourceId.value)) sourceId.value = codeSources.value[0]?.id || ''
  }
  if (results[1].status === 'fulfilled') credentials.value = canEdit.value ? results[1].value : []
  sourceError.value = results.flatMap(result => result.status === 'rejected' ? [errorText(result.reason)] : []).join('；')
}

async function refresh(): Promise<void> { await Promise.all([refreshCore(), loadSources()]); if (!issuesLoading.value) await loadIssues() }

function track(job: IndexJob): void {
  jobs.value = [job, ...jobs.value.filter(item => item.id !== job.id)]
  schedulePolling()
}

async function mutate(action: (kb: string, generation: number) => Promise<void>): Promise<void> {
  if (!canEdit.value || busy.value) return
  const generation = epoch
  busy.value = true; error.value = ''; message.value = ''
  try { await action(props.knowledgeBaseId, generation) }
  catch (cause) { if (active(generation)) error.value = errorText(cause) }
  finally { if (active(generation)) busy.value = false }
}

function checkSource(): void {
  if (!sourceId.value) return
  const id = sourceId.value
  void mutate(async (kb, generation) => {
    const result = await api.checkDataSource(kb, id)
    if (!active(generation) || id !== sourceId.value) return
    sourceHealth.value = result
    message.value = result.health_status === 'healthy' ? '代码源连接正常，可以开始构建。' : result.health_detail || '代码源检查未通过'
  })
}

function indexCode(): void {
  if (!sourceId.value) return
  const id = sourceId.value
  void mutate(async (kb, generation) => {
    const job = await api.requestCodeIndex(kb, id)
    if (!active(generation)) return
    track(job); message.value = `代码构建任务已创建：${job.id.slice(0, 8)}`
  })
}

function rebuildVectors(): void {
  void mutate(async (kb, generation) => {
    const job = await api.rebuildCodeVectors(kb)
    if (!active(generation)) return
    track(job); message.value = `代码索引任务已创建：${job.id.slice(0, 8)}`
  })
}

function createSource(): void {
  if (!sourceForm.name.trim() || !sourceForm.location.trim()) return
  const form = { ...sourceForm }
  void mutate(async (kb, generation) => {
    const config: Record<string, unknown> = { location: form.location.trim(), index_code: true }
    if (form.source_type === 'git' && form.branch.trim()) config.branch = form.branch.trim()
    if (form.source_type === 'git' && form.subdirectory.trim()) config.subdirectory = form.subdirectory.trim()
    const source = await api.createDataSource(kb, { source_type: form.source_type, name: form.name.trim(), connection_ref: form.connection_ref.trim() || null, config })
    if (!active(generation)) return
    sources.value.unshift(source); sourceId.value = source.id
    message.value = '代码源已保存，可创建索引任务。'
    Object.assign(sourceForm, { name: '', location: '', branch: '', subdirectory: '', connection_ref: '' })
    if (!canEdit.value) return
    const job = await api.requestCodeIndex(kb, source.id)
    if (!active(generation)) return
    track(job); message.value = `代码源已保存，正在构建：${job.id.slice(0, 8)}`
  })
}

function uploadArchive(event: Event): void {
  const input = event.target as HTMLInputElement, file = input.files?.[0]
  if (!file) return
  void mutate(async (kb, generation) => {
    const source = await api.uploadCodeArchive(kb, file)
    if (!active(generation)) return
    sources.value.unshift(source); sourceId.value = source.id; input.value = ''
    message.value = source.last_sync_job_id ? '压缩包已接收，正在后台解压并构建代码图谱。' : '压缩包已保存，可创建索引任务。'
    await refreshCore()
    // Upload starts a worker before the task list becomes visible.
    if (active(generation) && source.last_sync_job_id && !jobs.value.some(job => job.id === source.last_sync_job_id)) {
      track({ id: source.last_sync_job_id, job_type: 'code_index', status: 'pending', total_items: 0, processed_items: 0, succeeded_items: 0, failed_items: 0 } as IndexJob)
    }
  })
}

async function compareSnapshots(): Promise<void> {
  if (!canCompare.value || comparing.value) return
  const generation = epoch, request = ++diffRequest, target = targetSnapshotId.value, base = baseSnapshotId.value
  comparing.value = true; snapshotDiff.value = null; error.value = ''
  try {
    const result = await api.getCodeSnapshotDiff(props.knowledgeBaseId, target, base)
    if (active(generation) && request === diffRequest) snapshotDiff.value = result
  } catch (cause) { if (active(generation) && request === diffRequest) error.value = errorText(cause) }
  finally { if (active(generation) && request === diffRequest) comparing.value = false }
}

watch(sourceId, () => { sourceHealth.value = null })
watch(() => sourceForm.source_type, () => { sourceForm.connection_ref = '' })
watch(issueSnapshotId, () => { void loadIssues() })
watch(targetSnapshotId, () => {
  if (!baseOptions.value.some(snapshot => snapshot.id === baseSnapshotId.value)) baseSnapshotId.value = baseOptions.value[0]?.id || ''
})
watch([targetSnapshotId, baseSnapshotId], () => { ++diffRequest; snapshotDiff.value = null; comparing.value = false }, { flush: 'sync' })
watch(() => props.knowledgeBaseId, () => {
  ++epoch; stopPolling(); ++issueRequest; ++diffRequest; completionRefreshPending = false
  stats.value = null; snapshots.value = []; vector.value = null; jobs.value = []; sources.value = []; credentials.value = []
  sourceId.value = ''; sourceHealth.value = null; issueSnapshotId.value = ''; targetSnapshotId.value = ''; baseSnapshotId.value = ''
  fileIssues.value = []; issuesLoaded.value = false; issuesLoading.value = false; issuesError.value = ''; snapshotDiff.value = null
  comparing.value = false; loading.value = false; busy.value = false; message.value = ''; error.value = ''; sourceError.value = ''
  Object.assign(sourceForm, { name: '', location: '', branch: '', subdirectory: '', connection_ref: '' })
  void refreshCore(); void loadSources()
}, { immediate: true })
watch(canEdit, allowed => { if (allowed) void loadSources(); else { credentials.value = []; sourceForm.connection_ref = '' } })
onActivated(() => { viewActive = true; void refreshCore() })
onDeactivated(() => { viewActive = false; stopPolling() })
onBeforeUnmount(() => { disposed = true; ++epoch; stopPolling() })
</script>

<template>
  <div class="code-management">
    <header class="page-heading">
      <div><h2>代码图谱治理</h2><p>管理代码源、构建质量、版本变化与语义索引。</p></div>
      <button :disabled="loading || busy" @click="refresh">刷新治理状态</button>
    </header>
    <p v-if="error" class="notice error" role="alert">{{ error }}</p>
    <p v-if="sourceError" class="notice error" role="alert">代码源读取失败：{{ sourceError }}</p>
    <p v-if="message" class="notice message" role="status">{{ message }}</p>

    <div class="metrics">
      <article><span>源码文件</span><b>{{ stats?.files ?? '—' }}</b></article>
      <article><span>代码符号</span><b>{{ stats?.symbols ?? '—' }}</b></article>
      <article><span>关系解析率</span><b>{{ resolutionRate }}</b><small>未解析 {{ unresolved }} 条关系</small></article>
      <article class="revision"><span>当前版本</span><b>{{ stats?.revision || '尚未构建' }}</b></article>
    </div>

    <section class="card vector-state">
      <header><div><h3>代码语义索引</h3><p>用于代码符号与功能检索。</p></div><button v-if="canEdit" :disabled="busy || !stats?.active_snapshot_id || Boolean(vectorJob && isOpen(vectorJob)) || Boolean(codeJob && isOpen(codeJob))" @click="rebuildVectors">重建代码索引</button></header>
      <div class="vector-summary"><span class="badge" :class="vector?.status">{{ stateName(vector?.status) }}</span><span>{{ vector?.symbol_count ?? 0 }} 个符号已建立索引</span><span v-if="vectorStale" class="attention">索引落后于当前快照，请更新代码索引。</span><span v-else-if="vector?.status === 'ready'">已与当前快照同步</span><small v-if="vector?.updated_at">更新于 {{ new Date(vector.updated_at).toLocaleString() }}</small></div>
      <div v-if="vectorJob" class="vector-progress job-progress"><header><strong>索引任务：{{ stateName(vectorJob.status) }}</strong><span>{{ progress(vectorJob) }}%</span></header><progress :value="progress(vectorJob)" max="100" /><p v-if="vectorJob.error_message" class="attention">{{ vectorJob.error_message }}</p></div>
    </section>

    <section v-if="canEdit" class="card source-tools">
      <header><div><h3>代码源与构建</h3><p>从 Git、服务器目录或 ZIP 接入代码，按内容变化增量解析。</p></div></header>
      <div class="build-toolbar"><select v-model="sourceId" aria-label="代码数据源"><option value="">选择代码源</option><option v-for="source in codeSources" :key="source.id" :value="source.id">{{ source.name }} · {{ source.source_type }}</option></select><button :disabled="busy || !sourceId" @click="checkSource">检查连接</button><button class="primary" :disabled="busy || !sourceId || Boolean(codeJob && isOpen(codeJob))" @click="indexCode">创建索引任务</button></div>
      <p v-if="!codeSources.length" class="hint">尚未接入代码源，可在下方添加仓库或上传 ZIP。</p>
      <p v-if="sourceHealth" class="hint">连接状态：{{ sourceHealth.health_status === 'healthy' ? '正常' : '需要关注' }}{{ sourceHealth.health_detail ? ` · ${sourceHealth.health_detail}` : '' }}</p>
      <details class="source-builder"><summary>添加代码源或上传 ZIP</summary>
        <div class="upload-choice"><p>直接上传代码压缩包，适合一次性分析。</p><label class="upload-button">{{ busy ? '处理中…' : '选择 ZIP 文件' }}<input type="file" accept=".zip,application/zip" hidden :disabled="busy" @change="uploadArchive" /></label></div>
        <form @submit.prevent="createSource">
          <label>获取方式<select v-model="sourceForm.source_type"><option value="git">Git 仓库</option><option value="local_directory">服务器目录</option></select></label>
          <label>显示名称<input v-model="sourceForm.name" required placeholder="例如：订单服务" /></label>
          <label class="wide">{{ sourceForm.source_type === 'git' ? '仓库地址' : '目录路径' }}<input v-model="sourceForm.location" required :placeholder="sourceForm.source_type === 'git' ? 'https://git.example.com/team/order-service.git' : '/data/repos/order-service'" /></label>
          <template v-if="sourceForm.source_type === 'git'"><label>分支（可选）<input v-model="sourceForm.branch" placeholder="默认仓库分支" /></label><label>子目录（可选）<input v-model="sourceForm.subdirectory" placeholder="例如 services/order" /></label><label class="wide">授权凭据<select v-model="sourceForm.connection_ref"><option value="">公开仓库，无需凭据</option><option v-for="credential in credentials.filter(item => item.source_type === 'git')" :key="credential.id" :value="credential.connection_ref">{{ credential.id }}</option></select></label></template>
          <p class="hint wide">{{ sourceForm.source_type === 'git' ? '系统会自动识别提交版本。' : '服务器目录需要管理员为当前知识库登记访问范围。' }}</p>
          <button class="primary wide" :disabled="busy">保存并构建代码图谱</button>
        </form>
      </details>
    </section>

    <section v-if="codeJob" class="card build-progress job-progress"><header><div><h3>最近一次构建：{{ stateName(codeJob.status) }}</h3><p>{{ codeJob.processed_items }} / {{ codeJob.total_items || '待统计' }} 个文件 · 成功 {{ codeJob.succeeded_items }} · 失败 {{ codeJob.failed_items }}</p></div><b>{{ progress(codeJob) }}%</b></header><progress :value="progress(codeJob)" max="100" /><p v-if="codeJob.error_message" class="attention">{{ codeJob.error_message }}</p></section>

    <section class="card issues-section">
      <header><div><h3>解析问题</h3><p>代码符号和调用关系随源代码重新解析；修复后重新构建。</p></div><select v-model="issueSnapshotId" aria-label="问题快照"><option value="">选择快照</option><option v-for="snapshot in snapshots" :key="snapshot.id" :value="snapshot.id">{{ snapshotLabel(snapshot) }}</option></select></header>
      <p v-if="issuesError" class="notice error" role="alert">{{ issuesError }}</p>
      <p v-if="issueSnapshot?.error_message" class="attention">{{ issueSnapshot.error_message }}</p>
      <p v-if="issuesLoading" class="empty">正在读取解析问题…</p>
      <div v-else-if="issuesLoaded && fileIssues.length" class="issues"><strong>{{ fileIssues.length }} 个文件需要关注</strong><article v-for="item in fileIssues" :key="item.path"><div><b>{{ item.path }}</b><span class="badge">{{ stateName(item.status) }}</span></div><small>{{ item.language }}</small><ul><li v-for="(issue, index) in item.errors" :key="index">{{ issue }}</li><li v-if="!item.errors.length">未完成解析</li></ul></article></div>
      <p v-else-if="issuesLoaded" class="empty">当前快照没有解析异常文件。</p>
      <p v-else-if="!issueSnapshotId" class="empty">构建代码图谱后可查看解析问题。</p>
    </section>

    <section class="card version-compare">
      <header><div><h3>版本差异</h3><p>比较同一代码源的两个快照，查看新增、修改、删除与复用文件。</p></div><button :disabled="!canCompare || comparing" @click="compareSnapshots">{{ comparing ? '正在比较…' : '比较版本' }}</button></header>
      <div class="compare-selects"><label>基准快照<select v-model="baseSnapshotId" aria-label="基准快照"><option value="">选择基准版本</option><option v-for="snapshot in baseOptions" :key="snapshot.id" :value="snapshot.id">{{ snapshotLabel(snapshot) }}</option></select></label><span>→</span><label>目标快照<select v-model="targetSnapshotId" aria-label="目标快照"><option value="">选择目标版本</option><option v-for="snapshot in snapshots" :key="snapshot.id" :value="snapshot.id">{{ snapshotLabel(snapshot) }}</option></select></label></div>
      <p v-if="targetSnapshot && !baseOptions.length" class="hint">该代码源还没有其他快照，暂时无法比较版本。</p>
      <div v-if="snapshotDiff" class="diff-grid"><article><b>{{ snapshotDiff.added_files.length }}</b><span>新增文件</span></article><article><b>{{ snapshotDiff.modified_files.length }}</b><span>修改文件</span></article><article><b>{{ snapshotDiff.deleted_files.length }}</b><span>删除文件</span></article><article><b>{{ snapshotDiff.unchanged_files }}</b><span>复用文件</span></article></div>
      <details v-if="snapshotDiff" class="changed-files"><summary>查看变更文件</summary><p><b>新增：</b>{{ snapshotDiff.added_files.join('、') || '无' }}</p><p><b>修改：</b>{{ snapshotDiff.modified_files.join('、') || '无' }}</p><p><b>删除：</b>{{ snapshotDiff.deleted_files.join('、') || '无' }}</p></details>
    </section>

    <section class="card snapshots"><header><div><h3>代码快照历史</h3><p>显示最近 {{ snapshots.length }} 个快照。</p></div></header><p v-if="!snapshots.length" class="empty">尚无代码快照。</p><article v-for="snapshot in snapshots" :key="snapshot.id"><div><span class="badge" :class="snapshot.status">{{ stateName(snapshot.status) }}</span><b>{{ snapshot.revision }}</b><small>{{ sourceName(snapshot) }} · {{ new Date(snapshot.created_at).toLocaleString() }}</small></div><small>{{ snapshot.parser_version }}<template v-if="snapshot.language_stats_json"> · 复用 {{ snapshot.language_stats_json.files_reused || 0 }} / 新解析 {{ snapshot.language_stats_json.files_parsed || 0 }} / 删除 {{ snapshot.language_stats_json.files_deleted || 0 }}</template></small></article></section>
  </div>
</template>

<style scoped>
.code-management{color:#344159}.page-heading,header{display:flex;align-items:center;justify-content:space-between;gap:14px}.page-heading{margin-bottom:16px}h2,h3,p{margin:0}h2{font-size:17px}h3{font-size:13px}header p,.page-heading p{margin-top:5px;color:#7c899e;font-size:11px}button,input,select{font:inherit}button,.upload-button{padding:8px 11px;border:1px solid #d9e2f3;border-radius:8px;background:#f6f8ff;color:#4860c8;font-size:11px;cursor:pointer}button:hover{background:#edf1ff}button:disabled{opacity:.5;cursor:not-allowed}.primary,.upload-button{color:white;background:#4f6ef7;border-color:#4f6ef7}.primary:hover{background:#435ed8}.card{margin-top:14px;padding:16px;border:1px solid #e1e7f0;border-radius:12px;background:white}.metrics{display:grid;grid-template-columns:repeat(3,1fr) 1.6fr;gap:10px}.metrics article{padding:15px;border:1px solid #e1e7f0;border-radius:11px;background:#fff}.metrics span,.metrics b,.metrics small{display:block}.metrics span{color:#8190a5;font-size:11px}.metrics b{margin-top:7px;font-size:23px}.metrics small{margin-top:5px;color:#8b704e;font-size:10px}.metrics .revision b{font-size:13px;overflow-wrap:anywhere}.vector-summary{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-top:14px;font-size:11px}.vector-summary small{color:#8b97a8}.badge{display:inline-block;padding:4px 7px;border-radius:7px;color:#68768a;background:#eef2f7;font-size:10px}.badge.ready,.badge.active{color:#18794e;background:#e9f8ef}.badge.failed{color:#b42318;background:#fff1f0}.attention{color:#a76018;font-size:11px}.build-toolbar{display:flex;gap:9px;margin-top:14px}.build-toolbar select{flex:1;min-width:0}.source-builder{margin-top:14px;padding-top:12px;border-top:1px solid #edf1f6}summary{color:#4f6ef7;font-size:11px;cursor:pointer}.upload-choice{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-top:12px;padding:12px;border-radius:8px;background:#f7f9ff}.upload-choice p{color:#74829a;font-size:11px}.source-builder form{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}label{display:grid;gap:5px;color:#74829a;font-size:11px}.wide{grid-column:1/-1}input,select{box-sizing:border-box;padding:9px;border:1px solid #dce3ed;border-radius:8px;color:#344159;background:#fff;font-size:11px;max-width:100%}.hint{margin-top:10px;color:#7c899e;font-size:11px}.job-progress progress{width:100%;height:8px;margin-top:12px;accent-color:#4f6ef7}.vector-progress{margin-top:14px;padding-top:12px;border-top:1px solid #edf1f6;font-size:11px}.job-progress .attention{margin-top:8px}.notice{padding:10px 12px;border-radius:8px;font-size:12px;margin:10px 0}.error{color:#b42318;background:#fff1f0}.message{color:#18794e;background:#ecf9f1}.issues-section header select{max-width:50%}.issues{margin-top:14px;color:#8a4c17;font-size:11px}.issues article{margin-top:9px;padding:12px;border-radius:8px;background:#fff8ed}.issues article>div{display:flex;align-items:center;justify-content:space-between;gap:8px}.issues b{overflow-wrap:anywhere}.issues small{display:block;margin-top:4px;color:#94785c}.issues ul{margin:8px 0 0;padding-left:18px;line-height:1.6}.compare-selects{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:10px;margin-top:14px}.compare-selects label{min-width:0}.compare-selects select{width:100%}.diff-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:14px}.diff-grid article{padding:12px;border:1px solid #e6ebf3;border-radius:8px;background:#fafcff}.diff-grid b,.diff-grid span{display:block}.diff-grid b{font-size:19px}.diff-grid span{margin-top:4px;color:#8794a7;font-size:10px}.changed-files{margin-top:12px;font-size:11px}.changed-files p{margin-top:8px;overflow-wrap:anywhere}.snapshots article{display:flex;justify-content:space-between;gap:12px;padding:12px 0;border-top:1px solid #edf1f6;font-size:11px}.snapshots header{margin-bottom:12px}.snapshots article>div{display:flex;align-items:center;gap:9px;flex-wrap:wrap}.snapshots b{overflow-wrap:anywhere}.snapshots small{color:#8794a7;font-size:10px}.empty{padding:22px 8px;text-align:center;color:#94a0b1;font-size:11px}@media(max-width:900px){.metrics{grid-template-columns:1fr 1fr}.snapshots article{flex-direction:column}.issues-section header{align-items:stretch;flex-direction:column}.issues-section header select{max-width:100%;width:100%}}@media(max-width:600px){.page-heading{align-items:flex-start}.build-toolbar{flex-wrap:wrap}.build-toolbar select{flex-basis:100%}.source-builder form,.compare-selects{grid-template-columns:1fr}.wide{grid-column:auto}.compare-selects>span{display:none}.diff-grid{grid-template-columns:1fr 1fr}.upload-choice{align-items:flex-start;flex-direction:column}}
</style>
