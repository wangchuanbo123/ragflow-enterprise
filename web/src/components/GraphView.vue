<script setup lang="ts">
import cytoscape, { type Core, type ElementDefinition, type LayoutOptions } from 'cytoscape'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import * as api from '../api/client'
import type {
  DocumentSubgraph,
  DocumentChunk,
  EntitySubgraph,
  GraphCorrection,
  GraphCommunity,
  GraphEntity,
  GraphKind,
  GraphProfile,
  GraphStats,
  KnowledgeDocument,
  SubgraphEdge,
  SubgraphNode,
} from '../types'

const props = defineProps<{ knowledgeBaseId: string }>()
const stats = ref<GraphStats | null>(null)
const graphProfiles = ref<GraphProfile[]>([])
const activeGraphKind = ref<GraphKind>('general')
const documents = ref<KnowledgeDocument[]>([])
const documentId = ref('')
const subgraph = ref<DocumentSubgraph | null>(null)
const entityGraph = ref<EntitySubgraph | null>(null)
const entityQuery = ref('')
const entities = ref<GraphEntity[]>([])
const selectedEdge = ref<SubgraphEdge | null>(null)
const selectedNode = ref<SubgraphNode | null>(null)
const corrections = ref<GraphCorrection[]>([])
const communities = ref<GraphCommunity[]>([])
const documentChunks = ref<DocumentChunk[]>([])
const relationForm = reactive({ subject_entity_id: '', object_entity_id: '', predicate: 'RELATED_TO', chunk_id: '', evidence: '', reason: '' })
const loading = ref(false)
const error = ref('')
const graphContainer = ref<HTMLDivElement | null>(null)
const layoutMode = ref<'cose' | 'concentric' | 'breadthfirst'>('cose')
const densityMode = ref<'overview' | 'complete'>('complete')
const hops = ref<1 | 2>(1)
const minConfidence = ref(0)
const relationTypeFilter = ref('')
const entityTypeFilter = ref('')
const showRelationLabels = ref(false)
const communityMode = ref(false)
let cy: Core | null = null
let resizeObserver: ResizeObserver | null = null

const activeGraph = computed(() => entityGraph.value ?? subgraph.value)
const activeProfile = computed(() => graphProfiles.value.find(item => item.key === activeGraphKind.value) ?? null)
const nodeMap = computed(() => new Map((activeGraph.value?.nodes ?? []).map(node => [node.id, node])))
const relationTypes = computed(() => [...new Set((activeGraph.value?.edges ?? []).map(edge => edge.predicate))].sort())
const entityTypes = computed(() => [...new Set((activeGraph.value?.nodes ?? []).map(node => node.entity_type))].sort())
const topicCommunities = computed(() => communities.value.filter(item => item.level === 0).sort((a, b) => b.members.length - a.members.length || a.title.localeCompare(b.title)))
const budgets = computed(() => densityMode.value === 'overview' ? { nodes: 80, edges: 140 } : { nodes: Number.MAX_SAFE_INTEGER, edges: Number.MAX_SAFE_INTEGER })
const scoreFilterLabel = computed(() => minConfidence.value <= 0 ? '显示全部' : `${Math.round(minConfidence.value * 100)} 分以上`)
const currentGraphTitle = computed(() => {
  const document = documents.value.find(item => item.id === documentId.value)
  if (document) return `${document.original_filename} · ${activeProfile.value?.label ?? '文档知识图谱'}`
  const seed = entityGraph.value?.seed_entity_ids[0]
  return seed ? `${nodeMap.value.get(seed)?.label ?? '实体'} · ${activeProfile.value?.label ?? '关联知识图谱'}` : (activeProfile.value?.label ?? '知识图谱')
})
const displayedTypeStats = computed(() => {
  const counts = new Map<string, number>()
  projectedGraph.value.nodes.forEach(node => counts.set(node.entity_type, (counts.get(node.entity_type) ?? 0) + 1))
  return [...counts.entries()].map(([type, count]) => ({ type, count, color: typeColor(type) })).sort((a, b) => b.count - a.count || a.type.localeCompare(b.type))
})
const usingFastLargeLayout = computed(() => layoutMode.value === 'cose' && projectedGraph.value.nodes.length > 220)

const typePalette = ['#f3883b', '#339cff', '#8b6de9', '#5dc977', '#eb76b1', '#3ab9b1', '#4776bd', '#d87932']
function typeColor(type: string): string {
  const configured = activeProfile.value?.entity_types.find(item => item.name === type)?.color
    ?? graphProfiles.value.flatMap(item => item.entity_types).find(item => item.name === type)?.color
  if (configured) return configured
  let hash = 0
  for (const char of type) hash = ((hash << 5) - hash + char.charCodeAt(0)) | 0
  return typePalette[Math.abs(hash) % typePalette.length]
}

function relationColor(predicate: string): string {
  const profile = graphProfiles.value.find(item => item.relation_types.some(relation => relation.name === predicate))
  return profile?.color ?? '#9aa9bc'
}

function entityTypeLabel(type: string): string {
  return activeProfile.value?.entity_types.find(item => item.name === type)?.label ?? type
}

function relationTypeLabel(predicate: string): string {
  return activeProfile.value?.relation_types.find(item => item.name === predicate)?.label ?? predicate
}

function relationScoreText(edge: SubgraphEdge): string {
  return edge.manual ? '人工确认' : `模型评分 ${Math.round(edge.confidence * 100)} 分`
}

function evidenceScoreText(edge: SubgraphEdge, score: number): string {
  return edge.manual ? '人工证据' : `抽取评分 ${Math.round(score * 100)} 分`
}

const projectedGraph = computed(() => {
  const graph = activeGraph.value
  if (!graph) return { nodes: [] as SubgraphNode[], edges: [] as SubgraphEdge[], hiddenNodes: 0, hiddenEdges: 0 }
  const allowedNodes = new Map(
    graph.nodes
      .filter(node => !entityTypeFilter.value || node.entity_type === entityTypeFilter.value)
      .map(node => [node.id, node]),
  )
  const candidates = graph.edges
    .filter(edge => edge.confidence >= minConfidence.value)
    .filter(edge => !relationTypeFilter.value || edge.predicate === relationTypeFilter.value)
    .filter(edge => allowedNodes.has(edge.source) && allowedNodes.has(edge.target))
    .sort((a, b) => Number(Boolean(b.manual)) - Number(Boolean(a.manual)) || b.confidence - a.confidence || b.evidence.length - a.evidence.length || a.id.localeCompare(b.id))

  const visibleNodeIds = new Set<string>()
  if (entityGraph.value) {
    for (const id of entityGraph.value.seed_entity_ids) if (allowedNodes.has(id)) visibleNodeIds.add(id)
  }
  const visibleEdges: SubgraphEdge[] = []
  for (const edge of candidates) {
    if (visibleEdges.length >= budgets.value.edges) break
    const additions = [edge.source, edge.target].filter(id => !visibleNodeIds.has(id))
    if (visibleNodeIds.size + additions.length > budgets.value.nodes) continue
    visibleEdges.push(edge)
    additions.forEach(id => visibleNodeIds.add(id))
  }

  const remainingNodes = [...allowedNodes.values()]
    .filter(node => !visibleNodeIds.has(node.id))
    .sort((a, b) => b.mention_count - a.mention_count || a.label.localeCompare(b.label))
  for (const node of remainingNodes) {
    if (visibleNodeIds.size >= budgets.value.nodes) break
    visibleNodeIds.add(node.id)
  }
  const nodes = [...visibleNodeIds].map(id => allowedNodes.get(id)).filter((node): node is SubgraphNode => Boolean(node))
  return {
    nodes,
    edges: visibleEdges,
    hiddenNodes: Math.max(0, allowedNodes.size - nodes.length),
    hiddenEdges: Math.max(0, candidates.length - visibleEdges.length),
  }
})

function layoutOptions(): LayoutOptions {
  if (layoutMode.value === 'breadthfirst') return { name: 'breadthfirst', directed: true, spacingFactor: 1.45, padding: 45, animate: false }
  if (layoutMode.value === 'concentric' || usingFastLargeLayout.value) return { name: 'concentric', minNodeSpacing: 54, spacingFactor: 1.18, padding: 65, animate: false, fit: true }
  return {
    name: 'cose', animate: false, fit: true, padding: 65, randomize: true,
    nodeRepulsion: () => 8500, idealEdgeLength: () => 105, edgeElasticity: () => 90,
    nestingFactor: 1.15, gravity: 0.18, numIter: 480,
  }
}

function highlightNeighborhood(nodeId: string): void {
  if (!cy) return
  cy.elements().removeClass('faded focused')
  const neighborhood = cy.getElementById(nodeId).closedNeighborhood()
  cy.elements().difference(neighborhood).addClass('faded')
  neighborhood.addClass('focused')
}

function renderGraph(): void {
  if (!graphContainer.value) return
  const elements: ElementDefinition[] = [
    ...projectedGraph.value.nodes.map(node => ({ data: {
      id: node.id, label: node.label, entityType: node.entity_type, color: typeColor(node.entity_type),
      mentionCount: node.mention_count, seed: entityGraph.value?.seed_entity_ids.includes(node.id) ? 1 : 0,
    } })),
    ...projectedGraph.value.edges.map(edge => ({ data: {
      id: edge.id, source: edge.source, target: edge.target, label: edge.predicate,
      displayLabel: showRelationLabels.value ? relationTypeLabel(edge.predicate) : '', confidence: edge.confidence,
      edgeColor: relationColor(edge.predicate), manual: edge.manual ? 1 : 0,
    } })),
  ]
  cy?.destroy()
  cy = cytoscape({
    container: graphContainer.value,
    elements,
    wheelSensitivity: 0.24,
    minZoom: 0.08,
    maxZoom: 4,
    hideEdgesOnViewport: true,
    textureOnViewport: true,
    motionBlur: false,
    boxSelectionEnabled: false,
    selectionType: 'single',
    style: [
      { selector: 'node', style: {
        'width': 'mapData(mentionCount, 0, 20, 40, 58)', 'height': 'mapData(mentionCount, 0, 20, 40, 58)',
        'background-color': 'data(color)', 'background-opacity': 0.94, 'border-color': '#ffffff', 'border-width': 3,
        'label': 'data(label)', 'font-size': 10, 'font-weight': 650, 'color': '#273142', 'text-wrap': 'ellipsis',
        'text-max-width': '112px', 'text-valign': 'bottom', 'text-halign': 'center', 'text-margin-y': 8,
        'text-background-color': '#ffffff', 'text-background-opacity': 0.78, 'text-background-padding': '2px', 'overlay-opacity': 0,
      } },
      { selector: 'node[seed = 1]', style: { 'border-color': '#273f7a', 'border-width': 4, 'background-opacity': 1 } },
      { selector: 'node:selected', style: { 'border-color': '#263f8d', 'border-width': 5, 'background-opacity': 1 } },
      { selector: 'edge', style: {
        'width': 'mapData(confidence, 0, 1, 1, 2.4)', 'line-color': 'data(edgeColor)', 'target-arrow-color': 'data(edgeColor)', 'opacity': 0.64,
        'target-arrow-shape': 'triangle', 'arrow-scale': 0.72, 'curve-style': 'bezier', 'control-point-step-size': 36,
        'label': 'data(displayLabel)', 'font-size': 8, 'color': '#68768a', 'text-background-color': '#ffffff',
        'text-background-opacity': 0.9, 'text-background-padding': '2px', 'text-rotation': 'autorotate', 'overlay-opacity': 0,
      } },
      { selector: 'edge[confidence < 0.7]', style: { 'line-style': 'dashed', 'opacity': 0.62 } },
      { selector: 'edge[manual = 1]', style: { 'line-color': '#9a60b4', 'target-arrow-color': '#9a60b4', 'line-style': 'solid' } },
      { selector: 'edge:selected, edge.hovered', style: {
        'line-color': '#3155c6', 'target-arrow-color': '#3155c6', 'width': 4, 'label': 'data(label)', 'z-index': 10,
      } },
      { selector: '.faded', style: { 'opacity': 0.16, 'text-opacity': 0.08 } },
      { selector: '.focused', style: { 'z-index': 5 } },
    ],
    layout: layoutOptions(),
  })
  cy.on('tap', 'edge', event => {
    selectedNode.value = null
    selectedEdge.value = projectedGraph.value.edges.find(edge => edge.id === event.target.id()) ?? null
  })
  cy.on('mouseover', 'edge', event => event.target.addClass('hovered'))
  cy.on('mouseout', 'edge', event => event.target.removeClass('hovered'))
  cy.on('tap', 'node', event => {
    selectedEdge.value = null
    selectedNode.value = nodeMap.value.get(event.target.id()) ?? null
    highlightNeighborhood(event.target.id())
  })
  cy.on('tap', event => {
    if (event.target !== cy) return
    selectedNode.value = null
    selectedEdge.value = null
    cy?.elements().removeClass('faded focused')
  })
}

async function load(): Promise<void> {
  loading.value = true
  try {
    ;[stats.value, documents.value, corrections.value, communities.value, graphProfiles.value] = await Promise.all([
      api.getGraphStats(props.knowledgeBaseId), api.listDocuments(props.knowledgeBaseId), api.listGraphCorrections(props.knowledgeBaseId), api.listGraphCommunities(props.knowledgeBaseId), api.listGraphProfiles(props.knowledgeBaseId),
    ])
    if (documentId.value && !documents.value.some(item => item.id === documentId.value)) documentId.value = ''
    if ((stats.value?.entities ?? 0) > 100 && topicCommunities.value.length && !activeGraph.value) communityMode.value = true
    error.value = ''
  } catch (e) { error.value = (e as Error).message }
  finally { loading.value = false }
}

async function loadSubgraph(): Promise<void> {
  selectedEdge.value = null
  selectedNode.value = null
  if (!documentId.value) { subgraph.value = null; documentChunks.value = []; return }
  entityGraph.value = null
  communityMode.value = false
  try { [subgraph.value, documentChunks.value] = await Promise.all([api.getDocumentSubgraph(props.knowledgeBaseId, documentId.value, 1000, activeGraphKind.value), api.listDocumentChunks(props.knowledgeBaseId, documentId.value)]) }
  catch (e) { error.value = (e as Error).message }
}

async function search(): Promise<void> {
  if (!entityQuery.value.trim()) { entities.value = []; return }
  try { entities.value = await api.searchGraphEntities(props.knowledgeBaseId, entityQuery.value.trim(), activeGraphKind.value) }
  catch (e) { error.value = (e as Error).message }
}

async function expandEntity(entity: GraphEntity | SubgraphNode): Promise<void> {
  loading.value = true
  selectedEdge.value = null
  selectedNode.value = null
  communityMode.value = false
  documentId.value = ''
  subgraph.value = null
  try {
    entityGraph.value = await api.getEntitySubgraph(props.knowledgeBaseId, [entity.id], 0, 200, hops.value, activeGraphKind.value)
    error.value = ''
  } catch (e) { error.value = (e as Error).message }
  finally { loading.value = false }
}

async function openCommunity(community: GraphCommunity): Promise<void> {
  const representative = community.members[0]
  if (!representative) return
  await expandEntity({
    id: representative.id,
    label: representative.name,
    entity_type: representative.entity_type,
    description: community.summary,
    mention_count: 0,
  })
}

async function loadMoreEntityGraph(): Promise<void> {
  const graph = entityGraph.value
  if (!graph || graph.next_offset === null) return
  loading.value = true
  try {
    const next = await api.getEntitySubgraph(props.knowledgeBaseId, graph.seed_entity_ids, graph.next_offset, 200, hops.value, activeGraphKind.value)
    const nodes = new Map(graph.nodes.map(node => [node.id, node]))
    const edges = new Map(graph.edges.map(edge => [edge.id, edge]))
    next.nodes.forEach(node => nodes.set(node.id, node))
    next.edges.forEach(edge => edges.set(edge.id, edge))
    entityGraph.value = { ...next, nodes: [...nodes.values()], edges: [...edges.values()] }
  } catch (e) { error.value = (e as Error).message }
  finally { loading.value = false }
}

async function refreshOverlay(): Promise<void> {
  ;[stats.value, corrections.value] = await Promise.all([api.getGraphStats(props.knowledgeBaseId), api.listGraphCorrections(props.knowledgeBaseId)])
  if (documentId.value) await loadSubgraph()
  else if (entityGraph.value?.seed_entity_ids[0]) {
    const seed = nodeMap.value.get(entityGraph.value.seed_entity_ids[0])
    if (seed) await expandEntity(seed)
  }
}

async function reconcileCorrections(): Promise<void> {
  loading.value = true
  try { await api.reconcileGraphCorrections(props.knowledgeBaseId); await refreshOverlay(); error.value = '' }
  catch (e) { error.value = (e as Error).message }
  finally { loading.value = false }
}

async function hideEdge(edge: SubgraphEdge): Promise<void> {
  const reason = window.prompt('请填写隐藏该关系的原因（将写入审计记录）')?.trim()
  if (!reason) return
  try { await api.createGraphCorrection(props.knowledgeBaseId, { action: 'hide_relation', target_id: edge.id, reason }); await refreshOverlay() }
  catch (e) { error.value = (e as Error).message }
}

async function updateEdge(edge: SubgraphEdge): Promise<void> {
  const predicate = window.prompt('修正后的关系类型', edge.predicate)?.trim()
  if (!predicate || predicate === edge.predicate) return
  const reason = window.prompt('请填写修改原因（将写入审计记录）')?.trim()
  if (!reason) return
  try { await api.createGraphCorrection(props.knowledgeBaseId, { action: 'update_relation', target_id: edge.id, payload: { predicate }, reason }); await refreshOverlay() }
  catch (e) { error.value = (e as Error).message }
}

async function addRelation(): Promise<void> {
  if (!documentId.value) return
  try {
    await api.createGraphCorrection(props.knowledgeBaseId, { action: 'add_relation', reason: relationForm.reason, payload: { ...relationForm, document_id: documentId.value, confidence: 1 } })
    Object.assign(relationForm, { subject_entity_id: '', object_entity_id: '', predicate: 'RELATED_TO', chunk_id: '', evidence: '', reason: '' })
    await refreshOverlay()
  } catch (e) { error.value = (e as Error).message }
}

function useSelectedChunkEvidence(): void {
  const chunk = documentChunks.value.find(item => item.id === relationForm.chunk_id)
  relationForm.evidence = chunk?.content.slice(0, 1000) || ''
}

async function revertCorrection(correctionId: string): Promise<void> {
  try { await api.revertGraphCorrection(props.knowledgeBaseId, correctionId); await refreshOverlay() }
  catch (e) { error.value = (e as Error).message }
}

function fitGraph(): void { cy?.fit(undefined, 42) }
function zoomGraph(factor: number): void {
  if (!cy || !graphContainer.value) return
  const level = Math.max(cy.minZoom(), Math.min(cy.maxZoom(), cy.zoom() * factor))
  cy.zoom({ level, renderedPosition: { x: graphContainer.value.clientWidth / 2, y: graphContainer.value.clientHeight / 2 } })
}
function resetFocus(): void { selectedNode.value = null; selectedEdge.value = null; cy?.elements().removeClass('faded focused'); cy?.fit(undefined, 42) }
async function selectGraphKind(kind: GraphKind): Promise<void> {
  if (activeGraphKind.value === kind) return
  activeGraphKind.value = kind
  const profile = graphProfiles.value.find(item => item.key === kind)
  if (profile) layoutMode.value = profile.recommended_layout
  relationTypeFilter.value = ''
  entityTypeFilter.value = ''
  selectedNode.value = null
  selectedEdge.value = null
  entities.value = []
  entityGraph.value = null
  if (documentId.value) await loadSubgraph()
  else subgraph.value = null
}
async function toggleFullscreen(): Promise<void> {
  if (!graphContainer.value) return
  if (document.fullscreenElement) await document.exitFullscreen()
  else await graphContainer.value.parentElement?.requestFullscreen()
  window.setTimeout(() => { cy?.resize(); cy?.fit(undefined, 42) }, 120)
}

watch(() => props.knowledgeBaseId, () => {
  documentId.value = ''; subgraph.value = null; entityGraph.value = null; entities.value = []; communityMode.value = false; void load()
})
watch(documentId, loadSubgraph)
watch([projectedGraph, layoutMode], () => { void nextTick(renderGraph) }, { deep: true })
watch(showRelationLabels, value => { cy?.edges().forEach(edge => { edge.data('displayLabel', value ? relationTypeLabel(edge.data('label')) : '') }) })
watch([relationTypeFilter, entityTypeFilter], () => { selectedEdge.value = null; selectedNode.value = null })
onMounted(async () => {
  await load()
  if (activeProfile.value) layoutMode.value = activeProfile.value.recommended_layout
  await nextTick()
  resizeObserver = new ResizeObserver(() => cy?.resize())
  if (graphContainer.value) resizeObserver.observe(graphContainer.value)
})
onBeforeUnmount(() => { resizeObserver?.disconnect(); cy?.destroy(); cy = null })
</script>

<template>
  <div class="graph-page">
    <div class="stats-grid">
      <article><span>实体</span><strong>{{ stats?.entities ?? 0 }}</strong></article><article><span>关系</span><strong>{{ stats?.relations ?? 0 }}</strong></article><article><span>实体提及</span><strong>{{ stats?.mentions ?? 0 }}</strong></article><article><span>证据片段</span><strong>{{ stats?.evidence ?? 0 }}</strong></article><article><span>人工校正</span><strong>{{ stats?.active_corrections ?? 0 }}</strong></article><article><span>冲突校正</span><strong class="conflict-number">{{ stats?.conflict_corrections ?? 0 }}</strong></article>
    </div>
    <p v-if="error" class="error">{{ error }}</p>

    <section class="graph-catalog">
      <header><div><strong>知识图谱</strong><small>保留原有按文档查看的通用图谱，并提供四类专业视图；底层规范实体共享，Schema 分别管理。</small></div><span>五类图谱 · 五套独立 Schema</span></header>
      <nav>
        <button v-for="profile in graphProfiles" :key="profile.key" :class="{active:activeGraphKind===profile.key}" :style="{'--profile-color':profile.color}" @click="selectGraphKind(profile.key)">
          <i>{{profile.short_label.slice(0,1)}}</i><span><b>{{profile.label}}</b><small>{{profile.entity_count}} 个实体 · {{profile.relation_count}} 条关系</small></span>
        </button>
      </nav>
      <div v-if="activeProfile" class="profile-context">
        <span class="profile-purpose" :style="{borderColor:activeProfile.color}"><b>定位</b>{{activeProfile.positioning}}</span>
        <span class="profile-purpose"><b>数据范围</b>{{activeProfile.purpose}}</span>
        <details><summary>查看抽取规则与 Schema 范围</summary><p v-for="rule in activeProfile.extraction_rules" :key="rule">{{rule}}</p><small>{{activeProfile.entity_types.length}} 类实体 · {{activeProfile.relation_types.length}} 类关系 · 推荐{{activeProfile.recommended_layout==='cose'?'智能避让':activeProfile.recommended_layout==='concentric'?'中心辐射':'层级关系'}}布局</small></details>
      </div>
      <div class="knowledge-flow"><span>通用文档保留完整事实</span><i>→</i><span>概念统一术语</span><i>→</i><span>功能分配职责</span><i>→</i><span>配置项落实实现</span><i>→</i><span>行业标准建立符合性</span></div>
    </section>

    <div class="graph-toolbar">
      <label>按文档查看子图 <select v-model="documentId"><option value="">请选择文档</option><option v-for="document in documents" :key="document.id" :value="document.id">{{ document.original_filename }}</option></select></label>
      <form @submit.prevent="search"><input v-model="entityQuery" :placeholder="`在${activeProfile?.label||'知识图谱'}中搜索实体`" /><button>搜索</button></form>
    </div>

    <div class="view-toolbar">
      <label>展开范围<select v-model="hops"><option :value="1">一跳（推荐）</option><option :value="2">两跳</option></select></label>
      <label>显示范围<select v-model="densityMode"><option value="complete">完整图谱</option><option value="overview">精简视图 · 80/140</option></select></label>
      <label>布局<select v-model="layoutMode"><option value="cose">智能避让</option><option value="concentric">中心辐射</option><option value="breadthfirst">层级关系</option></select></label>
      <label>实体类型<select v-model="entityTypeFilter"><option value="">全部</option><option v-for="item in entityTypes" :key="item" :value="item">{{ entityTypeLabel(item) }}</option></select></label>
      <label>关系类型<select v-model="relationTypeFilter"><option value="">全部</option><option v-for="item in relationTypes" :key="item" :value="item">{{ relationTypeLabel(item) }}</option></select></label>
      <label class="confidence-filter">模型评分筛选 <b>{{ scoreFilterLabel }}</b><input v-model.number="minConfidence" type="range" min="0" max="1" step="0.05" /></label>
      <label class="check"><input v-model="showRelationLabels" type="checkbox" />常显关系名</label>
      <button :class="{enabled:communityMode}" @click="communityMode=!communityMode">{{communityMode?'返回实体图':'主题总览'}}</button><button title="缩小" @click="zoomGraph(0.78)">−</button><button title="放大" @click="zoomGraph(1.28)">＋</button><button @click="fitGraph">适应画布</button><button @click="resetFocus">复位</button><button @click="toggleFullscreen">全屏</button>
    </div>

    <div v-if="!communityMode" class="graph-layout">
      <section class="canvas-card">
        <header v-if="activeGraph" class="canvas-heading"><div><strong>{{ currentGraphTitle }}</strong><small>实体按 Schema 类型着色 · 滚轮缩放 · 拖拽查看 · 点击聚焦邻域</small></div><span>{{ projectedGraph.nodes.length }} 个实体 · {{ projectedGraph.edges.length }} 条关系</span></header>
        <div v-if="loading && !activeGraph" class="empty overlay"><strong>正在读取知识图谱…</strong></div>
        <div v-else-if="!activeGraph" class="empty overlay"><strong>选择文档或搜索实体</strong><span>默认只展示一跳重要关系，点击节点可聚焦，再从详情面板继续展开。</span></div>
        <div v-else-if="!activeGraph.nodes.length" class="empty overlay"><strong>当前范围尚无子图</strong><span>可在索引任务页重新执行图谱构建。</span></div>
        <div v-show="activeGraph?.nodes.length" ref="graphContainer" class="cy-canvas" role="img" aria-label="可缩放和拖拽的知识图谱子图"></div>
        <div v-if="activeGraph && displayedTypeStats.length" class="type-legend"><strong>实体类型</strong><span v-for="item in displayedTypeStats.slice(0,8)" :key="item.type"><i :style="{background:item.color}"></i>{{entityTypeLabel(item.type)}} <b>{{item.count}}</b></span><small v-if="displayedTypeStats.length>8">其余 {{displayedTypeStats.length-8}} 类</small></div>
        <div v-if="activeGraph" class="graph-summary">显示 {{ projectedGraph.nodes.length }} / {{ activeGraph.nodes.length }} 个实体、{{ projectedGraph.edges.length }} / {{ activeGraph.edges.length }} 条关系<span v-if="usingFastLargeLayout"> · 大图快速布局</span><span v-if="projectedGraph.hiddenNodes || projectedGraph.hiddenEdges"> · 精简 {{ projectedGraph.hiddenNodes }} 个实体、{{ projectedGraph.hiddenEdges }} 条关系</span></div>
        <button v-if="entityGraph?.next_offset !== null && entityGraph" class="load-more" :disabled="loading" @click="loadMoreEntityGraph">加载更多候选关系</button>
      </section>

      <aside v-if="selectedEdge || selectedNode || entities.length" class="inspector">
        <template v-if="selectedEdge"><header><span>关系证据</span><button @click="selectedEdge = null">×</button></header><h3>{{ nodeMap.get(selectedEdge.source)?.label }} <em>—{{ relationTypeLabel(selectedEdge.predicate) }}→</em> {{ nodeMap.get(selectedEdge.target)?.label }}</h3><small>{{selectedEdge.predicate}}</small><span v-if="selectedEdge.manual" class="manual-badge">人工确认</span><p v-else class="confidence">{{ relationScoreText(selectedEdge) }}<small>仅用于筛选和排序，不代表正确概率</small></p><article v-for="evidence in selectedEdge.evidence" :key="evidence.chunk_id"><strong>原文证据</strong><p>{{ evidence.quote }}</p><small>Chunk {{ evidence.chunk_id }} · {{ evidenceScoreText(selectedEdge, evidence.confidence) }}</small></article><div class="correction-actions"><button v-if="selectedEdge.correction_id" @click="revertCorrection(selectedEdge.correction_id)">撤销人工校正</button><template v-else><button @click="updateEdge(selectedEdge)">修改关系</button><button class="danger" @click="hideEdge(selectedEdge)">隐藏关系</button></template></div></template>
        <template v-else-if="selectedNode"><header><span>实体详情</span><button @click="selectedNode = null; resetFocus()">×</button></header><div class="entity-detail"><span class="entity-dot" :style="{ background: typeColor(selectedNode.entity_type) }"></span><h3>{{ selectedNode.label }}</h3><small>{{ entityTypeLabel(selectedNode.entity_type) }} · {{selectedNode.entity_type}} · {{ selectedNode.mention_count }} 次提及</small><p>{{ selectedNode.description || '暂无实体描述' }}</p><button @click="expandEntity(selectedNode)">从此实体展开{{ hops }}跳关系</button></div></template>
        <template v-else-if="entities.length"><header><span>实体搜索结果</span></header><article v-for="entity in entities" :key="entity.id" class="entity-result"><strong>{{ entity.canonical_name }}</strong><small>{{ entity.entity_type }} · {{ entity.aliases.join('、') || '无别名' }}</small><p>{{ entity.description || '暂无描述' }}</p><button @click="expandEntity(entity)">从此实体展开子图</button></article></template>
      </aside>
    </div>
    <section v-else class="community-overview">
      <header><div><strong>主题社区总览</strong><small>大型知识图谱先按主题浏览，再进入代表实体逐步展开；不会一次摊开全部节点。</small></div><span>{{topicCommunities.length}} 个主题</span></header>
      <div v-if="!topicCommunities.length" class="empty community-empty"><strong>尚未生成主题社区</strong><span>请在“图谱治理”中执行社区重建。</span></div>
      <div v-else class="community-grid">
        <article v-for="community in topicCommunities" :key="community.id">
          <div class="community-title"><span>{{community.members.length}}</span><div><b>{{community.title}}</b><small>{{community.members.length}} 个实体</small></div></div>
          <p>{{community.summary}}</p>
          <div class="member-chips"><button v-for="member in community.members.slice(0,8)" :key="member.id" @click="expandEntity({id:member.id,label:member.name,entity_type:member.entity_type,description:null,mention_count:0})">{{member.name}}</button><span v-if="community.members.length>8">还有 {{community.members.length-8}} 个</span></div>
          <button class="enter-community" :disabled="!community.members.length" @click="openCommunity(community)">进入主题子图</button>
        </article>
      </div>
    </section>

    <section class="corrections-card">
      <form v-if="documentId && subgraph?.nodes.length" @submit.prevent="addRelation"><header><strong>人工补充关系</strong><span>绑定真实 Chunk，保存后可被检索和引用</span></header><div class="correction-form"><select v-model="relationForm.subject_entity_id" required><option value="">起点实体</option><option v-for="node in subgraph.nodes" :key="node.id" :value="node.id">{{ node.label }}</option></select><select v-model="relationForm.predicate"><option v-for="predicate in ['RELATED_TO','DEPENDS_ON','USES','CALLS','CONTAINS','PART_OF','IMPLEMENTS','PROVIDES']" :key="predicate">{{ predicate }}</option></select><select v-model="relationForm.object_entity_id" required><option value="">终点实体</option><option v-for="node in subgraph.nodes" :key="node.id" :value="node.id">{{ node.label }}</option></select><select v-model="relationForm.chunk_id" required @change="useSelectedChunkEvidence"><option value="">来源 Chunk</option><option v-for="chunk in documentChunks" :key="chunk.id" :value="chunk.id">Chunk {{chunk.chunk_index+1}} · {{chunk.content.replace(/\s+/g,' ').slice(0,60)}}</option></select><input v-model="relationForm.evidence" required placeholder="所选 Chunk 中的原文证据" /><input v-model="relationForm.reason" required minlength="3" placeholder="校正原因" /><button>添加关系</button></div></form>
      <div class="history" :class="{ full: !documentId || !subgraph?.nodes.length }"><header><strong>校正历史</strong><div><span>{{ corrections.length }} 条</span><button :disabled="loading" @click="reconcileCorrections">检查冲突</button></div></header><div v-if="!corrections.length" class="empty history-empty">暂无人工校正</div><article v-for="correction in corrections.slice(0, 20)" :key="correction.id"><div><strong>{{ correction.action }}</strong><p>{{ correction.reason }}</p><p v-if="correction.conflict_reason" class="conflict-reason">{{ correction.conflict_reason }}</p><small>{{ new Date(correction.created_at).toLocaleString() }}</small></div><span :class="correction.status">{{ correction.status }}</span><button v-if="correction.status === 'active' || correction.status === 'conflict'" @click="revertCorrection(correction.id)">撤销</button></article></div>
    </section>
  </div>
</template>

<style scoped>
.graph-page{height:100%;overflow:auto;padding:18px;color:#344159;background:#f7f9fc}.stats-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:10px}.stats-grid article{padding:14px 16px;border:1px solid #e1e7f0;border-radius:11px;background:white}.stats-grid span,.stats-grid strong{display:block}.stats-grid span{color:#8b97a8;font-size:10px}.stats-grid strong{margin-top:5px;color:#263249;font-size:22px}.stats-grid .conflict-number{color:#b54708}.error{padding:9px;color:#b42318;background:#fff1f0;border-radius:8px}.graph-toolbar{display:flex;justify-content:space-between;gap:12px;margin:14px 0}.graph-toolbar label{color:#778598;font-size:11px}.graph-toolbar select,.graph-toolbar input,.graph-toolbar button,.view-toolbar select,.view-toolbar button{margin-left:8px;padding:8px 10px;border:1px solid #dce3ed;border-radius:8px;background:white}.graph-toolbar select{min-width:260px}.graph-toolbar button{margin-left:5px;color:white;background:#4f6ef7;border-color:#4f6ef7;cursor:pointer}.view-toolbar{display:flex;align-items:flex-end;gap:7px;flex-wrap:wrap;padding:9px;margin-bottom:12px;border:1px solid #e1e7f0;border-radius:10px;background:white}.view-toolbar label{display:grid;gap:4px;color:#7b8799;font-size:9px}.view-toolbar select{min-width:112px;margin:0;padding:6px}.view-toolbar button{margin:0;padding:7px 9px;color:#4561d8;cursor:pointer}.view-toolbar .confidence-filter{min-width:145px}.confidence-filter input{width:145px}.view-toolbar .check{display:flex;flex-direction:row;align-items:center;gap:5px;padding:7px}.view-toolbar .check input{width:auto}.graph-layout{display:flex;gap:12px}.canvas-card,.inspector{height:520px;border:1px solid #e1e7f0;border-radius:12px;background:white;overflow:auto}.canvas-card{position:relative;flex:1;min-width:0;overflow:hidden}.cy-canvas{width:100%;height:100%;background:radial-gradient(circle at 50% 45%,#fff,#f6f8fc)}.overlay{position:absolute;z-index:2;inset:0;background:white}.graph-summary{position:absolute;left:12px;bottom:10px;max-width:70%;padding:5px 8px;border:1px solid #e0e6ef;border-radius:8px;color:#7a8799;background:#ffffffe8;font-size:9px;pointer-events:none}.load-more{position:absolute;right:12px;bottom:10px;padding:7px 10px;border:1px solid #cfdaff;border-radius:8px;color:#3f5ee6;background:#f3f6ff;cursor:pointer}.inspector{width:320px;flex:0 0 320px;padding:14px}.inspector header{display:flex;justify-content:space-between;color:#7a879a;font-size:11px;text-transform:uppercase}.inspector header button{border:0;background:transparent;cursor:pointer}.inspector h3{font-size:14px;line-height:1.5}.inspector h3 em{color:#4f6ef7;font-style:normal}.manual-badge{padding:3px 7px;border-radius:9px;color:#6b46c1;background:#f1eafe;font-size:9px}.confidence{color:#7d8a9e;font-size:11px}.inspector article{padding:11px;margin-top:9px;border:1px solid #e7ecf3;border-radius:8px}.inspector article strong{font-size:11px}.inspector article p,.entity-detail p{color:#6f7c8e;font-size:11px;line-height:1.55}.inspector article small,.entity-detail small{display:block;color:#9aa5b4;font-size:9px}.entity-result button,.entity-detail button{margin-top:8px;padding:6px 9px;border:1px solid #d5dfff;border-radius:6px;color:#3f5ee6;background:#f3f6ff;cursor:pointer;font-size:10px}.entity-dot{display:inline-block;width:10px;height:10px;margin-right:5px;border-radius:50%}.entity-detail h3{display:inline}.correction-actions{display:flex;gap:7px;margin-top:12px}.correction-actions button{flex:1;padding:7px;border:1px solid #dce3ed;border-radius:7px;background:white;color:#536177;cursor:pointer;font-size:10px}.correction-actions button.danger{color:#b42318}.corrections-card{display:grid;grid-template-columns:1.2fr 1fr;gap:13px;margin-top:13px}.corrections-card>form,.history{padding:14px;border:1px solid #e1e7f0;border-radius:11px;background:white}.corrections-card header{display:flex;justify-content:space-between;margin-bottom:10px}.corrections-card header strong{font-size:12px}.corrections-card header span{color:#98a4b4;font-size:9px}.correction-form{display:grid;grid-template-columns:1fr 130px 1fr;gap:7px}.correction-form input{grid-column:span 2}.correction-form input,.correction-form select,.correction-form button{min-width:0;padding:8px;border:1px solid #dce3ed;border-radius:7px;background:white;font-size:10px}.correction-form button{grid-column:3;color:white;background:#4f6ef7;border-color:#4f6ef7;cursor:pointer}.history{max-height:230px;overflow:auto}.history.full{grid-column:1/-1}.history header>div{display:flex;align-items:center;gap:8px}.history header button{padding:4px 8px;border:1px solid #dce3ed;border-radius:6px;color:#4f6ef7;background:white;cursor:pointer;font-size:9px}.history article{display:flex;align-items:center;gap:8px;padding:8px 0;border-top:1px solid #edf1f6}.history article>div{flex:1}.history article strong,.history article p,.history article small{display:block}.history article strong{font-size:10px}.history article p{margin:2px 0;color:#738095;font-size:9px}.history article p.conflict-reason{color:#b54708}.history article small{color:#9ba6b5;font-size:8px}.history article>span{padding:2px 6px;border-radius:8px;background:#eef2f7;font-size:8px}.history article>span.active{color:#18794e;background:#e9f8ef}.history article>span.conflict{color:#b54708;background:#fff4e5}.history article button{border:0;background:transparent;color:#4f6ef7;cursor:pointer;font-size:9px}.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;color:#97a3b3;font-size:12px}.empty strong{color:#5e6c81;font-size:14px}.empty.small{height:100%;text-align:center;line-height:1.5}.history-empty{padding:25px}@media(max-width:1100px){.stats-grid{grid-template-columns:repeat(3,1fr)}}@media(max-width:950px){.stats-grid{grid-template-columns:repeat(2,1fr)}.graph-layout{display:block}.inspector{box-sizing:border-box;width:100%;height:auto;min-height:180px;margin-top:12px}.graph-toolbar{flex-direction:column}.graph-toolbar select{min-width:180px}.corrections-card{grid-template-columns:1fr}}
.view-toolbar button.enabled{color:#fff;border-color:#4f6ef7;background:#4f6ef7}.canvas-card:fullscreen{width:100vw;height:100vh;border:0;border-radius:0}.community-overview{min-height:480px;padding:16px;border:1px solid #e1e7f0;border-radius:12px;background:#fff}.community-overview>header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}.community-overview>header strong,.community-overview>header small{display:block}.community-overview>header small{margin-top:4px;color:#8995a7;font-size:10px}.community-overview>header>span{color:#637187;font-size:10px}.community-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.community-grid>article{display:flex;min-height:190px;flex-direction:column;padding:13px;border:1px solid #e4eaf3;border-radius:11px;background:linear-gradient(145deg,#fff,#f7f9ff)}.community-title{display:flex;align-items:center;gap:9px}.community-title>span{display:grid;width:38px;height:38px;flex:0 0 38px;place-items:center;border-radius:11px;color:#4561d8;background:#e9eeff;font-size:13px;font-weight:700}.community-title b,.community-title small{display:block}.community-title b{overflow:hidden;color:#344159;font-size:11px;text-overflow:ellipsis;white-space:nowrap}.community-title small{margin-top:3px;color:#8a97a9;font-size:9px}.community-grid article>p{display:-webkit-box;overflow:hidden;margin:10px 0;color:#6f7c8e;font-size:10px;line-height:1.5;-webkit-box-orient:vertical;-webkit-line-clamp:3}.member-chips{display:flex;gap:4px;flex-wrap:wrap}.member-chips button{max-width:110px;overflow:hidden;padding:3px 6px;border:1px solid #dce4f7;border-radius:8px;color:#5268bf;background:#f5f7ff;font-size:8px;text-overflow:ellipsis;white-space:nowrap;cursor:pointer}.member-chips span{padding:3px;color:#8a97a9;font-size:8px}.enter-community{align-self:flex-end;margin-top:auto;padding:6px 9px;border:1px solid #ced9ff;border-radius:7px;color:#4561d8;background:#f3f6ff;font-size:9px;cursor:pointer}.community-empty{min-height:360px}@media(max-width:1100px){.community-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:700px){.community-grid{grid-template-columns:1fr}}
.graph-layout{position:relative}.canvas-card{height:650px;background:#fff}.cy-canvas{background:radial-gradient(circle,#dfe5ee 1px,transparent 1.2px) 0 0/18px 18px}.canvas-heading{position:absolute;z-index:3;top:0;left:0;right:0;display:flex;align-items:center;justify-content:space-between;padding:11px 14px;border-bottom:1px solid #e7ebf1;background:#fffffff2;pointer-events:none}.canvas-heading strong,.canvas-heading small{display:block}.canvas-heading strong{color:#252d3b;font-size:13px}.canvas-heading small{margin-top:3px;color:#8a94a3;font-size:9px}.canvas-heading>span{color:#6f7b8d;font-size:10px}.type-legend{position:absolute;z-index:3;top:64px;left:12px;display:grid;gap:5px;max-width:180px;padding:9px 10px;border:1px solid #e1e6ee;border-radius:9px;background:#fffffff0;box-shadow:0 5px 18px #26384f12;pointer-events:none}.type-legend>strong{margin-bottom:2px;color:#68758a;font-size:9px}.type-legend>span{display:flex;align-items:center;gap:5px;color:#5d697a;font-size:8px}.type-legend i{width:8px;height:8px;flex:0 0 8px;border:2px solid #fff;border-radius:50%;box-shadow:0 0 0 1px #d6dde8}.type-legend b{margin-left:auto;color:#8792a2}.type-legend small{color:#98a3b1;font-size:8px}.graph-summary{bottom:12px;left:12px}.inspector{position:absolute;z-index:5;top:62px;right:12px;height:calc(100% - 76px);border-color:#dce3ed;background:#fffffff7;box-shadow:0 12px 32px #26384f22}.canvas-card:fullscreen .canvas-heading{padding:14px 18px}.canvas-card:fullscreen .inspector{top:68px}.canvas-card:fullscreen .type-legend{top:72px}@media(max-width:950px){.canvas-card{height:590px}.inspector{position:absolute;top:62px;right:10px;box-sizing:border-box;width:min(320px,calc(100% - 20px));height:calc(100% - 76px);margin:0}.canvas-heading>span{display:none}.type-legend{max-width:145px}}
.graph-catalog{margin:14px 0;padding:14px;border:1px solid #e1e7f0;border-radius:14px;background:linear-gradient(135deg,#fff 0%,#f8faff 55%,#f4f8ff 100%);box-shadow:0 8px 28px #334d7a0b}.graph-catalog>header{display:flex;align-items:center;justify-content:space-between}.graph-catalog>header strong,.graph-catalog>header small{display:block}.graph-catalog>header strong{font-size:14px}.graph-catalog>header small{margin-top:4px;color:#8491a4;font-size:9px}.graph-catalog>header>span{padding:5px 8px;border-radius:9px;color:#5268bf;background:#eef2ff;font-size:9px}.graph-catalog nav{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:9px;margin-top:12px}.graph-catalog nav button{display:flex;align-items:center;gap:9px;min-width:0;padding:10px;border:1px solid #e0e6ef;border-top:3px solid var(--profile-color);border-radius:10px;text-align:left;background:#fff;cursor:pointer;transition:transform .15s,box-shadow .15s}.graph-catalog nav button:hover{transform:translateY(-1px);box-shadow:0 8px 20px #263f7015}.graph-catalog nav button.active{border-color:var(--profile-color);box-shadow:0 8px 22px #263f701c}.graph-catalog nav i{display:grid;width:32px;height:32px;flex:0 0 32px;place-items:center;border-radius:9px;color:#fff;background:var(--profile-color);font-style:normal;font-weight:700}.graph-catalog nav span,.graph-catalog nav b,.graph-catalog nav small{display:block;min-width:0}.graph-catalog nav b{color:#344159;font-size:10px}.graph-catalog nav small{margin-top:3px;color:#8a96a7;font-size:8px}.profile-context{display:grid;grid-template-columns:1fr 1fr minmax(220px,.85fr);gap:8px;margin-top:10px}.profile-purpose,.profile-context details{padding:9px 10px;border:1px solid #e4e9f1;border-left:3px solid #b9c5d8;border-radius:8px;color:#647287;background:#fff;font-size:9px;line-height:1.5}.profile-purpose b{display:block;margin-bottom:2px;color:#344159}.profile-context summary{color:#5268bf;cursor:pointer}.profile-context details p{margin:5px 0}.profile-context details small{display:block;margin-top:6px;color:#8996a8}.knowledge-flow{display:flex;align-items:center;justify-content:center;gap:8px;flex-wrap:wrap;margin-top:10px;padding:8px;border-radius:9px;background:#eef3fa;color:#66758b;font-size:8px}.knowledge-flow span{padding:4px 7px;border-radius:7px;background:#fff}.knowledge-flow i{color:#92a2b9;font-style:normal}@media(max-width:1050px){.graph-catalog nav{grid-template-columns:repeat(2,minmax(0,1fr))}.profile-context{grid-template-columns:1fr}}@media(max-width:620px){.graph-catalog nav{grid-template-columns:1fr}.graph-catalog>header>span{display:none}}
</style>
