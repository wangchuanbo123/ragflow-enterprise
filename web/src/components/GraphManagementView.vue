<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import * as api from '../api/client'
import type {
  EntityMerge, GraphAutomationSettings, GraphCommunity, GraphCorrection,
  GraphEntity, GraphQuality, GraphReviewItem, GraphSchema, SubgraphEdge,
  SubgraphNode, EntityVectorState,
} from '../types'

const props = defineProps<{ knowledgeBaseId: string }>()
const quality = ref<GraphQuality | null>(null)
const schemas = ref<GraphSchema[]>([])
const automation = ref<GraphAutomationSettings>({ auto_build_enabled: true })
const reviews = ref<GraphReviewItem[]>([])
const merges = ref<EntityMerge[]>([])
const relationMerges = ref<GraphCorrection[]>([])
const corrections = ref<GraphCorrection[]>([])
const communities = ref<GraphCommunity[]>([])
const vectorState = ref<EntityVectorState | null>(null)
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
const mergeForm = reactive({ canonical: '', merged: '', reason: '' })
const relationMergeForm = reactive({ canonical: '', merged: '', reason: '' })
const entityEditForm = reactive({ name: '', description: '', reason: '' })
const relationEditForm = reactive({ predicate: '', confidence: 0.9, reason: '' })
const relationAddForm = reactive({ subject: '', object: '', predicate: '', evidence: '', reason: '' })

const activeSchema = computed(() => schemas.value.find(item => item.status === 'active') || null)
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
    ;[quality.value, schemas.value, automation.value, reviews.value, merges.value, relationMerges.value, corrections.value, communities.value, vectorState.value] = await Promise.all([
      api.getGraphQuality(props.knowledgeBaseId), api.listGraphSchemas(props.knowledgeBaseId), api.getGraphAutomation(props.knowledgeBaseId),
      api.listGraphReviews(props.knowledgeBaseId), api.listEntityMerges(props.knowledgeBaseId), api.listRelationMerges(props.knowledgeBaseId),
      api.listGraphCorrections(props.knowledgeBaseId), api.listGraphCommunities(props.knowledgeBaseId), api.getEntityVectorState(props.knowledgeBaseId),
    ])
    error.value = ''
  } catch (e) { fail(e) }
  finally { loading.value = false }
}

async function toggleAutomation(): Promise<void> {
  try { automation.value = await api.updateGraphAutomation(props.knowledgeBaseId, !automation.value.auto_build_enabled); flash(automation.value.auto_build_enabled ? '文档解析后将自动增量构图' : '已关闭自动构图') }
  catch (e) { fail(e) }
}
async function runBuild(mode: 'pending'|'retry_failed'|'full'): Promise<void> {
  if (mode === 'full' && !window.confirm('全量重建会重新调用 LLM 抽取所有 Chunk，确定继续吗？')) return
  try { const job = await api.requestGraphBuild(props.knowledgeBaseId, mode); flash(`图谱任务已创建：${job.id.slice(0, 8)}`) } catch (e) { fail(e) }
}
async function assess(): Promise<void> { try { quality.value = await api.assessGraphQuality(props.knowledgeBaseId) } catch(e) { fail(e) } }
async function rebuildVectors(): Promise<void> { try { const job=await api.rebuildEntityVectors(props.knowledgeBaseId); flash(`实体向量任务已创建：${job.id.slice(0,8)}`) } catch(e){ fail(e) } }
async function rebuildCommunities(): Promise<void> { try { const result=await api.rebuildGraphCommunities(props.knowledgeBaseId); flash(`已生成 ${result.communities} 个主题子图`); communities.value=await api.listGraphCommunities(props.knowledgeBaseId) } catch(e){ fail(e) } }
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
async function activate(schema: GraphSchema): Promise<void> { try { await api.activateGraphSchema(props.knowledgeBaseId,schema.id); schemas.value=await api.listGraphSchemas(props.knowledgeBaseId); flash('Schema 已激活，旧 Chunk 已标记为待重建') } catch(e){ fail(e) } }
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

watch(()=>props.knowledgeBaseId,()=>void load()); onMounted(load)
</script>

<template>
  <div class="management-page">
    <p v-if="error" class="notice error">{{ error }}</p><p v-if="message" class="notice message">{{ message }}</p>
    <section class="actions card">
      <div><strong>图谱构建</strong><small>文档解析后按活动 Schema 自动调用 LLM 增量抽取，并重建实体/关系向量与社区</small></div>
      <button :class="{enabled:automation.auto_build_enabled}" @click="toggleAutomation">自动构图：{{ automation.auto_build_enabled?'开':'关' }}</button>
      <button @click="runBuild('pending')">处理待构建</button><button @click="runBuild('retry_failed')">重试失败</button><button class="danger" @click="runBuild('full')">全量重建</button>
    </section>

    <div class="overview">
      <section class="card quality"><header><strong>质量门禁</strong><button @click="assess">立即检查</button></header><div v-if="!quality" class="empty">尚无质量报告</div><template v-else><p class="status" :class="quality.status">{{quality.status}}</p><div class="metric-grid"><article v-for="(value,key) in quality.metrics_json" :key="key"><span>{{key}}</span><strong>{{typeof value==='object'?JSON.stringify(value):value}}</strong></article></div><ul><li v-for="(gate,key) in quality.gate_results_json" :key="key" :class="{passed:gate.passed}">{{gate.passed?'✓':'!'}} {{key}}</li></ul></template></section>
      <section class="card indexes"><header><strong>派生索引</strong></header><article><div><b>实体语义向量</b><small>{{vectorState?.status||'empty'}} · {{vectorState?.entity_count||0}} entities</small></div><button @click="rebuildVectors">重建</button></article><article><div><b>社区层级图谱</b><small>{{communities.filter(c=>c.level===0).length}} 个主题子图</small></div><button @click="rebuildCommunities">重建</button></article></section>
    </div>

    <section class="card schema-section">
      <header><div><strong>Schema 与抽取约束</strong><small>活动 Schema 会进入 LLM Prompt，并用于输出和人工关系校验</small></div><span>{{schemas.length}} 个版本</span></header>
      <div v-if="activeSchema" class="active-schema"><b>活动版本 v{{activeSchema.version}}</b><div><span v-for="item in activeSchema.entity_types_json" :key="item" class="chip entity">{{item}}</span></div><div><span v-for="item in activeSchema.relation_types_json" :key="item" class="chip relation">{{item}}</span></div><p v-if="activeSchema.extraction_prompt">{{activeSchema.extraction_prompt}}</p></div>
      <div class="schema-layout"><form @submit.prevent="saveSchema"><b>{{schemaForm.id?'修改 Schema 草稿':'新建 Schema 版本'}}</b><label>实体类型（逗号分隔）<textarea v-model="schemaForm.entities" required /></label><label>关系类型（逗号分隔）<textarea v-model="schemaForm.relations" required /></label><label>LLM 抽取规则<textarea v-model="schemaForm.prompt" placeholder="领域定义、边界、反例等" /></label><div><button>{{schemaForm.id?'保存修改':'创建草稿'}}</button><button type="button" @click="resetSchemaForm">重置</button></div></form><div class="version-list"><article v-for="schema in schemas" :key="schema.id"><div><b>v{{schema.version}}</b><small>{{schema.entity_types_json.length}} 实体类型 · {{schema.relation_types_json.length}} 关系类型</small></div><span :class="schema.status">{{schema.status}}</span><button @click="editSchema(schema)">{{schema.status==='draft'?'编辑':'复制编辑'}}</button><button v-if="schema.status!=='active'" @click="activate(schema)">激活</button></article></div></div>
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
</style>
