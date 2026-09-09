<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import * as api from '../api/client'
import type { CodeFileIssue, CodeGraphStats, CodeSnapshot, CodeSnapshotDiff, CodeSubgraph, CodeSymbol, DataSource, IndexJob } from '../types'

const props = defineProps<{ knowledgeBaseId: string }>()
const stats = ref<CodeGraphStats | null>(null)
const snapshots = ref<CodeSnapshot[]>([])
const sources = ref<DataSource[]>([])
const sourceId = ref('')
const query = ref('')
const symbols = ref<CodeSymbol[]>([])
const graph = ref<CodeSubgraph | null>(null)
const selected = ref<CodeSymbol | null>(null)
const loading = ref(false)
const message = ref('')
const error = ref('')
const jobs = ref<IndexJob[]>([])
const sourceHealth = ref<DataSource|null>(null)
const baseSnapshotId = ref('')
const targetSnapshotId = ref('')
const snapshotDiff = ref<CodeSnapshotDiff|null>(null)
const fileIssues = ref<CodeFileIssue[]>([])
let pollTimer:number|null=null
const codeJob = computed(()=>jobs.value.find(item=>item.job_type==='code_index')||null)
const jobProgress = computed(()=>codeJob.value?.total_items ? Math.round(codeJob.value.processed_items/codeJob.value.total_items*100) : codeJob.value?.status==='completed'?100:0)
const statusNames:Record<string,string>={pending:'等待中',running:'构建中',completed:'已完成',partial:'部分完成',failed:'失败',active:'当前版本'}
const sourceForm = reactive({
  source_type: 'git' as 'git' | 'local_directory',
  name: '',
  location: '',
  branch: '',
  subdirectory: '',
  connection_ref: '',
})

async function load(): Promise<void> {
  loading.value = true
  try {
    ;[stats.value, snapshots.value, sources.value, jobs.value] = await Promise.all([
      api.getCodeGraphStats(props.knowledgeBaseId),
      api.listCodeSnapshots(props.knowledgeBaseId),
      api.listDataSources(props.knowledgeBaseId),
      api.listIndexJobs(props.knowledgeBaseId),
    ])
    const valid = sources.value.filter((item) => ['local_directory', 'git'].includes(item.source_type))
    if (!valid.some((item) => item.id === sourceId.value)) sourceId.value = valid[0]?.id ?? ''
    if(!targetSnapshotId.value)targetSnapshotId.value=snapshots.value[0]?.id||''
    if(!baseSnapshotId.value)baseSnapshotId.value=snapshots.value[1]?.id||''
    error.value = ''
  } catch (e) { error.value = (e as Error).message }
  finally { loading.value = false }
}

async function uploadArchive(event:Event):Promise<void>{
  const input=event.target as HTMLInputElement;const file=input.files?.[0];if(!file)return
  loading.value=true;error.value=''
  try{const source=await api.uploadCodeArchive(props.knowledgeBaseId,file);sourceId.value=source.id;message.value='压缩包已安全上传，请检查代码源后开始构建。';input.value='';await load()}
  catch(e){error.value=(e as Error).message}finally{loading.value=false}
}

async function checkSource():Promise<void>{
  if(!sourceId.value)return
  loading.value=true
  try{sourceHealth.value=await api.checkDataSource(props.knowledgeBaseId,sourceId.value);message.value=sourceHealth.value.health_status==='healthy'?'代码源连接正常，可以开始构建。':sourceHealth.value.health_detail||'代码源检查未通过'}catch(e){error.value=(e as Error).message}finally{loading.value=false}
}

async function loadDiff():Promise<void>{
  if(!baseSnapshotId.value||!targetSnapshotId.value||baseSnapshotId.value===targetSnapshotId.value)return
  try{snapshotDiff.value=await api.getCodeSnapshotDiff(props.knowledgeBaseId,targetSnapshotId.value,baseSnapshotId.value);fileIssues.value=await api.getCodeSnapshotIssues(props.knowledgeBaseId,targetSnapshotId.value)}catch(e){error.value=(e as Error).message}
}

async function indexCode(): Promise<void> {
  if (!sourceId.value) return
  loading.value = true
  try {
    const job = await api.requestCodeIndex(props.knowledgeBaseId, sourceId.value)
    message.value = `代码索引任务已创建：${job.id.slice(0, 8)}`
    error.value = ''
  } catch (e) { error.value = (e as Error).message }
  finally { loading.value = false }
}

async function createAndIndexCode(): Promise<void> {
  if (!sourceForm.name.trim() || !sourceForm.location.trim()) return
  loading.value = true
  try {
    const config: Record<string, unknown> = {
      location: sourceForm.location.trim(),
      index_code: true,
    }
    if (sourceForm.source_type === 'git' && sourceForm.branch.trim()) config.branch = sourceForm.branch.trim()
    if (sourceForm.source_type === 'git' && sourceForm.subdirectory.trim()) config.subdirectory = sourceForm.subdirectory.trim()
    const source = await api.createDataSource(props.knowledgeBaseId, {
      source_type: sourceForm.source_type,
      name: sourceForm.name.trim(),
      connection_ref: sourceForm.connection_ref.trim() || null,
      config,
    })
    sourceId.value = source.id
    const job = await api.requestCodeIndex(props.knowledgeBaseId, source.id)
    message.value = `已保存代码源并开始构建，任务：${job.id.slice(0, 8)}。Git 提交版本将自动识别。`
    Object.assign(sourceForm, { name: '', location: '', branch: '', subdirectory: '', connection_ref: '' })
    await load()
  } catch (e) { error.value = (e as Error).message }
  finally { loading.value = false }
}

async function search(): Promise<void> {
  if (!query.value.trim()) return
  loading.value = true
  try {
    symbols.value = await api.searchCodeSymbols(props.knowledgeBaseId, query.value.trim())
    graph.value = null
    selected.value = null
    error.value = ''
  } catch (e) { error.value = (e as Error).message }
  finally { loading.value = false }
}

async function inspect(symbol: CodeSymbol): Promise<void> {
  selected.value = symbol
  try { graph.value = await api.getCodeSubgraph(props.knowledgeBaseId, [symbol.id]) }
  catch (e) { error.value = (e as Error).message }
}

watch(() => props.knowledgeBaseId, () => { query.value = ''; symbols.value = []; graph.value = null; snapshotDiff.value=null; void load() })
onMounted(()=>{void load();pollTimer=window.setInterval(()=>{if(codeJob.value&&['pending','running'].includes(codeJob.value.status))void load()},2500)})
onBeforeUnmount(()=>{if(pollTimer)window.clearInterval(pollTimer)})
</script>

<template>
  <div class="code-page">
    <div class="stats">
      <article><span>源码文件</span><strong>{{ stats?.files ?? 0 }}</strong></article>
      <article><span>符号</span><strong>{{ stats?.symbols ?? 0 }}</strong></article>
      <article><span>调用与依赖</span><strong>{{ stats?.relations ?? 0 }}</strong></article>
      <article><span>已解析关系</span><strong>{{ stats?.resolved_relations ?? 0 }}</strong></article>
      <article class="revision"><span>当前版本</span><strong>{{ stats?.revision || '尚未构建' }}</strong></article>
    </div>
    <p v-if="error" class="error">{{ error }}</p><p v-if="message" class="message">{{ message }}</p>

    <section class="toolbar-card">
      <div><strong>构建代码图谱</strong><small>解析临时 AST、符号索引、调用图和模块依赖图</small></div>
      <select v-model="sourceId"><option value="">选择本地目录或 Git 数据源</option><option v-for="source in sources.filter((item) => ['local_directory','git'].includes(item.source_type))" :key="source.id" :value="source.id">{{ source.name }} · {{ source.source_type }}</option></select>
      <button class="check" :disabled="!sourceId || loading" @click="checkSource">检查连接</button><button :disabled="!sourceId || loading" @click="indexCode">创建索引任务</button>
    </section>
    <section v-if="codeJob" class="build-progress"><header><div><strong>最近一次构建：{{statusNames[codeJob.status]||codeJob.status}}</strong><small>{{codeJob.processed_items}} / {{codeJob.total_items||'待统计'}} 个文件 · 成功 {{codeJob.succeeded_items}} · 失败 {{codeJob.failed_items}}</small></div><b>{{jobProgress}}%</b></header><div><i :style="{width:`${jobProgress}%`}"></i></div><p v-if="codeJob.error_message">{{codeJob.error_message}}</p></section>

    <details class="source-builder">
      <summary><div><strong>首次接入代码仓库</strong><small>不需要填写仓库 ID 或 Commit SHA，系统会自动创建内部编号并识别版本</small></div><span>添加代码源</span></summary>
      <div class="upload-choice"><div><strong>直接上传代码压缩包</strong><small>适合本机代码或一次性分析；无需仓库地址，支持 ZIP。</small></div><label>{{loading?'正在上传…':'选择 ZIP 文件'}}<input type="file" accept=".zip,application/zip" hidden :disabled="loading" @change="uploadArchive" /></label></div>
      <form @submit.prevent="createAndIndexCode">
        <label>获取方式<select v-model="sourceForm.source_type"><option value="git">Git 仓库（推荐）</option><option value="local_directory">服务所在机器的目录</option></select></label>
        <label>显示名称<input v-model="sourceForm.name" required placeholder="例如：订单服务" /></label>
        <label class="wide">{{ sourceForm.source_type === 'git' ? '仓库地址' : '目录路径' }}<input v-model="sourceForm.location" required :placeholder="sourceForm.source_type === 'git' ? 'https://git.example.com/team/order-service.git' : '例如 /data/repos/order-service（容器需挂载）'" /></label>
        <template v-if="sourceForm.source_type === 'git'"><label>分支（可选）<input v-model="sourceForm.branch" placeholder="默认仓库分支" /></label><label>子目录（可选）<input v-model="sourceForm.subdirectory" placeholder="例如 services/order" /></label></template>
        <label class="wide">凭据引用（可选）<input v-model="sourceForm.connection_ref" placeholder="env://GIT_TOKEN，不直接保存密码或 Token" /></label>
        <p class="wide">{{ sourceForm.source_type === 'git' ? '笔记本和服务器部署都可使用仓库地址，服务会自动拉取并记录 Commit SHA。' : '“本地目录”指运行服务的笔记本或服务器可访问的目录；容器部署时需先把目录挂载进容器。' }}</p>
        <button class="wide" :disabled="loading">保存并构建代码图谱</button>
      </form>
    </details>

    <div class="body-grid">
      <section class="search-card">
        <form @submit.prevent="search"><input v-model="query" placeholder="搜索类、函数、方法、签名或限定名" /><button :disabled="loading">检索</button></form>
        <div v-if="!symbols.length" class="empty">输入符号名称查看源码位置及上下游调用</div>
        <button v-for="symbol in symbols" :key="symbol.id" class="symbol" :class="{ active: selected?.id === symbol.id }" @click="inspect(symbol)">
          <span><b>{{ symbol.name }}</b><em>{{ symbol.kind }}</em></span>
          <code>{{ symbol.qualified_name }}</code><small>{{ symbol.path }}:{{ symbol.start_line }}-{{ symbol.end_line }}</small>
        </button>
      </section>

      <section class="detail-card">
        <div v-if="!selected" class="empty">选择一个符号查看源码行号引用和关系子图</div>
        <template v-else>
          <header><div><strong>{{ selected.qualified_name }}</strong><span>{{ selected.language }} · {{ selected.kind }}</span></div><small>{{ selected.path }}:{{ selected.start_line }}-{{ selected.end_line }}</small></header>
          <pre><code>{{ selected.source_excerpt || '当前解析器未保存源码摘录' }}</code></pre>
          <h3>调用与依赖关系</h3>
          <div v-if="!graph?.edges.length" class="empty relations">当前符号没有可展示的上下游关系</div>
          <article v-for="edge in graph?.edges ?? []" :key="edge.id" class="relation">
            <span :class="edge.relation_type.toLowerCase()">{{ edge.relation_type }}</span>
            <div><strong>{{ edge.source_name }}</strong><b>→</b><strong>{{ edge.target_name }}</strong><small>{{ edge.path }}:{{ edge.start_line }} · {{ edge.resolution_status }} · 解析评分 {{ Math.round(edge.confidence * 100) }} 分</small></div>
          </article>
        </template>
      </section>
    </div>

    <section class="version-compare"><header><div><strong>版本差异与解析问题</strong><small>选择两个快照，查看本次增量构建实际新增、修改和删除了什么。</small></div><button :disabled="!baseSnapshotId||!targetSnapshotId||baseSnapshotId===targetSnapshotId" @click="loadDiff">比较版本</button></header><div class="compare-selects"><select v-model="baseSnapshotId"><option value="">基准版本</option><option v-for="item in snapshots" :key="'b'+item.id" :value="item.id">{{item.revision}}</option></select><span>→</span><select v-model="targetSnapshotId"><option value="">目标版本</option><option v-for="item in snapshots" :key="'t'+item.id" :value="item.id">{{item.revision}}</option></select></div><div v-if="snapshotDiff" class="diff-grid"><article><b>{{snapshotDiff.added_files.length}}</b><span>新增文件</span></article><article><b>{{snapshotDiff.modified_files.length}}</b><span>修改文件</span></article><article><b>{{snapshotDiff.deleted_files.length}}</b><span>删除文件</span></article><article><b>{{snapshotDiff.unchanged_files}}</b><span>复用文件</span></article></div><details v-if="snapshotDiff"><summary>查看变更文件</summary><p><b>新增：</b>{{snapshotDiff.added_files.join('、')||'无'}}</p><p><b>修改：</b>{{snapshotDiff.modified_files.join('、')||'无'}}</p><p><b>删除：</b>{{snapshotDiff.deleted_files.join('、')||'无'}}</p></details><div v-if="fileIssues.length" class="issues"><strong>{{fileIssues.length}} 个文件需要关注</strong><article v-for="item in fileIssues" :key="item.path"><b>{{item.path}}</b><small>{{item.language}} · {{statusNames[item.status]||item.status}} · {{item.errors[0]||'未完成解析'}}</small></article></div></section>
    <section class="snapshots"><header><strong>代码快照历史</strong><button @click="load">刷新</button></header><article v-for="item in snapshots" :key="item.id"><span :class="item.status">{{ statusNames[item.status]||item.status }}</span><b>{{ item.revision }}</b><small>{{ item.parser_version }} · {{ new Date(item.created_at).toLocaleString() }}<template v-if="item.language_stats_json"> · 复用 {{item.language_stats_json.files_reused||0}} / 新解析 {{item.language_stats_json.files_parsed||0}} / 删除 {{item.language_stats_json.files_deleted||0}}</template></small></article></section>
  </div>
</template>

<style scoped>
.code-page { height: 100%; overflow: auto; padding: 18px; color: #344159; }.stats { display: grid; grid-template-columns: repeat(4, 1fr) 1.5fr; gap: 10px; }.stats article,.toolbar-card,.source-builder,.search-card,.detail-card,.snapshots { border: 1px solid #e1e7f0; border-radius: 11px; background: white; }.stats article { padding: 13px 15px; }.stats span,.stats strong { display:block }.stats span { color:#8b97a8;font-size:10px }.stats strong{margin-top:5px;font-size:22px}.stats .revision strong{font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.toolbar-card{display:flex;align-items:center;gap:12px;margin:12px 0;padding:13px}.toolbar-card>div{flex:1}.toolbar-card strong,.toolbar-card small{display:block}.toolbar-card small{margin-top:3px;color:#8794a7;font-size:10px}.toolbar-card select{min-width:280px}.toolbar-card select,.toolbar-card button,.search-card input,.search-card form button{padding:8px 10px;border:1px solid #dce3ed;border-radius:8px;background:white}.toolbar-card button,.search-card form button{color:white;background:#4f6ef7;border-color:#4f6ef7;cursor:pointer}.source-builder{margin:-2px 0 12px;overflow:hidden}.source-builder summary{display:flex;align-items:center;gap:12px;padding:13px;cursor:pointer}.source-builder summary div{flex:1}.source-builder summary strong,.source-builder summary small{display:block}.source-builder summary small{margin-top:3px;color:#8794a7;font-size:10px}.source-builder summary span{color:#4f6ef7;font-size:11px}.source-builder form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;padding:0 13px 14px;border-top:1px solid #edf1f6}.source-builder label{display:grid;gap:5px;padding-top:12px;color:#66758a;font-size:10px}.source-builder input,.source-builder select{box-sizing:border-box;width:100%;padding:8px 9px;border:1px solid #dce3ed;border-radius:7px}.source-builder .wide{grid-column:1/-1}.source-builder p{margin:0;padding:8px 10px;border-radius:7px;color:#68778d;background:#f5f8fc;font-size:10px}.source-builder button{padding:9px;border:0;border-radius:8px;color:white;background:#4f6ef7;cursor:pointer}.body-grid{display:grid;grid-template-columns:350px 1fr;gap:12px;min-height:480px}.search-card,.detail-card{padding:13px}.search-card form{display:flex;gap:6px;margin-bottom:10px}.search-card input{min-width:0;flex:1}.symbol{display:block;width:100%;padding:10px;margin:6px 0;border:1px solid #e5eaf1;border-radius:8px;background:white;text-align:left;cursor:pointer}.symbol.active{border-color:#8297f9;background:#f4f6ff}.symbol span{display:flex;justify-content:space-between}.symbol em{font-size:9px;color:#4f6ef7;font-style:normal}.symbol code,.symbol small{display:block;margin-top:4px;overflow:hidden;text-overflow:ellipsis;color:#7a8799;font-size:9px}.detail-card header{display:flex;justify-content:space-between}.detail-card header strong,.detail-card header span{display:block}.detail-card header span,.detail-card header small{color:#8996a8;font-size:10px}.detail-card pre{max-height:220px;overflow:auto;padding:13px;border-radius:8px;background:#101827;color:#dce5f2;font-size:11px;line-height:1.55}.detail-card h3{font-size:12px}.relation{display:flex;gap:10px;align-items:flex-start;padding:9px 0;border-top:1px solid #edf1f6}.relation>span{padding:3px 6px;border-radius:6px;background:#eef2ff;color:#4b63d9;font-size:8px}.relation div{flex:1}.relation strong,.relation small{display:block;font-size:10px}.relation b{color:#9aa5b4}.relation small{margin-top:3px;color:#8f9bad;font-size:9px}.snapshots{margin-top:12px;padding:13px}.snapshots header{display:flex;justify-content:space-between}.snapshots header button{border:0;background:transparent;color:#4f6ef7;cursor:pointer}.snapshots article{display:grid;grid-template-columns:90px 1fr 1fr;gap:10px;padding:8px 0;border-top:1px solid #edf1f6;font-size:10px}.snapshots small{color:#8d99aa}.snapshots span.active{color:#18794e}.error,.message{padding:8px;border-radius:7px;font-size:11px}.error{color:#b42318;background:#fff1f0}.message{color:#18794e;background:#ecf9f1}.empty{display:grid;place-items:center;min-height:100px;color:#99a5b5;font-size:11px}.empty.relations{min-height:70px}@media(max-width:1000px){.stats{grid-template-columns:repeat(2,1fr)}.body-grid{grid-template-columns:1fr}.toolbar-card{align-items:stretch;flex-direction:column}.toolbar-card select{min-width:0}}@media(max-width:650px){.source-builder form{grid-template-columns:1fr}.source-builder .wide{grid-column:auto}}
.build-progress,.version-compare{border:1px solid #e1e7f0;border-radius:11px;background:#fff}.toolbar-card button.check{color:#4f62bb;background:#f6f8ff}.build-progress{margin:-3px 0 12px;padding:12px}.build-progress header{display:flex;align-items:center}.build-progress header div{flex:1}.build-progress strong,.build-progress small{display:block}.build-progress small{margin-top:3px;color:#8491a4;font-size:9px}.build-progress header>b{color:#4f6ef7}.build-progress>div{height:7px;margin-top:9px;overflow:hidden;border-radius:5px;background:#edf1f6}.build-progress i{display:block;height:100%;border-radius:5px;background:linear-gradient(90deg,#4f6ef7,#43a5ee);transition:width .3s}.build-progress p{margin:7px 0 0;color:#b42318;font-size:9px}.upload-choice{display:flex;align-items:center;gap:12px;padding:12px 13px;border-top:1px solid #edf1f6;background:#f8fbff}.upload-choice>div{flex:1}.upload-choice strong,.upload-choice small{display:block}.upload-choice small{margin-top:3px;color:#8390a3;font-size:9px}.upload-choice label{padding:7px 10px;border-radius:7px;color:#fff;background:#4f6ef7;cursor:pointer;font-size:10px}.version-compare{margin-top:12px;padding:13px}.version-compare>header{display:flex;align-items:center}.version-compare header div{flex:1}.version-compare header strong,.version-compare header small{display:block}.version-compare header small{margin-top:3px;color:#8996a8;font-size:9px}.version-compare header button{padding:7px 10px;border:1px solid #d7e0ff;border-radius:7px;color:#4762df;background:#f5f7ff;cursor:pointer}.compare-selects{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:8px;margin-top:10px}.compare-selects select{padding:8px;border:1px solid #dce3ed;border-radius:7px}.diff-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin-top:10px}.diff-grid article{padding:9px;border:1px solid #e5eaf1;border-radius:8px;background:#fafcff}.diff-grid b,.diff-grid span{display:block}.diff-grid b{font-size:16px}.diff-grid span{color:#8996a8;font-size:9px}.version-compare details{margin-top:9px;color:#64738a;font-size:9px}.version-compare summary{cursor:pointer;color:#4f6ef7}.version-compare details p{overflow-wrap:anywhere}.issues{margin-top:10px;padding:10px;border-radius:8px;color:#8a4c17;background:#fff8ed;font-size:10px}.issues article{padding-top:6px}.issues b,.issues small{display:block}.issues small{margin-top:2px;color:#8e7157}@media(max-width:650px){.upload-choice{align-items:flex-start;flex-direction:column}.diff-grid{grid-template-columns:repeat(2,1fr)}}
</style>
