<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import * as api from '../api/client'
import type { CodeGraphStats, CodeSnapshot, CodeSubgraph, CodeSymbol, DataSource } from '../types'

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

async function load(): Promise<void> {
  loading.value = true
  try {
    ;[stats.value, snapshots.value, sources.value] = await Promise.all([
      api.getCodeGraphStats(props.knowledgeBaseId),
      api.listCodeSnapshots(props.knowledgeBaseId),
      api.listDataSources(props.knowledgeBaseId),
    ])
    const valid = sources.value.filter((item) => ['local_directory', 'git'].includes(item.source_type))
    if (!valid.some((item) => item.id === sourceId.value)) sourceId.value = valid[0]?.id ?? ''
    error.value = ''
  } catch (e) { error.value = (e as Error).message }
  finally { loading.value = false }
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

watch(() => props.knowledgeBaseId, () => { query.value = ''; symbols.value = []; graph.value = null; void load() })
onMounted(load)
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
      <button :disabled="!sourceId || loading" @click="indexCode">创建索引任务</button>
    </section>

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
            <div><strong>{{ edge.source_name }}</strong><b>→</b><strong>{{ edge.target_name }}</strong><small>{{ edge.path }}:{{ edge.start_line }} · {{ edge.resolution_status }} · {{ Math.round(edge.confidence * 100) }}%</small></div>
          </article>
        </template>
      </section>
    </div>

    <section class="snapshots"><header><strong>代码快照历史</strong><button @click="load">刷新</button></header><article v-for="item in snapshots" :key="item.id"><span :class="item.status">{{ item.status }}</span><b>{{ item.revision }}</b><small>{{ item.parser_version }} · {{ new Date(item.created_at).toLocaleString() }}</small></article></section>
  </div>
</template>

<style scoped>
.code-page { height: 100%; overflow: auto; padding: 18px; color: #344159; }.stats { display: grid; grid-template-columns: repeat(4, 1fr) 1.5fr; gap: 10px; }.stats article,.toolbar-card,.search-card,.detail-card,.snapshots { border: 1px solid #e1e7f0; border-radius: 11px; background: white; }.stats article { padding: 13px 15px; }.stats span,.stats strong { display:block }.stats span { color:#8b97a8;font-size:10px }.stats strong{margin-top:5px;font-size:22px}.stats .revision strong{font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.toolbar-card{display:flex;align-items:center;gap:12px;margin:12px 0;padding:13px}.toolbar-card>div{flex:1}.toolbar-card strong,.toolbar-card small{display:block}.toolbar-card small{margin-top:3px;color:#8794a7;font-size:10px}.toolbar-card select{min-width:280px}.toolbar-card select,.toolbar-card button,.search-card input,.search-card form button{padding:8px 10px;border:1px solid #dce3ed;border-radius:8px;background:white}.toolbar-card button,.search-card form button{color:white;background:#4f6ef7;border-color:#4f6ef7;cursor:pointer}.body-grid{display:grid;grid-template-columns:350px 1fr;gap:12px;min-height:480px}.search-card,.detail-card{padding:13px}.search-card form{display:flex;gap:6px;margin-bottom:10px}.search-card input{min-width:0;flex:1}.symbol{display:block;width:100%;padding:10px;margin:6px 0;border:1px solid #e5eaf1;border-radius:8px;background:white;text-align:left;cursor:pointer}.symbol.active{border-color:#8297f9;background:#f4f6ff}.symbol span{display:flex;justify-content:space-between}.symbol em{font-size:9px;color:#4f6ef7;font-style:normal}.symbol code,.symbol small{display:block;margin-top:4px;overflow:hidden;text-overflow:ellipsis;color:#7a8799;font-size:9px}.detail-card header{display:flex;justify-content:space-between}.detail-card header strong,.detail-card header span{display:block}.detail-card header span,.detail-card header small{color:#8996a8;font-size:10px}.detail-card pre{max-height:220px;overflow:auto;padding:13px;border-radius:8px;background:#101827;color:#dce5f2;font-size:11px;line-height:1.55}.detail-card h3{font-size:12px}.relation{display:flex;gap:10px;align-items:flex-start;padding:9px 0;border-top:1px solid #edf1f6}.relation>span{padding:3px 6px;border-radius:6px;background:#eef2ff;color:#4b63d9;font-size:8px}.relation div{flex:1}.relation strong,.relation small{display:block;font-size:10px}.relation b{color:#9aa5b4}.relation small{margin-top:3px;color:#8f9bad;font-size:9px}.snapshots{margin-top:12px;padding:13px}.snapshots header{display:flex;justify-content:space-between}.snapshots header button{border:0;background:transparent;color:#4f6ef7;cursor:pointer}.snapshots article{display:grid;grid-template-columns:90px 1fr 1fr;gap:10px;padding:8px 0;border-top:1px solid #edf1f6;font-size:10px}.snapshots small{color:#8d99aa}.snapshots span.active{color:#18794e}.error,.message{padding:8px;border-radius:7px;font-size:11px}.error{color:#b42318;background:#fff1f0}.message{color:#18794e;background:#ecf9f1}.empty{display:grid;place-items:center;min-height:100px;color:#99a5b5;font-size:11px}.empty.relations{min-height:70px}@media(max-width:1000px){.stats{grid-template-columns:repeat(2,1fr)}.body-grid{grid-template-columns:1fr}.toolbar-card{align-items:stretch;flex-direction:column}.toolbar-card select{min-width:0}}
</style>
