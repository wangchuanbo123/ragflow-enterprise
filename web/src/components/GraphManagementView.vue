<script setup lang="ts">
import { onMounted, reactive, ref, watch } from 'vue'
import * as api from '../api/client'
import type { EntityMerge, EntityVectorState, GraphCommunity, GraphEntity, GraphQuality, GraphReviewItem, GraphSchema } from '../types'

const props = defineProps<{ knowledgeBaseId: string }>()
const quality = ref<GraphQuality | null>(null)
const schemas = ref<GraphSchema[]>([])
const reviews = ref<GraphReviewItem[]>([])
const merges = ref<EntityMerge[]>([])
const communities = ref<GraphCommunity[]>([])
const vectorState = ref<EntityVectorState | null>(null)
const entities = ref<GraphEntity[]>([])
const entityQuery = ref('')
const loading = ref(false)
const message = ref('')
const error = ref('')
const mergeForm = reactive({ canonical: '', merged: '', reason: '' })
const defaultEntities = ['SYSTEM','MODULE','INTERFACE','REQUIREMENT','TEST_CASE','ROLE','ENVIRONMENT','DOCUMENT','VERSION','TERM','OTHER']
const defaultRelations = ['CONTAINS','PART_OF','DEPENDS_ON','CALLS','PROVIDES','IMPLEMENTS','SATISFIES','VERIFIED_BY','DEPLOYED_ON','OWNED_BY','USES','RELATED_TO']
const schemaForm = reactive({ entities: defaultEntities.join(', '), relations: defaultRelations.join(', '), prompt: '' })

async function load(): Promise<void> {
  loading.value = true
  try {
    ;[quality.value, schemas.value, reviews.value, merges.value, communities.value, vectorState.value] = await Promise.all([
      api.getGraphQuality(props.knowledgeBaseId), api.listGraphSchemas(props.knowledgeBaseId),
      api.listGraphReviews(props.knowledgeBaseId), api.listEntityMerges(props.knowledgeBaseId),
      api.listGraphCommunities(props.knowledgeBaseId), api.getEntityVectorState(props.knowledgeBaseId),
    ])
    error.value = ''
  } catch (e) { error.value = (e as Error).message }
  finally { loading.value = false }
}

async function runBuild(mode: 'pending'|'retry_failed'|'full'): Promise<void> {
  if (mode === 'full' && !window.confirm('全量重建会重新抽取所有 Chunk，确定创建任务吗？')) return
  try { const job = await api.requestGraphBuild(props.knowledgeBaseId, mode); message.value = `图谱任务已创建：${job.id.slice(0, 8)}` }
  catch (e) { error.value = (e as Error).message }
}
async function assess(): Promise<void> { try { quality.value = await api.assessGraphQuality(props.knowledgeBaseId) } catch(e) { error.value=(e as Error).message } }
async function rebuildVectors(): Promise<void> { try { const job=await api.rebuildEntityVectors(props.knowledgeBaseId); message.value=`实体向量任务已创建：${job.id.slice(0,8)}` } catch(e){error.value=(e as Error).message} }
async function rebuildCommunities(): Promise<void> { try { const result=await api.rebuildGraphCommunities(props.knowledgeBaseId); message.value=`已生成 ${result.communities} 个主题子图`; communities.value=await api.listGraphCommunities(props.knowledgeBaseId) } catch(e){error.value=(e as Error).message} }
async function resolve(review: GraphReviewItem, decision: 'accepted'|'rejected'): Promise<void> { try { await api.resolveGraphReview(props.knowledgeBaseId,review.id,decision); reviews.value=await api.listGraphReviews(props.knowledgeBaseId) } catch(e){error.value=(e as Error).message} }
async function activate(schema: GraphSchema): Promise<void> { try { await api.activateGraphSchema(props.knowledgeBaseId,schema.id); schemas.value=await api.listGraphSchemas(props.knowledgeBaseId); message.value='Schema 已激活，旧 Chunk 已标记为待重建' } catch(e){error.value=(e as Error).message} }
async function createSchema(): Promise<void> {
  try {
    await api.createGraphSchema(props.knowledgeBaseId,{ entity_types:schemaForm.entities.split(',').map(v=>v.trim()).filter(Boolean), relation_types:schemaForm.relations.split(',').map(v=>v.trim()).filter(Boolean), normalization_rules:{unicode:'NFKC',case:'lower_ascii'}, extraction_prompt:schemaForm.prompt||null })
    schemas.value=await api.listGraphSchemas(props.knowledgeBaseId); message.value='Schema 草稿已创建'
  } catch(e){error.value=(e as Error).message}
}
async function searchEntities(): Promise<void> { if(!entityQuery.value.trim())return; try{entities.value=await api.searchGraphEntities(props.knowledgeBaseId,entityQuery.value.trim())}catch(e){error.value=(e as Error).message} }
async function scanCandidates(): Promise<void> { try{const result=await api.scanEntityMergeCandidates(props.knowledgeBaseId);message.value=`已扫描 ${result.scanned} 个实体，新增 ${result.candidates} 个候选`;reviews.value=await api.listGraphReviews(props.knowledgeBaseId)}catch(e){error.value=(e as Error).message} }
async function merge(): Promise<void> { try{await api.createEntityMerge(props.knowledgeBaseId,mergeForm.canonical,mergeForm.merged,mergeForm.reason); Object.assign(mergeForm,{canonical:'',merged:'',reason:''}); merges.value=await api.listEntityMerges(props.knowledgeBaseId); message.value='实体合并 Overlay 已生效'}catch(e){error.value=(e as Error).message} }
async function revertMerge(id:string):Promise<void>{try{await api.revertEntityMerge(props.knowledgeBaseId,id);merges.value=await api.listEntityMerges(props.knowledgeBaseId)}catch(e){error.value=(e as Error).message}}

watch(()=>props.knowledgeBaseId,()=>void load()); onMounted(load)
</script>

<template>
  <div class="management-page">
    <p v-if="error" class="error">{{ error }}</p><p v-if="message" class="message">{{ message }}</p>
    <section class="actions">
      <div><strong>图谱构建与就绪状态</strong><small>清理残留 processing、抽取、去重、质量门禁、实体向量和分层图谱由持久化任务串联</small></div>
      <button @click="runBuild('pending')">处理待构建</button><button @click="runBuild('retry_failed')">重试失败项</button><button class="danger" @click="runBuild('full')">全量重建</button>
    </section>
    <div class="overview">
      <section class="quality">
        <header><strong>质量门禁</strong><button @click="assess">立即检查</button></header>
        <div v-if="!quality" class="empty">尚无质量报告</div>
        <template v-else><p class="status" :class="quality.status">{{ quality.status }}</p><div class="metric-grid"><article v-for="(value,key) in quality.metrics_json" :key="key"><span>{{ key }}</span><strong>{{ typeof value==='object'?JSON.stringify(value):value }}</strong></article></div><ul><li v-for="(gate,key) in quality.gate_results_json" :key="key" :class="{passed:gate.passed}">{{ gate.passed?'✓':'!'}} {{ key }}</li></ul><small>{{ new Date(quality.created_at).toLocaleString() }} · {{ quality.graph_version.slice(0,12) }}</small></template>
      </section>
      <section class="indexes">
        <header><strong>派生索引</strong></header>
        <article><div><b>实体语义向量</b><small>{{ vectorState?.status || 'empty' }} · {{ vectorState?.entity_count || 0 }} entities</small></div><button @click="rebuildVectors">重建</button></article>
        <article><div><b>社区层级图谱</b><small>{{ communities.filter(c=>c.level===0).length }} 个主题子图</small></div><button @click="rebuildCommunities">重建</button></article>
        <div class="community" v-for="community in communities.slice(0,6)" :key="community.id"><b>{{ community.title }}</b><small>{{ community.summary }}</small></div>
      </section>
    </div>

    <div class="governance-grid">
      <section>
        <header><strong>Schema 与关系字典</strong><span>{{ schemas.length }} 个版本</span></header>
        <details><summary>创建新草稿</summary><form @submit.prevent="createSchema"><label>实体类型<textarea v-model="schemaForm.entities" /></label><label>关系字典<textarea v-model="schemaForm.relations" /></label><label>附加抽取约束<textarea v-model="schemaForm.prompt" placeholder="可选：领域抽取规则" /></label><button>保存草稿</button></form></details>
        <article v-for="schema in schemas" :key="schema.id"><div><b>v{{ schema.version }}</b><small>{{ schema.entity_types_json.length }} entities · {{ schema.relation_types_json.length }} relations</small></div><span :class="schema.status">{{ schema.status }}</span><button v-if="schema.status!=='active'" @click="activate(schema)">激活</button></article>
      </section>
      <section>
        <header><strong>人工复核队列</strong><span>{{ reviews.length }} 项</span></header>
        <div v-if="!reviews.length" class="empty">没有待复核项</div><article v-for="review in reviews" :key="review.id"><div><b>{{ review.item_type }}</b><small>{{ review.reason }} · {{ Math.round(review.confidence*100) }}%</small></div><button @click="resolve(review,'accepted')">通过</button><button class="reject" @click="resolve(review,'rejected')">驳回</button></article>
      </section>
      <section class="merge-section">
        <header><strong>实体标准化与合并</strong><div><span>可随时拆分回退</span><button @click="scanCandidates">Embedding 候选扫描</button></div></header>
        <form class="entity-search" @submit.prevent="searchEntities"><input v-model="entityQuery" placeholder="搜索候选实体"/><button>搜索</button></form>
        <form class="merge-form" @submit.prevent="merge"><select v-model="mergeForm.canonical" required><option value="">保留的标准实体</option><option v-for="entity in entities" :key="'c'+entity.id" :value="entity.id">{{entity.canonical_name}} · {{entity.entity_type}}</option></select><select v-model="mergeForm.merged" required><option value="">被合并实体</option><option v-for="entity in entities" :key="'m'+entity.id" :value="entity.id">{{entity.canonical_name}} · {{entity.entity_type}}</option></select><input v-model="mergeForm.reason" required minlength="3" placeholder="合并依据"/><button>合并</button></form>
        <article v-for="item in merges" :key="item.id"><div><b>{{ item.canonical_entity_id.slice(0,8) }} ← {{ item.merged_entity_id.slice(0,8) }}</b><small>{{item.reason}}</small></div><span :class="item.status">{{item.status}}</span><button v-if="item.status==='active'" @click="revertMerge(item.id)">拆分</button></article>
      </section>
    </div>
  </div>
</template>

<style scoped>
.management-page{height:100%;overflow:auto;padding:18px;color:#344159}.error,.message{padding:8px;border-radius:7px;font-size:11px}.error{color:#b42318;background:#fff1f0}.message{color:#18794e;background:#ecf9f1}.actions,.overview>section,.governance-grid>section{border:1px solid #e1e7f0;border-radius:11px;background:white}.actions{display:flex;align-items:center;gap:8px;padding:14px}.actions>div{flex:1}.actions strong,.actions small{display:block}.actions small{margin-top:4px;color:#8794a7;font-size:10px}.actions button,header button,form button,article button{padding:7px 9px;border:1px solid #d7e0ff;border-radius:7px;color:#4762df;background:#f5f7ff;cursor:pointer;font-size:10px}.actions .danger,.reject{color:#b42318;border-color:#f2cbc7;background:#fff6f5}.overview{display:grid;grid-template-columns:1.6fr 1fr;gap:12px;margin-top:12px}.overview>section,.governance-grid>section{padding:14px}header{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}header strong{font-size:12px}header span{color:#929eae;font-size:9px}.quality .status{display:inline-block;padding:3px 8px;border-radius:8px;background:#fff4e5;color:#b54708;font-size:9px}.quality .status.ready{background:#e9f8ef;color:#18794e}.metric-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}.metric-grid article{padding:8px;border:1px solid #edf1f6;border-radius:7px}.metric-grid span,.metric-grid strong{display:block;overflow:hidden;text-overflow:ellipsis}.metric-grid span{color:#8895a7;font-size:8px}.metric-grid strong{margin-top:3px;font-size:11px}.quality ul{display:flex;gap:7px;flex-wrap:wrap;padding:0;list-style:none}.quality li{padding:3px 7px;border-radius:7px;color:#b54708;background:#fff4e5;font-size:9px}.quality li.passed{color:#18794e;background:#e9f8ef}.quality>small{color:#98a3b2;font-size:8px}.indexes article,.governance-grid article{display:flex;align-items:center;gap:7px;padding:8px 0;border-top:1px solid #edf1f6}.indexes article div,.governance-grid article div{flex:1}.indexes b,.indexes small,.governance-grid article b,.governance-grid article small{display:block}.indexes small,.governance-grid article small{margin-top:3px;color:#8b97a8;font-size:9px}.community{padding:6px 0}.community b,.community small{display:block;font-size:9px}.community small{color:#929dac}.governance-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}.merge-section{grid-column:1/-1}.governance-grid article>span{padding:3px 6px;border-radius:7px;background:#eef2f7;font-size:8px}.governance-grid article>span.active{color:#18794e;background:#e9f8ef}.governance-grid details{padding:8px;border:1px solid #edf1f6;border-radius:7px}.governance-grid summary{cursor:pointer;font-size:10px}.governance-grid details form{display:grid;gap:7px;margin-top:8px}.governance-grid label{color:#7c899b;font-size:9px}.governance-grid textarea{display:block;width:100%;min-height:50px;margin-top:3px;padding:7px;border:1px solid #dce3ed;border-radius:7px}.entity-search{display:flex;gap:6px}.entity-search input{flex:1}.entity-search input,.merge-form input,.merge-form select{padding:8px;border:1px solid #dce3ed;border-radius:7px}.merge-form{display:grid;grid-template-columns:1fr 1fr 1fr auto;gap:7px;margin:8px 0}.empty{display:grid;place-items:center;min-height:90px;color:#99a5b5;font-size:10px}@media(max-width:1000px){.overview,.governance-grid{grid-template-columns:1fr}.merge-section{grid-column:auto}.actions{align-items:stretch;flex-direction:column}.merge-form{grid-template-columns:1fr}}
</style>
