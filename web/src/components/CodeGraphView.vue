<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import * as api from '../api/client'
import { navigate } from '../composables/useNavigation'
import { usePermissions } from '../composables/usePermissions'
import type { CodeGraphStats, CodeSubgraph, CodeSymbol } from '../types'

const props = defineProps<{ knowledgeBaseId: string }>()
const { canEdit } = usePermissions()
const stats = ref<CodeGraphStats | null>(null)
const query = ref('')
const symbols = ref<CodeSymbol[]>([])
const graph = ref<CodeSubgraph | null>(null)
const selected = ref<CodeSymbol | null>(null)
const loading = ref(false)
const inspecting = ref(false)
const searched = ref(false)
const statsError = ref('')
const searchError = ref('')
const detailError = ref('')
let epoch = 0
let searchRequest = 0
let detailRequest = 0
let disposed = false
const active = (generation: number) => !disposed && generation === epoch

function manageCode(): void {
  navigate({ page: 'graph-management', kb: props.knowledgeBaseId, graphKind: 'code' })
}

async function loadStats(generation: number): Promise<void> {
  try {
    const result = await api.getCodeGraphStats(props.knowledgeBaseId)
    if (active(generation)) { stats.value = result; statsError.value = '' }
  } catch (cause) {
    if (active(generation)) statsError.value = (cause as Error).message
  }
}

async function search(): Promise<void> {
  if (!query.value.trim()) return
  const generation = epoch, request = ++searchRequest
  ++detailRequest
  loading.value = true; searchError.value = ''; detailError.value = ''
  selected.value = null; graph.value = null; inspecting.value = false
  try {
    const result = await api.searchCodeSymbols(props.knowledgeBaseId, query.value.trim())
    if (!active(generation) || request !== searchRequest) return
    symbols.value = result; searched.value = true
  } catch (cause) {
    if (active(generation) && request === searchRequest) searchError.value = (cause as Error).message
  } finally {
    if (active(generation) && request === searchRequest) loading.value = false
  }
}

async function inspect(symbol: CodeSymbol): Promise<void> {
  const generation = epoch, request = ++detailRequest
  selected.value = symbol; graph.value = null; detailError.value = ''; inspecting.value = true
  try {
    const result = await api.getCodeSubgraph(props.knowledgeBaseId, [symbol.id])
    if (active(generation) && request === detailRequest) graph.value = result
  } catch (cause) {
    if (active(generation) && request === detailRequest) detailError.value = (cause as Error).message
  } finally {
    if (active(generation) && request === detailRequest) inspecting.value = false
  }
}

watch(() => props.knowledgeBaseId, () => {
  ++epoch; ++searchRequest; ++detailRequest
  stats.value = null; query.value = ''; symbols.value = []; graph.value = null; selected.value = null
  loading.value = false; inspecting.value = false; searched.value = false
  statsError.value = ''; searchError.value = ''; detailError.value = ''
  void loadStats(epoch)
}, { immediate: true })
onBeforeUnmount(() => { disposed = true; ++epoch })
</script>

<template>
  <div class="code-page">
    <header class="browse-heading">
      <div><h2>代码图谱</h2><p>检索代码符号，查看源码位置、上下游调用与模块依赖。</p></div>
      <button v-if="canEdit" class="manage-code" @click="manageCode">管理代码图谱</button>
    </header>
    <p v-if="statsError" class="error">{{ statsError }} <button @click="loadStats(epoch)">重试</button></p>
    <div class="stats">
      <article><span>源码文件</span><strong>{{ stats?.files ?? '—' }}</strong></article>
      <article><span>符号</span><strong>{{ stats?.symbols ?? '—' }}</strong></article>
      <article><span>调用与依赖</span><strong>{{ stats?.relations ?? '—' }}</strong></article>
      <article><span>已解析关系</span><strong>{{ stats?.resolved_relations ?? '—' }}</strong></article>
      <article class="revision"><span>当前版本</span><strong>{{ stats?.revision || '尚未构建' }}</strong></article>
    </div>
    <p v-if="stats && !stats.active_snapshot_id" class="browse-notice">尚未构建代码图谱。{{ canEdit ? '请在图谱治理中接入代码源并构建。' : '请联系知识库维护者接入代码源并构建。' }}</p>
    <div class="body-grid">
      <section class="search-card">
        <form @submit.prevent="search"><input v-model="query" aria-label="代码符号关键词" placeholder="搜索类、函数、方法或限定名" /><button :disabled="loading || !query.trim()">{{ loading ? '检索中…' : '检索' }}</button></form>
        <p v-if="searchError" class="error">{{ searchError }}</p>
        <div v-if="!symbols.length && !loading" class="empty">{{ searched ? '没有匹配的代码符号，请调整关键词。' : '输入符号名称查看源码位置及上下游调用。' }}</div>
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
          <p v-if="detailError" class="error">{{ detailError }} <button @click="inspect(selected)">重试</button></p>
          <div v-else-if="inspecting" class="empty relations">正在读取调用与依赖关系…</div>
          <div v-else-if="!graph?.edges.length" class="empty relations">当前符号没有可展示的上下游关系</div>
          <article v-for="edge in graph?.edges ?? []" :key="edge.id" class="relation">
            <span :class="edge.relation_type.toLowerCase()">{{ edge.relation_type }}</span>
            <div><strong>{{ edge.source_name }}</strong><b>→</b><strong>{{ edge.target_name }}</strong><small>{{ edge.path }}:{{ edge.start_line }} · {{ edge.resolution_status }} · 解析评分 {{ Math.round(edge.confidence * 100) }} 分</small></div>
          </article>
        </template>
      </section>
    </div>
  </div>
</template>

<style scoped>
.code-page { height: 100%; overflow: auto; padding: 18px; color: #344159; }
.browse-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 16px; }
h2 { margin: 0; color: #172033; font-size: 17px; }
.browse-heading p { margin: 5px 0 0; color: #7a8799; font-size: 12px; }
button { cursor: pointer; } button:disabled { opacity: .5; cursor: not-allowed; }
.manage-code { flex-shrink: 0; padding: 9px 14px; border: 1px solid #d7e0ff; border-radius: 8px; color: #4762df; background: #f5f7ff; }
.stats { display: grid; grid-template-columns: repeat(4, 1fr) 1.5fr; gap: 10px; margin-bottom: 14px; }
.stats article, .search-card, .detail-card { border: 1px solid #e1e7f0; border-radius: 11px; background: white; }
.stats article { padding: 13px 15px; }.stats span, .stats strong { display: block; }.stats span { color: #8b97a8; font-size: 10px; }.stats strong { margin-top: 5px; font-size: 22px; }
.stats .revision strong { font-size: 12px; overflow-wrap: anywhere; }
.browse-notice { padding: 12px; border: 1px solid #dbe5ff; border-radius: 8px; color: #607394; background: #f2f6ff; font-size: 12px; }
.body-grid { display: grid; grid-template-columns: 350px 1fr; gap: 12px; min-height: 480px; }
.search-card, .detail-card { min-width: 0; padding: 13px; }
.search-card form { display: flex; gap: 6px; margin-bottom: 10px; }.search-card input { min-width: 0; flex: 1; }
.search-card input, .search-card form button { padding: 8px 10px; border: 1px solid #dce3ed; border-radius: 8px; background: white; }.search-card form button { color: white; background: #4f6ef7; border-color: #4f6ef7; }
.symbol { display: block; width: 100%; padding: 10px; margin: 6px 0; border: 1px solid #e5eaf1; border-radius: 8px; background: white; text-align: left; }.symbol.active { border-color: #8297f9; background: #f4f6ff; }.symbol span { display: flex; justify-content: space-between; }.symbol em { color: #4f6ef7; font-size: 9px; font-style: normal; }.symbol code, .symbol small { display: block; margin-top: 4px; overflow: hidden; text-overflow: ellipsis; color: #7a8799; font-size: 9px; }
.detail-card header { display: flex; justify-content: space-between; gap: 10px; overflow-wrap: anywhere; }.detail-card header strong, .detail-card header span { display: block; }.detail-card header span, .detail-card header small { color: #8996a8; font-size: 10px; }.detail-card pre { max-height: 220px; overflow: auto; padding: 13px; border-radius: 8px; background: #101827; color: #dce5f2; font-size: 11px; line-height: 1.55; }.detail-card h3 { font-size: 12px; }
.relation { display: flex; gap: 10px; align-items: flex-start; padding: 9px 0; border-top: 1px solid #edf1f6; }.relation > span { padding: 3px 6px; border-radius: 6px; background: #eef2ff; color: #4b63d9; font-size: 8px; }.relation div { min-width: 0; flex: 1; overflow-wrap: anywhere; }.relation strong, .relation small { display: block; font-size: 10px; }.relation b { color: #9aa5b4; }.relation small { margin-top: 3px; color: #8f9bad; font-size: 9px; }
.error { padding: 8px; border-radius: 7px; color: #b42318; background: #fff1f0; font-size: 11px; }.error button { border: 0; color: #4762df; background: transparent; }
.empty { display: grid; place-items: center; min-height: 100px; color: #99a5b5; font-size: 11px; }.empty.relations { min-height: 70px; }
@media (max-width: 1000px) { .stats { grid-template-columns: repeat(2, 1fr); }.body-grid { grid-template-columns: 1fr; } }
@media (max-width: 650px) { .browse-heading { align-items: flex-start; flex-direction: column; } }
</style>
