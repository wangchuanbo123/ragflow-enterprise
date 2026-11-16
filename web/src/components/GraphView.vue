<script setup lang="ts">
import cytoscape, { type Core, type ElementDefinition, type LayoutOptions } from 'cytoscape'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import * as api from '../api/client'
import { pageLabel } from '../documentPresentation'
import StructuredResult from './StructuredResult.vue'
import { graphTypeLabels } from '../graphLabels'
import { useScopedPageState } from '../composables/useScopedPageState'
import type {
  ChunkDetail,
  DocumentSubgraph,
  DocumentChunk,
  EntitySubgraph,
  GraphCorrection,
  GraphCommunity,
  GraphEntity,
  GraphFilterPreview,
  SavedGraphFilter,
  GraphStats,
  KnowledgeDocument,
  SubgraphEdge,
  SubgraphNode,
} from '../types'

import { usePermissions } from '../composables/usePermissions'
const { canEdit } = usePermissions()

const props = defineProps<{ knowledgeBaseId: string }>()
type FilterPayload={principles:string[];max_nodes:number;max_edges:number}
type GraphDraft={principles:Array<{id:number;text:string;enabled:boolean}>;maxNodes:number;maxEdges:number;layout:'cose'|'concentric'|'breadthfirst';density:'overview'|'complete';hops:1|2;score:number;entityType:string;relationType:string;labels:boolean;advanced:boolean;documentId:string;entityQuery:string;selectedView:string;viewName:string;selection:{nodeId:string;edgeId:string};applied:FilterPayload}
function initialGraphDraft():GraphDraft{return {principles:[],maxNodes:200,maxEdges:400,layout:'cose',density:'complete',hops:1,score:0,entityType:'',relationType:'',labels:false,advanced:false,documentId:'',entityQuery:'',selectedView:'',viewName:'',selection:{nodeId:'',edgeId:''},applied:{principles:[],max_nodes:200,max_edges:400}}}
const graphDraft=useScopedPageState<GraphDraft>(()=>['graph',props.knowledgeBaseId],initialGraphDraft,value=>{
  const draft=value as GraphDraft|null
  if(!draft||!Array.isArray(draft.principles)||draft.principles.length>20||!draft.principles.every(item=>Number.isInteger(item.id)&&typeof item.text==='string'&&item.text.length<=500&&typeof item.enabled==='boolean'))return null
  if(!['cose','concentric','breadthfirst'].includes(draft.layout)||!['overview','complete'].includes(draft.density)||![1,2].includes(draft.hops)||typeof draft.labels!=='boolean'||typeof draft.advanced!=='boolean'||!Number.isFinite(draft.score)||draft.score<0||draft.score>1)return null
  if(![draft.entityType,draft.relationType,draft.documentId,draft.entityQuery,draft.selectedView,draft.viewName].every(item=>typeof item==='string'&&item.length<=500))return null
  const payload=draft.applied
  if(!Number.isInteger(draft.maxNodes)||draft.maxNodes<1||draft.maxNodes>500||!Number.isInteger(draft.maxEdges)||draft.maxEdges<0||draft.maxEdges>1000||!payload||!Array.isArray(payload.principles)||payload.principles.length>20||!payload.principles.every(item=>typeof item==='string'&&item.length<=500)||!Number.isInteger(payload.max_nodes)||payload.max_nodes<1||payload.max_nodes>500||!Number.isInteger(payload.max_edges)||payload.max_edges<0||payload.max_edges>1000)return null
  return {...draft,selection:draft.selection&&typeof draft.selection.nodeId==='string'&&typeof draft.selection.edgeId==='string'?draft.selection:{nodeId:'',edgeId:''}}
})
function stored<K extends keyof GraphDraft>(key:K){return computed({get:()=>graphDraft.value[key],set:(value:GraphDraft[K])=>graphDraft.value[key]=value})}
const stats = ref<GraphStats | null>(null)
const typeLabels = ref(graphTypeLabels([]))
const filteredGraph = ref<GraphFilterPreview|null>(null)
const principles = stored('principles')
const savedFilters = ref<SavedGraphFilter[]>([])
const selectedSavedId = stored('selectedView')
const viewName = stored('viewName')
const viewNotice = ref('')
const savingView = ref(false)
const applyingFilter = ref(false)
const filterFailed = ref(false)
const filterFailureDetails = ref<unknown>(null)
const maxNodes = stored('maxNodes')
const maxEdges = stored('maxEdges')
const appliedSignature = ref<string|null>(null)
const appliedPrinciples = ref<string[]>([])
const enabledPrinciples = computed(()=>principles.value.filter(item=>item.enabled).map(item=>item.text.trim()).filter(Boolean))
const draftSignature = computed(()=>JSON.stringify({principles:enabledPrinciples.value,max_nodes:maxNodes.value,max_edges:maxEdges.value}))
const principlesPending = computed(()=>filterFailed.value||appliedSignature.value!==draftSignature.value)
type PrincipleCandidate={id:string;label:string;value:string;description?:string}
const unresolvedPrinciples=computed(()=>{
  const details=filterFailureDetails.value as {unapplied_principles?:unknown[]}|null
  return (Array.isArray(details?.unapplied_principles)?details.unapplied_principles:[]).flatMap(value=>{
    const item=value as {index?:unknown;principle?:unknown;reason?:unknown;candidates?:unknown[]}
    if(!item||!Number.isInteger(item.index))return []
    return [{index:item.index as number,principle:typeof item.principle==='string'?item.principle:'',reason:typeof item.reason==='string'?item.reason:'请明确筛选范围',candidates:(Array.isArray(item.candidates)?item.candidates:[]).filter((candidate):candidate is PrincipleCandidate=>{const choice=candidate as PrincipleCandidate;return !!choice&&typeof choice.label==='string'&&typeof choice.value==='string'}).slice(0,10)}]
  })
})
function chooseCandidate(index:number,candidate:PrincipleCandidate):void{
  const enabled=principles.value.filter(item=>item.enabled&&item.text.trim()),principle=enabled[index]
  if(!principle)return
  const suffix=`（其中“${candidate.label}”明确指 ID ${candidate.value}）`
  if(principle.text.length+suffix.length>500){viewNotice.value='候选已找到，但原则过长。请先精简该原则，再选择候选。';return}
  principle.text+=suffix
  viewNotice.value=`已在原则 ${index+1} 中明确“${candidate.label}”。请检查文字后点击“应用原则”。`
}
let principleId=Math.max(0,...principles.value.map(item=>item.id)), previewSequence=0, loadSequence=0, focusSequence=0
let previewController:AbortController|null=null
let disposed=false
const documents = ref<KnowledgeDocument[]>([])
const documentId = stored('documentId')
const subgraph = ref<DocumentSubgraph | null>(null)
const entityGraph = ref<EntitySubgraph | null>(null)
const entityQuery = stored('entityQuery')
const entities = ref<GraphEntity[]>([])
const selectedEdge = ref<SubgraphEdge | null>(null)
const selectedNode = ref<SubgraphNode | null>(null)
const corrections = ref<GraphCorrection[]>([])
const communities = ref<GraphCommunity[]>([])
const documentChunks = ref<DocumentChunk[]>([])
const documentChunkLoading=ref(false)
const documentChunkTotal=computed(()=>documents.value.find(item=>item.id===documentId.value)?.chunk_count||0)
const relationForm = reactive({ subject_entity_id: '', object_entity_id: '', predicate: 'RELATED_TO', chunk_id: '', evidence: '', reason: '' })
const loading = ref(false)
const error = ref('')
const graphContainer = ref<HTMLDivElement | null>(null)
const layoutMode = stored('layout')
const densityMode = stored('density')
const hops = stored('hops')
const minConfidence = stored('score')
const relationTypeFilter = stored('relationType')
const entityTypeFilter = stored('entityType')
const showRelationLabels = stored('labels')
const advancedOpen=stored('advanced')
const evidenceDetail=ref<ChunkDetail|null>(null),evidenceError=ref(''),evidenceLoading=ref(false),evidenceQuote=ref('')
let evidenceRequest=0
const communityMode = ref(false)
let cy: Core | null = null
let resizeObserver: ResizeObserver | null = null

const activeGraph = computed(() => entityGraph.value ?? subgraph.value ?? filteredGraph.value)
const nodeMap = computed(() => new Map((activeGraph.value?.nodes ?? []).map(node => [node.id, node])))
const relationTypes = computed(() => [...new Set((activeGraph.value?.edges ?? []).map(edge => edge.predicate))].sort())
const entityTypes = computed(() => [...new Set((activeGraph.value?.nodes ?? []).map(node => node.entity_type))].sort())
const topicCommunities = computed(() => communities.value.filter(item => item.level === 0).sort((a, b) => b.members.length - a.members.length || a.title.localeCompare(b.title)))
const budgets = computed(() => densityMode.value === 'overview' ? { nodes: 80, edges: 140 } : { nodes: Number.MAX_SAFE_INTEGER, edges: Number.MAX_SAFE_INTEGER })
const scoreFilterLabel = computed(() => minConfidence.value <= 0 ? '显示全部' : `${Math.round(minConfidence.value * 100)} 分以上`)
const currentGraphTitle = computed(() => {
  const loadedDocumentId = entityGraph.value ? undefined : subgraph.value?.document_id
  const document = documents.value.find(item => item.id === loadedDocumentId)
  if (document) return `${document.original_filename} · 当前图谱范围`
  const seed = entityGraph.value?.seed_entity_ids[0]
  return seed ? `${nodeMap.value.get(seed)?.label ?? '实体'} · 当前图谱范围` : '知识库总图谱'
})
const displayedTypeStats = computed(() => {
  const counts = new Map<string, number>()
  projectedGraph.value.nodes.forEach(node => counts.set(node.entity_type, (counts.get(node.entity_type) ?? 0) + 1))
  return [...counts.entries()].map(([type, count]) => ({ type, count, color: typeColor(type) })).sort((a, b) => b.count - a.count || a.type.localeCompare(b.type))
})
const usingFastLargeLayout = computed(() => layoutMode.value === 'cose' && projectedGraph.value.nodes.length > 220)

const typePalette = ['#f3883b', '#339cff', '#8b6de9', '#5dc977', '#eb76b1', '#3ab9b1', '#4776bd', '#d87932']
function typeColor(type: string): string {
  let hash = 0
  for (const char of type) hash = ((hash << 5) - hash + char.charCodeAt(0)) | 0
  return typePalette[Math.abs(hash) % typePalette.length]
}

function relationColor(predicate: string): string {
  return typeColor(predicate)
}

function entityTypeLabel(type: string): string {
  return typeLabels.value.entities.get(type.toUpperCase())?.label || type
}

function relationTypeLabel(predicate: string): string {
  return typeLabels.value.relations.get(predicate.toUpperCase())?.label || predicate
}
function entityTypeDefinition(type:string):string { return typeLabels.value.entities.get(type.toUpperCase())?.definition || type }
function relationTypeDefinition(type:string):string { return typeLabels.value.relations.get(type.toUpperCase())?.definition || type }

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
      id: edge.id, source: edge.source, target: edge.target, label: relationTypeLabel(edge.predicate),
      displayLabel: showRelationLabels.value ? relationTypeLabel(edge.predicate) : '', confidence: edge.confidence,
      edgeColor: relationColor(edge.predicate), manual: edge.manual ? 1 : 0,
    } })),
  ]
  cy?.destroy()
  cy = cytoscape({
    container: graphContainer.value,
    elements,
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
        'label': 'data(label)', 'font-size': 10, 'font-weight': 600, 'color': '#273142', 'text-wrap': 'ellipsis',
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

function addPrinciple(text=''):void { if(principles.value.length<20){principleId=Math.max(principleId,...principles.value.map(item=>item.id));principles.value.push({id:++principleId,text,enabled:true})} }
function filterPayload(){
  if(principles.value.some(item=>item.enabled&&!item.text.trim()))throw new Error('请填写原则内容，或停用、删除空白原则。')
  if(enabledPrinciples.value.some(item=>item.length>500))throw new Error('每条原则最多 500 个字符。')
  if(!Number.isInteger(maxNodes.value)||maxNodes.value<1||maxNodes.value>500||!Number.isInteger(maxEdges.value)||maxEdges.value<0||maxEdges.value>1000)throw new Error('加载上限须为 1–500 个实体、0–1000 条关系。')
  return {principles:[...enabledPrinciples.value],max_nodes:maxNodes.value,max_edges:maxEdges.value}
}
async function applyPrinciples(saved?:SavedGraphFilter,restore=false):Promise<void>{
  const kb=props.knowledgeBaseId, sequence=++previewSequence
  previewController?.abort(); previewController=new AbortController();applyingFilter.value=false
  if(saved){principles.value=saved.principles.map(text=>({id:++principleId,text,enabled:true}));selectedSavedId.value=saved.id}
  let payload:ReturnType<typeof filterPayload>
  try{payload=restore?{...graphDraft.value.applied,principles:[...graphDraft.value.applied.principles]}:filterPayload()}catch(e){error.value=(e as Error).message;filterFailed.value=true;return}
  applyingFilter.value=true;filterFailureDetails.value=null;error.value='';viewNotice.value=''
  try{
    const result=saved?await api.previewSavedGraphFilter(kb,saved.id,{max_nodes:payload.max_nodes,max_edges:payload.max_edges},previewController.signal):await api.previewGraphFilter(kb,payload,previewController.signal)
    if(disposed||sequence!==previewSequence||kb!==props.knowledgeBaseId)return
    if(!restore)graphDraft.value.selection={nodeId:'',edgeId:''}
    focusSequence++;filteredGraph.value=result;appliedPrinciples.value=payload.principles;appliedSignature.value=JSON.stringify(payload);filterFailed.value=false
    graphDraft.value.applied=payload
    documentId.value='';subgraph.value=null;entityGraph.value=null;entities.value=[];selectedNode.value=null;selectedEdge.value=null;communityMode.value=false
  }catch(e){if(disposed||sequence!==previewSequence||kb!==props.knowledgeBaseId||(e as Error).name==='AbortError')return;filterFailed.value=true;error.value=(e as Error).message;filterFailureDetails.value=(e as {details?:unknown}).details||null}
  finally{if(sequence===previewSequence&&kb===props.knowledgeBaseId)applyingFilter.value=false}
}
async function clearPrinciples():Promise<void>{principles.value=[];selectedSavedId.value='';await applyPrinciples()}
async function saveView():Promise<void>{
  const kb=props.knowledgeBaseId
  try{
    const payload=filterPayload(),name=viewName.value.trim()
    if(!name||name.length>120)throw new Error('请为个人视图填写 1–120 个字符的名称。')
    savingView.value=true
    const saved=await api.saveGraphFilter(kb,{name,principles:payload.principles})
    if(disposed||kb!==props.knowledgeBaseId)return
    savedFilters.value=[saved,...savedFilters.value.filter(item=>item.id!==saved.id)];selectedSavedId.value=saved.id;viewName.value='';viewNotice.value='个人视图已保存；刷新后仍可选择并重新应用。'
  }catch(e){if(kb===props.knowledgeBaseId){error.value=(e as Error).message;filterFailureDetails.value=(e as {details?:unknown}).details||null}}
  finally{if(kb===props.knowledgeBaseId)savingView.value=false}
}
async function applySavedView():Promise<void>{const saved=savedFilters.value.find(item=>item.id===selectedSavedId.value);if(saved)await applyPrinciples(saved)}
async function removeSavedView():Promise<void>{
  const kb=props.knowledgeBaseId,id=selectedSavedId.value
  if(!id)return
  try{await api.deleteGraphFilter(kb,id);if(kb!==props.knowledgeBaseId||disposed)return;savedFilters.value=savedFilters.value.filter(item=>item.id!==id);selectedSavedId.value='';viewNotice.value='个人视图已删除；当前画布保留已应用的原则。'}catch(e){if(kb===props.knowledgeBaseId)error.value=(e as Error).message}
}
function withinCurrentView<T extends {nodes:SubgraphNode[];edges:SubgraphEdge[]}>(graph:T):T{
  const nodes=new Set(filteredGraph.value?.nodes.map(item=>item.id)||[]),edges=new Set(filteredGraph.value?.edges.map(item=>item.id)||[])
  return {...graph,nodes:graph.nodes.filter(item=>nodes.has(item.id)),edges:graph.edges.filter(item=>edges.has(item.id)&&nodes.has(item.source)&&nodes.has(item.target))}
}
async function load(): Promise<void> {
  const kb=props.knowledgeBaseId, sequence=++loadSequence
  const restoreDocument=documentId.value
  loading.value=true
  try{
    const [result, labels]=await Promise.all([
      Promise.all([api.getGraphStats(kb),api.listDocuments(kb),api.listGraphCorrections(kb),api.listGraphCommunities(kb),api.listGraphFilters(kb)]),
      Promise.allSettled([api.listGraphProfiles(kb),api.listGraphSchemas(kb)]),
    ])
    if(disposed||kb!==props.knowledgeBaseId||sequence!==loadSequence)return
    ;[stats.value,documents.value,corrections.value,communities.value,savedFilters.value]=result
    typeLabels.value=graphTypeLabels(labels[0].status==='fulfilled'?labels[0].value:[],labels[1].status==='fulfilled'?labels[1].value.find(item=>item.graph_kind==='general'&&item.status==='active'):undefined)
    error.value='';await applyPrinciples(undefined,true)
    if(!disposed&&kb===props.knowledgeBaseId&&sequence===loadSequence&&filteredGraph.value&&restoreDocument&&documents.value.some(item=>item.id===restoreDocument))documentId.value=restoreDocument
  }catch(e){if(kb===props.knowledgeBaseId&&sequence===loadSequence)error.value=(e as Error).message}
  finally{if(kb===props.knowledgeBaseId&&sequence===loadSequence)loading.value=false}
}

async function openEvidence(chunkId:string,quote:string):Promise<void>{
  const request=++evidenceRequest,kb=props.knowledgeBaseId
  evidenceDetail.value=null;evidenceError.value='';evidenceLoading.value=true;evidenceQuote.value=quote
  try{const result=await api.getChunkDetail(kb,chunkId,1);if(!disposed&&request===evidenceRequest&&kb===props.knowledgeBaseId)evidenceDetail.value=result}
  catch(e){if(!disposed&&request===evidenceRequest&&kb===props.knowledgeBaseId&&(e as Error).name!=='AbortError')evidenceError.value=(e as Error).message}
  finally{if(request===evidenceRequest)evidenceLoading.value=false}
}
function closeEvidence():void{evidenceRequest++;evidenceDetail.value=null;evidenceLoading.value=false;evidenceError.value='';evidenceQuote.value=''}
const evidenceContent=computed(()=>{
  const content=evidenceDetail.value?.content||'',quote=evidenceQuote.value,index=quote?content.indexOf(quote):-1
  return index<0?{before:content,match:'',after:''}:{before:content.slice(0,index),match:quote,after:content.slice(index+quote.length)}
})

async function loadMoreDocumentChunks():Promise<void>{
  const kb=props.knowledgeBaseId,id=documentId.value,sequence=focusSequence
  if(!id||documentChunkLoading.value)return
  documentChunkLoading.value=true
  try{const next=await api.listDocumentChunks(kb,id,{offset:documentChunks.value.length,limit:200});if(!disposed&&kb===props.knowledgeBaseId&&id===documentId.value&&sequence===focusSequence)documentChunks.value.push(...next)}
  catch(e){if(sequence===focusSequence)error.value=(e as Error).message}
  finally{documentChunkLoading.value=false}
}

async function loadSubgraph(): Promise<void> {
  const kb=props.knowledgeBaseId,id=documentId.value,sequence=++focusSequence
  if(!id){subgraph.value=null;documentChunks.value=[];return}
  try{
    const [graph,chunks]=await Promise.all([api.getDocumentSubgraph(kb,id,1000,'general'),api.listDocumentChunks(kb,id)])
    if(disposed||kb!==props.knowledgeBaseId||sequence!==focusSequence)return
    subgraph.value=withinCurrentView(graph);documentChunks.value=chunks;entityGraph.value=null;communityMode.value=false
  }catch(e){if(kb===props.knowledgeBaseId&&sequence===focusSequence)error.value=(e as Error).message}
}
async function search(): Promise<void> {
  const kb=props.knowledgeBaseId,query=entityQuery.value.trim(),sequence=++focusSequence
  if(!query){entities.value=[];return}
  try{
    const result=await api.searchGraphEntities(kb,query,'general')
    if(disposed||kb!==props.knowledgeBaseId||sequence!==focusSequence)return
    const allowed=new Set(filteredGraph.value?.nodes.map(item=>item.id)||[])
    entities.value=result.filter(item=>allowed.has(item.id));selectedEdge.value=null;selectedNode.value=null
    if(!entities.value.length)viewNotice.value='当前已加载的匹配图谱中没有搜索结果；可调整原则或加载上限。'
  }catch(e){if(kb===props.knowledgeBaseId&&sequence===focusSequence)error.value=(e as Error).message}
}
async function expandEntity(entity: GraphEntity | SubgraphNode): Promise<void> {
  const kb=props.knowledgeBaseId
  loading.value=true;selectedEdge.value=null;selectedNode.value=null;communityMode.value=false;documentId.value=''
  await nextTick()
  const sequence=++focusSequence
  try{
    const graph=await api.getEntitySubgraph(kb,[entity.id],0,200,hops.value,'general')
    if(disposed||kb!==props.knowledgeBaseId||sequence!==focusSequence)return
    entityGraph.value=withinCurrentView(graph);subgraph.value=null
  }catch(e){if(kb===props.knowledgeBaseId&&sequence===focusSequence)error.value=(e as Error).message}
  finally{if(kb===props.knowledgeBaseId&&sequence===focusSequence)loading.value=false}
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
  const kb=props.knowledgeBaseId,sequence=++focusSequence
  loading.value = true
  try {
    const next = await api.getEntitySubgraph(props.knowledgeBaseId, graph.seed_entity_ids, graph.next_offset, 200, hops.value, 'general')
    if(disposed||kb!==props.knowledgeBaseId||sequence!==focusSequence)return
    const nodes = new Map(graph.nodes.map(node => [node.id, node]))
    const edges = new Map(graph.edges.map(edge => [edge.id, edge]))
    next.nodes.forEach(node => nodes.set(node.id, node))
    next.edges.forEach(edge => edges.set(edge.id, edge))
    entityGraph.value = withinCurrentView({ ...next, nodes: [...nodes.values()], edges: [...edges.values()] })
  } catch (e) { error.value = (e as Error).message }
  finally { loading.value = false }
}

async function refreshOverlay(): Promise<void> {
  const kb=props.knowledgeBaseId,sequence=++previewSequence
  previewController?.abort();previewController=new AbortController()
  const result=await Promise.all([api.getGraphStats(kb),api.listGraphCorrections(kb)])
  if(disposed||kb!==props.knowledgeBaseId||sequence!==previewSequence)return
  ;[stats.value,corrections.value]=result
  if(!appliedSignature.value)return
  const preview=await api.previewGraphFilter(kb,JSON.parse(appliedSignature.value),previewController.signal)
  if(disposed||kb!==props.knowledgeBaseId||sequence!==previewSequence)return
  filteredGraph.value=preview;showFilteredGraph()
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
function resetFocus(): void { graphDraft.value.selection={nodeId:'',edgeId:''};selectedNode.value = null; selectedEdge.value = null; cy?.elements().removeClass('faded focused'); cy?.fit(undefined, 42) }
function showFilteredGraph():void {graphDraft.value.selection={nodeId:'',edgeId:''};focusSequence++;documentId.value='';subgraph.value=null;entityGraph.value=null;entities.value=[];selectedNode.value=null;selectedEdge.value=null;communityMode.value=false}
async function toggleFullscreen(): Promise<void> {
  if (!graphContainer.value) return
  if (document.fullscreenElement) await document.exitFullscreen()
  else await graphContainer.value.parentElement?.requestFullscreen()
  window.setTimeout(() => { cy?.resize(); cy?.fit(undefined, 42) }, 120)
}

watch(() => props.knowledgeBaseId, () => {
  typeLabels.value=graphTypeLabels([])
  previewSequence++;focusSequence++;previewController?.abort();filteredGraph.value=null;appliedPrinciples.value=[];appliedSignature.value=null;savedFilters.value=[];viewNotice.value='';filterFailed.value=false;filterFailureDetails.value=null;stats.value=null;documents.value=[];corrections.value=[];communities.value=[];error.value='';applyingFilter.value=false;savingView.value=false
  closeEvidence();subgraph.value = null; entityGraph.value = null; entities.value = []; communityMode.value = false; void load()
})
watch(activeGraph,graph=>{
  if(!graph)return
  const selection=graphDraft.value.selection
  selectedNode.value=graph.nodes.find(node=>node.id===selection.nodeId)||null
  selectedEdge.value=graph.edges.find(edge=>edge.id===selection.edgeId)||null
})
watch([selectedNode,selectedEdge],([node,edge])=>{graphDraft.value.selection={nodeId:node?.id||'',edgeId:edge?.id||''}})
watch(documentId, loadSubgraph)
watch([projectedGraph, layoutMode], () => { void nextTick(renderGraph) }, { deep: true })
watch(showRelationLabels, value => { cy?.edges().forEach(edge => { edge.data('displayLabel', value ? relationTypeLabel(edge.data('label')) : '') }) })
watch([relationTypeFilter, entityTypeFilter], () => { selectedEdge.value = null; selectedNode.value = null })
onMounted(async () => {
  await load()
  await nextTick()
  resizeObserver = new ResizeObserver(() => cy?.resize())
  if (graphContainer.value) resizeObserver.observe(graphContainer.value)
})
onBeforeUnmount(() => { disposed=true;previewSequence++;focusSequence++;evidenceRequest++;previewController?.abort();resizeObserver?.disconnect(); cy?.destroy(); cy = null })
</script>

<template>
  <div class="graph-page">
    <div class="stats-grid">
      <article><span>实体</span><strong>{{ stats?.entities ?? 0 }}</strong></article><article><span>关系</span><strong>{{ stats?.relations ?? 0 }}</strong></article><article><span>实体提及</span><strong>{{ stats?.mentions ?? 0 }}</strong></article><article><span>证据片段</span><strong>{{ stats?.evidence ?? 0 }}</strong></article><article><span>人工校正</span><strong>{{ stats?.active_corrections ?? 0 }}</strong></article><article><span>冲突校正</span><strong class="conflict-number">{{ stats?.conflict_corrections ?? 0 }}</strong></article>
    </div>
    <p v-if="error" class="error">{{ error }}</p>

    <section class="graph-principles">
      <header><div><h2>知识库总图谱</h2><p>全部文档使用同一套 Schema。用简单的话说明想看什么，原则只改变展示，不改动知识和证据。</p></div><span>全库统一结构</span></header>
      <div class="principle-examples"><span>试试：</span><button v-for="example in ['只看和订单服务有关的关系','隐藏文档类实体','只显示有原文证据的关系']" :key="example" :disabled="principles.length>=20" @click="addPrinciple(example)">{{example}}</button></div>
      <form @submit.prevent="applyPrinciples()">
        <div v-for="(principle,index) in principles" :key="principle.id" class="principle-row">
          <input v-model="principle.enabled" type="checkbox" :aria-label="`启用原则 ${index+1}`"/>
          <input v-model="principle.text" :aria-label="`过滤原则 ${index+1}`" maxlength="500" placeholder="例如：只看和订单服务有关的关系" :class="{disabled:!principle.enabled}"/>
          <button type="button" :aria-label="`删除原则 ${index+1}`" @click="principles=principles.filter(item=>item.id!==principle.id)">删除</button>
        </div>
        <p v-if="!principles.length" class="principle-hint">尚无过滤原则，显示知识库总图谱。可添加多条原则，系统会逐条解释。</p>
        <div class="principle-actions"><button type="button" :disabled="principles.length>=20" @click="addPrinciple()">添加原则</button><label>最多加载实体<input v-model.number="maxNodes" aria-label="最多加载实体" type="number" min="1" max="500"/></label><label>最多加载关系<input v-model.number="maxEdges" aria-label="最多加载关系" type="number" min="0" max="1000"/></label><button :disabled="applyingFilter">{{applyingFilter?'正在理解并筛选…':'应用原则'}}</button><button type="button" :disabled="applyingFilter" @click="clearPrinciples">清除原则，显示全库</button></div>
      </form>
      <p v-if="principlesPending" class="pending-principles" role="status">当前原则尚未应用。{{filteredGraph?'画布仍显示上次成功应用的图谱。':'请应用原则后查看结果。'}}</p>
      <div v-if="filteredGraph" class="filter-understanding"><strong>系统理解</strong><p>{{filteredGraph.summary}}</p><small>当前画布已应用 {{appliedPrinciples.length}} 条原则</small><details v-if="filteredGraph.plan.principles.length"><summary>逐条查看理解</summary><p v-for="item in filteredGraph.plan.principles" :key="item.index">{{item.index+1}}. {{item.summary}}<small v-if="filteredGraph.per_principle_counts?.find(count=>count.index===item.index)" class="principle-count"> → 累计匹配 {{filteredGraph.per_principle_counts.find(count=>count.index===item.index)?.matched_nodes}} 个实体、{{filteredGraph.per_principle_counts.find(count=>count.index===item.index)?.matched_edges}} 条关系</small></p></details><p v-for="warning in filteredGraph.warnings" :key="warning" class="filter-warning">{{warning}}</p><p class="filter-counts">匹配 {{filteredGraph.stats.matched_nodes}} / {{filteredGraph.stats.total_nodes}} 个实体、{{filteredGraph.stats.matched_edges}} / {{filteredGraph.stats.total_edges}} 条关系；已加载 {{filteredGraph.stats.returned_nodes}} 个实体、{{filteredGraph.stats.returned_edges}} 条关系。<strong v-if="filteredGraph.stats.truncated">结果已截断，可提高加载上限或缩小原则范围。</strong></p></div>
      <section v-if="filterFailureDetails" class="filter-failure"><strong>这些原则尚不能应用</strong><article v-for="item in unresolvedPrinciples" :key="item.index"><p>原则 {{item.index+1}}：{{item.reason}}</p><div class="ambiguity-candidates"><button v-for="candidate in item.candidates" :key="candidate.id||candidate.value" type="button" :title="candidate.description" @click="chooseCandidate(item.index,candidate)">明确为：{{candidate.label}}<small v-if="candidate.description"> · {{candidate.description}}</small></button></div></article><details><summary>查看详细原因</summary><StructuredResult :value="filterFailureDetails"/></details></section>
      <div class="saved-views"><strong>我的视图</strong><input v-model="viewName" aria-label="个人视图名称" maxlength="120" placeholder="为当前原则命名"/><button :disabled="savingView" @click="saveView">{{savingView?'正在保存…':'保存当前原则'}}</button><select v-model="selectedSavedId" aria-label="已保存的个人视图"><option value="">选择个人视图</option><option v-for="view in savedFilters" :key="view.id" :value="view.id">{{view.name}}</option></select><button :disabled="!selectedSavedId||applyingFilter" @click="applySavedView">应用个人视图</button><button :disabled="!selectedSavedId" @click="removeSavedView">删除个人视图</button><small>仅你可见；重新应用时会按当前知识和 Schema 理解原则。</small></div>
      <p v-if="viewNotice" class="view-notice" role="status">{{viewNotice}}</p>
    </section>

    <div class="graph-toolbar">
      <label>按文档查看子图 <select v-model="documentId"><option value="">请选择文档</option><option v-for="document in documents" :key="document.id" :value="document.id">{{ document.original_filename }}</option></select></label>
      <form @submit.prevent="search"><input v-model="entityQuery" placeholder="在当前已加载图谱中搜索实体" /><button>搜索</button></form>
    </div>

    <details class="advanced-controls" :open="advancedOpen" @toggle="advancedOpen=($event.target as HTMLDetailsElement).open"><summary>高级显示设置 <small>布局、类型、评分与画布操作</small></summary><div class="view-toolbar">
      <label>展开范围<select v-model="hops"><option :value="1">一跳（推荐）</option><option :value="2">两跳</option></select></label>
      <label>显示范围<select v-model="densityMode"><option value="complete">当前已加载图谱</option><option value="overview">精简视图 · 80/140</option></select></label>
      <label>布局<select v-model="layoutMode"><option value="cose">智能避让</option><option value="concentric">中心辐射</option><option value="breadthfirst">层级关系</option></select></label>
      <label>实体类型<select v-model="entityTypeFilter"><option value="">全部</option><option v-for="item in entityTypes" :key="item" :value="item" :title="entityTypeDefinition(item)">{{ entityTypeLabel(item) }}</option></select></label>
      <label>关系类型<select v-model="relationTypeFilter"><option value="">全部</option><option v-for="item in relationTypes" :key="item" :value="item" :title="relationTypeDefinition(item)">{{ relationTypeLabel(item) }}</option></select></label>
      <label class="confidence-filter">模型评分筛选 <b>{{ scoreFilterLabel }}</b><input v-model.number="minConfidence" type="range" min="0" max="1" step="0.05" /></label>
      <label class="check"><input v-model="showRelationLabels" type="checkbox" />常显关系名</label>
      <button :disabled="appliedPrinciples.length>0" :title="appliedPrinciples.length?'当前过滤图请直接浏览实体；清除原则后可看全库主题':'查看全库主题'" :class="{enabled:communityMode}" @click="communityMode=!communityMode">{{communityMode?'返回实体图':'主题总览'}}</button><button title="缩小" @click="zoomGraph(0.78)">−</button><button title="放大" @click="zoomGraph(1.28)">＋</button><button @click="fitGraph">适应画布</button><button @click="resetFocus">复位</button><button @click="showFilteredGraph">返回当前筛选全图</button><button @click="toggleFullscreen">全屏</button>
    </div></details>

    <div v-if="!communityMode" class="graph-layout">
      <section class="canvas-card">
        <header v-if="activeGraph" class="canvas-heading"><div><strong>{{ currentGraphTitle }}</strong><small>实体按 Schema 类型着色 · 滚轮缩放 · 拖拽查看 · 点击聚焦邻域</small></div><span>{{ projectedGraph.nodes.length }} 个实体 · {{ projectedGraph.edges.length }} 条关系</span></header>
        <div v-if="loading && !activeGraph" class="empty overlay"><strong>正在读取知识图谱…</strong></div>
        <div v-else-if="!activeGraph" class="empty overlay"><strong>知识库总图谱尚未加载</strong><span>请应用原则或重试；加载失败时不会改为显示未过滤图谱。</span></div>
        <div v-else-if="!activeGraph.nodes.length" class="empty overlay"><strong>{{filteredGraph?.stats.total_nodes===0?'知识库尚无图谱事实':'当前原则或查看范围没有匹配结果'}}</strong><span>{{filteredGraph?.stats.total_nodes===0?'上传文档并完成知识抽取后，可在这里查看关系。':'多条原则同时生效，可停用其中一条再应用，逐步确认筛选范围。'}}</span><ul v-if="filteredGraph?.plan.principles.length" class="empty-principles"><li v-for="item in filteredGraph.plan.principles" :key="item.index">原则 {{item.index+1}}：{{item.summary}}</li></ul></div>
        <div v-show="activeGraph?.nodes.length" ref="graphContainer" class="cy-canvas" role="img" aria-label="可缩放和拖拽的知识图谱子图"></div>
        <div v-if="activeGraph && displayedTypeStats.length" class="type-legend"><strong>实体类型</strong><span v-for="item in displayedTypeStats.slice(0,8)" :key="item.type" :title="entityTypeDefinition(item.type)"><i :style="{background:item.color}"></i>{{entityTypeLabel(item.type)}} <b>{{item.count}}</b></span><small v-if="displayedTypeStats.length>8">其余 {{displayedTypeStats.length-8}} 类</small></div>
        <div v-if="activeGraph" class="graph-summary">显示 {{ projectedGraph.nodes.length }} / {{ activeGraph.nodes.length }} 个实体、{{ projectedGraph.edges.length }} / {{ activeGraph.edges.length }} 条关系<span v-if="usingFastLargeLayout"> · 大图快速布局</span><span v-if="projectedGraph.hiddenNodes || projectedGraph.hiddenEdges"> · 精简 {{ projectedGraph.hiddenNodes }} 个实体、{{ projectedGraph.hiddenEdges }} 条关系</span></div>
        <button v-if="entityGraph?.next_offset !== null && entityGraph" class="load-more" :disabled="loading" @click="loadMoreEntityGraph">加载更多候选关系</button>
      </section>

      <aside v-if="selectedEdge || selectedNode || entities.length" class="inspector">
        <template v-if="selectedEdge"><header><span>关系证据</span><button @click="selectedEdge = null">×</button></header><h3>{{ nodeMap.get(selectedEdge.source)?.label }} <em>—{{ relationTypeLabel(selectedEdge.predicate) }}→</em> {{ nodeMap.get(selectedEdge.target)?.label }}</h3><small>{{selectedEdge.predicate}}</small><span v-if="selectedEdge.manual" class="manual-badge">人工确认</span><p v-else class="confidence">{{ relationScoreText(selectedEdge) }}<small>仅用于筛选和排序，不代表正确概率</small></p><article v-for="evidence in selectedEdge.evidence" :key="evidence.chunk_id"><strong>原文证据</strong><p>{{ evidence.quote }}</p><small>{{ evidenceScoreText(selectedEdge, evidence.confidence) }}</small><button class="evidence-open" @click="openEvidence(evidence.chunk_id,evidence.quote)">查看文件、页码与上下文</button></article><div v-if="canEdit" class="correction-actions"><button v-if="selectedEdge.correction_id" @click="revertCorrection(selectedEdge.correction_id)">撤销人工校正</button><template v-else><button @click="updateEdge(selectedEdge)">修改关系</button><button class="danger" @click="hideEdge(selectedEdge)">隐藏关系</button></template></div></template>
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

    <section v-if="canEdit" class="corrections-card">
      <form v-if="documentId && subgraph?.nodes.length" @submit.prevent="addRelation"><header><strong>人工补充关系</strong><span>绑定真实 Chunk，保存后可被检索和引用</span></header><div class="correction-form"><select v-model="relationForm.subject_entity_id" required><option value="">起点实体</option><option v-for="node in subgraph.nodes" :key="node.id" :value="node.id">{{ node.label }}</option></select><select v-model="relationForm.predicate"><option v-for="predicate in ['RELATED_TO','DEPENDS_ON','USES','CALLS','CONTAINS','PART_OF','IMPLEMENTS','PROVIDES']" :key="predicate">{{ predicate }}</option></select><select v-model="relationForm.object_entity_id" required><option value="">终点实体</option><option v-for="node in subgraph.nodes" :key="node.id" :value="node.id">{{ node.label }}</option></select><select v-model="relationForm.chunk_id" required @change="useSelectedChunkEvidence"><option value="">来源 Chunk</option><option v-for="chunk in documentChunks" :key="chunk.id" :value="chunk.id">Chunk {{chunk.chunk_index+1}} · {{chunk.content.replace(/\s+/g,' ').slice(0,60)}}</option></select><small>已加载 {{documentChunks.length}} / {{documentChunkTotal}} 个片段</small><button v-if="documentChunks.length<documentChunkTotal" type="button" :disabled="documentChunkLoading" @click="loadMoreDocumentChunks">{{documentChunkLoading?'加载中…':'加载更多来源片段'}}</button><input v-model="relationForm.evidence" required placeholder="所选 Chunk 中的原文证据" /><input v-model="relationForm.reason" required minlength="3" placeholder="校正原因" /><button>添加关系</button></div></form>
      <div class="history" :class="{ full: !documentId || !subgraph?.nodes.length }"><header><strong>校正历史</strong><div><span>{{ corrections.length }} 条</span><button :disabled="loading" @click="reconcileCorrections">检查冲突</button></div></header><div v-if="!corrections.length" class="empty history-empty">暂无人工校正</div><article v-for="correction in corrections.slice(0, 20)" :key="correction.id"><div><strong>{{ correction.action }}</strong><p>{{ correction.reason }}</p><p v-if="correction.conflict_reason" class="conflict-reason">{{ correction.conflict_reason }}</p><small>{{ new Date(correction.created_at).toLocaleString() }}</small></div><span :class="correction.status">{{ correction.status }}</span><button v-if="correction.status === 'active' || correction.status === 'conflict'" @click="revertCorrection(correction.id)">撤销</button></article></div>
    </section>
    <div v-if="evidenceDetail||evidenceLoading||evidenceError" class="evidence-backdrop" @click.self="closeEvidence"><section class="evidence-dialog" role="dialog" aria-modal="true" aria-label="图谱原文证据" @keydown.esc="closeEvidence"><header><strong>{{evidenceDetail?.document_name||'读取原文证据'}}</strong><button aria-label="关闭图谱原文证据" @click="closeEvidence">×</button></header><p v-if="evidenceLoading">正在读取原文与相邻上下文…</p><p v-else-if="evidenceError" class="error">{{evidenceError}}</p><template v-else-if="evidenceDetail"><p class="evidence-location">{{[pageLabel(evidenceDetail.page),evidenceDetail.section,`片段 ${evidenceDetail.chunk_index+1}`].filter(Boolean).join(' · ')}}</p><article v-for="chunk in evidenceDetail.previous" :key="chunk.chunk_id" class="context-chunk"><small>前文{{chunk.page!=null?` · ${pageLabel(chunk.page)}`:''}}</small><p>{{chunk.content}}</p></article><article class="current-chunk"><small>证据所在原文</small><p>{{evidenceContent.before}}<mark v-if="evidenceContent.match">{{evidenceContent.match}}</mark>{{evidenceContent.after}}</p></article><article v-for="chunk in evidenceDetail.next" :key="chunk.chunk_id" class="context-chunk"><small>后文{{chunk.page!=null?` · ${pageLabel(chunk.page)}`:''}}</small><p>{{chunk.content}}</p></article></template></section></div>
  </div>
</template>

<style scoped>
.advanced-controls{border:1px solid #e1e7f0;border-radius:10px;background:white;margin-bottom:12px}.advanced-controls summary{padding:11px 12px;cursor:pointer;font-size:12px;color:#425b88}.advanced-controls summary small{color:#8a97a9;margin-left:8px;font-size:10px}.advanced-controls .view-toolbar{border:0;margin:0}.empty-principles{text-align:left;max-width:600px;font-size:11px;line-height:1.8}.evidence-backdrop{position:fixed;inset:0;z-index:100;background:#172c4f66;display:grid;place-items:center;padding:25px}.evidence-dialog{background:white;border-radius:14px;max-width:850px;width:100%;max-height:85vh;overflow:auto;padding:22px;box-shadow:0 15px 70px #14284944}.evidence-dialog header{display:flex;align-items:center;justify-content:space-between;gap:20px}.evidence-dialog header button{border:0;background:transparent;font-size:23px;cursor:pointer}.evidence-dialog p{white-space:pre-wrap;font-size:13px;line-height:1.8}.evidence-location{color:#78899f}.context-chunk{color:#788399;border-top:1px solid #e7ecf3;padding:12px 0}.current-chunk{background:#f5f8ff;border:1px solid #dce5ff;border-radius:9px;padding:15px}.evidence-dialog small{font-size:11px}.current-chunk mark{background:#ffedb1}
.graph-page{height:100%;overflow:auto;padding:18px;color:#344159;background:#f7f9fc}.stats-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:10px}.stats-grid article{padding:14px 16px;border:1px solid #e1e7f0;border-radius:11px;background:white}.stats-grid span,.stats-grid strong{display:block}.stats-grid span{color:#8b97a8;font-size:10px}.stats-grid strong{margin-top:5px;color:#263249;font-size:22px}.stats-grid .conflict-number{color:#b54708}.error{padding:9px;color:#b42318;background:#fff1f0;border-radius:8px}.graph-toolbar{display:flex;justify-content:space-between;gap:12px;margin:14px 0}.graph-toolbar label{color:#778598;font-size:11px}.graph-toolbar select,.graph-toolbar input,.graph-toolbar button,.view-toolbar select,.view-toolbar button{margin-left:8px;padding:8px 10px;border:1px solid #dce3ed;border-radius:8px;background:white}.graph-toolbar select{min-width:260px}.graph-toolbar button{margin-left:5px;color:white;background:#4f6ef7;border-color:#4f6ef7;cursor:pointer}.view-toolbar{display:flex;align-items:flex-end;gap:7px;flex-wrap:wrap;padding:9px;margin-bottom:12px;border:1px solid #e1e7f0;border-radius:10px;background:white}.view-toolbar label{display:grid;gap:4px;color:#7b8799;font-size:9px}.view-toolbar select{min-width:112px;margin:0;padding:6px}.view-toolbar button{margin:0;padding:7px 9px;color:#4561d8;cursor:pointer}.view-toolbar .confidence-filter{min-width:145px}.confidence-filter input{width:145px}.view-toolbar .check{display:flex;flex-direction:row;align-items:center;gap:5px;padding:7px}.view-toolbar .check input{width:auto}.graph-layout{display:flex;gap:12px}.canvas-card,.inspector{height:520px;border:1px solid #e1e7f0;border-radius:12px;background:white;overflow:auto}.canvas-card{position:relative;flex:1;min-width:0;overflow:hidden}.cy-canvas{width:100%;height:100%;background:radial-gradient(circle at 50% 45%,#fff,#f6f8fc)}.overlay{position:absolute;z-index:2;inset:0;background:white}.graph-summary{position:absolute;left:12px;bottom:10px;max-width:70%;padding:5px 8px;border:1px solid #e0e6ef;border-radius:8px;color:#7a8799;background:#ffffffe8;font-size:9px;pointer-events:none}.load-more{position:absolute;right:12px;bottom:10px;padding:7px 10px;border:1px solid #cfdaff;border-radius:8px;color:#3f5ee6;background:#f3f6ff;cursor:pointer}.inspector{width:320px;flex:0 0 320px;padding:14px}.inspector header{display:flex;justify-content:space-between;color:#7a879a;font-size:11px;text-transform:uppercase}.inspector header button{border:0;background:transparent;cursor:pointer}.inspector h3{font-size:14px;line-height:1.5}.inspector h3 em{color:#4f6ef7;font-style:normal}.manual-badge{padding:3px 7px;border-radius:9px;color:#6b46c1;background:#f1eafe;font-size:9px}.confidence{color:#7d8a9e;font-size:11px}.inspector article{padding:11px;margin-top:9px;border:1px solid #e7ecf3;border-radius:8px}.inspector article strong{font-size:11px}.inspector article p,.entity-detail p{color:#6f7c8e;font-size:11px;line-height:1.55}.inspector article small,.entity-detail small{display:block;color:#9aa5b4;font-size:9px}.entity-result button,.entity-detail button{margin-top:8px;padding:6px 9px;border:1px solid #d5dfff;border-radius:6px;color:#3f5ee6;background:#f3f6ff;cursor:pointer;font-size:10px}.entity-dot{display:inline-block;width:10px;height:10px;margin-right:5px;border-radius:50%}.entity-detail h3{display:inline}.correction-actions{display:flex;gap:7px;margin-top:12px}.correction-actions button{flex:1;padding:7px;border:1px solid #dce3ed;border-radius:7px;background:white;color:#536177;cursor:pointer;font-size:10px}.correction-actions button.danger{color:#b42318}.corrections-card{display:grid;grid-template-columns:1.2fr 1fr;gap:13px;margin-top:13px}.corrections-card>form,.history{padding:14px;border:1px solid #e1e7f0;border-radius:11px;background:white}.corrections-card header{display:flex;justify-content:space-between;margin-bottom:10px}.corrections-card header strong{font-size:12px}.corrections-card header span{color:#98a4b4;font-size:9px}.correction-form{display:grid;grid-template-columns:1fr 130px 1fr;gap:7px}.correction-form input{grid-column:span 2}.correction-form input,.correction-form select,.correction-form button{min-width:0;padding:8px;border:1px solid #dce3ed;border-radius:7px;background:white;font-size:10px}.correction-form button{grid-column:3;color:white;background:#4f6ef7;border-color:#4f6ef7;cursor:pointer}.history{max-height:230px;overflow:auto}.history.full{grid-column:1/-1}.history header>div{display:flex;align-items:center;gap:8px}.history header button{padding:4px 8px;border:1px solid #dce3ed;border-radius:6px;color:#4f6ef7;background:white;cursor:pointer;font-size:9px}.history article{display:flex;align-items:center;gap:8px;padding:8px 0;border-top:1px solid #edf1f6}.history article>div{flex:1}.history article strong,.history article p,.history article small{display:block}.history article strong{font-size:10px}.history article p{margin:2px 0;color:#738095;font-size:9px}.history article p.conflict-reason{color:#b54708}.history article small{color:#9ba6b5;font-size:8px}.history article>span{padding:2px 6px;border-radius:8px;background:#eef2f7;font-size:8px}.history article>span.active{color:#18794e;background:#e9f8ef}.history article>span.conflict{color:#b54708;background:#fff4e5}.history article button{border:0;background:transparent;color:#4f6ef7;cursor:pointer;font-size:9px}.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;color:#97a3b3;font-size:12px}.empty strong{color:#5e6c81;font-size:14px}.empty.small{height:100%;text-align:center;line-height:1.5}.history-empty{padding:25px}@media(max-width:1100px){.stats-grid{grid-template-columns:repeat(3,1fr)}}@media(max-width:950px){.stats-grid{grid-template-columns:repeat(2,1fr)}.graph-layout{display:block}.inspector{box-sizing:border-box;width:100%;height:auto;min-height:180px;margin-top:12px}.graph-toolbar{flex-direction:column}.graph-toolbar select{min-width:180px}.corrections-card{grid-template-columns:1fr}}
.view-toolbar button.enabled{color:#fff;border-color:#4f6ef7;background:#4f6ef7}.canvas-card:fullscreen{width:100vw;height:100vh;border:0;border-radius:0}.community-overview{min-height:480px;padding:16px;border:1px solid #e1e7f0;border-radius:12px;background:#fff}.community-overview>header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}.community-overview>header strong,.community-overview>header small{display:block}.community-overview>header small{margin-top:4px;color:#8995a7;font-size:10px}.community-overview>header>span{color:#637187;font-size:10px}.community-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.community-grid>article{display:flex;min-height:190px;flex-direction:column;padding:13px;border:1px solid #e4eaf3;border-radius:11px;background:linear-gradient(145deg,#fff,#f7f9ff)}.community-title{display:flex;align-items:center;gap:9px}.community-title>span{display:grid;width:38px;height:38px;flex:0 0 38px;place-items:center;border-radius:11px;color:#4561d8;background:#e9eeff;font-size:13px;font-weight:700}.community-title b,.community-title small{display:block}.community-title b{overflow:hidden;color:#344159;font-size:11px;text-overflow:ellipsis;white-space:nowrap}.community-title small{margin-top:3px;color:#8a97a9;font-size:9px}.community-grid article>p{display:-webkit-box;overflow:hidden;margin:10px 0;color:#6f7c8e;font-size:10px;line-height:1.5;-webkit-box-orient:vertical;-webkit-line-clamp:3}.member-chips{display:flex;gap:4px;flex-wrap:wrap}.member-chips button{max-width:110px;overflow:hidden;padding:3px 6px;border:1px solid #dce4f7;border-radius:8px;color:#5268bf;background:#f5f7ff;font-size:8px;text-overflow:ellipsis;white-space:nowrap;cursor:pointer}.member-chips span{padding:3px;color:#8a97a9;font-size:8px}.enter-community{align-self:flex-end;margin-top:auto;padding:6px 9px;border:1px solid #ced9ff;border-radius:7px;color:#4561d8;background:#f3f6ff;font-size:9px;cursor:pointer}.community-empty{min-height:360px}@media(max-width:1100px){.community-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:700px){.community-grid{grid-template-columns:1fr}}
.graph-layout{position:relative}.canvas-card{height:650px;background:#fff}.cy-canvas{background:radial-gradient(circle,#dfe5ee 1px,transparent 1.2px) 0 0/18px 18px}.canvas-heading{position:absolute;z-index:3;top:0;left:0;right:0;display:flex;align-items:center;justify-content:space-between;padding:11px 14px;border-bottom:1px solid #e7ebf1;background:#fffffff2;pointer-events:none}.canvas-heading strong,.canvas-heading small{display:block}.canvas-heading strong{color:#252d3b;font-size:13px}.canvas-heading small{margin-top:3px;color:#8a94a3;font-size:9px}.canvas-heading>span{color:#6f7b8d;font-size:10px}.type-legend{position:absolute;z-index:3;top:64px;left:12px;display:grid;gap:5px;max-width:180px;padding:9px 10px;border:1px solid #e1e6ee;border-radius:9px;background:#fffffff0;box-shadow:0 5px 18px #26384f12;pointer-events:none}.type-legend>strong{margin-bottom:2px;color:#68758a;font-size:9px}.type-legend>span{display:flex;align-items:center;gap:5px;color:#5d697a;font-size:8px}.type-legend i{width:8px;height:8px;flex:0 0 8px;border:2px solid #fff;border-radius:50%;box-shadow:0 0 0 1px #d6dde8}.type-legend b{margin-left:auto;color:#8792a2}.type-legend small{color:#98a3b1;font-size:8px}.graph-summary{bottom:12px;left:12px}.inspector{position:absolute;z-index:5;top:62px;right:12px;height:calc(100% - 76px);border-color:#dce3ed;background:#fffffff7;box-shadow:0 12px 32px #26384f22}.canvas-card:fullscreen .canvas-heading{padding:14px 18px}.canvas-card:fullscreen .inspector{top:68px}.canvas-card:fullscreen .type-legend{top:72px}@media(max-width:950px){.canvas-card{height:590px}.inspector{position:absolute;top:62px;right:10px;box-sizing:border-box;width:min(320px,calc(100% - 20px));height:calc(100% - 76px);margin:0}.canvas-heading>span{display:none}.type-legend{max-width:145px}}

.graph-principles{margin:14px 0;padding:18px;border:1px solid #e0e7f1;border-radius:12px;background:white}.graph-principles header{display:flex;gap:16px;justify-content:space-between}.graph-principles h2{margin:0;font-size:17px;color:#293951}.graph-principles header p{margin:7px 0;color:#708098;font-size:12px;line-height:1.7}.graph-principles header>span{white-space:nowrap;color:#5166af;font-size:11px}.principle-examples{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin:10px 0;font-size:11px;color:#758399}.graph-principles button,.graph-principles select,.saved-views input{padding:8px 10px;border:1px solid #dce4ee;border-radius:7px;background:white;color:#4562bb;font-size:11px;cursor:pointer}.graph-principles button:disabled{opacity:.5;cursor:default}.principle-row{display:flex;align-items:center;gap:9px;margin:8px 0}.principle-row input[type=text],.principle-row>input:not([type]){flex:1;min-width:0;padding:10px;border:1px solid #dce4ee;border-radius:8px;font-size:12px}.principle-row input.disabled{color:#8d98a8;background:#f5f7fa}.principle-actions{display:flex;align-items:end;gap:8px;flex-wrap:wrap;margin-top:12px}.principle-actions label{display:grid;gap:5px;color:#748196;font-size:10px}.principle-actions input{width:90px;padding:7px;border:1px solid #dce4ee;border-radius:7px}.principle-actions button[type=submit],.principle-actions button:not([type]){color:white;background:#4f6ef7;border-color:#4f6ef7}.principle-hint{color:#8b97a7;font-size:12px}.pending-principles{padding:10px;border-radius:8px;color:#9a6509;background:#fff7e4;font-size:12px}.filter-understanding{margin-top:14px;padding:13px;border:1px solid #e1e9f4;border-radius:9px;background:#f8faff;color:#56677e;font-size:12px;line-height:1.7}.filter-understanding>strong{color:#344b73}.filter-understanding small{color:#8491a5}.filter-understanding p{margin:5px 0}.filter-understanding summary{cursor:pointer;color:#5268b5}.filter-warning,.filter-failure{color:#9b6408}.filter-counts strong{margin-left:6px;color:#9b6408}.saved-views{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin-top:14px;padding-top:13px;border-top:1px solid #edf1f6}.saved-views>strong{font-size:12px;color:#4b5b74}.saved-views>small{width:100%;color:#8a97a8;font-size:10px}.saved-views input{color:#344159;cursor:text}.filter-failure{margin-top:12px;padding:10px;border:1px solid #f1dbb6;border-radius:8px;font-size:12px}.view-notice{color:#237658;font-size:11px}.schema-scope{padding:10px;border:1px solid #dfe7f4;border-radius:8px;background:#f7faff;color:#65748c;font-size:12px}
@media(max-width:650px){.graph-principles{padding:12px}.graph-principles header{display:block}.saved-views input,.saved-views select{width:100%}.principle-actions{align-items:stretch}}
</style>
