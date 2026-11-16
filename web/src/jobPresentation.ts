import type { ProcessingStage } from './types'

const jobNames:Record<string,string>={file:'文档处理',sync:'目录同步',rebuild:'重建文档索引',delete:'删除文档',connector_sync:'数据源同步',graph_rebuild:'知识图谱抽取',graph_maintenance:'主题社区维护',schema_generation:'生成知识结构',schema_drift:'检查知识结构变化',agent_workflow:'知识智能体任务',code_index:'代码分析',entity_vector_rebuild:'更新实体索引',relation_vector_rebuild:'更新关系索引',code_vector_rebuild:'更新代码索引'}
const statusNames:Record<string,string>={pending:'排队中',queued:'排队中',running:'处理中',processing:'处理中',completed:'已完成',succeeded:'已完成',partial:'部分完成',failed:'失败',cancelled:'已取消',retry:'等待重试',unknown:'未记录',waiting:'等待中',skipped:'无需执行'}
export const documentStages=[{id:'parse',name:'解析'},{id:'chunk',name:'切片'},{id:'vector',name:'向量索引'},{id:'schema',name:'知识结构'},{id:'graph',name:'图谱抽取'}] as const
export const jobName=(name:string):string=>jobNames[name]||'其他后台任务'
export const statusName=(status:string):string=>statusNames[status]||'状态待确认'
export const stageName=(name?:string|null):string=>documentStages.find(item=>item.id===name)?.name||({document:'处理原件',download:'读取文件',upload:'接收文件',index:'建立索引',embedding:'生成向量',extract:'抽取知识',queued:'等待执行'} as Record<string,string>)[name||'']||'等待阶段记录'
export function stageState(stages:ProcessingStage[]|undefined,name:string):string{
  const status=stages?.find(item=>item.name===name)?.status
  return status==='completed'||status==='succeeded'?'done':status==='running'||status==='processing'?'active':status==='failed'?'failed':status==='cancelled'?'cancelled':'waiting'
}
