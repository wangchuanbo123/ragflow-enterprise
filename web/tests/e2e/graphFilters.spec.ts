import { expect, test, type Page } from '@playwright/test'

async function graphApi(page:Page,editor=false){
  const principles:unknown[]=[];const schemas:unknown[]=[]
  let saved:Array<{id:string;name:string;principles:string[];schema_fingerprint:string;created_at:string;updated_at:string}>=[]
  const nodes=[{id:'order',label:'订单服务',entity_type:'SYSTEM',description:'接收订单',mention_count:2},{id:'payment',label:'支付服务',entity_type:'SYSTEM',description:'处理支付',mention_count:1}]
  const edges=[{id:'calls',source:'order',target:'payment',predicate:'CALLS',confidence:1,evidence:[{chunk_id:'chunk',document_id:'doc',quote:'订单服务调用支付服务。',confidence:1}]}]
  const preview=(rules:string[])=>({nodes:rules.includes('没有匹配')?[]:nodes,edges:rules.includes('没有匹配')?[]:edges,summary:rules.length?`系统已理解：${rules.join('；')}`:'全库图谱，未添加过滤原则',plan:{version:1,principles:rules.map((summary,index)=>({index,status:'applied',summary}))},warnings:[],unapplied_principles:[],schema_fingerprint:'schema',stats:{total_nodes:10,total_edges:12,matched_nodes:rules.includes('没有匹配')?0:10,matched_edges:rules.includes('没有匹配')?0:12,returned_nodes:rules.includes('没有匹配')?0:2,returned_edges:rules.includes('没有匹配')?0:1,truncated:!rules.includes('没有匹配')}})
  const activeSchema={id:'schema',graph_kind:'general',version:1,status:'active',entity_types_json:['SYSTEM'],relation_types_json:['CALLS'],normalization_rules_json:{},generation_method:'manual',generation_summary_json:{},source_document_ids_json:[],graph_profiles_json:null,extraction_prompt:null}
  await page.route('**/api/v1/**',async route=>{
    const path=new URL(route.request().url()).pathname.replace('/api/v1',''),method=route.request().method()
    const ok=(body:unknown,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)})
    if(path==='/auth/me')return ok({id:'user',username:'viewer',display_name:'Viewer',role:editor?'admin':'user',is_active:true})
    if(path==='/knowledge-bases')return ok([{id:'kb',name:'订单知识库',slug:'kb',is_active:true,is_default:true,current_user_role:editor?'admin':'viewer',capabilities:{can_read:true,can_edit:editor,can_manage_members:editor,can_archive:editor}}])
    if(path.endsWith('/filter-preview')){
      const body=route.request().postDataJSON();principles.push(body)
      if(body.principles.includes('含糊的原则'))return ok({error:{code:'GRAPH_FILTER_UNSUPPORTED',message:'原则含义不明确',details:{summary:'请说明希望保留什么',unapplied_principles:[{index:0,principle:'含糊的原则',reason:'无法确定筛选条件'}]}}},422)
      return ok(preview(body.principles))
    }
    if(path.endsWith('/knowledge-graph/filters')&&method==='GET')return ok(saved)
    if(path.endsWith('/knowledge-graph/filters')&&method==='POST'){
      const body=route.request().postDataJSON();const value={id:'mine',...body,schema_fingerprint:'schema',created_at:'',updated_at:''};saved.push(value);return ok(value,201)
    }
    if(path.endsWith('/knowledge-graph/filters/mine/preview'))return ok(preview(saved[0].principles))
    if(path.endsWith('/knowledge-graph/filters/mine')&&method==='DELETE'){saved=[];return route.fulfill({status:204})}
    if(path.endsWith('/knowledge-graph/stats'))return ok({entities:10,relations:12,mentions:15,evidence:12,aliases:0,active_corrections:0,conflict_corrections:0})
    if(path.endsWith('/knowledge-graph/entities'))return ok([{id:'order',canonical_name:'订单服务',entity_type:'SYSTEM',description:'接收订单',aliases:[]}])
    if(path.endsWith('/knowledge-graph/subgraph'))return ok({seed_entity_ids:['order'],nodes,edges,offset:0,next_offset:null})
    if(path.endsWith('/knowledge-graph/schemas')&&method==='POST'){const body=route.request().postDataJSON();schemas.push(body);return ok({...activeSchema,id:'draft',status:'draft',...body},201)}
    if(path.endsWith('/knowledge-graph/schemas'))return ok([activeSchema])
    if(path.endsWith('/automation'))return ok({auto_build_enabled:true,auto_schema_generation_enabled:true})
    if(path.endsWith('/knowledge-graph/coverage'))return ok({summary:{documents:0,chunks:0,processed_chunks:0,fact_chunks:0,processing_rate:0,fact_coverage_rate:0},documents:[]})
    if(path.endsWith('/knowledge-graph/quality'))return ok(null)
    if(path.endsWith('/documents/tag-map'))return ok({})
    if(path.endsWith('/documents'))return ok([{id:'doc',original_filename:'订单说明.md',status:'ready'}])
    return ok([])
  })
  return {principles,schemas}
}

test('a viewer applies spoken principles, persists a personal view across refresh, and deletes it',async({page})=>{
  const captured=await graphApi(page);await page.goto('/#page=graph&kb=kb')
  await expect(page.getByRole('heading',{name:'知识库总图谱',exact:true})).toBeVisible()
  await expect(page.locator('.filter-understanding')).toContainText('全库图谱')
  await expect(page.locator('.type-legend')).toContainText('系统')
  await expect(page.locator('.view-toolbar')).toContainText('调用')
  await expect(page.locator('.graph-catalog')).toHaveCount(0)
  await page.getByRole('button',{name:'添加原则',exact:true}).click()
  await page.getByLabel('过滤原则 1',{exact:true}).fill('只看订单服务及其关联关系')
  await page.getByRole('button',{name:'添加原则',exact:true}).click()
  await page.getByLabel('过滤原则 2',{exact:true}).fill('隐藏文档类实体')
  await page.getByLabel('启用原则 2',{exact:true}).uncheck()
  await expect(page.locator('.pending-principles')).toContainText('尚未应用')
  await page.getByRole('button',{name:'应用原则',exact:true}).click()
  await expect(page.locator('.filter-understanding')).toContainText('系统已理解：只看订单服务及其关联关系')
  expect(captured.principles.at(-1)).toMatchObject({principles:['只看订单服务及其关联关系']})
  await expect(page.locator('.filter-counts')).toContainText('结果已截断')
  await page.getByLabel('个人视图名称').fill('订单关联视图');await page.getByRole('button',{name:'保存当前原则',exact:true}).click()
  await expect(page.getByLabel('已保存的个人视图')).toHaveValue('mine')
  await page.reload();await page.getByLabel('已保存的个人视图').selectOption('mine')
  await page.getByRole('button',{name:'应用个人视图',exact:true}).click()
  await expect(page.getByLabel('过滤原则 1',{exact:true})).toHaveValue('只看订单服务及其关联关系')
  await expect(page.locator('.filter-understanding')).toContainText('系统已理解')
  await page.getByRole('button',{name:'删除个人视图',exact:true}).click()
  await expect(page.getByLabel('已保存的个人视图').locator('option')).toHaveCount(1)
})

test('unsupported principles keep the old graph visible and empty matches never fall back to the full graph',async({page})=>{
  await graphApi(page);await page.goto('/#page=graph&kb=kb')
  await expect(page.locator('.filter-understanding')).toContainText('全库图谱')
  await page.getByRole('button',{name:'添加原则',exact:true}).click();await page.getByLabel('过滤原则 1',{exact:true}).fill('含糊的原则')
  await page.getByRole('button',{name:'应用原则',exact:true}).click()
  await expect(page.locator('.filter-failure')).toContainText('无法确定筛选条件')
  await expect(page.locator('.filter-understanding')).toContainText('全库图谱')
  await expect(page.locator('.pending-principles')).toContainText('仍显示上次成功应用的图谱')
  await page.getByLabel('过滤原则 1',{exact:true}).fill('没有匹配');await page.getByRole('button',{name:'应用原则',exact:true}).click()
  await expect(page.getByText('当前原则或查看范围没有匹配结果',{exact:true})).toBeVisible()
  await expect(page.locator('.filter-counts')).toContainText('匹配 0 / 10')
  await page.getByRole('button',{name:'清除原则，显示全库',exact:true}).click()
  await expect(page.locator('.filter-understanding')).toContainText('全库图谱，未添加过滤原则')
})

test('Schema management has one knowledge-base schema and creates only general drafts',async({page})=>{
  const captured=await graphApi(page,true);await page.goto('/#page=graph-management&kb=kb')
  await page.getByRole('button',{name:'Schema 管理',exact:true}).click()
  await expect(page.getByText('全库只有一个活动 Schema',{exact:false})).toBeVisible()
  await expect(page.locator('.profile-schema-builder')).toHaveCount(0)
  await expect(page.locator('.schema-layout select')).toHaveCount(0)
  await page.locator('.schema-layout').getByRole('button',{name:'创建草稿',exact:true}).click()
  await expect.poll(()=>captured.schemas.length).toBe(1)
  expect(captured.schemas[0]).toMatchObject({graph_kind:'general'})
  expect(captured.schemas[0]).not.toHaveProperty('graph_profiles')
})
