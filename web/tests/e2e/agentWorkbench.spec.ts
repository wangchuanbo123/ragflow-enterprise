import { expect, test, type Page } from '@playwright/test'
import { readFile } from 'node:fs/promises'
import { agentTasks, type AgentRun } from '../../src/agents'

async function agentApi(page:Page,viewer=false,failed=false){
  const captured:unknown[]=[]
  const allowed:Record<string,string[]>={extraction:['reextract_documents'],ingestion:['reindex_documents','sync_sources','tag_documents'],schema_governance:['create_schema_draft'],quality_review:['propose_correction','apply_correction'],incremental_maintenance:['reextract_documents','reindex_documents'],knowledge_organization:['tag_documents','rebuild_communities'],evaluation:['evaluate_cases'],business_analysis:[],operations:['retry_jobs','adjust_batch','select_model_profile']}
  const detailReads=new Map<string,number>()
  const output=(task:string)=>({summary:`${agentTasks.find(item=>item.id===task)?.label}已完成：已核对文档证据并给出可审阅产物。见[来源1]`,items:[{title:'执行发现',description:'关键器件依赖单一供应商，需准备替代方案。'}],evidence:[{document_id:'doc',chunk_id:'chunk',quote:'关键器件依赖单一供应商。'}],actions:[{kind:'review',reason:'已校验任务白名单与文档范围'}],diagnostics:[{status:'ok',message:'codecode执行后端响应正常'}],metrics:{evidence_count:1,processed_documents:1}})
  const runs:AgentRun[]=agentTasks.map((task,index)=>({id:`run-${task.id}`,knowledge_base_id:'kb',task:task.id,status:failed&&index===0?'failed':'succeeded',instructions:`${task.label}验收目标`,document_ids:['doc'],options:{apply:false},created_at:'2026-11-05T02:00:00Z',started_at:'2026-11-05T02:00:00Z',finished_at:'2026-11-05T02:00:02Z',backend:'codecode',model:'vendor/model',session_id:`session-${index}`,usage:{input_tokens:100,output_tokens:20,total_tokens:120,cost:0},output:output(task.id),progress:{stage:'completed',current:3,total:3},events:[{id:`event-${index}`,sequence:1,type:'completed',message:'证据核对完成',data:{evidence_count:1},created_at:'2026-11-05T02:00:02Z'}],error_message:failed&&index===0?'模型服务暂时不可用':null}))
  await page.route('**/api/v1/**',async route=>{
    const path=new URL(route.request().url()).pathname.replace('/api/v1','');const method=route.request().method()
    const ok=(body:unknown,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)})
    if(path==='/auth/me')return ok({id:'user',username:'reviewer',display_name:'Reviewer',role:viewer?'user':'admin',is_active:true})
    if(path==='/knowledge-bases')return ok([{id:'kb',name:'采购知识库',slug:'kb',is_default:true,is_active:true,current_user_role:viewer?'viewer':'admin',capabilities:{can_read:true,can_edit:!viewer,can_manage_members:!viewer,can_archive:!viewer}}])
    if(path.endsWith('/agent-task-catalog'))return ok(agentTasks.map(task=>({task:task.id,label:task.label,description:task.description,allowed_actions:allowed[task.id],options:{apply:false,runtime_profiles:['default','quality']}})))
    if(path.endsWith('/documents/tag-map'))return ok({doc:['tag']})
    if(path.endsWith('/documents'))return ok([{id:'doc',original_filename:'采购协议.md',status:'ready',folder_id:'child'},{id:'other',original_filename:'其他文档.md',status:'ready',folder_id:null}])
    if(path.endsWith('/folders'))return ok([{id:'folder',name:'采购',path:'采购',parent_id:null},{id:'child',name:'协议',path:'采购/协议',parent_id:'folder'}])
    if(path.endsWith('/tags'))return ok([{id:'tag',name:'已审核',color:'#123456'}])
    if(path.endsWith('/sources'))return ok([{id:'source',name:'业务文档仓库',source_type:'git',status:'active'}])
    if(path.endsWith('/chunks/chunk'))return ok({chunk_id:'chunk',document_id:'doc',document_name:'采购协议.md',chunk_index:0,content:'完整证据：关键器件依赖单一供应商。',images:[],previous:[],next:[]})
    if(path.endsWith('/agent-runs/page')&&method==='GET'){const query=new URL(route.request().url()).searchParams;const matches=runs.filter(run=>(!query.get('task')||query.get('task')===run.task)&&(!query.get('status')||query.get('status')===run.status));const offset=Number(query.get('offset')||0),limit=Number(query.get('limit')||50);return ok({items:matches.slice(offset,offset+limit),total:matches.length,offset,limit})}
    if(path.endsWith('/agent-runs')&&method==='GET')return ok(runs)
    if(path.endsWith('/agent-runs')&&method==='POST'){
      const payload=route.request().postDataJSON();captured.push(payload)
      const run:AgentRun={...runs[0],...payload,id:'created',status:'queued',output:{},progress:{stage:'queued',current:0,total:3},events:[]}
      runs.unshift(run);return ok(run,201)
    }
    const match=path.match(/\/agent-runs\/([^/]+)(?:\/(cancel|retry|artifact))?$/)
    if(match){
      const run=runs.find(item=>item.id===match[1]);if(!run)return ok({detail:'not found'},404)
      if(match[2]==='artifact')return ok(run)
      if(match[2]==='cancel'){run.status='cancelled';return ok(run)}
      if(match[2]==='retry'){const next:AgentRun={...run,id:'retried',parent_run_id:run.id,status:'queued',output:{},events:[],attempt:2};runs.unshift(next);return ok(next,201)}
      if(run.status==='queued'||run.status==='running'){
        const count=(detailReads.get(run.id)||0)+1;detailReads.set(run.id,count)
        if(count===2){run.status='running';run.progress={stage:'核对证据',current:1,total:3}}
        if(count>=3){run.status='succeeded';run.output=output(run.task);run.progress={stage:'completed',current:3,total:3};run.events=[{id:'done',type:'completed',message:'授权动作已完成',data:{count:1},created_at:'2026-11-05T02:00:02Z'}]}
      }
      return ok(run)
    }
    return ok([])
  })
  return captured
}

test('creates a scoped authorized task, follows progress, and displays results with original evidence',async({page})=>{
  const created=await agentApi(page);await page.goto('/#page=agents&kb=kb')
  await expect(page.getByLabel('智能体任务类型').locator('option')).toHaveCount(9)
  await page.getByLabel('智能体任务类型').selectOption('quality_review')
  await page.getByLabel('任务文档范围').selectOption('folder');await page.getByLabel('任务文件夹',{exact:true}).selectOption('folder')
  await page.getByLabel('智能体任务目标').fill('核对采购协议中的风险和证据，修复允许的图谱质量问题')
  await page.getByLabel('授权应用任务动作').check();await page.getByLabel('允许应用经过校验的修复',{exact:true}).uncheck();await page.getByRole('button',{name:'创建已授权任务',exact:true}).click()
  await expect.poll(()=>created.length).toBe(1)
  expect(created[0]).toMatchObject({task:'quality_review',document_ids:['doc'],options:{apply:true,allowed_actions:['propose_correction'],max_actions:10}})
  await expect(page.locator('.run-header')).toContainText('等待执行')
  await expect(page.locator('.run-detail .answer-content')).toContainText('质量检查与修复已完成',{timeout:15000})
  await expect(page.locator('.run-detail')).toContainText('关键器件依赖单一供应商，需准备替代方案')
  await page.locator('.run-detail .evidence-card').click();await expect(page.locator('.evidence-drawer')).toContainText('完整证据：关键器件依赖单一供应商')
  await expect(page).toHaveURL(/run=created/)
})

test('viewer can inspect concrete outputs for all nine workflows and cannot create or control runs',async({page})=>{
  await agentApi(page,true);await page.goto('/#page=agents&kb=kb')
  await expect(page.locator('.create-panel')).toHaveCount(0)
  await expect(page.getByText('创建和控制任务需要编辑权限',{exact:false})).toBeVisible()
  for(const task of agentTasks){
    await page.locator('.run-item').filter({hasText:task.label}).click()
    await expect(page.locator('.run-detail .answer-content')).toContainText(`${task.label}已完成`)
    await expect(page.locator('.run-detail')).toContainText('关键器件依赖单一供应商，需准备替代方案')
  }
  await expect(page.getByRole('button',{name:'取消任务',exact:true})).toHaveCount(0)
  await expect(page.getByRole('button',{name:'重试任务',exact:true})).toHaveCount(0)
  const report=page.waitForEvent('download');await page.getByRole('button',{name:'导出报告',exact:true}).click();const markdown=await report
  expect(markdown.suggestedFilename()).toMatch(/\.md$/)
  expect(await readFile((await markdown.path())!,'utf8')).toContain('关键器件依赖单一供应商')
  const exported=page.waitForEvent('download');await page.getByRole('button',{name:'导出 JSON',exact:true}).click();const json=await exported
  expect(JSON.parse(await readFile((await json.path())!,'utf8')).task).toBe('operations')
})

test('failed runs show diagnostic messages and support linked retry and cancellation',async({page})=>{
  await agentApi(page,false,true);await page.goto('/#page=agents&kb=kb')
  await expect(page.locator('.run-detail')).toContainText('模型服务暂时不可用')
  await page.getByRole('button',{name:'重试任务',exact:true}).click()
  await expect(page.locator('.run-header')).toContainText('retried')
  await page.getByRole('button',{name:'取消任务',exact:true}).click()
  await expect(page.locator('.run-header')).toContainText('已取消')
})

test('evaluation edits gold evidence, registered models and prompt variants with an explicit action subset',async({page})=>{
  const created=await agentApi(page);await page.goto('/#page=agents&kb=kb')
  await page.getByLabel('智能体任务类型').selectOption('evaluation')
  await page.getByLabel('任务文档范围').selectOption('documents');await page.getByLabel('任务选择文档').selectOption('doc')
  await page.getByLabel('智能体评测问题').fill('有哪些单一供应风险？')
  await page.getByText('金标准、模型与提示词对照',{exact:false}).click()
  await page.getByLabel('启用评测金标准').check()
  await page.getByLabel('问题1参考答案').fill('关键器件依赖单一供应商。')
  await page.getByLabel('问题1期望文档').selectOption('doc')
  await page.getByLabel('评测比较模型').selectOption(['default','quality'])
  await page.getByRole('button',{name:'添加提示词对照',exact:true}).click()
  await page.getByLabel('提示词对照1名称').fill('证据优先')
  await page.getByLabel('提示词对照1内容').fill('逐项引用原文，明确区分事实与推断。')
  await page.getByRole('button',{name:'创建预览任务',exact:true}).click()
  await expect.poll(()=>created.length).toBe(1)
  expect(created[0]).toMatchObject({task:'evaluation',document_ids:['doc'],options:{apply:false,allowed_actions:['evaluate_cases'],questions:[],comparison_profiles:['default','quality'],prompt_variants:[{name:'证据优先',instructions:'逐项引用原文，明确区分事实与推断。'}],gold_cases:[{question:'有哪些单一供应风险？',reference_answer:'关键器件依赖单一供应商。',expected_document_ids:['doc'],expected_chunk_ids:[]}]}})
})
