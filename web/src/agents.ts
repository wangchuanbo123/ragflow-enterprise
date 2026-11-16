export const agentTasks = [
  { id: 'extraction', label: '知识抽取', description: '抽取实体、关系和原文证据，检查哪些事实可以进入图谱。', changes: '按授权保存经过校验的抽取结果，并记录文档与片段证据。' },
  { id: 'ingestion', label: '接入与索引编排', description: '检查数据来源，规划文档解析、切片、索引与后续抽取。', changes: '按授权提交来源同步和索引任务；可在索引任务页继续查看。' },
  { id: 'schema_governance', label: '知识结构治理', description: '以所选文档为样本，改进全库统一的 Schema 草案。', changes: '只保存全库 Schema 草稿；全部文档使用同一活动版本，发布和激活需在图谱治理中操作。' },
  { id: 'quality_review', label: '质量检查与修复', description: '检查重复概念、缺失证据和关系冲突，提出可追溯的修复建议。', changes: '只执行后端允许且本次明确授权的修复，所有操作保留记录。' },
  { id: 'incremental_maintenance', label: '增量维护', description: '识别待处理文档与过期索引，规划必要的增量维护。', changes: '按授权提交增量构图和派生索引维护任务。' },
  { id: 'knowledge_organization', label: '知识组织', description: '结合文档内容，提出文档标签和主题组织方案。', changes: '按授权调整文档标签并重建主题社区，保留应用记录。' },
  { id: 'evaluation', label: '检索与回答评测', description: '用指定问题检查检索结果、引用与回答，输出指标和失败案例。', changes: '产生评测报告；不会修改知识库内容。' },
  { id: 'business_analysis', label: '业务分析', description: '依据知识库中的文档和事实，生成业务判断与行动建议。', changes: '产生带证据的分析报告；不会修改知识库内容。' },
  { id: 'operations', label: '运行诊断', description: '检查运行状态、索引任务及后台诊断，给出维护方案。', changes: '只执行本次允许的维护动作，不运行任意系统命令。' },
] as const

export type AgentTask = typeof agentTasks[number]['id']
export type AgentRunStatus = 'queued'|'running'|'succeeded'|'failed'|'cancelled'
export interface AgentRunEvent { id?: string|number; sequence?: number; type: string; message?: string; data?: unknown; created_at?: string }
export interface AgentRun {
  id: string
  knowledge_base_id: string
  task: AgentTask
  status: AgentRunStatus
  instructions: string
  document_ids: string[]
  options: Record<string, unknown>
  output?: unknown
  events?: AgentRunEvent[]
  actions?: unknown[]
  usage?: Record<string, unknown>
  progress?: number | { stage?: string; current?: number; total?: number; percent?: number; message?: string }
  error_message?: string | null
  diagnostics?: unknown
  created_at: string
  started_at?: string | null
  finished_at?: string | null
  elapsed_ms?: number | null
  attempt?: number
  parent_run_id?: string | null
  cancel_requested?: boolean
  job_id?: string | null
  backend?: string | null
  model?: string | null
  session_id?: string | null
  followup_jobs?: unknown[]
}
export interface AgentRunCreate { task: AgentTask; instructions: string; document_ids: string[]; options: Record<string, unknown> }
export interface AgentTaskCatalog { task: AgentTask; label: string; description: string; allowed_actions: string[]; options: Record<string, unknown> }
export interface AgentEvidence { citation_id?: string; document_id?: string; chunk_id?: string; source?: string; title?: string; quote?: string; preview?: string; content?: string; [key: string]: unknown }
export interface GeneratedEvaluationCase {
  id?: string
  kind?: string
  question: string
  reference_answer?: string
  expected_document_ids?: string[]
  expected_chunk_ids?: string[]
  evidence?: AgentEvidence[]
  provenance?: string
  review_status?: string
  source_run_id?: string
  human_review_required?: boolean
}
export function generatedEvaluationCases(output:unknown):GeneratedEvaluationCase[] {
  const cases=asRecord(output)?.generated_cases
  return Array.isArray(cases)?cases.filter(item=>{const candidate=asRecord(item);return candidate&&typeof candidate.question==='string'&&candidate.question.trim()}) as GeneratedEvaluationCase[]:[]
}

export const agentStatusLabels: Record<AgentRunStatus,string> = { queued:'等待执行',running:'执行中',succeeded:'已完成',failed:'执行失败',cancelled:'已取消' }
export function isActiveAgentRun(run: AgentRun): boolean { return run.status==='queued'||run.status==='running' }
export function agentTaskLabel(task: string): string { return agentTasks.find(item=>item.id===task)?.label || task }
export function asRecord(value: unknown): Record<string, unknown> | null { return value && typeof value==='object' && !Array.isArray(value) ? value as Record<string, unknown> : null }
export function agentProgress(run: AgentRun): number {
  if (run.status==='succeeded') return 100
  const progress=run.progress
  const amount=typeof progress==='number' ? progress : progress?.percent ?? (progress?.total ? (progress.current||0)/progress.total*100 : 0)
  return Number.isFinite(amount) ? Math.max(0,Math.min(100,amount)) : 0
}
export function agentSummary(output: unknown): string {
  if (typeof output==='string') return output
  const record=asRecord(output)
  return String(record?.summary || record?.answer || record?.report || '')
}
export function agentEvidence(output: unknown): AgentEvidence[] {
  const record=asRecord(output)
  const raw=record?.evidence || record?.sources || []
  return Array.isArray(raw) ? raw.filter(item=>asRecord(item)) as AgentEvidence[] : []
}
export function agentElapsed(run: AgentRun): string {
  const elapsed=run.elapsed_ms ?? (run.started_at ? parseAgentTime(run.finished_at||new Date().toISOString())-parseAgentTime(run.started_at) : 0)
  if (!Number.isFinite(elapsed) || elapsed<=0) return '—'
  return elapsed>=60000 ? `${(elapsed/60000).toFixed(1)} 分钟` : `${(elapsed/1000).toFixed(1)} 秒`
}
export function parseAgentTime(value:string):number {return Date.parse(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)?value:`${value}Z`)}
export function agentCostSummary(usage:Record<string,unknown>):{label:string;note:string} {
  const cost=usage.cost
  const numeric=typeof cost==='number'&&Number.isFinite(cost)
  const status=String(usage.cost_status||usage.cost_coverage||(numeric&&cost!==0?'reported':'unknown'))
  if(usage.currency==='mixed')return {label:'多币种，无法合计',note:'请在用量详情中分别核对各执行会话的费用。'}
  const label=numeric&&status!=='unknown'?`${cost} ${usage.currency||''}${status==='partial'?'（已知部分）':''}`.trim():'未知'
  let note=status==='partial'?'仅汇总已报告的费用，仍有执行会话费用未知。':status==='complete'?'已汇总全部执行会话报告的费用。':status==='unknown'?'缺少完整计价信息，费用未知不代表免费。':'服务已报告费用，统计完整性尚未确认。'
  if(usage.incomplete)note+=' 本次执行尚未完整结束。'
  if(typeof usage.cost_note==='string')note+=` ${usage.cost_note}`
  return {label,note}
}
export function agentRunMarkdown(run:AgentRun):string {
  const output=asRecord(run.output)
  const json=(value:unknown)=>{
    const content=JSON.stringify(value,null,2)||'null'
    const longest=Math.max(2,...(content.match(/`+/g)||[]).map(item=>item.length))
    const fence='`'.repeat(longest+1)
    return `${fence}json\n${content}\n${fence}`
  }
  const evidence=agentEvidence(run.output).map((item,index)=>[
    `### ${item.citation_id||`来源${index+1}`}`,
    `文档：${item.source||item.title||item.document_id||'未提供'} · 片段：${item.chunk_id||'未提供'}`,
    String(item.quote||item.preview||item.content||'').split('\n').map(line=>`> ${line}`).join('\n'),
  ].join('\n\n')).join('\n\n')
  return [
    `# ${agentTaskLabel(run.task)}报告`,
    `任务：${run.id}  \n状态：${agentStatusLabels[run.status]||run.status}  \n创建：${run.created_at}  \n耗时：${agentElapsed(run)}  \n执行方式：${run.options?.apply?'已授权应用':'分析预览'}`,
    '## 任务目标',run.instructions||'未填写',
    '## 执行结果',agentSummary(run.output)||'任务尚未返回摘要。',
    '## 原文证据',evidence||'本次结果未提供原文证据。',
    '## 详细产物与应用记录',json(output||run.output||{}),
    '## 执行后端与用量',json({backend:run.backend,model:run.model,session_id:run.session_id,usage:run.usage,options:run.options,document_ids:run.document_ids,followup_jobs:run.followup_jobs}),
    '## 执行事件',json(run.events||[]),
  ].join('\n\n')+'\n'
}
