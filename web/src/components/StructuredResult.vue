<script setup lang="ts">
import { computed } from 'vue'
import { asRecord } from '../agents'
const props=withDefaults(defineProps<{value:unknown;depth?:number}>(),{depth:0})
const record=computed(()=>asRecord(props.value))
const entries=computed(()=>Object.entries(record.value||{}))
const list=computed(()=>Array.isArray(props.value)?props.value:[])
const labels:Record<string,string>={summary:'摘要',answer:'回答',report:'报告',items:'结果条目',results:'执行结果',metrics:'评测指标',score:'评分',status:'状态',task:'任务',name:'名称',title:'标题',description:'说明',reason:'原因',suggestion:'建议',recommendations:'建议',actions:'操作记录',action:'操作',evidence:'证据',sources:'来源',source:'来源',quote:'原文',content:'内容',document_id:'文档编号',document_ids:'文档范围',chunk_id:'片段编号',chunk_ids:'片段范围',entities:'实体',relations:'关系',entity_count:'实体数量',relation_count:'关系数量',documents:'文档',document_count:'文档数量',chunks:'文档片段',chunk_count:'片段数量',jobs:'后台任务',job_id:'任务编号',job_ids:'任务编号',schema:'知识结构',schema_id:'结构编号',schema_version:'结构版本',draft:'草稿',changes:'变更',applied:'已应用',proposed:'待应用',skipped:'已跳过',errors:'错误',error:'错误',warnings:'提醒',diagnostics:'诊断',checks:'检查项',quality:'质量',coverage:'覆盖情况',question:'评测问题',query:'问题',expected_answer:'参考答案',latency_ms:'耗时（毫秒）',elapsed_ms:'耗时（毫秒）',input_tokens:'输入 Token',output_tokens:'输出 Token',total_tokens:'总 Token',cost:'费用',model:'模型',provider:'服务',runtime:'执行后端',backend:'执行后端',session_id:'执行会话',files:'文件',folders:'文件夹',tags:'标签',health:'健康状态',confidence:'模型评分',message:'说明',type:'类型',data:'详细结果',value:'数值',count:'数量',total:'总数',current:'当前',operation:'操作',target:'目标',target_id:'目标编号',details:'详情'}
Object.assign(labels,{generated_cases:'自动生成的评测候选',provenance:'来源',review_status:'审阅状态',human_review_required:'须人工审阅',source_run_id:'来源任务',candidate_id:'候选编号',reference_answer:'参考答案',expected_document_ids:'期望文档',expected_chunk_ids:'期望片段',mode:'执行方式',max_runtime_seconds:'任务总时限（秒）',max_agent_calls:'智能体调用上限',cost_status:'费用完整性',cost_coverage:'费用完整性',incomplete:'执行未完整结束',kind:'产物或动作类型',result:'执行产物',params:'动作参数',fingerprint:'执行记录标识',followup_job_ids:'关联任务编号',job_type:'任务类型',processed_items:'已处理数量',total_items:'总数量',comparisons:'对照结果',model_prompt_comparison:'模型与提示词对照',agent_metrics:'智能体报告指标',scope_diagnostics:'任务范围诊断',evaluation:'检索评测',evidence_count:'证据数量',processed_documents:'已处理文档',evidence_coverage:'引用覆盖率',precision:'命中精确率',recall:'召回率',mrr:'首个命中倒数排名',mean_recall:'平均召回率',reference_character_recall:'参考答案字符重合率',answer_metric:'回答指标定义',coverage_metric:'覆盖指标定义',cost_note:'费用统计说明',reported_cost:'服务报告的原始费用',profile:'执行配置',profiles:'执行配置',prompt_variant:'提示词版本',prompt_variants:'提示词版本',case_count:'用例数量',cases:'评测用例',completed:'完成数量',nodes:'图谱节点',dependencies:'依赖关系',impact_paths:'影响路径',cycles:'依赖环',timeline:'时间线',date_text:'原文日期',entity_ids:'实体编号',relation_ids:'关系编号',interpretation:'结果解释',limits:'分析范围限制',canonical_name:'标准名称',label:'名称',entity_type:'实体类型',predicate:'关系类型',subject:'起点',object:'终点',target:'目标',source:'来源',parent_run_id:'上次执行记录'})
Object.assign(labels,{reasoning_tokens:'推理 Token',cache_read_tokens:'缓存读取 Token',cache_write_tokens:'缓存写入 Token',known_total_tokens:'已知部分总 Token',total_tokens_coverage:'总 Token 完整性',token_note:'Token 统计说明',currency:'币种',sessions:'执行会话明细'})
const label=(key:string)=>labels[key]||key.replace(/_/g,' ')
function displayValue(key:string,value:unknown):unknown {
  if(key==='provenance'&&value==='agent_generated')return '机器生成'
  if(key==='review_status'&&value==='candidate')return '待人工审阅'
  if(key==='mode'&&value==='case_generation_only')return '仅生成评测候选，未执行评分'
  if(['cost_status','cost_coverage','total_tokens_coverage'].includes(key)&&typeof value==='string')return ({unknown:'未知',partial:'仅部分报告',complete:'完整',reported:'已报告'} as Record<string,string>)[value]||value
  if(key!=='status'||typeof value!=='string')return value
  return ({proposed:'待审阅',applied:'已应用',already_applied:'复用此前已应用的结果',partial:'部分完成',completed:'已完成',succeeded:'已完成',failed:'失败',queued:'等待执行',pending:'等待执行',running:'执行中',cancelled:'已取消',draft:'草稿',ready:'已就绪',ok:'正常',fail:'异常'} as Record<string,string>)[value]||value
}
function scalar(value:unknown):string { if(value===null||value===undefined)return '暂无';if(typeof value==='boolean')return value?'是':'否';return String(value) }
const tooDeep=computed(()=>props.depth>=8)
const fallback=computed(()=>{try{return JSON.stringify(props.value,null,2)}catch{return String(props.value)}})
</script>
<template>
  <pre v-if="tooDeep" class="result-json">{{fallback}}</pre>
  <dl v-else-if="record" class="result-fields"><div v-for="[key,item] in entries" :key="key"><dt>{{label(key)}}</dt><dd><StructuredResult :value="displayValue(key,item)" :depth="depth+1" /></dd></div><div v-if="!entries.length" class="result-empty">暂无详细条目</div></dl>
  <ol v-else-if="Array.isArray(value)" class="result-list"><li v-for="(item,index) in list" :key="index"><StructuredResult :value="item" :depth="depth+1" /></li><li v-if="!list.length" class="result-empty">无</li></ol>
  <span v-else class="result-value">{{scalar(value)}}</span>
</template>
<style scoped>
.result-fields{margin:0}.result-fields>div{display:grid;grid-template-columns:minmax(90px,150px) minmax(0,1fr);gap:12px;padding:9px 0;border-bottom:1px solid #edf1f6}.result-fields dt{color:#7a8799;font-size:11px;overflow-wrap:anywhere}.result-fields dd{min-width:0;margin:0;color:#344159;font-size:12px}.result-value{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7}.result-list{margin:0;padding-left:21px}.result-list>li{padding:5px 0}.result-list>li::marker{color:#7a8bc4}.result-empty{color:#8b97a8;font-size:11px}.result-json{margin:0;max-height:360px;overflow:auto;white-space:pre-wrap;font-size:11px}@media(max-width:700px){.result-fields>div{grid-template-columns:1fr;gap:4px}}
</style>
