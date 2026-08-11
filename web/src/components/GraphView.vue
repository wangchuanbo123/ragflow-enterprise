<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import * as api from '../api/client'
import type {
  DocumentSubgraph,
  EntitySubgraph,
  GraphCorrection,
  GraphEntity,
  GraphStats,
  KnowledgeDocument,
  SubgraphEdge,
} from '../types'

const props = defineProps<{ knowledgeBaseId: string }>()
const stats = ref<GraphStats | null>(null)
const documents = ref<KnowledgeDocument[]>([])
const documentId = ref('')
const subgraph = ref<DocumentSubgraph | null>(null)
const entityGraph = ref<EntitySubgraph | null>(null)
const entityQuery = ref('')
const entities = ref<GraphEntity[]>([])
const selectedEdge = ref<SubgraphEdge | null>(null)
const corrections = ref<GraphCorrection[]>([])
const relationForm = reactive({ subject_entity_id: '', object_entity_id: '', predicate: 'RELATED_TO', evidence: '', reason: '' })
const loading = ref(false)
const error = ref('')

const activeGraph = computed(() => entityGraph.value ?? subgraph.value)
const nodeMap = computed(() => new Map((activeGraph.value?.nodes ?? []).map((node) => [node.id, node])))

function position(index: number, total: number): { x: number; y: number } {
  const angle = (Math.PI * 2 * index) / Math.max(total, 1) - Math.PI / 2
  return { x: 320 + Math.cos(angle) * 230, y: 180 + Math.sin(angle) * 125 }
}

function nodePosition(id: string): { x: number; y: number } {
  const nodes = activeGraph.value?.nodes ?? []
  return position(Math.max(0, nodes.findIndex((node) => node.id === id)), nodes.length)
}

async function load(): Promise<void> {
  loading.value = true
  try {
    ;[stats.value, documents.value, corrections.value] = await Promise.all([
      api.getGraphStats(props.knowledgeBaseId),
      api.listDocuments(props.knowledgeBaseId),
      api.listGraphCorrections(props.knowledgeBaseId),
    ])
    if (documentId.value && !documents.value.some((item) => item.id === documentId.value)) documentId.value = ''
    error.value = ''
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

async function loadSubgraph(): Promise<void> {
  selectedEdge.value = null
  entityGraph.value = null
  if (!documentId.value) {
    subgraph.value = null
    return
  }
  try {
    subgraph.value = await api.getDocumentSubgraph(props.knowledgeBaseId, documentId.value)
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function search(): Promise<void> {
  if (!entityQuery.value.trim()) {
    entities.value = []
    return
  }
  try {
    entities.value = await api.searchGraphEntities(props.knowledgeBaseId, entityQuery.value.trim())
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function expandEntity(entity: GraphEntity): Promise<void> {
  loading.value = true
  selectedEdge.value = null
  try {
    entityGraph.value = await api.getEntitySubgraph(props.knowledgeBaseId, [entity.id], 0, 100)
    documentId.value = ''
    subgraph.value = null
    error.value = ''
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

async function loadMoreEntityGraph(): Promise<void> {
  const graph = entityGraph.value
  if (!graph || graph.next_offset === null) return
  loading.value = true
  try {
    const next = await api.getEntitySubgraph(props.knowledgeBaseId, graph.seed_entity_ids, graph.next_offset, 100)
    const nodes = new Map(graph.nodes.map((node) => [node.id, node]))
    const edges = new Map(graph.edges.map((edge) => [edge.id, edge]))
    next.nodes.forEach((node) => nodes.set(node.id, node))
    next.edges.forEach((edge) => edges.set(edge.id, edge))
    entityGraph.value = { ...next, nodes: [...nodes.values()], edges: [...edges.values()] }
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

async function refreshOverlay(): Promise<void> {
  ;[stats.value, corrections.value] = await Promise.all([
    api.getGraphStats(props.knowledgeBaseId),
    api.listGraphCorrections(props.knowledgeBaseId),
  ])
  if (documentId.value) await loadSubgraph()
}

async function reconcileCorrections(): Promise<void> {
  loading.value = true
  try {
    await api.reconcileGraphCorrections(props.knowledgeBaseId)
    await refreshOverlay()
    error.value = ''
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

async function hideEdge(edge: SubgraphEdge): Promise<void> {
  const reason = window.prompt('请填写隐藏该关系的原因（将写入审计记录）')?.trim()
  if (!reason) return
  try {
    await api.createGraphCorrection(props.knowledgeBaseId, { action: 'hide_relation', target_id: edge.id, reason })
    await refreshOverlay()
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function updateEdge(edge: SubgraphEdge): Promise<void> {
  const predicate = window.prompt('修正后的关系类型', edge.predicate)?.trim()
  if (!predicate || predicate === edge.predicate) return
  const reason = window.prompt('请填写修改原因（将写入审计记录）')?.trim()
  if (!reason) return
  try {
    await api.createGraphCorrection(props.knowledgeBaseId, { action: 'update_relation', target_id: edge.id, payload: { predicate }, reason })
    await refreshOverlay()
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function addRelation(): Promise<void> {
  if (!documentId.value) return
  try {
    await api.createGraphCorrection(props.knowledgeBaseId, {
      action: 'add_relation',
      reason: relationForm.reason,
      payload: { ...relationForm, document_id: documentId.value, confidence: 1 },
    })
    Object.assign(relationForm, { subject_entity_id: '', object_entity_id: '', predicate: 'RELATED_TO', evidence: '', reason: '' })
    await refreshOverlay()
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function revertCorrection(correctionId: string): Promise<void> {
  try {
    await api.revertGraphCorrection(props.knowledgeBaseId, correctionId)
    await refreshOverlay()
  } catch (e) {
    error.value = (e as Error).message
  }
}

watch(() => props.knowledgeBaseId, () => {
  documentId.value = ''
  subgraph.value = null
  entityGraph.value = null
  entities.value = []
  void load()
})
watch(documentId, loadSubgraph)
onMounted(load)
</script>

<template>
  <div class="graph-page">
    <div class="stats-grid">
      <article><span>实体</span><strong>{{ stats?.entities ?? 0 }}</strong></article>
      <article><span>关系</span><strong>{{ stats?.relations ?? 0 }}</strong></article>
      <article><span>实体提及</span><strong>{{ stats?.mentions ?? 0 }}</strong></article>
      <article><span>证据片段</span><strong>{{ stats?.evidence ?? 0 }}</strong></article>
      <article><span>生效中的人工校正</span><strong>{{ stats?.active_corrections ?? 0 }}</strong></article>
      <article><span>冲突校正</span><strong class="conflict-number">{{ stats?.conflict_corrections ?? 0 }}</strong></article>
    </div>
    <p v-if="error" class="error">{{ error }}</p>

    <div class="graph-toolbar">
      <label>按文档查看子图
        <select v-model="documentId">
          <option value="">请选择文档</option>
          <option v-for="document in documents" :key="document.id" :value="document.id">{{ document.original_filename }}</option>
        </select>
      </label>
      <form @submit.prevent="search">
        <input v-model="entityQuery" placeholder="搜索实体名称或别名" />
        <button>搜索</button>
      </form>
    </div>

    <div class="graph-layout">
      <section class="canvas-card">
        <div v-if="loading && !activeGraph" class="empty">正在读取知识图谱…</div>
        <div v-else-if="!activeGraph" class="empty">
          <strong>选择文档或搜索实体</strong>
          <span>可按文档查看抽取结果，也可从任意实体按需展开全库关系。</span>
        </div>
        <div v-else-if="!activeGraph.nodes.length" class="empty">
          <strong>当前范围尚无子图</strong>
          <span>可在索引任务页重新执行图谱构建。</span>
        </div>
        <svg v-else viewBox="0 0 640 360" role="img" aria-label="知识图谱子图">
          <defs><marker id="arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7 Z" fill="#9aa9c0" /></marker></defs>
          <g v-for="edge in activeGraph.edges" :key="edge.id" class="edge" @click="selectedEdge = edge">
            <line :x1="nodePosition(edge.source).x" :y1="nodePosition(edge.source).y" :x2="nodePosition(edge.target).x" :y2="nodePosition(edge.target).y" marker-end="url(#arrow)" />
            <text :x="(nodePosition(edge.source).x + nodePosition(edge.target).x) / 2" :y="(nodePosition(edge.source).y + nodePosition(edge.target).y) / 2 - 5">{{ edge.predicate }}</text>
          </g>
          <g v-for="(node, index) in activeGraph.nodes" :key="node.id" class="node" :transform="`translate(${position(index, activeGraph.nodes.length).x},${position(index, activeGraph.nodes.length).y})`">
            <circle r="27" /><text y="4">{{ node.label.slice(0, 7) }}</text><text class="node-type" y="43">{{ node.entity_type }}</text>
          </g>
        </svg>
        <button v-if="entityGraph?.next_offset !== null && entityGraph" class="load-more" :disabled="loading" @click="loadMoreEntityGraph">继续展开关系</button>
      </section>

      <aside class="inspector">
        <template v-if="selectedEdge">
          <header><span>关系证据</span><button @click="selectedEdge = null">×</button></header>
          <h3>{{ nodeMap.get(selectedEdge.source)?.label }} <em>—{{ selectedEdge.predicate }}→</em> {{ nodeMap.get(selectedEdge.target)?.label }}</h3>
          <span v-if="selectedEdge.manual" class="manual-badge">人工覆盖</span>
          <p class="confidence">关系置信度 {{ Math.round(selectedEdge.confidence * 100) }}%</p>
          <article v-for="evidence in selectedEdge.evidence" :key="evidence.chunk_id">
            <strong>原文证据</strong><p>{{ evidence.quote }}</p><small>Chunk {{ evidence.chunk_id }} · {{ Math.round(evidence.confidence * 100) }}%</small>
          </article>
          <div v-if="documentId" class="correction-actions">
            <button v-if="selectedEdge.correction_id" @click="revertCorrection(selectedEdge.correction_id)">撤销人工校正</button>
            <template v-else><button @click="updateEdge(selectedEdge)">修改关系类型</button><button class="danger" @click="hideEdge(selectedEdge)">隐藏关系</button></template>
          </div>
        </template>
        <template v-else-if="entities.length">
          <header><span>实体搜索结果</span></header>
          <article v-for="entity in entities" :key="entity.id" class="entity-result">
            <strong>{{ entity.canonical_name }}</strong>
            <small>{{ entity.entity_type }} · {{ entity.aliases.join('、') || '无别名' }}</small>
            <p>{{ entity.description || '暂无描述' }}</p>
            <button @click="expandEntity(entity)">从此实体展开子图</button>
          </article>
        </template>
        <div v-else class="empty small"><strong>查看细节</strong><span>点击图中的关系可查看证据与置信度，也可以搜索全库实体。</span></div>
      </aside>
    </div>

    <section class="corrections-card">
      <form v-if="documentId && subgraph?.nodes.length" @submit.prevent="addRelation">
        <header><strong>人工补充关系</strong><span>作为 Overlay 保存，不修改机器抽取基础图</span></header>
        <div class="correction-form">
          <select v-model="relationForm.subject_entity_id" required><option value="">起点实体</option><option v-for="node in subgraph.nodes" :key="node.id" :value="node.id">{{ node.label }}</option></select>
          <select v-model="relationForm.predicate"><option v-for="predicate in ['RELATED_TO','DEPENDS_ON','USES','CALLS','CONTAINS','PART_OF','IMPLEMENTS','PROVIDES']" :key="predicate">{{ predicate }}</option></select>
          <select v-model="relationForm.object_entity_id" required><option value="">终点实体</option><option v-for="node in subgraph.nodes" :key="node.id" :value="node.id">{{ node.label }}</option></select>
          <input v-model="relationForm.evidence" required placeholder="原文证据或人工确认依据" />
          <input v-model="relationForm.reason" required minlength="3" placeholder="校正原因" />
          <button>添加关系</button>
        </div>
      </form>
      <div class="history" :class="{ full: !documentId || !subgraph?.nodes.length }">
        <header><strong>校正历史</strong><div><span>{{ corrections.length }} 条</span><button :disabled="loading" @click="reconcileCorrections">检查冲突</button></div></header>
        <div v-if="!corrections.length" class="empty history-empty">暂无人工校正</div>
        <article v-for="correction in corrections.slice(0, 20)" :key="correction.id">
          <div><strong>{{ correction.action }}</strong><p>{{ correction.reason }}</p><p v-if="correction.conflict_reason" class="conflict-reason">{{ correction.conflict_reason }}</p><small>{{ new Date(correction.created_at).toLocaleString() }}</small></div>
          <span :class="correction.status">{{ correction.status }}</span>
          <button v-if="correction.status === 'active' || correction.status === 'conflict'" @click="revertCorrection(correction.id)">撤销</button>
        </article>
      </div>
    </section>
  </div>
</template>

<style scoped>
.graph-page { height: 100%; overflow: auto; padding: 18px; }
.stats-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px; }
.stats-grid article { padding: 14px 16px; border: 1px solid #e1e7f0; border-radius: 11px; background: white; }
.stats-grid span, .stats-grid strong { display: block; }
.stats-grid span { color: #8b97a8; font-size: 10px; }
.stats-grid strong { margin-top: 5px; color: #263249; font-size: 22px; }
.stats-grid .conflict-number { color: #b54708; }
.error { padding: 9px; color: #b42318; background: #fff1f0; border-radius: 8px; }
.graph-toolbar { display: flex; justify-content: space-between; gap: 12px; margin: 14px 0; }
.graph-toolbar label { color: #778598; font-size: 11px; }
.graph-toolbar select, .graph-toolbar input, .graph-toolbar button { margin-left: 8px; padding: 8px 10px; border: 1px solid #dce3ed; border-radius: 8px; background: white; }
.graph-toolbar select { min-width: 260px; }
.graph-toolbar button { margin-left: 5px; color: white; background: #4f6ef7; border-color: #4f6ef7; cursor: pointer; }
.graph-layout { display: flex; gap: 12px; }
.canvas-card, .inspector { height: 485px; border: 1px solid #e1e7f0; border-radius: 12px; background: white; overflow: auto; }
.canvas-card { position: relative; flex: 1; min-width: 0; display: grid; place-items: center; }
.canvas-card svg { width: 100%; height: 100%; }
.load-more { position: absolute; right: 12px; bottom: 12px; padding: 7px 10px; border: 1px solid #cfdaff; border-radius: 8px; color: #3f5ee6; background: #f3f6ff; cursor: pointer; }
.edge { cursor: pointer; }
.edge line { stroke: #9aa9c0; stroke-width: 1.5; }
.edge:hover line { stroke: #4f6ef7; stroke-width: 2.5; }
.edge text { fill: #7c899c; font-size: 8px; text-anchor: middle; paint-order: stroke; stroke: white; stroke-width: 3px; }
.node circle { fill: #eef2ff; stroke: #7890f8; stroke-width: 1.5; }
.node text { fill: #34466b; font-size: 8px; text-anchor: middle; }
.node text.node-type { fill: #8a97a9; font-size: 7px; }
.inspector { width: 310px; flex: 0 0 310px; padding: 14px; }
.inspector header { display: flex; justify-content: space-between; color: #7a879a; font-size: 11px; text-transform: uppercase; }
.inspector header button { border: 0; background: transparent; cursor: pointer; }
.inspector h3 { font-size: 14px; line-height: 1.5; }
.inspector h3 em { color: #4f6ef7; font-style: normal; }
.manual-badge { padding: 3px 7px; border-radius: 9px; color: #6b46c1; background: #f1eafe; font-size: 9px; }
.confidence { color: #7d8a9e; font-size: 11px; }
.inspector article { padding: 11px; margin-top: 9px; border: 1px solid #e7ecf3; border-radius: 8px; }
.inspector article strong { color: #344159; font-size: 11px; }
.inspector article p { color: #6f7c8e; font-size: 11px; line-height: 1.55; }
.inspector article small { display: block; color: #9aa5b4; font-size: 9px; }
.entity-result button { margin-top: 8px; padding: 5px 8px; border: 1px solid #d5dfff; border-radius: 6px; color: #3f5ee6; background: #f3f6ff; cursor: pointer; font-size: 10px; }
.correction-actions { display: flex; gap: 7px; margin-top: 12px; }
.correction-actions button { flex: 1; padding: 7px; border: 1px solid #dce3ed; border-radius: 7px; background: white; color: #536177; cursor: pointer; font-size: 10px; }
.correction-actions button.danger { color: #b42318; }
.corrections-card { display: grid; grid-template-columns: 1.2fr 1fr; gap: 13px; margin-top: 13px; }
.corrections-card > form, .history { padding: 14px; border: 1px solid #e1e7f0; border-radius: 11px; background: white; }
.corrections-card header { display: flex; justify-content: space-between; margin-bottom: 10px; }
.corrections-card header strong { color: #344159; font-size: 12px; }
.corrections-card header span { color: #98a4b4; font-size: 9px; }
.correction-form { display: grid; grid-template-columns: 1fr 130px 1fr; gap: 7px; }
.correction-form input { grid-column: span 2; }
.correction-form input, .correction-form select, .correction-form button { min-width: 0; padding: 8px; border: 1px solid #dce3ed; border-radius: 7px; background: white; font-size: 10px; }
.correction-form button { grid-column: 3; color: white; background: #4f6ef7; border-color: #4f6ef7; cursor: pointer; }
.history { max-height: 230px; overflow: auto; }
.history.full { grid-column: 1 / -1; }
.history header > div { display: flex; align-items: center; gap: 8px; }
.history header button { padding: 4px 8px; border: 1px solid #dce3ed; border-radius: 6px; color: #4f6ef7; background: white; cursor: pointer; font-size: 9px; }
.history article { display: flex; align-items: center; gap: 8px; padding: 8px 0; border-top: 1px solid #edf1f6; }
.history article > div { flex: 1; }
.history article strong, .history article p, .history article small { display: block; }
.history article strong { font-size: 10px; }
.history article p { margin: 2px 0; color: #738095; font-size: 9px; }
.history article p.conflict-reason { color: #b54708; }
.history article small { color: #9ba6b5; font-size: 8px; }
.history article > span { padding: 2px 6px; border-radius: 8px; background: #eef2f7; font-size: 8px; }
.history article > span.active { color: #18794e; background: #e9f8ef; }
.history article > span.conflict { color: #b54708; background: #fff4e5; }
.history article button { border: 0; background: transparent; color: #4f6ef7; cursor: pointer; font-size: 9px; }
.history-empty { padding: 25px; }
.empty { display: flex; flex-direction: column; align-items: center; gap: 6px; color: #97a3b3; font-size: 12px; }
.empty strong { color: #5e6c81; font-size: 14px; }
.empty.small { height: 100%; justify-content: center; text-align: center; line-height: 1.5; }
@media(max-width: 1050px) { .stats-grid { grid-template-columns: repeat(3, 1fr); } }
@media(max-width: 950px) { .stats-grid { grid-template-columns: repeat(2, 1fr); } .graph-layout { display: block; } .inspector { width: 100%; height: auto; min-height: 180px; margin-top: 12px; } .graph-toolbar { flex-direction: column; } .graph-toolbar select { min-width: 180px; } .corrections-card { grid-template-columns: 1fr; } }
</style>
