<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import * as api from '../api/client'
import type { KnowledgeDocument, RetrievalDebugResult } from '../types'

const props = defineProps<{ knowledgeBaseId: string }>()
const documents = ref<KnowledgeDocument[]>([])
const query = ref('')
const channels = ref<string[]>(['text'])
const documentIds = ref<string[]>([])
const result = ref<RetrievalDebugResult | null>(null)
const running = ref(false)
const error = ref('')

async function load(): Promise<void> {
  try { documents.value = await api.listDocuments(props.knowledgeBaseId) } catch (e) { error.value = (e as Error).message }
}
async function run(): Promise<void> {
  if (!query.value.trim()) return
  running.value = true
  try {
    result.value = await api.debugRetrieval(props.knowledgeBaseId, query.value.trim(), {
      channels: channels.value,
      scope: { document_ids: documentIds.value, tag_ids: [], folder_id: null, metadata: {} },
    })
    error.value = ''
  } catch (e) { error.value = (e as Error).message } finally { running.value = false }
}
watch(() => props.knowledgeBaseId, () => { result.value = null; documentIds.value = []; void load() })
onMounted(load)
</script>

<template>
  <div class="debug-page">
    <section class="query-card"><div><h2>检索调试</h2><p>只执行改写、路由、检索、融合和重排，不调用回答生成模型。</p></div><textarea v-model="query" placeholder="输入一个需要验证的检索问题"></textarea><div class="options"><fieldset><legend>检索通道</legend><label><input v-model="channels" type="checkbox" value="text" />文档向量 + BM25</label><label><input v-model="channels" type="checkbox" value="graph" />知识图谱</label><label><input v-model="channels" type="checkbox" value="code" />代码图（未就绪时自动降级）</label></fieldset><label class="scope">检索文档范围<select v-model="documentIds" multiple><option v-for="document in documents" :key="document.id" :value="document.id">{{ document.original_filename }}</option></select><small>不选择表示检索整个知识库，可按 Ctrl/⌘ 多选。</small></label></div><button :disabled="running || !query.trim() || !channels.length" @click="run">{{ running ? '正在执行检索…' : '运行检索调试' }}</button></section>
    <p v-if="error" class="error">{{ error }}</p>
    <template v-if="result">
      <div class="route-grid"><article><span>路由方式</span><strong>{{ result.route.method }}</strong></article><article><span>实际通道</span><strong>{{ result.route.selected_channels.join(' + ') }}</strong></article><article><span>路由置信度</span><strong>{{ Math.round(result.route.confidence * 100) }}%</strong></article><article><span>融合候选</span><strong>{{ result.route.fused_count ?? 0 }}</strong></article></div>
      <div class="result-grid"><section><header>路由解释</header><ul><li v-for="reason in result.route.reasons" :key="reason">{{ reason }}</li></ul><div v-for="(channel, name) in result.route.channel_results" :key="name" class="channel"><b>{{ name }}</b><span>{{ channel.status }} · {{ channel.count }} 条 · {{ channel.elapsed_ms }} ms</span></div></section><section><header>引用候选（{{ result.sources.length }}）</header><article v-for="source in result.sources" :key="source.source"><strong>{{ source.source }}</strong><p>{{ source.preview }}</p></article></section><section class="context"><header>最终上下文预览</header><pre>{{ result.context_preview || '没有构建出上下文' }}</pre></section></div>
    </template>
  </div>
</template>

<style scoped>
.debug-page { height: 100%; overflow: auto; padding: 20px; }.query-card { padding: 17px; border: 1px solid #e1e7f0; border-radius: 12px; background: white; }.query-card h2 { margin: 0; font-size: 18px; }.query-card p { margin: 4px 0 13px; color: #8995a7; font-size: 11px; }.query-card textarea { width: 100%; min-height: 72px; padding: 10px; resize: vertical; border: 1px solid #dce3ed; border-radius: 8px; font: inherit; }.options { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin: 12px 0; }.options fieldset { display: flex; gap: 15px; border: 1px solid #e1e7f0; border-radius: 8px; color: #68768a; font-size: 10px; }.options legend { color: #8c98a9; }.scope { display: grid; gap: 4px; color: #68768a; font-size: 10px; }.scope select { min-height: 60px; border: 1px solid #dce3ed; border-radius: 8px; }.scope small { color: #9aa5b4; }.query-card > button { padding: 9px 14px; border: 0; border-radius: 8px; background: #4f6ef7; color: white; cursor: pointer; }.query-card > button:disabled { opacity: .5; }.error { padding: 9px; color: #b42318; background: #fff1f0; border-radius: 8px; }.route-grid { display: grid; grid-template-columns: repeat(4,1fr); gap: 10px; margin: 13px 0; }.route-grid article,.result-grid section { padding: 13px; border: 1px solid #e1e7f0; border-radius: 10px; background: white; }.route-grid span,.route-grid strong { display: block; }.route-grid span { color: #8a96a8; font-size: 9px; }.route-grid strong { margin-top: 5px; color: #2d3a51; font-size: 15px; }.result-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }.result-grid section > header { padding-bottom: 9px; border-bottom: 1px solid #edf1f6; color: #344159; font-size: 12px; font-weight: 600; }.result-grid ul { padding-left: 18px; color: #69778b; font-size: 11px; }.channel { display: flex; justify-content: space-between; padding: 7px; margin-top: 5px; border-radius: 7px; background: #f6f8fb; color: #788598; font-size: 10px; }.result-grid article { padding: 8px 0; border-bottom: 1px solid #edf1f6; }.result-grid article strong { color: #43516a; font-size: 10px; }.result-grid article p { color: #7b8799; font-size: 10px; line-height: 1.5; }.result-grid .context { grid-column: 1/-1; }.context pre { max-height: 300px; overflow: auto; white-space: pre-wrap; color: #59677c; font: 10px/1.6 ui-monospace, monospace; }
@media(max-width: 850px) { .options,.result-grid { grid-template-columns: 1fr; }.route-grid { grid-template-columns: repeat(2,1fr); }.result-grid .context { grid-column: auto; } }
</style>
