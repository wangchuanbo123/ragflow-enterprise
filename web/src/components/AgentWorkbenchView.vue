<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import * as api from '../api/client'
import { agentCostSummary, agentElapsed, agentEvidence, agentProgress, agentRunMarkdown, agentStatusLabels, agentSummary, agentTaskLabel, agentTasks, asRecord, generatedEvaluationCases, isActiveAgentRun, parseAgentTime, type AgentEvidence, type AgentRun, type AgentRunEvent, type AgentTask, type AgentTaskCatalog, type GeneratedEvaluationCase } from '../agents'
import type { ChunkDetail, DataSource, Folder, IndexJob, KnowledgeDocument, Tag } from '../types'
import { usePermissions } from '../composables/usePermissions'
import { navigate, navigationState } from '../composables/useNavigation'
import AnswerContent from './AnswerContent.vue'
import StructuredResult from './StructuredResult.vue'

const props=defineProps<{knowledgeBaseId:string}>()
const {canEdit}=usePermissions()
const catalog=ref<AgentTaskCatalog[]>([])
const documents=ref<KnowledgeDocument[]>([])
const folders=ref<Folder[]>([])
const tags=ref<Tag[]>([])
const tagMap=ref<Record<string,string[]>>({})
const sources=ref<DataSource[]>([])
const jobs=ref<IndexJob[]>([])
const jobIds=ref<string[]>([])
const selectedActions=ref<string[]>([])
const runs=ref<AgentRun[]>([])
const selected=ref<AgentRun|null>(null)
const selectedId=ref('')
const error=ref('')
const notice=ref('')
const loading=ref(false)
const detailLoading=ref(false)
const submitting=ref(false)
const actionPending=ref(false)
const exporting=ref(false)
const taskFilter=ref('')
const statusFilter=ref('')
const scopeKind=ref<'all'|'documents'|'folder'|'tag'>('all')
const documentIds=ref<string[]>([])
const folderId=ref('')
const tagId=ref('')
const questions=ref('')
const generateCases=ref(false)
const maxGeneratedCases=ref(5)
const importedCandidates=ref<GeneratedEvaluationCase[]>([])
const createPanel=ref<HTMLDetailsElement|null>(null)
const useGoldCases=ref(false)
const goldAnswers=reactive<Record<number,string>>({})
const goldDocuments=reactive<Record<number,string[]>>({})
const goldChunks=reactive<Record<number,string>>({})
const comparisonProfiles=ref<string[]>([])
const comparisonProfilesText=ref('')
const promptVariants=ref<Array<{name:string;instructions:string}>>([])
const pageOffset=ref(0)
const runTotal=ref(0)
const pageSize=50
const form=reactive({task:'extraction' as AgentTask,instructions:'从文档中提取关键实体、关系及支持这些事实的原文证据。',apply:false,mode:'pending',data_source_id:'',runtime_profile:'',max_actions:10,max_runtime_seconds:900,max_agent_calls:40})
const evidence=ref<AgentEvidence|null>(null)
const evidenceDetail=ref<ChunkDetail|null>(null)
const evidenceLoading=ref(false)
const evidenceError=ref('')
let disposed=false
let timer:number|undefined
let listRequest=0
let detailRequest=0
let evidenceRequest=0
let detailController:AbortController|null=null

const taskInfo=computed(()=>agentTasks.find(item=>item.id===form.task)!)
const taskCatalog=computed(()=>catalog.value.find(item=>item.task===form.task))
const allowedActions=computed(()=>taskCatalog.value?.allowed_actions||[])
const runtimeProfiles=computed(()=>{const value=taskCatalog.value?.options.runtime_profiles;return Array.isArray(value)?value.filter(item=>typeof item==='string') as string[]:[]})
const parsedQuestions=computed(()=>questions.value.split('\n').map(item=>item.trim()).filter(Boolean))
const evaluationProfiles=computed(()=>[...new Set(runtimeProfiles.value.length?comparisonProfiles.value:comparisonProfilesText.value.split('\n').map(item=>item.trim()).filter(Boolean))])
const comparisonCount=computed(()=>parsedQuestions.value.length*Math.max(1,evaluationProfiles.value.length)*Math.max(1,promptVariants.value.length))
const scopeIds=computed(()=>{
  if(scopeKind.value==='documents')return documentIds.value
  if(scopeKind.value==='tag')return documents.value.filter(item=>(tagMap.value[item.id]||[]).includes(tagId.value)).map(item=>item.id)
  if(scopeKind.value==='folder'){
    const folder=folders.value.find(item=>item.id===folderId.value)
    if(!folder)return []
    const ids=new Set(folders.value.filter(item=>item.id===folder.id||item.path.startsWith(`${folder.path}/`)).map(item=>item.id))
    return documents.value.filter(item=>item.folder_id&&ids.has(item.folder_id)).map(item=>item.id)
  }
  return []
})
const summary=computed(()=>agentSummary(selected.value?.output))
const generatedCases=computed(()=>generatedEvaluationCases(selected.value?.output))
const generationOnly=computed(()=>form.task==='evaluation'&&generateCases.value&&!parsedQuestions.value.length)
const outputRecord=computed(()=>asRecord(selected.value?.output))
const evidenceItems=computed(()=>agentEvidence(selected.value?.output))
const actions=computed(()=>{const value=outputRecord.value?.actions;return Array.isArray(value)&&value.length?value:selected.value?.actions||[]})
const diagnostics=computed(()=>outputRecord.value?.diagnostics||selected.value?.diagnostics)
const metrics=computed(()=>outputRecord.value?.metrics)
const otherOutput=computed(()=>{
  const output=outputRecord.value
  if(!output)return typeof selected.value?.output==='string'?null:selected.value?.output
  return Object.fromEntries(Object.entries(output).filter(([key])=>!['summary','answer','report','evidence','sources','actions','diagnostics','metrics','generated_cases'].includes(key)))
})
const hasOtherOutput=computed(()=>{const value=otherOutput.value;return value!==null&&value!==undefined&&(Array.isArray(value)?value.length>0:asRecord(value)?Object.keys(value as object).length>0:true)})
const progressMessage=computed(()=>{const progress=selected.value?.progress;const stages:Record<string,string>={queued:'等待执行',collecting_context:'收集文档与图谱上下文',agent_planning:'智能体正在规划与分析',applying:'执行已授权动作',review:'整理可审阅结果',waiting_for_jobs:'等待关联业务任务完成',completed:'任务完成',failed:'执行失败',cancelled:'已取消'};return typeof progress==='object' ? [progress.message||stages[progress.stage||'']||progress.stage,progress.total?`${progress.current||0} / ${progress.total}`:''].filter(Boolean).join(' · '):''})
const usage=computed(()=>selected.value?.usage||{})
const costSummary=computed(()=>agentCostSummary(usage.value))
const tokenTotal=computed(()=>{
  return typeof usage.value.total_tokens==='number'&&Number.isFinite(usage.value.total_tokens)?usage.value.total_tokens:null
})
const hasMetrics=computed(()=>{const value=metrics.value;return value&&(!asRecord(value)||Object.keys(value as object).length>0)})
const activeSources=computed(()=>sources.value.filter(source=>source.status==='active'&&source.source_type!=='upload'))
const retryableJobs=computed(()=>jobs.value.filter(job=>job.job_type!=='agent_workflow'&&['failed','cancelled','partial'].includes(job.status)))

function actionLabel(action:string):string {
  return ({build_graph:'构建图谱',extract:'保存知识抽取',extract_document:'保存文档抽取',reextract_documents:'重新抽取指定文档',sync_source:'同步数据源',sync_sources:'同步数据源',index_documents:'创建索引任务',create_schema_draft:'保存结构草稿',rebuild_entity_vectors:'重建实体向量',rebuild_relation_vectors:'重建关系向量',rebuild_communities:'重建主题社区',create_folder:'创建文件夹',create_tag:'创建标签',tag_documents:'调整文档标签',move_documents:'移动文档分类',retry_job:'重试后台任务',retry_jobs:'重试指定后台任务',reindex_documents:'重建文档索引',reconcile_corrections:'检查人工修订冲突',propose_correction:'记录修复建议',apply_correction:'应用经过校验的修复',evaluate_cases:'执行评测用例',adjust_batch:'调整知识库处理批次',select_model_profile:'切换已登记的后续执行配置'} as Record<string,string>)[action]||action.replace(/_/g,' ')
}
function timeLabel(value?:string|null):string {return value?new Date(parseAgentTime(value)).toLocaleString():'—'}
function eventLabel(event:AgentRunEvent):string {
  if(event.message)return event.message
  const data=asRecord(event.data)
  const status=String(data?.status||'')
  const state=({completed:'已完成',running:'执行中',pending:'等待执行',error:'失败',failed:'失败'} as Record<string,string>)[status]||status
  if(data?.tool)return `工具步骤：${data.tool}${state?` · ${state}`:''}`
  const kind=String(data?.event||event.type)
  return ({step:'推理步骤完成',session:'执行会话已创建',tool:'知识工具调用',agent_event:'智能体执行事件'} as Record<string,string>)[kind]||kind
}
function updateRun(run:AgentRun):void {
  const index=runs.value.findIndex(item=>item.id===run.id)
  const matches=(!taskFilter.value||run.task===taskFilter.value)&&(!statusFilter.value||run.status===statusFilter.value)
  if(index>=0){if(matches)runs.value[index]=run;else void loadRuns(true)}
}
async function loadRuns(silent=false):Promise<void> {
  const requested=++listRequest
  if(!silent)loading.value=true
  try {
    const loaded=await api.listAgentRunPage(props.knowledgeBaseId,{offset:pageOffset.value,limit:pageSize,task:taskFilter.value,status:statusFilter.value})
    if(disposed||requested!==listRequest)return
    runs.value=loaded.items;runTotal.value=loaded.total
    if(pageOffset.value>0&&!loaded.items.length&&loaded.total<=pageOffset.value){pageOffset.value=Math.max(0,Math.floor(Math.max(0,loaded.total-1)/pageSize)*pageSize);await loadRuns(true)}
  }catch(e){if(!disposed&&requested===listRequest&&(e as Error).name!=='AbortError')error.value=(e as Error).message}
  finally{if(requested===listRequest)loading.value=false}
}
async function selectRun(id:string,silent=false):Promise<void> {
  if(silent&&detailLoading.value)return
  const requested=++detailRequest
  detailController?.abort();detailController=new AbortController()
  if(selectedId.value!==id){selected.value=null;evidence.value=null}
  selectedId.value=id
  if(!silent)detailLoading.value=true
  try {
    const run=await api.getAgentRun(props.knowledgeBaseId,id,detailController.signal)
    if(disposed||requested!==detailRequest)return
    selected.value=run;updateRun(run)
    if(navigationState.value.page==='agents'&&navigationState.value.kb===props.knowledgeBaseId)navigate({agentRun:id},silent)
  }catch(e){if(!disposed&&requested===detailRequest&&(e as Error).name!=='AbortError')error.value=(e as Error).message}
  finally{if(requested===detailRequest)detailLoading.value=false}
}
async function initialize():Promise<void> {
  loading.value=true;error.value=''
  try {
    const loaded=await Promise.all([api.listAgentTaskCatalog(props.knowledgeBaseId),api.listDocuments(props.knowledgeBaseId),api.listFolders(props.knowledgeBaseId),api.listTags(props.knowledgeBaseId),api.getDocumentTagMap(props.knowledgeBaseId),canEdit.value?api.listDataSources(props.knowledgeBaseId):Promise.resolve([]),canEdit.value?api.listIndexJobs(props.knowledgeBaseId):Promise.resolve([])])
    if(disposed)return
    ;[catalog.value,documents.value,folders.value,tags.value,tagMap.value,sources.value,jobs.value]=loaded
    await loadRuns()
    if(disposed)return
    const id=navigationState.value.agentRun||runs.value[0]?.id
    if(id)await selectRun(id)
  }catch(e){if(!disposed&&(e as Error).name!=='AbortError')error.value=(e as Error).message}
  finally{loading.value=false}
}
async function submit():Promise<void> {
  if(!canEdit.value||submitting.value)return
  error.value='';notice.value=''
  if(scopeKind.value!=='all'&&!scopeIds.value.length){error.value='请至少选择一份文档；空范围不会自动扩大为整个知识库';return}
  if(scopeIds.value.length>500){error.value='单次任务最多选择 500 份文档，请缩小范围';return}
  if(!form.instructions.trim()){error.value='请填写本次任务目标';return}
  if(!Number.isInteger(form.max_actions)||form.max_actions<1||form.max_actions>50){error.value='授权动作上限须为 1–50 的整数';return}
  if(!Number.isInteger(form.max_runtime_seconds)||form.max_runtime_seconds<30||form.max_runtime_seconds>3600){error.value='任务总时限须为 30–3600 秒的整数';return}
  if(!Number.isInteger(form.max_agent_calls)||form.max_agent_calls<1||form.max_agent_calls>200){error.value='智能体调用上限须为 1–200 的整数';return}
  const evaluationQuestions=parsedQuestions.value
  if(form.task==='evaluation'&&((!evaluationQuestions.length&&!generateCases.value)||evaluationQuestions.length>20||evaluationQuestions.some(item=>item.length>4000))){error.value='评测需填写 1–20 个问题，或开启自动生成评测样例；每个问题最多 4000 字';return}
  if(form.apply&&!selectedActions.value.length){error.value='请至少授权一个此任务允许的动作';return}
  if(!selectedActions.value.every(action=>allowedActions.value.includes(action))){error.value='任务动作已变更，请重新选择';return}
  const options:Record<string,unknown>={apply:form.apply,allowed_actions:[...selectedActions.value],max_actions:form.max_actions,max_runtime_seconds:form.max_runtime_seconds,max_agent_calls:form.max_agent_calls,graph_kind:'general',mode:form.mode,runtime_profile:form.runtime_profile.trim()||null}
  if(form.task==='ingestion')options.data_source_id=form.data_source_id||null
  if(form.task==='operations'){
    if(jobIds.value.length>20){error.value='单次最多授权重试 20 个业务任务';return}
    options.job_ids=jobIds.value
  }
  if(form.task==='evaluation'){
    if(generateCases.value&&(!Number.isInteger(maxGeneratedCases.value)||maxGeneratedCases.value<1||maxGeneratedCases.value>20)){error.value='自动生成样例数量须为 1–20 的整数';return}
    options.generate_cases=generateCases.value
    options.max_generated_cases=generateCases.value?maxGeneratedCases.value:5
    if(!generationOnly.value&&(evaluationProfiles.value.length>5||promptVariants.value.length>5||comparisonCount.value>20)){error.value='单次评测最多 20 个问题、模型、提示词组合；请减少比较范围';return}
    if(!generationOnly.value&&promptVariants.value.some(variant=>!variant.name.trim()||!variant.instructions.trim())){error.value='请填写每组对照提示词的名称和内容';return}
    options.comparison_profiles=generationOnly.value?[]:[...new Set(evaluationProfiles.value)]
    options.prompt_variants=generationOnly.value?[]:promptVariants.value.map(variant=>({name:variant.name.trim(),instructions:variant.instructions.trim()}))
    if(useGoldCases.value){
      const scope=new Set(scopeKind.value==='all'?documents.value.map(document=>document.id):scopeIds.value)
      if(evaluationQuestions.some((_question,index)=>(goldDocuments[index]||[]).some(id=>!scope.has(id)))){error.value='期望命中的文档必须位于本次任务范围内';return}
      if(evaluationQuestions.some((_question,index)=>(goldDocuments[index]||[]).length>100||(goldChunks[index]||'').split(/[\s,，]+/).filter(Boolean).length>100)){error.value='每个评测问题最多指定 100 份期望文档或片段';return}
      options.gold_cases=evaluationQuestions.map((question,index)=>({question,reference_answer:goldAnswers[index]||'',expected_document_ids:goldDocuments[index]||[],expected_chunk_ids:(goldChunks[index]||'').split(/[\s,，]+/).filter(Boolean),...(importedCandidates.value[index]?{provenance:'agent_generated',source_run_id:importedCandidates.value[index].source_run_id||selected.value?.id,candidate_id:importedCandidates.value[index].id}:{} )}))
      options.questions=[]
    }else options.questions=evaluationQuestions
  }
  submitting.value=true
  try {
    const run=await api.createAgentRun(props.knowledgeBaseId,{task:form.task,instructions:form.instructions.trim(),document_ids:[...new Set(scopeIds.value)],options})
    if(disposed)return
    detailRequest++;detailController?.abort()
    pageOffset.value=0;selected.value=run;selectedId.value=run.id;updateRun(run)
    navigate({agentRun:run.id});notice.value='任务已创建，可查看执行事件和后续结果'
    await loadRuns(true);await selectRun(run.id)
  }catch(e){if(!disposed&&(e as Error).name!=='AbortError')error.value=(e as Error).message}
  finally{submitting.value=false}
}
async function control(kind:'cancel'|'retry'):Promise<void> {
  if(!selected.value||!canEdit.value||actionPending.value)return
  const id=selected.value.id;actionPending.value=true;error.value=''
  try {
    const run=kind==='cancel'?await api.cancelAgentRun(props.knowledgeBaseId,id):await api.retryAgentRun(props.knowledgeBaseId,id)
    if(disposed)return
    detailRequest++;detailController?.abort()
    updateRun(run);selected.value=run;selectedId.value=run.id
    notice.value=kind==='cancel'?'取消请求已提交，已完成的动作可在结果中核对':'重试任务已创建'
    await loadRuns(true);await selectRun(run.id)
  }catch(e){if(!disposed&&(e as Error).name!=='AbortError')error.value=(e as Error).message}
  finally{actionPending.value=false}
}
async function changePage(delta:number):Promise<void> {pageOffset.value=Math.max(0,pageOffset.value+delta*pageSize);await loadRuns()}
async function openEvidence(item:AgentEvidence):Promise<void> {
  const requested=++evidenceRequest;evidence.value=item;evidenceDetail.value=null;evidenceError.value=''
  if(!item.chunk_id)return
  evidenceLoading.value=true
  try{const result=await api.getChunkDetail(props.knowledgeBaseId,item.chunk_id);if(requested===evidenceRequest)evidenceDetail.value=result}
  catch(e){if(requested===evidenceRequest&&(e as Error).name!=='AbortError')evidenceError.value=(e as Error).message}
  finally{if(requested===evidenceRequest)evidenceLoading.value=false}
}
function openCitation(label:string):void {
  const index=Number(label.replace(/\D/g,''))-1
  const item=evidenceItems.value.find(source=>source.citation_id?.replace(/[\s\[\]]/g,'')===label.replace(/[\s\[\]]/g,''))||evidenceItems.value[index]
  if(item)void openEvidence(item)
}
async function downloadResult(format:'json'|'markdown'='json'):Promise<void> {
  if(!selected.value||exporting.value)return
  const id=selected.value.id;exporting.value=true
  try{
    const artifact=await api.getAgentRunArtifact(props.knowledgeBaseId,id)
    if(disposed)return
    const blob=new Blob([format==='json'?JSON.stringify(artifact,null,2):agentRunMarkdown(artifact)],{type:format==='json'?'application/json;charset=utf-8':'text/markdown;charset=utf-8'})
    const url=URL.createObjectURL(blob);const anchor=document.createElement('a');anchor.href=url;anchor.download=`知识智能体-${artifact.task}-${artifact.id}.${format==='json'?'json':'md'}`;anchor.click();window.setTimeout(()=>URL.revokeObjectURL(url),1000)
  }catch(e){if(!disposed&&(e as Error).name!=='AbortError')error.value=`导出完整产物失败：${(e as Error).message}`}
  finally{exporting.value=false}
}
async function useGeneratedCases():Promise<void> {
  if(!canEdit.value||!generatedCases.value.length||!selected.value)return
  const source=selected.value
  const candidates=generatedCases.value.slice(0,20).map(item=>({...item,source_run_id:item.source_run_id||source.id}))
  form.task='evaluation'
  await nextTick()
  generateCases.value=false;form.apply=false;useGoldCases.value=false
  importedCandidates.value=candidates
  questions.value=candidates.map(item=>item.question.replace(/[\r\n]+/g,' ').slice(0,4000)).join('\n')
  const scope=source.document_ids.filter(id=>documents.value.some(document=>document.id===id))
  scopeKind.value=scope.length?'documents':'all';documentIds.value=scope
  await nextTick()
  candidates.forEach((item,index)=>{goldAnswers[index]=item.reference_answer||'';goldDocuments[index]=[...(item.expected_document_ids||[])];goldChunks[index]=(item.expected_chunk_ids||[]).join(', ')})
  form.instructions='评测这些已生成的问题；审阅参考答案和原文证据后再决定是否将其用于对照。'
  if(createPanel.value){createPanel.value.open=true;createPanel.value.scrollIntoView({behavior:'smooth',block:'start'})}
  notice.value='候选已填回表单，尚未创建新任务。当前只作为普通问题；参考答案和期望证据仍须人工审阅，机器生成来源会保留。'
}
watch(()=>form.task,()=>{form.apply=false;form.instructions=taskInfo.value.description;notice.value='';error.value='';importedCandidates.value=[]})
watch(allowedActions,actions=>{selectedActions.value=[...actions];form.apply=false},{immediate:true})
watch(parsedQuestions,list=>{list.forEach((_question,index)=>{goldDocuments[index]??=[];goldAnswers[index]??='';goldChunks[index]??=''})})
watch(()=>navigationState.value.agentRun,id=>{if(id&&id!==selectedId.value&&navigationState.value.page==='agents'&&navigationState.value.kb===props.knowledgeBaseId)void selectRun(id)})
watch([taskFilter,statusFilter],()=>{pageOffset.value=0;void loadRuns()})
onMounted(()=>{void initialize();timer=window.setInterval(()=>{if(disposed)return;if(runs.value.some(isActiveAgentRun))void loadRuns(true);if(selected.value&&isActiveAgentRun(selected.value))void selectRun(selected.value.id,true)},2500)})
onBeforeUnmount(()=>{disposed=true;listRequest++;detailRequest++;evidenceRequest++;detailController?.abort();if(timer)window.clearInterval(timer)})
</script>

<template>
  <div class="agent-page">
    <header class="agent-intro"><div><h2>知识智能体工作台</h2><p>从接入、抽取到知识治理与业务分析，保留任务范围、执行记录、结果和证据。</p></div><button :disabled="loading" @click="loadRuns()">刷新任务</button></header>
    <p v-if="error" class="agent-error" role="alert">{{error}}</p><p v-if="notice" class="agent-notice" role="status">{{notice}}</p>
    <details v-if="canEdit" ref="createPanel" class="create-panel" open><summary>发起知识任务 <small>默认预览，只有明确授权的动作才会应用到知识库</small></summary>
      <form @submit.prevent="submit">
        <div class="form-columns"><label>任务类型<select v-model="form.task" aria-label="智能体任务类型"><option v-for="task in agentTasks" :key="task.id" :value="task.id">{{task.label}}</option></select><small>{{taskCatalog?.description||taskInfo.description}}</small></label>
          <label>文档范围<select v-model="scopeKind" aria-label="任务文档范围"><option value="all">整个当前知识库</option><option value="documents">选择文档</option><option value="folder">指定文件夹（含子目录）</option><option value="tag">指定标签</option></select><small>{{scopeKind==='all'?`当前知识库 · ${documents.length} 份文档`:`已限定 ${scopeIds.length} 份文档`}}</small></label>
        </div>
        <label v-if="scopeKind==='documents'">选择文档<select v-model="documentIds" multiple aria-label="任务选择文档"><option v-for="document in documents" :key="document.id" :value="document.id">{{document.original_filename}} · {{document.status}}</option></select><small>可多选；单次最多 500 份，空选择不会扩大范围。</small></label>
        <label v-else-if="scopeKind==='folder'">选择文件夹<select v-model="folderId" aria-label="任务文件夹"><option value="">请选择文件夹</option><option v-for="folder in folders" :key="folder.id" :value="folder.id">{{folder.path}}</option></select></label>
        <label v-else-if="scopeKind==='tag'">选择标签<select v-model="tagId" aria-label="任务标签"><option value="">请选择标签</option><option v-for="tag in tags" :key="tag.id" :value="tag.id">{{tag.name}}</option></select></label>
        <label>任务目标<textarea v-model="form.instructions" required maxlength="12000" rows="3" placeholder="说明希望解决的问题、验收标准和需要关注的风险" aria-label="智能体任务目标"></textarea></label>
        <div class="form-columns task-options">
          <label v-if="form.task==='incremental_maintenance'">维护范围<select v-model="form.mode"><option value="pending">待处理内容</option><option value="retry_failed">失败内容</option><option value="full">全部重建</option></select></label>
          <label v-if="form.task==='ingestion'">接入数据源<select v-model="form.data_source_id" aria-label="任务数据源"><option value="">由任务检查已配置的数据源</option><option v-for="source in activeSources" :key="source.id" :value="source.id">{{source.name}} · {{source.source_type}}</option></select><small>凭据与可读取目录沿用数据源的授权配置。</small></label>
          <label v-if="form.task==='operations'">允许重试的业务任务<select v-model="jobIds" multiple aria-label="允许重试的业务任务"><option v-for="job in retryableJobs" :key="job.id" :value="job.id">{{job.job_type}} · {{job.status}} · {{job.id}}</option></select><small>不选择时不授权重试；最多选择 20 项。</small></label>
        </div>
        <template v-if="form.task==='evaluation'"><div class="generation-options"><label class="check"><input v-model="generateCases" type="checkbox" aria-label="自动生成评测样例" /> 自动生成评测样例</label><label v-if="generateCases">候选数量<input v-model.number="maxGeneratedCases" type="number" min="1" max="20" required aria-label="自动生成样例数量" /></label><small>基于范围内原文构建有证据的候选；机器生成样例须人工审阅，不是人工金标准。</small></div><label>评测问题<textarea v-model="questions" rows="4" :placeholder="generateCases?'可留空：本次只构建待审阅候选，不执行评分':'每行一个问题，最多 20 个'" aria-label="智能体评测问题"></textarea><small>{{generationOnly?'本次只生成候选，不执行检索评分或模型/提示词对照。':'有现成问题时返回真实评测结果；新生成的候选另行保存，不自动作为评分标准。'}}</small></label><p v-if="importedCandidates.length" class="agent-notice imported-candidates">这些题目来自机器生成候选，尚非人工金标准。当前仅作为普通问题；参考答案与原文证据须人工核验，启用对照后仍保留机器生成来源。</p></template>
        <details v-if="form.task==='evaluation'" class="evaluation-options"><summary>金标准、模型与提示词对照 <small>{{comparisonCount}} / 20 个执行组合</small></summary><label class="check"><input v-model="useGoldCases" type="checkbox" aria-label="启用评测金标准" /> {{importedCandidates.length?'使用已审阅候选的参考答案和期望证据（保留机器生成来源）':'为问题设置参考答案或期望命中的证据'}}</label><div v-if="useGoldCases" class="gold-cases"><article v-for="(question,index) in parsedQuestions.slice(0,20)" :key="index"><b>{{index+1}}. {{question}}</b><label>参考答案<textarea v-model="goldAnswers[index]" maxlength="10000" rows="2" :aria-label="`问题${index+1}参考答案`"></textarea></label><label>期望命中的文档<select v-model="goldDocuments[index]" multiple :aria-label="`问题${index+1}期望文档`"><option v-for="document in documents.filter(item=>scopeKind==='all'||scopeIds.includes(item.id))" :key="document.id" :value="document.id">{{document.original_filename}}</option></select></label><label>期望片段编号（可选）<input v-model="goldChunks[index]" placeholder="多个编号以逗号分隔，需属于任务范围" /></label></article></div><div class="form-columns"><label>比较执行配置<select v-if="runtimeProfiles.length" v-model="comparisonProfiles" multiple :disabled="generationOnly" aria-label="评测比较模型"><option v-for="profile in runtimeProfiles" :key="profile" :value="profile">{{profile}}</option></select><textarea v-else v-model="comparisonProfilesText" rows="3" :disabled="generationOnly" placeholder="每行一个服务器已登记的配置名称" aria-label="评测比较模型"></textarea><small>最多 5 个；留空使用本次执行配置。</small></label><div class="prompt-variants"><label>提示词对照 <small>留空使用默认提示词，最多 5 组。</small></label><article v-for="(variant,index) in promptVariants" :key="index"><input v-model="variant.name" :disabled="generationOnly" maxlength="80" placeholder="对照名称" :aria-label="`提示词对照${index+1}名称`" /><textarea v-model="variant.instructions" :disabled="generationOnly" maxlength="4000" rows="3" placeholder="本组回答提示词" :aria-label="`提示词对照${index+1}内容`"></textarea><button type="button" @click="promptVariants.splice(index,1)">移除此组</button></article><button type="button" :disabled="generationOnly||promptVariants.length>=5" @click="promptVariants.push({name:`对照${promptVariants.length+1}`,instructions:''})">添加提示词对照</button></div></div><small>问题数 × 执行配置数 × 提示词数不得超过 20；指标的实际定义会在结果中保留。</small></details>
        <details class="advanced-options"><summary>执行选项</summary><div class="form-columns"><label>执行配置（可选）<select v-if="runtimeProfiles.length" v-model="form.runtime_profile" aria-label="任务执行配置"><option value="">使用服务器默认配置</option><option v-for="profile in runtimeProfiles" :key="profile" :value="profile">{{profile}}</option></select><input v-else v-model="form.runtime_profile" maxlength="80" placeholder="留空使用服务器默认配置" aria-label="任务执行配置" /></label><label>最多授权动作<input v-model.number="form.max_actions" type="number" min="1" max="50" required /></label><label>任务总时限（秒）<input v-model.number="form.max_runtime_seconds" type="number" min="30" max="3600" required aria-label="任务总时限" /></label><label>智能体调用上限<input v-model.number="form.max_agent_calls" type="number" min="1" max="200" required aria-label="智能体调用上限" /></label></div><small>预算涵盖本任务及其抽取、结构生成、评测等子调用；达到上限会停止，并保留已完成记录。</small></details>
        <div class="authorization"><p>{{taskInfo.changes}}</p><template v-if="allowedActions.length"><label class="check"><input v-model="form.apply" type="checkbox" aria-label="授权应用任务动作" /> 授权应用本次任务的允许动作</label><div class="allowed-actions"><label v-for="action in allowedActions" :key="action" class="check"><input v-model="selectedActions" type="checkbox" :value="action" :disabled="!form.apply" :aria-label="`允许${actionLabel(action)}`" />{{actionLabel(action)}}</label></div><small>{{form.apply?'仅执行勾选的白名单动作，并限定在所选文档范围内；执行结果保留审计记录。':'当前只分析与预览，结果可核对后再发起已授权任务。'}}</small></template><small v-else>此任务只生成可审阅的报告与产物。</small></div>
        <button class="primary create-run" :disabled="submitting||loading||!catalog.length">{{submitting?'正在创建…':form.apply?'创建已授权任务':'创建预览任务'}}</button>
      </form>
    </details>
    <p v-else class="read-only">你可以查看本知识库的任务、执行结果与证据；创建和控制任务需要编辑权限。</p>

    <div class="workbench-layout">
      <aside class="run-list"><header><b>任务记录</b><span>共 {{runTotal}} 条 · {{runs.length?pageOffset+1:0}}–{{pageOffset+runs.length}}</span></header><div class="list-filters"><select v-model="taskFilter" aria-label="筛选任务类型"><option value="">全部任务</option><option v-for="task in agentTasks" :key="task.id" :value="task.id">{{task.label}}</option></select><select v-model="statusFilter" aria-label="筛选任务状态"><option value="">全部状态</option><option v-for="(label,status) in agentStatusLabels" :key="status" :value="status">{{label}}</option></select></div>
        <p v-if="loading&&!runs.length" class="empty">正在读取任务…</p><p v-else-if="!runs.length" class="empty">当前没有匹配的任务</p>
        <button v-for="run in runs" :key="run.id" :class="['run-item',{selected:selectedId===run.id}]" @click="selectRun(run.id)"><div><b>{{agentTaskLabel(run.task)}}</b><span :class="['run-status',run.status]">{{agentStatusLabels[run.status]||run.status}}</span></div><p>{{run.instructions||'未填写任务目标'}}</p><small>{{timeLabel(run.created_at)}}</small><div class="mini-progress"><i :style="{width:`${agentProgress(run)}%`}"></i></div></button>
        <footer class="list-pagination"><button :disabled="!pageOffset||loading" @click="changePage(-1)">上一页</button><button :disabled="pageOffset+pageSize>=runTotal||loading" @click="changePage(1)">下一页</button></footer>
      </aside>
      <section class="run-detail">
        <div v-if="detailLoading&&!selected" class="empty">正在读取任务详情…</div><div v-else-if="!selected" class="empty">选择一项任务，查看进度、结果与证据</div>
        <template v-else>
          <header class="run-header"><div><h3>{{agentTaskLabel(selected.task)}}</h3><small>{{selected.id}}</small></div><span :class="['run-status',selected.status]">{{selected.cancel_requested?'正在取消':agentStatusLabels[selected.status]||selected.status}}</span><button :disabled="exporting" @click="downloadResult('markdown')">导出报告</button><button :disabled="exporting" @click="downloadResult('json')">导出 JSON</button><button v-if="canEdit&&isActiveAgentRun(selected)" :disabled="actionPending||selected.cancel_requested" @click="control('cancel')">取消任务</button><button v-if="canEdit&&['failed','cancelled'].includes(selected.status)" :disabled="actionPending" @click="control('retry')">重试任务</button></header>
          <p class="run-goal">{{selected.instructions}}</p>
          <div class="run-metadata"><span>{{selected.options?.apply?'已授权应用':'分析预览'}}</span><span>{{selected.document_ids?.length?`限定 ${selected.document_ids.length} 份文档`:'整个当前知识库'}}</span><span>开始 {{timeLabel(selected.started_at)}}</span><span>耗时 {{agentElapsed(selected)}}</span><span v-if="selected.attempt">第 {{selected.attempt}} 次执行</span></div>
          <div class="run-progress" role="progressbar" aria-label="智能体任务进度" :aria-valuenow="agentProgress(selected)" aria-valuemin="0" aria-valuemax="100"><i :style="{width:`${agentProgress(selected)}%`}"></i></div><p v-if="progressMessage" class="progress-message">{{progressMessage}}</p>
          <p v-if="selected.error_message" class="agent-error" role="alert">{{selected.error_message}}</p>
          <div class="usage-grid"><article><small>输入 Token</small><b>{{usage.input_tokens??'—'}}</b></article><article><small>输出 Token</small><b>{{usage.output_tokens??'—'}}</b></article><article><small>总 Token</small><b>{{tokenTotal??'—'}}</b></article><article><small>报告费用</small><b>{{costSummary.label}}</b></article></div>
          <p v-if="tokenTotal===null&&(usage.input_tokens!==undefined||usage.output_tokens!==undefined)" class="hint">服务未提供总 Token；输入、输出、推理和缓存按原始分类保留，可在用量详情查看。</p>
          <p class="progress-message cost-note">{{costSummary.note}}</p>
          <section v-if="summary" class="output-section"><h4>执行结果</h4><AnswerContent :content="summary" @citation="openCitation" /></section>
          <section v-if="generatedCases.length" class="output-section generated-cases"><h4>自动生成的评测候选 <small>{{generatedCases.length}} 项</small></h4><p class="agent-notice">机器生成，须人工审阅；这些候选不是人工金标准，也不代表已完成评分。</p><StructuredResult :value="generatedCases" /><button v-if="canEdit" class="use-generated-cases" @click="useGeneratedCases">使用这些候选创建评测</button></section>
          <section v-if="hasMetrics" class="output-section"><h4>结果指标</h4><StructuredResult :value="metrics" /></section>
          <section v-if="hasOtherOutput" class="output-section"><h4>详细产物</h4><StructuredResult :value="otherOutput" /></section>
          <p v-if="selected.status==='succeeded'&&!summary&&!hasOtherOutput" class="agent-error">任务标记为完成，但没有返回可展示的结果，请查看执行事件与诊断。</p>
          <section v-if="evidenceItems.length" class="output-section"><h4>原文证据 <small>{{evidenceItems.length}} 条</small></h4><button v-for="(item,index) in evidenceItems" :key="`${item.chunk_id||index}-${index}`" class="evidence-card" @click="openEvidence(item)"><b>{{item.citation_id||`来源${index+1}`}} · {{item.title||item.source||documents.find(document=>document.id===item.document_id)?.original_filename||item.document_id||'文档证据'}}</b><p>{{item.quote||item.preview||item.content||'查看原始片段'}}</p><small v-if="item.chunk_id">片段 {{item.chunk_id}}</small></button></section>
          <section v-if="actions.length" class="output-section"><h4>动作与应用记录</h4><StructuredResult :value="actions" /></section>
          <section v-if="selected.followup_jobs?.length" class="output-section"><h4>关联业务任务</h4><p class="progress-message">任务在这些后续处理完成后才会结束。</p><StructuredResult :value="selected.followup_jobs" /><button @click="navigate({page:'jobs',kb:knowledgeBaseId})">查看索引任务</button></section>
          <details v-if="diagnostics" class="output-section diagnostics"><summary>后端诊断</summary><StructuredResult :value="diagnostics" /></details>
          <details class="output-section backend-details"><summary>执行后端与用量详情</summary><StructuredResult :value="{backend:selected.backend||'未报告',model:selected.model||'未报告',session_id:selected.session_id||'未报告',job_id:selected.job_id||'未报告',usage:selected.usage||{},options:selected.options}" /></details>
          <section class="output-section event-section"><h4>执行事件 <small>{{selected.events?.length||0}} 条</small></h4><ol v-if="selected.events?.length" class="events"><li v-for="(event,index) in selected.events" :key="event.id||event.sequence||index"><header><b>{{eventLabel(event)}}</b><time>{{timeLabel(event.created_at)}}</time></header><small>{{event.type}}</small><details v-if="event.data&&Object.keys(asRecord(event.data)||{}).length"><summary>查看事件详情</summary><StructuredResult :value="event.data" /></details></li></ol><p v-else class="empty">尚无执行事件；等待任务开始或刷新记录。</p></section>
        </template>
      </section>
    </div>
    <div v-if="evidence" class="evidence-mask" @click.self="evidence=null"><aside class="evidence-drawer"><header><h3>{{evidence.title||evidence.source||'原文证据'}}</h3><button @click="evidence=null">关闭</button></header><p v-if="evidenceLoading">正在读取原始片段…</p><p v-if="evidenceError" class="agent-error">{{evidenceError}}</p><blockquote>{{evidence.quote||evidence.preview||evidence.content}}</blockquote><template v-if="evidenceDetail"><h4>{{evidenceDetail.document_name}} · 片段 #{{evidenceDetail.chunk_index}}</h4><p class="original-content">{{evidenceDetail.content}}</p></template><small>文档 {{evidence.document_id||'未提供'}} · 片段 {{evidence.chunk_id||'未提供'}}</small></aside></div>
  </div>
</template>

<style scoped>
.agent-page{height:100%;overflow:auto;padding:22px;color:#344159}.agent-intro{display:flex;gap:16px;align-items:center;margin-bottom:16px}.agent-intro>div{flex:1}.agent-intro h2{margin:0;font-size:19px}.agent-intro p{margin:6px 0 0;color:#8491a3;font-size:12px}button{padding:8px 11px;border:1px solid #dce3ed;border-radius:8px;color:#526177;background:#fff;cursor:pointer;font:inherit;font-size:11px}button:disabled{cursor:not-allowed;opacity:.5}.primary{color:#fff;background:#4f6ef7;border-color:#4f6ef7}.agent-error,.agent-notice,.read-only{padding:10px 13px;border-radius:9px;font-size:12px;line-height:1.7}.agent-error{color:#b42318;background:#fff1f0}.agent-notice{color:#18794e;background:#eaf8f0}.read-only{color:#5268b6;background:#edf2ff}.create-panel,.run-list,.run-detail{border:1px solid #dfe6f0;border-radius:12px;background:#fff}.create-panel{margin-bottom:18px}.create-panel>summary{padding:14px 17px;color:#344159;font-size:13px;font-weight:650;cursor:pointer}.create-panel>summary small{margin-left:12px;color:#8491a3;font-size:10px;font-weight:400}.create-panel form{display:grid;gap:12px;padding:0 17px 17px}.form-columns{display:grid;grid-template-columns:1fr 1fr;gap:14px}label{display:grid;gap:6px;color:#526177;font-size:12px}input,select,textarea{box-sizing:border-box;max-width:100%;width:100%;padding:9px 10px;border:1px solid #dce3ed;border-radius:8px;color:#344159;background:#fff;font:inherit;font-size:12px}textarea{resize:vertical;line-height:1.6}select[multiple]{min-height:100px}small{color:#8491a3;font-size:10px;line-height:1.6}.advanced-options summary{color:#64748b;font-size:11px;cursor:pointer}.advanced-options .form-columns{margin-top:10px}.authorization{padding:12px;border:1px solid #dce4f5;border-radius:9px;background:#f7faff}.authorization p{margin:0 0 8px;color:#5e6d82;font-size:11px}.authorization .check{display:flex;align-items:center;gap:7px;font-weight:650}.check input{width:auto}.allowed-actions{display:flex;gap:5px;flex-wrap:wrap;margin:8px 0}.allowed-actions label{padding:3px 6px;border-radius:6px;color:#5268b6;background:#eaf0ff;font-size:10px}.create-run{justify-self:end;min-width:150px}.workbench-layout{display:grid;grid-template-columns:290px minmax(0,1fr);gap:15px;align-items:start}.run-list{position:sticky;top:0;max-height:78vh;overflow:auto}.run-list>header{display:flex;justify-content:space-between;padding:13px 14px;font-size:12px}.run-list>header span{color:#8491a3;font-size:10px}.list-filters{display:flex;gap:5px;padding:0 10px 10px}.list-filters select{min-width:0;padding:7px 5px;font-size:10px}.run-item{display:block;width:calc(100% - 16px);margin:0 8px 7px;padding:11px;text-align:left;border-color:#e6ebf3;border-left:3px solid transparent}.run-item.selected{border-color:#cbd8ff;border-left-color:#4f6ef7;background:#f5f8ff}.run-item>div:first-child{display:flex;align-items:center;justify-content:space-between;gap:6px}.run-item b{font-size:11px}.run-item p{display:-webkit-box;overflow:hidden;margin:7px 0;color:#748196;font-size:10px;line-height:1.6;-webkit-line-clamp:2;-webkit-box-orient:vertical}.run-item small{font-size:9px}.run-status{padding:3px 7px;border-radius:8px;color:#64748b;background:#eef2f7;font-size:10px;white-space:nowrap}.run-status.succeeded{color:#18794e;background:#eaf8f0}.run-status.failed{color:#b42318;background:#fff1f0}.run-status.running{color:#3156cf;background:#eaf0ff}.mini-progress,.run-progress{height:4px;overflow:hidden;border-radius:6px;background:#edf1f6}.mini-progress{margin-top:8px}.mini-progress i,.run-progress i{display:block;height:100%;border-radius:6px;background:#4f6ef7;transition:width .2s}.list-pagination{display:flex;justify-content:space-between;padding:10px}.run-detail{min-height:420px;min-width:0;padding:17px}.run-header{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.run-header>div{flex:1;min-width:150px}.run-header h3{margin:0;font-size:16px}.run-header small{font-size:9px;overflow-wrap:anywhere}.run-goal{color:#526177;font-size:12px;line-height:1.8;white-space:pre-wrap}.run-metadata{display:flex;gap:7px 13px;flex-wrap:wrap;color:#8491a3;font-size:10px}.run-progress{height:6px;margin-top:12px}.progress-message{margin:6px 0;color:#7b8799;font-size:10px}.usage-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:15px 0}.usage-grid article{padding:10px;border:1px solid #e6ebf3;border-radius:9px;background:#fafcff}.usage-grid b,.usage-grid small{display:block;overflow-wrap:anywhere}.usage-grid b{margin-top:5px;font-size:15px;color:#344159}.output-section{margin-top:18px;padding-top:15px;border-top:1px solid #edf1f6}.output-section h4{margin:0 0 11px;font-size:13px}.output-section h4 small{margin-left:7px;font-weight:400}.output-section>summary{margin-bottom:10px;color:#5268b6;font-size:11px;cursor:pointer}.evidence-card{display:block;width:100%;margin-bottom:8px;padding:11px;text-align:left;background:#fafdff;border-color:#dfe8f6}.evidence-card b{font-size:11px;color:#4f64ad}.evidence-card p{margin:7px 0;font-size:11px;line-height:1.8;color:#64738a;white-space:pre-wrap}.events{list-style:none;margin:0;padding:0}.events li{position:relative;margin:0 0 9px;padding:10px 12px;border-left:3px solid #cdd8f7;border-radius:0 8px 8px 0;background:#f8faff}.events header{display:flex;justify-content:space-between;gap:10px;font-size:11px}.events time{color:#8b97a8;font-size:9px;white-space:nowrap}.events details{margin-top:7px}.events summary{font-size:10px;color:#6478bd;cursor:pointer}.empty{padding:35px 15px;color:#8b97a8;text-align:center;font-size:12px}.evidence-mask{position:fixed;z-index:60;inset:0;display:flex;justify-content:flex-end;background:#10182770}.evidence-drawer{width:min(650px,95vw);height:100%;padding:22px;overflow:auto;background:#fff;box-shadow:-15px 0 45px #10182730}.evidence-drawer header{display:flex;align-items:center;gap:15px}.evidence-drawer h3{flex:1;font-size:16px}.evidence-drawer h4{font-size:12px}.evidence-drawer blockquote{margin:15px 0;padding:12px;border-left:3px solid #7da4ef;background:#f4f8ff;color:#526177;font-size:12px;line-height:1.8;white-space:pre-wrap}.original-content{font-size:12px;line-height:1.9;white-space:pre-wrap;color:#526177}@media(max-width:1050px){.workbench-layout{grid-template-columns:240px minmax(0,1fr)}}@media(max-width:850px){.workbench-layout{grid-template-columns:1fr}.run-list{position:static;max-height:330px}.form-columns{grid-template-columns:1fr}.create-panel>summary small{display:block;margin:6px 0 0}.usage-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.agent-page{padding:14px}.events header{flex-wrap:wrap}}
.evaluation-options{padding:12px;border:1px solid #dfe6f0;border-radius:9px;background:#fcfdff}.evaluation-options>summary{margin-bottom:10px;color:#5268b6;font-size:12px;cursor:pointer}.evaluation-options .form-columns{margin:12px 0}.gold-cases,.prompt-variants{display:grid;gap:10px}.gold-cases article,.prompt-variants article{display:grid;gap:8px;padding:12px;border:1px solid #e4eaf3;border-radius:9px;background:#fff}.gold-cases b{font-size:12px}.prompt-variants button{justify-self:start}
.generation-options{display:grid;gap:9px;padding:12px;border:1px solid #dfe6f0;border-radius:9px;background:#f8faff}.generation-options>label:not(.check){max-width:160px}.generated-cases>button{margin-top:12px}
</style>
