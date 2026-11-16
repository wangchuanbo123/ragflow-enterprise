<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as api from '../api/client'
import type { IndexJob } from '../types'
import { usePermissions } from '../composables/usePermissions'
import { jobName, stageName, statusName } from '../jobPresentation'
import { navigate, navigationState } from '../composables/useNavigation'
const { canEdit } = usePermissions()

const props = defineProps<{ knowledgeBaseId: string }>()
const jobs = ref<IndexJob[]>([])
const loading = ref(false)
const error = ref('')
const statusFilter = ref('')
function linkedQuery(): string { return navigationState.value.page === 'jobs' && navigationState.value.kb === props.knowledgeBaseId ? navigationState.value.query : '' }
const query=ref(linkedQuery()),offset=ref(0),total=ref(0)
const limit=50
const filtered = computed(() => jobs.value)
let timer: number | undefined
let searchTimer:number|undefined,sequence=0,disposed=false

async function load(silent = false): Promise<void> {
  const request=++sequence,kb=props.knowledgeBaseId
  if (!silent) loading.value = true
  try {
    const page=await api.listIndexJobPage(kb,{offset:offset.value,limit,status:statusFilter.value,query:query.value})
    if(disposed||kb!==props.knowledgeBaseId||request!==sequence)return
    jobs.value=page.items;total.value=page.total
    if(offset.value>0&&!page.items.length&&page.total<=offset.value){offset.value=Math.max(0,Math.floor(Math.max(0,page.total-1)/limit)*limit);await load(true);return}
    error.value = ''
  } catch (e) {
    if(!disposed&&kb===props.knowledgeBaseId&&request===sequence&&(e as Error).name!=='AbortError')error.value = (e as Error).message
  } finally {
    if(request===sequence)loading.value = false
  }
}

async function sync(): Promise<void> {
  try { await api.startIndexSync(props.knowledgeBaseId); await load() } catch (e) { error.value = (e as Error).message }
}
async function cancel(job: IndexJob): Promise<void> {
  try { await api.cancelIndexJob(props.knowledgeBaseId, job.id); await load() } catch (e) { error.value = (e as Error).message }
}
async function retry(job: IndexJob): Promise<void> {
  try { await api.retryIndexJob(props.knowledgeBaseId, job.id); await load() } catch (e) { error.value = (e as Error).message }
}
function progress(job: IndexJob): number {
  return job.total_items ? Math.min(100, Math.round(job.processed_items / job.total_items * 100)) : (job.status === 'completed' ? 100 : 0)
}

function pageBy(delta:number):void{offset.value=Math.max(0,offset.value+delta*limit);void load()}
onMounted(() => { void load(); timer = window.setInterval(() => {if(!document.hidden)void load(true)}, 5000) })
onBeforeUnmount(() => { disposed=true;sequence++;if (timer) window.clearInterval(timer);window.clearTimeout(searchTimer) })
watch(() => props.knowledgeBaseId, () => {offset.value=0;query.value=linkedQuery();statusFilter.value='';void load()})
watch(() => [navigationState.value.page,navigationState.value.kb,navigationState.value.query], () => {
  if (navigationState.value.page !== 'jobs' || navigationState.value.kb !== props.knowledgeBaseId) return
  query.value = linkedQuery();statusFilter.value = ''
})
watch([statusFilter,query],()=>{offset.value=0;window.clearTimeout(searchTimer);searchTimer=window.setTimeout(()=>void load(),200)})
</script>

<template>
  <div class="jobs-page">
    <div class="toolbar">
      <div><h2>索引任务</h2><p>查看上传、重建、删除和图谱构建任务的实时状态。</p></div>
      <div class="toolbar-actions"><input v-model="query" placeholder="搜索文档或任务" aria-label="搜索任务"/><select v-model="statusFilter"><option value="">全部状态</option><option value="pending">排队中</option><option value="running">运行中</option><option value="completed">已完成</option><option value="partial">部分完成</option><option value="failed">失败</option><option value="cancelled">已取消</option></select><button @click="load()">刷新</button><button v-if="canEdit" class="primary" @click="sync">同步本地目录</button></div>
    </div>
    <p v-if="error" class="error">{{ error }}</p>
    <section class="job-card">
      <div v-if="loading" class="empty">正在读取任务…</div>
      <div v-else-if="!filtered.length" class="empty">当前没有索引任务。</div>
      <article v-for="job in filtered" v-else :key="job.id" class="job-row">
        <div class="job-icon">{{ job.job_type.includes('delete') ? '×' : job.job_type.includes('graph') ? '⌘' : '↻' }}</div>
        <div class="job-main">
          <header><strong>{{ jobName(job.job_type) }}</strong><span :class="['status', job.status]">{{ statusName(job.status) }}</span><small>{{ new Date(job.created_at).toLocaleString() }}</small></header>
          <p class="job-stage">{{stageName(job.current_stage)}}<span v-if="job.waiting_reason"> · {{job.waiting_reason}}</span></p>
          <p class="job-id">任务 ID：{{job.id}}</p>
          <div v-if="job.document_ids?.length" class="document-links"><button v-for="(id,index) in job.document_ids" :key="id" @click="navigate({page:'documents',kb:knowledgeBaseId,document:id})">查看 {{job.document_names?.[index]||'关联文档'}}</button></div>
          <div class="progress"><i :style="{ width: `${progress(job)}%` }"></i></div>
          <footer><span>{{ job.processed_items }} / {{ job.total_items }} 项</span><span>成功 {{ job.succeeded_items }} · 失败 {{ job.failed_items }}</span><span>尝试 {{ job.attempt_count }} / {{ job.max_attempts }}</span></footer>
          <p v-if="job.error_message" class="job-error">{{ job.error_message }}</p>
        </div>
        <div v-if="canEdit" class="job-actions"><button v-if="['pending','running'].includes(job.status)" @click="cancel(job)">取消</button><button v-if="['failed','cancelled','partial'].includes(job.status)" @click="retry(job)">重试</button></div>
      </article>
      <footer class="pagination"><span>{{total}} 个任务 · 第 {{Math.floor(offset/limit)+1}} / {{Math.max(1,Math.ceil(total/limit))}} 页</span><button :disabled="loading||offset===0" @click="pageBy(-1)">上一页</button><button :disabled="loading||offset+limit>=total" @click="pageBy(1)">下一页</button></footer>
    </section>
  </div>
</template>

<style scoped>
.job-id{margin:6px 0;font-size:10px;color:#7c8a9d;overflow-wrap:anywhere}
.job-stage{font-size:11px;color:#687c94;margin:8px 0}.document-links{display:flex;gap:6px;flex-wrap:wrap}.document-links button,.pagination button{border:1px solid #dce3ed;border-radius:6px;padding:5px 8px;background:white;color:#4265bd;font-size:11px}.pagination{display:flex;justify-content:flex-end;align-items:center;gap:10px;padding:12px;color:#7c8a9d;font-size:11px}.pagination button:disabled{opacity:.4}.toolbar-actions{flex-wrap:wrap}.toolbar-actions input{border:1px solid #dce3ed;border-radius:8px;padding:8px}
.jobs-page { height: 100%; overflow: auto; padding: 20px; }.toolbar { display: flex; justify-content: space-between; align-items: end; margin-bottom: 16px; }.toolbar h2 { margin: 0; font-size: 18px; }.toolbar p { margin: 5px 0 0; color: #8b97a8; font-size: 12px; }.toolbar-actions { display: flex; gap: 8px; }.toolbar select,.toolbar button { padding: 8px 11px; border: 1px solid #dce3ed; border-radius: 8px; background: white; color: #526177; cursor: pointer; }.toolbar button.primary { background: #4f6ef7; color: white; border-color: #4f6ef7; }.error,.job-error { color: #b42318; }.error { padding: 9px; background: #fff1f0; border-radius: 8px; }.job-card { border: 1px solid #e1e7f0; border-radius: 12px; background: white; overflow: hidden; }.empty { padding: 50px; text-align: center; color: #97a3b3; }.job-row { display: flex; gap: 13px; padding: 15px 16px; border-top: 1px solid #edf1f6; }.job-row:first-child { border-top: 0; }.job-icon { display: grid; place-items: center; width: 38px; height: 38px; flex: 0 0 38px; border-radius: 10px; background: #eef2ff; color: #4f6ef7; font-size: 17px; }.job-main { flex: 1; min-width: 0; }.job-main header { display: flex; align-items: center; gap: 8px; }.job-main strong { color: #263249; font-size: 13px; }.job-main small { margin-left: auto; color: #98a4b4; }.status { padding: 2px 7px; border-radius: 10px; background: #eef2f7; color: #64748b; font-size: 10px; }.status.completed { background: #e9f8ef; color: #18794e; }.status.running { background: #eaf0ff; color: #3156cf; }.status.failed { background: #fff0ee; color: #b42318; }.progress { height: 5px; margin: 10px 0 7px; overflow: hidden; border-radius: 4px; background: #edf1f6; }.progress i { display: block; height: 100%; background: #4f6ef7; }.job-main footer { display: flex; gap: 20px; color: #8a96a8; font-size: 10px; }.job-error { margin: 7px 0 0; font-size: 11px; }.job-actions button { padding: 6px 10px; border: 1px solid #dce3ed; border-radius: 7px; background: white; cursor: pointer; color: #526177; }
</style>
