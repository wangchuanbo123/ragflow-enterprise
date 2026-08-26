<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import * as api from '../api/client'
import type {
  DocumentGraphCoverage,
  EntityMerge, GraphAutomationSettings, GraphCommunity, GraphCorrection,
  Folder, GraphCommunityState, GraphEntity, GraphQuality, GraphReviewItem, GraphSchema,
  GraphSchemaImpact, RelationVectorState, SubgraphEdge, SubgraphNode,
  EntityVectorState, KnowledgeDocument, Tag,
} from '../types'

const props = defineProps<{ knowledgeBaseId: string }>()
const quality = ref<GraphQuality | null>(null)
const schemas = ref<GraphSchema[]>([])
const automation = ref<GraphAutomationSettings>({ auto_build_enabled: true, auto_schema_generation_enabled: true })
const reviews = ref<GraphReviewItem[]>([])
const merges = ref<EntityMerge[]>([])
const relationMerges = ref<GraphCorrection[]>([])
const corrections = ref<GraphCorrection[]>([])
const communities = ref<GraphCommunity[]>([])
const vectorState = ref<EntityVectorState | null>(null)
const relationVectorState = ref<RelationVectorState | null>(null)
const communityState = ref<GraphCommunityState | null>(null)
const coverage = ref<DocumentGraphCoverage>({ summary: { documents: 0, chunks: 0, processed_chunks: 0, fact_chunks: 0, processing_rate: 0, fact_coverage_rate: 0 }, documents: [] })
const schemaImpacts = ref<Record<string, GraphSchemaImpact>>({})
const sourceDocuments = ref<KnowledgeDocument[]>([])
const sourceFolders = ref<Folder[]>([])
const sourceTags = ref<Tag[]>([])
const documentTagMap = ref<Record<string, string[]>>({})
const selectedSourceDocumentIds = ref<string[]>([])
const sourceSelectionMode = ref<'all' | 'selected'>('all')
const sourceQuery = ref('')
const sourceFolderId = ref('')
const sourceTagId = ref('')
const schemaGenerationMode = ref<'new' | 'overwrite'>('new')
const generationTargetSchemaId = ref('')
const schemaJobRunning = ref(false)
const entities = ref<GraphEntity[]>([])
const graphNodes = ref<SubgraphNode[]>([])
const graphEdges = ref<SubgraphEdge[]>([])
const entityQuery = ref('')
const loading = ref(false)
const message = ref('')
const error = ref('')
const selectedNodeId = ref('')
const selectedEdgeId = ref('')
const defaultEntities = ['SYSTEM','MODULE','INTERFACE','REQUIREMENT','TEST_CASE','ROLE','ENVIRONMENT','DOCUMENT','VERSION','TERM','OTHER']
const defaultRelations = ['CONTAINS','PART_OF','DEPENDS_ON','CALLS','PROVIDES','IMPLEMENTS','SATISFIES','VERIFIED_BY','DEPLOYED_ON','OWNED_BY','USES','RELATED_TO']
const schemaForm = reactive({ id: '', entities: defaultEntities.join(', '), relations: defaultRelations.join(', '), prompt: '' })
const schemaGenerationInstructions = ref('')
const mergeForm = reactive({ canonical: '', merged: '', reason: '' })
const relationMergeForm = reactive({ canonical: '', merged: '', reason: '' })
const entityEditForm = reactive({ name: '', description: '', reason: '' })
const relationEditForm = reactive({ predicate: '', confidence: 0.9, reason: '' })
const relationAddForm = reactive({ subject: '', object: '', predicate: '', evidence: '', reason: '' })

const activeSchema = computed(() => schemas.value.find(item => item.status === 'active') || null)
const draftSchemas = computed(() => schemas.value.filter(item => item.status === 'draft'))
const sourceDocumentNameMap = computed(() => new Map(sourceDocuments.value.map(item => [item.id, item.original_filename])))
const filteredSourceDocuments = computed(() => sourceDocuments.value.filter(document =>
  document.status === 'ready' &&
  (!sourceQuery.value || document.original_filename.toLowerCase().includes(sourceQuery.value.toLowerCase())) &&
  (!sourceFolderId.value || document.folder_id === sourceFolderId.value) &&
  (!sourceTagId.value || (documentTagMap.value[document.id] || []).includes(sourceTagId.value)),
))
const relationDictionary = computed(() => activeSchema.value?.relation_types_json || defaultRelations)
const selectedNode = computed(() => graphNodes.value.find(item => item.id === selectedNodeId.value) || null)
const selectedEdge = computed(() => graphEdges.value.find(item => item.id === selectedEdgeId.value) || null)
const selectableEntities = computed(() => {
  const byId = new Map<string, { id: string; label: string; entity_type: string }>()
  for (const entity of entities.value) byId.set(entity.id, { id: entity.id, label: entity.canonical_name, entity_type: entity.entity_type })
  for (const node of graphNodes.value) byId.set(node.id, { id: node.id, label: node.label, entity_type: node.entity_type })
  return [...byId.values()]
})

function flash(text: string): void { message.value = text; error.value = '' }
function fail(e: unknown): void { error.value = (e as Error).message; message.value = '' }

async function load(): Promise<void> {
  loading.value = true
  try {
    ;[quality.value, schemas.value, automation.value, reviews.value, merges.value, relationMerges.value, corrections.value, communities.value, vectorState.value, relationVectorState.value, communityState.value, coverage.value, sourceDocuments.value, sourceFolders.value, sourceTags.value, documentTagMap.value] = await Promise.all([
      api.getGraphQuality(props.knowledgeBaseId), api.listGraphSchemas(props.knowledgeBaseId), api.getGraphAutomation(props.knowledgeBaseId),
      api.listGraphReviews(props.knowledgeBaseId), api.listEntityMerges(props.knowledgeBaseId), api.listRelationMerges(props.knowledgeBaseId),
      api.listGraphCorrections(props.knowledgeBaseId), api.listGraphCommunities(props.knowledgeBaseId), api.getEntityVectorState(props.knowledgeBaseId),
      api.getRelationVectorState(props.knowledgeBaseId), api.getGraphCommunityState(props.knowledgeBaseId), api.getDocumentGraphCoverage(props.knowledgeBaseId),
      api.listDocuments(props.knowledgeBaseId), api.listFolders(props.knowledgeBaseId), api.listTags(props.knowledgeBaseId), api.getDocumentTagMap(props.knowledgeBaseId),
    ])
    selectedSourceDocumentIds.value = selectedSourceDocumentIds.value.filter(id => sourceDocuments.value.some(document => document.id === id && document.status === 'ready'))
    if (generationTargetSchemaId.value && !draftSchemas.value.some(schema => schema.id === generationTargetSchemaId.value)) generationTargetSchemaId.value = ''
    error.value = ''
  } catch (e) { fail(e) }
  finally { loading.value = false }
}

async function toggleAutomation(): Promise<void> {
  try { automation.value = await api.updateGraphAutomation(props.knowledgeBaseId, !automation.value.auto_build_enabled, automation.value.auto_schema_generation_enabled); flash(automation.value.auto_build_enabled ? '文档解析后将自动增量构图' : '已关闭自动构图') }
  catch (e) { fail(e) }
}
async function toggleSchemaAutomation(): Promise<void> {
  try { automation.value = await api.updateGraphAutomation(props.knowledgeBaseId, automation.value.auto_build_enabled, !automation.value.auto_schema_generation_enabled); flash(automation.value.auto_schema_generation_enabled ? '首次文档解析后将自动生成 Schema 草稿' : '已关闭自动生成 Schema 草稿') }
  catch (e) { fail(e) }
}
async function runBuild(profile: 'fast'|'standard'|'deep'): Promise<void> {
  if (profile === 'deep' && !window.confirm('深度构建会全量调用 LLM、重建全部向量和社区，确定继续吗？')) return
  try { const job = await api.requestGraphBuild(props.knowledgeBaseId, profile); flash(`${profile === 'fast' ? '快速' : profile === 'standard' ? '标准' : '深度'}图谱任务已创建：${job.id.slice(0, 8)}`) } catch (e) { fail(e) }
}
async function assess(): Promise<void> { try { quality.value = await api.assessGraphQuality(props.knowledgeBaseId) } catch(e) { fail(e) } }
async function rebuildVectors(): Promise<void> { try { const job=await api.rebuildEntityVectors(props.knowledgeBaseId); flash(`实体向量任务已创建：${job.id.slice(0,8)}`) } catch(e){ fail(e) } }
async function rebuildRelationVectors(): Promise<void> { try { const job=await api.rebuildRelationVectors(props.knowledgeBaseId); flash(`关系向量任务已创建：${job.id.slice(0,8)}`) } catch(e){ fail(e) } }
async function rebuildCommunities(): Promise<void> { try { const result=await api.rebuildGraphCommunities(props.knowledgeBaseId); flash(`已生成 ${result.communities} 个主题子图`); [communities.value, communityState.value]=await Promise.all([api.listGraphCommunities(props.knowledgeBaseId), api.getGraphCommunityState(props.knowledgeBaseId)]) } catch(e){ fail(e) } }
function selectedGenerationDocuments(): string[] | null {
  if (sourceSelectionMode.value === 'all') return []
  if (!selectedSourceDocumentIds.value.length) { fail(new Error('请至少选择一份已就绪文档')); return null }
  if (selectedSourceDocumentIds.value.length > 100) { fail(new Error('一次最多选择 100 份文档')); return null }
  return selectedSourceDocumentIds.value
}
function toggleFilteredSourceDocuments(event: Event): void {
  const ids = filteredSourceDocuments.value.map(document => document.id)
  if ((event.target as HTMLInputElement).checked) selectedSourceDocumentIds.value = [...new Set([...selectedSourceDocumentIds.value, ...ids])]
  else selectedSourceDocumentIds.value = selectedSourceDocumentIds.value.filter(id => !ids.includes(id))
}
function schemaSourceNames(schema: GraphSchema): string {
  const ids = schema.source_document_ids_json || []
  const names = ids.map(id => sourceDocumentNameMap.value.get(id) || id.slice(0, 8))
  return names.slice(0, 5).join('、') + (names.length > 5 ? ` 等 ${names.length} 份` : '')
}
async function trackSchemaJob(jobId: string, action: string): Promise<void> {
  schemaJobRunning.value = true
  for (let attempt = 0; attempt < 60; attempt += 1) {
    await new Promise(resolveDelay => window.setTimeout(resolveDelay, 2000))
    try {
      const job = (await api.listIndexJobs(props.knowledgeBaseId)).find(item => item.id === jobId)
      if (!job || ['pending','running'].includes(job.status)) continue
      schemaJobRunning.value = false
      if (job.status === 'completed' || job.status === 'partial') {
        await load()
        flash(`${action}已完成；若检测到新概念，版本列表中会出现待审核草稿`)
      }
      else fail(new Error(job.error_message || `${action}失败`))
      return
    } catch (e) { schemaJobRunning.value = false; fail(e); return }
  }
  schemaJobRunning.value = false
  flash(`${action}仍在后台运行，可在索引任务页查看进度`)
}
async function generateSchema(): Promise<void> {
  const documentIds=selectedGenerationDocuments(); if(documentIds===null) return
  if(schemaGenerationMode.value==='overwrite' && !generationTargetSchemaId.value){fail(new Error('请选择要覆盖的 Schema 草稿'));return}
  try { const job=await api.generateGraphSchema(props.knowledgeBaseId,schemaGenerationInstructions.value,documentIds,schemaGenerationMode.value,schemaGenerationMode.value==='overwrite'?generationTargetSchemaId.value:null); flash(`已提交文档 Schema 生成任务：${job.id.slice(0,8)}`); void trackSchemaJob(job.id,'Schema 生成') } catch(e){ fail(e) }
}
async function checkSchemaDrift(): Promise<void> {
  const documentIds=selectedGenerationDocuments(); if(documentIds===null) return
  try { const job=await api.checkGraphSchemaDrift(props.knowledgeBaseId,schemaGenerationInstructions.value,documentIds); flash(`已提交 Schema 漂移检查：${job.id.slice(0,8)}`); void trackSchemaJob(job.id,'Schema 漂移检查') } catch(e){ fail(e) }
}
async function previewImpact(schema: GraphSchema): Promise<GraphSchemaImpact | null> { try { const impact=await api.getGraphSchemaImpact(props.knowledgeBaseId,schema.id); schemaImpacts.value={...schemaImpacts.value,[schema.id]:impact}; return impact } catch(e){ fail(e); return null } }
async function resolve(review: GraphReviewItem, decision: 'accepted'|'rejected'): Promise<void> { try { await api.resolveGraphReview(props.knowledgeBaseId,review.id,decision); reviews.value=await api.listGraphReviews(props.knowledgeBaseId) } catch(e){ fail(e) } }
async function prepareReview(review: GraphReviewItem): Promise<void> {
  try {
    if (review.item_type.includes('entity_merge')) {
      const ids = (review.payload_json.entity_ids || []) as string[]
      if (ids.length < 2) return
      const graph = await api.getEntitySubgraph(props.knowledgeBaseId, ids, 0, 100)
      graphNodes.value = graph.nodes; graphEdges.value = graph.edges
      Object.assign(mergeForm, { canonical: ids[0], merged: ids[1], reason: review.reason })
      flash('实体候选已载入，请确认保留方向后合并')
    } else if (review.item_type === 'relation_merge_candidate') {
      const ids = (review.payload_json.relation_ids || []) as string[]
      const triple = (review.payload_json.resolved_triple || []) as string[]
      if (ids.length < 2 || triple.length < 3) return
      const graph = await api.getEntitySubgraph(props.knowledgeBaseId, [triple[0], triple[2]], 0, 100)
      graphNodes.value = graph.nodes; graphEdges.value = graph.edges
      Object.assign(relationMergeForm, { canonical: ids[0], merged: ids[1], reason: review.reason })
      flash('关系候选已载入，请确认保留方向后合并')
    }
  } catch(e) { fail(e) }
}
async function activate(schema: GraphSchema): Promise<void> { const impact=schemaImpacts.value[schema.id] || await previewImpact(schema); if(!impact) return; if(!window.confirm(`激活 v${schema.version} 将影响 ${impact.affected_documents} 个文档、${impact.affected_chunks} 个 Chunk，预计 ${impact.estimated_llm_requests} 次 LLM 请求。确定继续吗？`)) return; try { await api.activateGraphSchema(props.knowledgeBaseId,schema.id,'auto'); schemas.value=await api.listGraphSchemas(props.knowledgeBaseId); flash('Schema 已激活，系统将按影响范围增量重建') } catch(e){ fail(e) } }
function editSchema(schema: GraphSchema): void {
  schemaForm.id = schema.status === 'draft' ? schema.id : ''
  schemaForm.entities = schema.entity_types_json.join(', ')
  schemaForm.relations = schema.relation_types_json.join(', ')
  schemaForm.prompt = schema.extraction_prompt || ''
}
function resetSchemaForm(): void { Object.assign(schemaForm, { id: '', entities: defaultEntities.join(', '), relations: defaultRelations.join(', '), prompt: '' }) }
async function saveSchema(): Promise<void> {
  const payload = { entity_types:schemaForm.entities.split(',').map(v=>v.trim().toUpperCase()).filter(Boolean), relation_types:schemaForm.relations.split(',').map(v=>v.trim().toUpperCase()).filter(Boolean), normalization_rules:{unicode:'NFKC',case:'lower_ascii'}, extraction_prompt:schemaForm.prompt||null }
  try {
    if (schemaForm.id) await api.updateGraphSchema(props.knowledgeBaseId, schemaForm.id, payload)
    else await api.createGraphSchema(props.knowledgeBaseId, payload)
    schemas.value=await api.listGraphSchemas(props.knowledgeBaseId); flash(schemaForm.id ? 'Schema 草稿已更新' : 'Schema 草稿已创建'); resetSchemaForm()
  } catch(e){ fail(e) }
}

async function searchEntities(): Promise<void> {
  if(!entityQuery.value.trim()) return
  try { entities.value=await api.searchGraphEntities(props.knowledgeBaseId,entityQuery.value.trim()); if (entities.value[0]) await openEntity(entities.value[0]) } catch(e){ fail(e) }
}
async function openEntity(entity: GraphEntity | { id: string }): Promise<void> {
  try {
    const graph = await api.getEntitySubgraph(props.knowledgeBaseId, [entity.id], 0, 100)
    graphNodes.value = graph.nodes; graphEdges.value = graph.edges; selectNode(entity.id); selectedEdgeId.value = ''
  } catch(e){ fail(e) }
}
function selectNode(id: string): void {
  selectedNodeId.value = id
  const node = graphNodes.value.find(item => item.id === id)
  if (node) Object.assign(entityEditForm, { name: node.label, description: node.description || '', reason: '' })
}
function selectEdge(edge: SubgraphEdge): void {
  selectedEdgeId.value = edge.id
  Object.assign(relationEditForm, { predicate: edge.predicate, confidence: edge.confidence, reason: '' })
}
async function saveEntityCorrection(): Promise<void> {
  if (!selectedNode.value) return
  try { await api.createGraphCorrection(props.knowledgeBaseId, { action:'update_entity', target_id:selectedNode.value.id, payload:{canonical_name:entityEditForm.name,description:entityEditForm.description}, reason:entityEditForm.reason }); flash('实体人工校正已生效'); await openEntity({id:selectedNode.value.id}); corrections.value=await api.listGraphCorrections(props.knowledgeBaseId) } catch(e){ fail(e) }
}
async function addRelation(): Promise<void> {
  try { const source=relationAddForm.subject; await api.createGraphCorrection(props.knowledgeBaseId, { action:'add_relation', payload:{subject_entity_id:source,object_entity_id:relationAddForm.object,predicate:relationAddForm.predicate,evidence:relationAddForm.evidence,confidence:1}, reason:relationAddForm.reason }); flash('人工关系已新增'); await openEntity({id:source}); corrections.value=await api.listGraphCorrections(props.knowledgeBaseId); Object.assign(relationAddForm,{subject:'',object:'',predicate:'',evidence:'',reason:''}) } catch(e){ fail(e) }
}
async function updateRelation(): Promise<void> {
  if (!selectedEdge.value || selectedEdge.value.id.startsWith('manual:')) return
  try { await api.createGraphCorrection(props.knowledgeBaseId,{action:'update_relation',target_id:selectedEdge.value.id,payload:{predicate:relationEditForm.predicate,confidence:Number(relationEditForm.confidence)},reason:relationEditForm.reason}); flash('关系人工校正已生效'); await openEntity({id:selectedEdge.value.source}); corrections.value=await api.listGraphCorrections(props.knowledgeBaseId) } catch(e){ fail(e) }
}
async function hideRelation(): Promise<void> {
  if (!selectedEdge.value || selectedEdge.value.id.startsWith('manual:') || !window.confirm('确定隐藏这条关系吗？可在校正历史中回退。')) return
  try { const source=selectedEdge.value.source; await api.createGraphCorrection(props.knowledgeBaseId,{action:'hide_relation',target_id:selectedEdge.value.id,reason:relationEditForm.reason || '人工确认隐藏该关系'}); flash('关系已隐藏'); await openEntity({id:source}); corrections.value=await api.listGraphCorrections(props.knowledgeBaseId) } catch(e){ fail(e) }
}
async function revertCorrection(id:string):Promise<void>{try{await api.revertGraphCorrection(props.knowledgeBaseId,id);corrections.value=await api.listGraphCorrections(props.knowledgeBaseId);flash('人工校正已回退')}catch(e){fail(e)}}
async function scanCandidates(): Promise<void> { try{const result=await api.scanEntityMergeCandidates(props.knowledgeBaseId);flash(`已扫描 ${result.scanned} 个实体，新增 ${result.candidates} 个候选`);reviews.value=await api.listGraphReviews(props.knowledgeBaseId)}catch(e){fail(e)} }
async function merge(): Promise<void> { try{await api.createEntityMerge(props.knowledgeBaseId,mergeForm.canonical,mergeForm.merged,mergeForm.reason);Object.assign(mergeForm,{canonical:'',merged:'',reason:''});merges.value=await api.listEntityMerges(props.knowledgeBaseId);flash('实体合并已生效')}catch(e){fail(e)} }
async function revertMerge(id:string):Promise<void>{try{await api.revertEntityMerge(props.knowledgeBaseId,id);merges.value=await api.listEntityMerges(props.knowledgeBaseId);flash('实体合并已回退')}catch(e){fail(e)}}
async function scanRelationCandidates():Promise<void>{try{const result=await api.scanRelationMergeCandidates(props.knowledgeBaseId);flash(`已扫描 ${result.scanned} 条关系，新增 ${result.candidates} 个候选`);reviews.value=await api.listGraphReviews(props.knowledgeBaseId)}catch(e){fail(e)}}
async function mergeRelations():Promise<void>{try{await api.createRelationMerge(props.knowledgeBaseId,relationMergeForm.canonical,relationMergeForm.merged,relationMergeForm.reason);Object.assign(relationMergeForm,{canonical:'',merged:'',reason:''});relationMerges.value=await api.listRelationMerges(props.knowledgeBaseId);flash('关系合并已生效')}catch(e){fail(e)}}
async function revertRelationMerge(id:string):Promise<void>{try{await api.revertRelationMerge(props.knowledgeBaseId,id);relationMerges.value=await api.listRelationMerges(props.knowledgeBaseId);flash('关系合并已回退')}catch(e){fail(e)}}

watch(()=>props.knowledgeBaseId,()=>{selectedSourceDocumentIds.value=[];void load()}); onMounted(load)
</script>

<template>
  <div class="management-page">
    <p v-if="error" class="notice error">{{ error }}</p><p v-if="message" class="notice message">{{ message }}</p>
    <section class="actions card">
      <div><strong>图谱构建策略</strong><small>快速：只处理新增内容；标准：含失败重试与受影响社区；深度：全量抽取和派生索引</small></div>
      <button :class="{enabled:automation.auto_build_enabled}" @click="toggleAutomation">自动构图：{{ automation.auto_build_enabled?'开':'关' }}</button>
      <button :class="{enabled:automation.auto_schema_generation_enabled}" @click="toggleSchemaAutomation">自动 Schema：{{ automation.auto_schema_generation_enabled?'开':'关' }}</button>
      <button @click="runBuild('fast')">快速</button><button @click="runBuild('standard')">标准</button><button class="danger" @click="runBuild('deep')">深度</button>
    </section>

    <div class="overview">
      <section class="card quality"><header><strong>质量门禁</strong><button @click="assess">立即检查</button></header><div v-if="!quality" class="empty">尚无质量报告</div><template v-else><p class="status" :class="quality.status">{{quality.status}}</p><div class="metric-grid"><article v-for="(value,key) in quality.metrics_json" :key="key"><span>{{key}}</span><strong>{{typeof value==='object'?JSON.stringify(value):value}}</strong></article></div><ul><li v-for="(gate,key) in quality.gate_results_json" :key="key" :class="{passed:gate.passed}">{{gate.passed?'✓':'!'}} {{key}}</li></ul></template></section>
      <section class="card indexes"><header><strong>派生索引</strong></header><article><div><b>实体语义向量</b><small>{{vectorState?.status||'empty'}} · {{vectorState?.entity_count||0}} entities</small></div><button @click="rebuildVectors">重建</button></article><article><div><b>关系语义向量</b><small>{{relationVectorState?.status||'empty'}} · {{relationVectorState?.relation_count||0}} relations</small></div><button @click="rebuildRelationVectors">重建</button></article><article><div><b>社区层级图谱</b><small>{{communityState?.status||'empty'}} · {{communities.filter(c=>c.level===0).length}} 个主题子图</small></div><button @click="rebuildCommunities">重建</button></article></section>
    </div>

    <section class="card schema-section">
      <header><div><strong>Schema 与抽取约束</strong><small>活动 Schema 会进入 LLM Prompt，并用于输出和人工关系校验</small></div><span>{{schemas.length}} 个版本</span></header>
      <div class="schema-generator">
        <div class="generator-head"><div><b>从文档生成 Schema 草稿</b><small>每个知识库始终只有一个活动 Schema；生成和漂移检查只产生待审核草稿。</small></div><div class="generator-actions"><button :disabled="schemaJobRunning" @click="checkSchemaDrift">检查 Schema 漂移</button><button :disabled="schemaJobRunning" @click="generateSchema">{{schemaJobRunning?'任务运行中…':'生成 Schema 草稿'}}</button></div></div>
        <div class="source-mode"><label><input v-model="sourceSelectionMode" type="radio" value="all"/>全部已就绪文档（自动采样最多 30 份）</label><label><input v-model="sourceSelectionMode" type="radio" value="selected"/>手动选择文档</label></div>
        <template v-if="sourceSelectionMode==='selected'">
          <div class="source-filters"><input v-model="sourceQuery" placeholder="按文件名筛选"/><select v-model="sourceFolderId"><option value="">全部目录</option><option v-for="folder in sourceFolders" :key="folder.id" :value="folder.id">{{folder.path}}</option></select><select v-model="sourceTagId"><option value="">全部标签</option><option v-for="tag in sourceTags" :key="tag.id" :value="tag.id">{{tag.name}}</option></select></div>
          <div class="source-list"><label class="select-all"><input type="checkbox" :checked="filteredSourceDocuments.length>0 && filteredSourceDocuments.every(document=>selectedSourceDocumentIds.includes(document.id))" @change="toggleFilteredSourceDocuments"/>选择当前筛选结果 · 已选 {{selectedSourceDocumentIds.length}} 份</label><label v-for="document in filteredSourceDocuments" :key="document.id"><input v-model="selectedSourceDocumentIds" type="checkbox" :value="document.id"/><span>{{document.original_filename}}</span><small>{{document.chunk_count}} Chunk</small></label><p v-if="!filteredSourceDocuments.length">没有符合条件的已就绪文档</p></div>
        </template>
        <div class="generation-options"><input v-model="schemaGenerationInstructions" placeholder="可选：补充领域边界或重点概念"/><select v-model="schemaGenerationMode"><option value="new">生成新草稿版本</option><option value="overwrite">覆盖现有草稿</option></select><select v-if="schemaGenerationMode==='overwrite'" v-model="generationTargetSchemaId"><option value="">选择要覆盖的草稿</option><option v-for="schema in draftSchemas" :key="schema.id" :value="schema.id">v{{schema.version}} · {{schema.generation_method}}</option></select></div>
      </div>
      <div v-if="activeSchema" class="active-schema"><b>活动版本 v{{activeSchema.version}}</b><small>{{activeSchema.generation_method==='document_llm'?'文档自动归纳':activeSchema.generation_method==='document_drift'?'漂移建议':'人工创建'}}<template v-if="activeSchema.generation_summary_json"> · {{activeSchema.generation_summary_json.source_documents||0}} 份来源文档</template></small><small v-if="activeSchema.source_document_ids_json?.length">来源：{{schemaSourceNames(activeSchema)}}</small><div><span v-for="item in activeSchema.entity_types_json" :key="item" class="chip entity">{{item}}</span></div><div><span v-for="item in activeSchema.relation_types_json" :key="item" class="chip relation">{{item}}</span></div><p v-if="activeSchema.extraction_prompt">{{activeSchema.extraction_prompt}}</p></div>
      <div class="schema-layout"><form @submit.prevent="saveSchema"><b>{{schemaForm.id?'修改 Schema 草稿':'新建 Schema 版本'}}</b><label>实体类型（逗号分隔）<textarea v-model="schemaForm.entities" required /></label><label>关系类型（逗号分隔）<textarea v-model="schemaForm.relations" required /></label><label>LLM 抽取规则<textarea v-model="schemaForm.prompt" placeholder="领域定义、边界、反例等" /></label><div><button>{{schemaForm.id?'保存修改':'创建草稿'}}</button><button type="button" @click="resetSchemaForm">重置</button></div></form><div class="version-list"><article v-for="schema in schemas" :key="schema.id"><div><b>v{{schema.version}} · {{schema.generation_method==='document_llm'?'文档生成':schema.generation_method==='document_drift'?'漂移建议':'人工'}}</b><small>{{schema.entity_types_json.length}} 实体类型 · {{schema.relation_types_json.length}} 关系类型<template v-if="schemaImpacts[schema.id]"> · 影响 {{schemaImpacts[schema.id].affected_chunks}} Chunk</template></small><small v-if="schema.source_document_ids_json?.length">来源：{{schemaSourceNames(schema)}}</small></div><span :class="schema.status">{{schema.status}}</span><button @click="editSchema(schema)">{{schema.status==='draft'?'编辑':'复制编辑'}}</button><button v-if="schema.status!=='active'" @click="previewImpact(schema)">评估</button><button v-if="schema.status!=='active'" @click="activate(schema)">激活</button></article></div></div>
    </section>

    <section class="card coverage-section">
      <header><div><strong>文档级图谱覆盖报告</strong><small>处理完成率表示构图任务是否结束；事实覆盖率表示产生实体或关系证据的 Chunk 占比。</small></div><button @click="load">刷新报告</button></header>
      <div class="coverage-summary"><article><span>文档</span><strong>{{coverage.summary.documents}}</strong></article><article><span>处理完成率</span><strong>{{Math.round(coverage.summary.processing_rate*100)}}%</strong></article><article><span>事实覆盖率</span><strong>{{Math.round(coverage.summary.fact_coverage_rate*100)}}%</strong></article><article><span>事实 Chunk</span><strong>{{coverage.summary.fact_chunks}} / {{coverage.summary.chunks}}</strong></article></div>
      <div class="coverage-table"><table><thead><tr><th>文档</th><th>状态</th><th>处理完成</th><th>事实覆盖</th><th>实体</th><th>关系</th><th>Schema</th></tr></thead><tbody><tr v-for="item in coverage.documents" :key="item.document_id"><td><b>{{item.filename}}</b><small v-if="item.errors.length">{{item.errors[0]}}</small></td><td><span class="coverage-status" :class="item.coverage_status">{{item.coverage_status}}</span></td><td>{{Math.round(item.processing_rate*100)}}% <small>{{item.completed_chunks+item.failed_chunks}}/{{item.total_chunks}}</small></td><td>{{Math.round(item.fact_coverage_rate*100)}}% <small>{{item.fact_chunks}}/{{item.total_chunks}}</small></td><td>{{item.unique_entities}} <small>{{item.entity_mentions}} 次提及</small></td><td>{{item.unique_relations}} <small>{{item.relation_evidence}} 条证据</small></td><td>{{item.schema_versions.join(', ')||'-'}}</td></tr><tr v-if="!coverage.documents.length"><td colspan="7" class="empty">暂无文档数据</td></tr></tbody></table></div>
    </section>

    <section class="card graph-editor">
      <header><div><strong>图谱人工编辑</strong><small>搜索实体加载两跳子图，所有修改以可审计 Overlay 保存</small></div></header>
      <form class="search" @submit.prevent="searchEntities"><input v-model="entityQuery" placeholder="输入实体名称"/><button>搜索并加载子图</button></form>
      <div v-if="entities.length" class="search-results"><button v-for="entity in entities" :key="entity.id" @click="openEntity(entity)">{{entity.canonical_name}} · {{entity.entity_type}}</button></div>
      <div class="graph-layout">
        <div class="subgraph"><div v-if="!graphNodes.length" class="empty">搜索实体后在此编辑子图</div><template v-else><div class="nodes"><button v-for="node in graphNodes" :key="node.id" :class="{selected:selectedNodeId===node.id}" @click="selectNode(node.id)"><b>{{node.label}}</b><small>{{node.entity_type}}</small></button></div><div class="edges"><button v-for="edge in graphEdges" :key="edge.id" :class="{selected:selectedEdgeId===edge.id}" @click="selectEdge(edge)"><b>{{graphNodes.find(n=>n.id===edge.source)?.label}} → {{graphNodes.find(n=>n.id===edge.target)?.label}}</b><small>{{edge.predicate}} · {{Math.round(edge.confidence*100)}}%</small></button></div></template></div>
        <div class="editors"><form v-if="selectedNode" @submit.prevent="saveEntityCorrection"><b>修改实体</b><input v-model="entityEditForm.name" required placeholder="标准名称"/><textarea v-model="entityEditForm.description" placeholder="实体描述"/><input v-model="entityEditForm.reason" required minlength="3" placeholder="修改原因"/><button>保存实体校正</button></form><form v-if="selectedEdge" @submit.prevent="updateRelation"><b>修改关系</b><select v-model="relationEditForm.predicate" required><option v-for="item in relationDictionary" :key="item">{{item}}</option></select><input v-model.number="relationEditForm.confidence" type="number" min="0" max="1" step="0.01"/><input v-model="relationEditForm.reason" required minlength="3" placeholder="修改/隐藏原因"/><div><button :disabled="selectedEdge.id.startsWith('manual:')">保存关系校正</button><button type="button" class="danger" @click="hideRelation">隐藏</button></div></form><form @submit.prevent="addRelation"><b>新增人工关系</b><select v-model="relationAddForm.subject" required><option value="">主语实体</option><option v-for="item in selectableEntities" :key="'s'+item.id" :value="item.id">{{item.label}}</option></select><select v-model="relationAddForm.predicate" required><option value="">关系类型</option><option v-for="item in relationDictionary" :key="item">{{item}}</option></select><select v-model="relationAddForm.object" required><option value="">宾语实体</option><option v-for="item in selectableEntities" :key="'o'+item.id" :value="item.id">{{item.label}}</option></select><textarea v-model="relationAddForm.evidence" required placeholder="人工证据或依据"/><input v-model="relationAddForm.reason" required minlength="3" placeholder="新增原因"/><button>新增关系</button></form></div>
      </div>
    </section>

    <div class="governance-grid">
      <section class="card"><header><strong>人工复核队列</strong><span>{{reviews.length}} 项</span></header><div v-if="!reviews.length" class="empty">没有待复核项</div><article v-for="review in reviews" :key="review.id"><div><b>{{review.item_type}}</b><small>{{review.reason}} · {{Math.round(review.confidence*100)}}%</small></div><button v-if="review.item_type.includes('merge_candidate')" @click="prepareReview(review)">载入合并</button><button v-else @click="resolve(review,'accepted')">通过</button><button class="danger" @click="resolve(review,'rejected')">驳回</button></article></section>
      <section class="card"><header><strong>校正历史</strong><span>{{corrections.length}} 项</span></header><article v-for="item in corrections.slice(0,20)" :key="item.id"><div><b>{{item.action}}</b><small>{{item.reason}}{{item.conflict_reason?' · '+item.conflict_reason:''}}</small></div><span :class="item.status">{{item.status}}</span><button v-if="item.status==='active'" @click="revertCorrection(item.id)">回退</button></article></section>
      <section class="card merge-section"><header><div><strong>实体消歧与合并</strong><small>语义向量只生成候选，人工确认后生效并可拆分</small></div><button @click="scanCandidates">扫描候选</button></header><form class="merge-form" @submit.prevent="merge"><select v-model="mergeForm.canonical" required><option value="">保留的标准实体</option><option v-for="entity in selectableEntities" :key="'c'+entity.id" :value="entity.id">{{entity.label}} · {{entity.entity_type}}</option></select><select v-model="mergeForm.merged" required><option value="">被合并实体</option><option v-for="entity in selectableEntities" :key="'m'+entity.id" :value="entity.id">{{entity.label}} · {{entity.entity_type}}</option></select><input v-model="mergeForm.reason" required minlength="3" placeholder="合并依据"/><button>合并</button></form><article v-for="item in merges" :key="item.id"><div><b>{{item.canonical_entity_id.slice(0,8)}} ← {{item.merged_entity_id.slice(0,8)}}</b><small>{{item.reason}}</small></div><span :class="item.status">{{item.status}}</span><button v-if="item.status==='active'" @click="revertMerge(item.id)">拆分</button></article></section>
      <section class="card merge-section"><header><div><strong>关系消歧与合并</strong><small>选择当前子图中的两条语义重复关系，保留标准关系并汇聚证据</small></div><button @click="scanRelationCandidates">扫描候选</button></header><form class="merge-form" @submit.prevent="mergeRelations"><select v-model="relationMergeForm.canonical" required><option value="">保留的标准关系</option><option v-for="edge in graphEdges.filter(e=>!e.id.startsWith('manual:'))" :key="'rc'+edge.id" :value="edge.id">{{edge.predicate}} · {{edge.id.slice(0,8)}}</option></select><select v-model="relationMergeForm.merged" required><option value="">被合并关系</option><option v-for="edge in graphEdges.filter(e=>!e.id.startsWith('manual:'))" :key="'rm'+edge.id" :value="edge.id">{{edge.predicate}} · {{edge.id.slice(0,8)}}</option></select><input v-model="relationMergeForm.reason" required minlength="3" placeholder="关系消歧依据"/><button>合并</button></form><article v-for="item in relationMerges" :key="item.id"><div><b>{{String(item.payload.canonical_relation_id||'').slice(0,8)}} ← {{String(item.target_id||'').slice(0,8)}}</b><small>{{item.reason}}</small></div><span :class="item.status">{{item.status}}</span><button v-if="item.status==='active'" @click="revertRelationMerge(item.id)">拆分</button></article></section>
    </div>
  </div>
</template>

<style scoped>
.management-page{height:100%;overflow:auto;padding:18px;color:#344159;background:#f7f9fc}.notice{padding:9px 11px;border-radius:8px;font-size:12px}.error{color:#b42318;background:#fff1f0}.message{color:#18794e;background:#ecf9f1}.card{border:1px solid #e1e7f0;border-radius:12px;background:#fff;box-shadow:0 2px 8px #23395d0a}.actions{display:flex;align-items:center;gap:8px;padding:14px}.actions>div{flex:1}.actions strong,.actions small,header strong,header small{display:block}.actions small,header small{margin-top:4px;color:#8794a7;font-size:10px}button{padding:7px 10px;border:1px solid #d7e0ff;border-radius:7px;color:#4762df;background:#f5f7ff;cursor:pointer;font-size:10px}button:hover{background:#eaf0ff}button.enabled{color:#18794e;border-color:#b9e4ca;background:#ecf9f1}button.danger{color:#b42318;border-color:#f2cbc7;background:#fff6f5}button:disabled{opacity:.5;cursor:not-allowed}.overview{display:grid;grid-template-columns:1.6fr 1fr;gap:12px;margin-top:12px}.overview>section,.schema-section,.graph-editor,.governance-grid>section{padding:14px}header{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}.quality .status{display:inline-block;padding:3px 8px;border-radius:8px;background:#fff4e5;color:#b54708;font-size:9px}.quality .status.ready{background:#e9f8ef;color:#18794e}.metric-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}.metric-grid article{padding:8px;border:1px solid #edf1f6;border-radius:7px}.metric-grid span,.metric-grid strong{display:block;overflow:hidden;text-overflow:ellipsis}.metric-grid span{color:#8895a7;font-size:8px}.metric-grid strong{margin-top:3px;font-size:11px}.quality ul{display:flex;gap:7px;flex-wrap:wrap;padding:0;list-style:none}.quality li{padding:3px 7px;border-radius:7px;color:#b54708;background:#fff4e5;font-size:9px}.quality li.passed{color:#18794e;background:#e9f8ef}.indexes article,.version-list article,.governance-grid article{display:flex;align-items:center;gap:7px;padding:8px 0;border-top:1px solid #edf1f6}.indexes article div,.version-list article div,.governance-grid article div{flex:1}.indexes b,.indexes small,.version-list b,.version-list small,.governance-grid b,.governance-grid small{display:block}.indexes small,.version-list small,.governance-grid small{margin-top:3px;color:#8b97a8;font-size:9px}.schema-section,.graph-editor{margin-top:12px}.active-schema{padding:10px;border-radius:9px;background:linear-gradient(120deg,#f2f6ff,#f5fbff)}.active-schema>div{margin-top:6px}.active-schema p{margin:7px 0 0;color:#64738a;font-size:10px}.chip{display:inline-block;margin:2px;padding:3px 6px;border-radius:8px;font-size:8px}.chip.entity{color:#3155c6;background:#e8eeff}.chip.relation{color:#8756b3;background:#f4eaff}.schema-layout{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:12px}.schema-layout form,.editors form{display:grid;gap:7px;padding:10px;border:1px solid #edf1f6;border-radius:9px}.schema-layout label{font-size:9px;color:#6f7d90}input,select,textarea{box-sizing:border-box;width:100%;padding:8px;border:1px solid #dce3ed;border-radius:7px;background:#fff;color:#344159;font-size:10px}textarea{min-height:55px;resize:vertical}.search{display:flex;gap:7px}.search input{flex:1}.search-results{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}.graph-layout{display:grid;grid-template-columns:1.7fr 1fr;gap:12px;margin-top:10px}.subgraph{min-height:280px;padding:12px;border:1px solid #e6ebf2;border-radius:10px;background:radial-gradient(circle at 50% 20%,#f4f7ff,#fbfcfe)}.nodes{display:flex;justify-content:center;gap:8px;flex-wrap:wrap;padding:12px}.nodes button{min-width:100px;border-color:#cbd8ff;background:#fff;box-shadow:0 2px 7px #526ba91a}.nodes button b,.nodes button small,.edges button b,.edges button small{display:block}.nodes button small,.edges button small{margin-top:3px;color:#8895a7}.nodes button.selected,.edges button.selected{outline:2px solid #7088ee}.edges{display:grid;grid-template-columns:repeat(2,1fr);gap:6px}.edges button{text-align:left;background:#fff}.editors{display:grid;gap:9px}.editors form b{font-size:11px}.governance-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}.merge-section{grid-column:1/-1}.merge-form{display:grid;grid-template-columns:1fr 1fr 1fr auto;gap:7px;margin:8px 0}.governance-grid article>span,.version-list article>span{padding:3px 6px;border-radius:7px;background:#eef2f7;font-size:8px}.governance-grid article>span.active,.version-list article>span.active{color:#18794e;background:#e9f8ef}.empty{display:grid;place-items:center;min-height:90px;color:#99a5b5;font-size:10px}@media(max-width:1000px){.overview,.schema-layout,.graph-layout,.governance-grid{grid-template-columns:1fr}.merge-section{grid-column:auto}.actions{align-items:stretch;flex-direction:column}.merge-form{grid-template-columns:1fr}.edges{grid-template-columns:1fr}}
.schema-generator{display:grid;grid-template-columns:1.1fr 1fr auto;align-items:center;gap:9px;padding:10px;margin-bottom:10px;border:1px solid #d9e4ff;border-radius:9px;background:#f7f9ff}.schema-generator b,.schema-generator small{display:block}.schema-generator small{margin-top:3px;color:#75839a;font-size:9px}.active-schema>small{display:block;margin-top:3px;color:#718096;font-size:9px}.coverage-section{margin-top:12px;padding:14px}.coverage-summary{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:10px}.coverage-summary article{padding:10px;border:1px solid #e5ebf4;border-radius:8px;background:#fafcff}.coverage-summary span,.coverage-summary strong{display:block}.coverage-summary span{color:#8290a4;font-size:9px}.coverage-summary strong{margin-top:4px;font-size:15px}.coverage-table{overflow:auto}.coverage-table table{width:100%;border-collapse:collapse;font-size:10px}.coverage-table th,.coverage-table td{padding:8px;border-top:1px solid #edf1f6;text-align:left;white-space:nowrap}.coverage-table th{color:#7c899d;font-weight:600}.coverage-table td:first-child{max-width:280px;overflow:hidden;text-overflow:ellipsis}.coverage-table td b,.coverage-table td small{display:block}.coverage-table td small{margin-top:2px;color:#8a97aa;font-size:8px}.coverage-status{display:inline-block;padding:3px 6px;border-radius:7px;background:#eef2f7}.coverage-status.covered{color:#18794e;background:#e9f8ef}.coverage-status.no_facts{color:#8b5e00;background:#fff7df}.coverage-status.attention{color:#b42318;background:#fff1f0}.coverage-status.processing{color:#3155c6;background:#e8eeff}@media(max-width:1000px){.schema-generator{grid-template-columns:1fr}.coverage-summary{grid-template-columns:repeat(2,1fr)}}
.schema-generator{display:block}.generator-head{display:flex;align-items:center;justify-content:space-between;gap:12px}.generator-actions{display:flex;gap:7px}.source-mode{display:flex;gap:18px;margin-top:10px;padding:8px;border-radius:8px;background:#fff}.source-mode label{display:flex;align-items:center;gap:6px;font-size:10px}.source-mode input,.source-list input{width:auto}.source-filters{display:grid;grid-template-columns:1.2fr 1fr 1fr;gap:7px;margin-top:8px}.source-list{max-height:190px;overflow:auto;margin-top:7px;padding:7px;border:1px solid #e4e9f2;border-radius:8px;background:#fff}.source-list label{display:grid;grid-template-columns:auto 1fr auto;align-items:center;gap:7px;padding:6px;border-top:1px solid #f0f3f7;font-size:10px}.source-list label:first-child{border-top:0}.source-list label.select-all{color:#4964d7;background:#f7f9ff}.source-list label small{color:#8b97a8}.source-list p{padding:12px;text-align:center;color:#98a4b4;font-size:10px}.generation-options{display:grid;grid-template-columns:1fr auto auto;gap:7px;margin-top:8px}.version-list article>div>small+small{color:#64738a}.active-schema>small+small{color:#64738a}@media(max-width:1000px){.generator-head{align-items:stretch;flex-direction:column}.source-mode{align-items:flex-start;flex-direction:column;gap:7px}.source-filters,.generation-options{grid-template-columns:1fr}}
</style>
