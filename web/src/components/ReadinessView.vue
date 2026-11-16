<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import * as api from '../api/client'
import type { ModelProbe, Readiness } from '../types'
import { useAuth } from '../composables/useAuth'
import { useOperationsOverview } from '../composables/useOperationsOverview'
import type { OperationAlert } from '../types/operations'
const { user } = useAuth()
const { isAdmin, overview, loading: operationsLoading, error: operationsError, refresh: refreshOperations } = useOperationsOverview(user)
const probe = ref<ModelProbe|null>(null)
const probing = ref(false)
const profileLabels: Record<string,string> = {answer:'回答模型',embedding:'向量模型',reranker:'重排模型'}

const readiness = ref<Readiness | null>(null)
const loading = ref(false)
const error = ref('')
const entries = computed(() => Object.entries(readiness.value?.components ?? {}))
const labels: Record<string, string> = { database: '元数据数据库', object_store: '对象存储', task_queue: '任务队列', cache: '缓存', vector_store: '向量库', model_profiles: '模型配置', ollama: '本地模型服务', index_worker: '后台任务执行器', scheduler:'定时维护调度器', agent_backend:'知识智能体执行后端', model_probe:'模型推理验证' }
const metricWindow = computed(() => {
  if (overview.value?.source === 'shared_redis') return `共享指标：最近 ${overview.value.retention_days} 个 UTC 自然日（含今天）`
  return `${overview.value?.source === 'process_fallback' ? '共享指标暂不可用；' : ''}当前进程累计（重启清零，不代表完整 7 日）`
})
const backupLabels: Record<string, string> = { backed_up: '最近备份成功', failed: '最近备份失败', not_recorded: '尚无备份记录' }
const mirrorLabels: Record<string, string> = { verified: '异机副本校验通过', unconfigured: '异机备份未配置', failed: '异机复制失败' }
const backupStatus = computed(() => backupLabels[overview.value?.backup.status ?? ''] || '备份状态待核对')
const mirrorStatus = computed(() => mirrorLabels[overview.value?.backup.mirror_status ?? ''] || '异机副本尚未验证')
const alertLabels: Record<string, string> = {
  QUEUE_AGE: '后台任务等待时间超过阈值', DISK_LOW: '数据目录所在磁盘剩余空间不足',
  MEMORY_HIGH: '内存使用超过阈值', RECENT_TASK_FAILURES: '最近 15 分钟存在失败或部分完成的任务',
  BACKUP_STALE: '完整备份已超过恢复点目标', BACKUP_NOT_RECORDED: '尚无可核对的完整服务器备份记录',
  BACKUP_FAILED: '最近一次完整备份或镜像复制失败', BACKUP_MIRROR_UNVERIFIED: '异机备份副本尚未配置或未通过校验',
}
function alertText(alert: OperationAlert): string {
  const detail = alert.count !== undefined ? `（${alert.count} 项）` : alert.seconds !== undefined ? `（已等待 ${duration(alert.seconds)}）` : ''
  return (alertLabels[alert.code] || alert.message || '运行状态需要检查') + detail
}
function duration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '暂无数据'
  if (seconds < 60) return `${seconds.toFixed(1)} 秒`
  if (seconds < 3600) return `${(seconds / 60).toFixed(1)} 分钟`
  return `${(seconds / 3600).toFixed(1)} 小时`
}
function meanDuration(name: string): string {
  const metric = overview.value?.metrics[name]
  return metric && metric.observations > 0 ? duration(metric.mean) : '暂无样本'
}
function bytes(value: number): string {
  return Number.isFinite(value) && value >= 0 ? `${(value / 1024 ** 3).toFixed(2)} GiB` : '暂无数据'
}
function dateTime(value?: string): string {
  if (!value || Number.isNaN(Date.parse(value))) return '尚无成功记录'
  return new Date(value).toLocaleString()
}
function poolCount(value?: number): string {
  return value === undefined ? '未提供' : String(Math.max(0, value))
}
function refreshAll(): void { void load(); void refreshOperations() }

async function load(): Promise<void> {
  loading.value = true
  readiness.value = null
  try {
    readiness.value = await api.getReadiness()
    const cached = readiness.value.components.model_probe
    probe.value = cached?.profiles && typeof cached.checked_at === 'string' ? {
      status: cached.status === 'ok' ? 'verified' : 'failed',
      profiles: cached.profiles as ModelProbe['profiles'], checked_at: cached.checked_at,
    } : null
    error.value = ''
  } catch (e) { error.value = (e as Error).message } finally { loading.value = false }
}
async function verifyModels(): Promise<void> {
  probing.value = true; error.value = ''
  try { await api.probeModels(); await load() } catch (e) { error.value = (e as Error).message }
  finally { probing.value = false }
}
onMounted(load)
</script>

<template>
  <div class="ready-page">
    <div class="ready-head"><div><h2>运行状态</h2><p>检查数据库、存储与任务执行器。模型配置检查不代表真实推理可用；管理员可发送短请求验证模型连接。</p></div><button v-if="user?.role==='admin'" :disabled="probing" @click="verifyModels">{{probing?'正在验证模型…':'验证模型连接'}}</button><button @click="refreshAll">重新检查</button></div>
    <p v-if="error" class="error">{{ error }}</p>
    <div v-if="loading" class="loading">正在检查各组件…</div>
    <template v-else-if="readiness">
      <section :class="['summary', readiness.status]"><div class="pulse"></div><div><strong>{{ readiness.status === 'ready' ? '基础服务检查通过' : '系统尚未完全就绪' }}</strong><p>检查时间：{{ new Date(readiness.checked_at).toLocaleString() }} · {{readiness.model_inference_verified?'模型推理已验证':'模型推理尚未验证'}}</p></div></section>
      <div class="component-grid">
        <article v-for="[name, component] in entries" :key="name">
          <header><span>{{ labels[name] || name }}</span><b :class="component.status">{{ component.status }}</b></header>
          <p>{{ component.detail || (component.status === 'ok' ? '连接与配置正常' : '需要检查配置或服务状态') }}</p>
          <details v-if="name === 'model_profiles' && component.profiles"><summary>查看模型角色</summary><pre>{{ JSON.stringify(component.profiles, null, 2) }}</pre></details>
        </article>
      </div>
    </template>
    <section v-if="isAdmin" class="operations-overview" aria-labelledby="operations-heading">
      <header class="operations-head"><div><h3 id="operations-heading">运行概览</h3><p>任务与资源为检查时的快照；模型耗时为已记录调用的算术均值。</p></div><button :disabled="operationsLoading" @click="refreshOperations">{{ operationsLoading ? '正在刷新…' : '刷新概览' }}</button></header>
      <p v-if="operationsError" class="error" role="alert">运行指标加载失败：{{ operationsError }}。可点击“刷新概览”重试。</p>
      <p v-if="operationsLoading && !overview" class="operations-empty" role="status">正在加载运行指标…</p>
      <template v-if="overview">
        <p class="operations-timestamp">检查时间：{{ dateTime(overview.checked_at) }}<span v-if="operationsError"> · 以下为上次成功获取的数据</span></p>
        <div class="operations-grid">
          <article><h4>后台任务</h4><dl><dt>排队中</dt><dd>{{ overview.tasks.pending ?? 0 }} 项</dd><dt>运行中</dt><dd>{{ overview.tasks.running ?? 0 }} 项</dd><dt>最长等待</dt><dd>{{ duration(overview.oldest_pending_seconds) }}</dd></dl></article>
          <article class="model-metrics"><h4>模型耗时均值</h4><p class="metric-window">{{ metricWindow }}</p><dl><dt>流式首字均值</dt><dd>{{ meanDuration('model_first_token_seconds') }}</dd><dt>流式总时均值</dt><dd>{{ meanDuration('model_stream_seconds') }}</dd><dt>非流式总时均值</dt><dd>{{ meanDuration('model_generate_seconds') }}</dd><dt>智能体总时均值</dt><dd>{{ meanDuration('agent_model_seconds') }}</dd></dl><p class="operations-note">包含等待模型配额的时间；流式总时包含已结束或中断的调用，首字仅统计实际输出过内容的流。</p></article>
          <article><h4>数据库连接池</h4><dl><dt>基础池大小</dt><dd>{{ poolCount(overview.database_pool.size) }}</dd><dt>使用中 / 空闲</dt><dd>{{ poolCount(overview.database_pool.checkedout) }} / {{ poolCount(overview.database_pool.checkedin) }}</dd><dt>溢出连接</dt><dd>{{ poolCount(overview.database_pool.overflow) }}</dd></dl></article>
          <article><h4>内存与磁盘</h4><dl><dt>{{ overview.memory.scope === 'container' ? '当前容器内存' : '主机内存' }}</dt><dd>{{ overview.memory.percent.toFixed(1) }}%</dd><dt>当前进程内存</dt><dd>{{ bytes(overview.process.rss_bytes) }}</dd><dt>数据盘剩余 / 总量</dt><dd>{{ bytes(overview.disk.free_bytes) }} / {{ bytes(overview.disk.total_bytes) }}</dd></dl></article>
          <article><h4>服务器完整备份</h4><p class="backup-state">{{ backupStatus }}</p><dl><dt>最近成功时间</dt><dd>{{ dateTime(overview.backup.last_success_at) }}</dd><dt>异机副本</dt><dd :class="{ 'needs-attention': overview.backup.mirror_status !== 'verified' }">{{ mirrorStatus }}</dd><template v-if="overview.backup.rpo_target_hours !== undefined"><dt>恢复点目标</dt><dd>{{ overview.backup.rpo_target_hours }} 小时</dd></template></dl><p v-if="overview.backup.rto_target_minutes !== undefined" class="operations-note">恢复时长目标 {{ overview.backup.rto_target_minutes }} 分钟{{ overview.backup.rto_verified ? '（已验证）' : '（尚未通过恢复演练验证）' }}</p></article>
        </div>
        <div v-if="overview.alerts.length" class="operations-alerts" role="status"><h4>运行提醒</h4><ul><li v-for="(alert, index) in overview.alerts" :key="`${alert.code}-${index}`">{{ alertText(alert) }}</li></ul></div>
        <p v-else class="operations-clear">当前未触发运行告警。</p>
      </template>
    </section>
    <section v-if="probe" class="probe-results"><h3>{{probe.status==='verified'?'模型连接验证通过':'模型连接验证失败'}}</h3><p>验证时间：{{new Date(probe.checked_at).toLocaleString()}}</p><article v-for="(profile,name) in probe.profiles" :key="name"><b>{{profileLabels[name]||name}}</b><span>{{profile.status==='ok'?'可用':'失败'}} · {{profile.elapsed_ms}} ms</span><small v-if="profile.error_type">{{profile.error_type}}<template v-if="profile.http_status"> · HTTP {{profile.http_status}}</template></small><span v-if="profile.detail">{{profile.detail}}</span></article></section>
  </div>
</template>

<style scoped>
.ready-page { height: 100%; overflow: auto; padding: 22px; }.ready-head { display: flex; justify-content: space-between; align-items: center; }.ready-head h2 { margin: 0; font-size: 18px; }.ready-head p { margin: 5px 0 0; color: #8b97a8; font-size: 12px; }.ready-head button { padding: 8px 12px; border: 1px solid #dce3ed; border-radius: 8px; background: white; cursor: pointer; }.error { color: #b42318; }.loading { padding: 50px; text-align: center; color: #8c98aa; }.summary { display: flex; align-items: center; gap: 13px; margin: 20px 0; padding: 18px; border: 1px solid #e1e7f0; border-radius: 12px; background: white; }.summary.ready { border-color: #bde5cc; background: #f4fcf7; }.summary .pulse { width: 13px; height: 13px; border-radius: 50%; background: #e15241; box-shadow: 0 0 0 6px #fbe4e0; }.summary.ready .pulse { background: #24a467; box-shadow: 0 0 0 6px #dff5e8; }.summary strong { color: #243047; }.summary p { margin: 4px 0 0; color: #8b97a8; font-size: 11px; }.component-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }.component-grid article { min-height: 120px; padding: 15px; border: 1px solid #e1e7f0; border-radius: 11px; background: white; }.component-grid header { display: flex; justify-content: space-between; color: #2b374d; font-size: 13px; }.component-grid b { padding: 2px 7px; border-radius: 9px; background: #fff0ee; color: #b42318; font-size: 9px; text-transform: uppercase; }.component-grid b.ok { background: #e9f8ef; color: #18794e; }.component-grid p { color: #8491a3; font-size: 11px; line-height: 1.5; }.component-grid summary { color: #4f6ef7; cursor: pointer; font-size: 10px; }.component-grid pre { max-height: 200px; overflow: auto; padding: 8px; background: #f6f8fb; font-size: 9px; }.error { padding: 9px; background: #fff1f0; border-radius: 8px; }
.ready-head{gap:8px}.ready-head>div{flex:1}.probe-results{margin-top:16px;padding:16px;border:1px solid #e1e7f0;border-radius:12px;background:#fff}.probe-results h3{margin:0;font-size:14px}.probe-results p{font-size:11px;color:#8491a3}.probe-results article{display:flex;gap:12px;flex-wrap:wrap;padding:9px;border-top:1px solid #edf1f6;font-size:12px}.probe-results small{color:#b42318}
.operations-overview{margin-top:20px;padding:18px;border:1px solid #e1e7f0;border-radius:12px;background:#fff}.operations-head{display:flex;align-items:center;justify-content:space-between;gap:12px}.operations-head h3{margin:0;font-size:15px;color:#243047}.operations-head p,.operations-timestamp,.operations-note,.metric-window,.operations-empty{color:#64748b;font-size:12px;line-height:1.6}.operations-head p{margin:6px 0 0}.operations-head button{flex-shrink:0;padding:7px 11px;border:1px solid #dce3ed;border-radius:8px;background:#fff;cursor:pointer}.operations-head button:disabled{opacity:.6;cursor:wait}.operations-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}.operations-grid article{padding:14px;border:1px solid #e8edf4;border-radius:9px;background:#fbfcfe;min-width:0}.operations-grid h4,.operations-alerts h4{margin:0 0 10px;font-size:13px;color:#2b374d}.operations-grid dl{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:9px 12px;margin:0;font-size:12px}.operations-grid dt{color:#64748b}.operations-grid dd{margin:0;text-align:right;color:#243047;overflow-wrap:anywhere}.operations-grid .needs-attention{color:#9a6700}.metric-window{margin:0 0 12px}.operations-note{margin:10px 0 0}.backup-state{margin:0 0 12px;font-size:12px;color:#334155}.operations-alerts{margin-top:14px;padding:12px 14px;border:1px solid #f3d9a4;border-radius:8px;background:#fffbef;color:#805b11;font-size:12px;line-height:1.8}.operations-alerts h4{color:inherit}.operations-alerts ul{padding-left:18px;margin:0}.operations-clear{font-size:12px;color:#18794e;margin-bottom:0}@media(max-width:640px){.ready-page{padding:14px}.ready-head{flex-wrap:wrap}.ready-head>div{flex-basis:100%}.operations-overview{padding:12px}.operations-head{align-items:flex-start}.operations-grid{grid-template-columns:minmax(0,1fr)}.operations-grid dl{grid-template-columns:minmax(80px,1fr) minmax(0,1.4fr)}}
</style>
