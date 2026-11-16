import { expect, test, type Page } from '@playwright/test'

async function mockApi(page: Page, viewer = false) {
  let user: Record<string,unknown> | null = { id:'a',username:'alice',display_name:'Alice',role:viewer?'user':'admin',is_active:true }
  const capabilities = { can_read:true,can_edit:!viewer,can_manage_members:!viewer,can_archive:!viewer }
  const bases = ['kb','other'].map(id=>({id,name:id==='kb'?'产品知识库':'其他知识库',slug:id,description:'',is_default:id==='kb',is_active:true,current_user_role:viewer?'viewer':'admin',capabilities}))
  const conversations: Record<string,unknown>[] = []
  const folders: Record<string,unknown>[] = []
  const tags: Record<string,unknown>[] = []
  const users: Record<string,unknown>[] = [{...user}]
  let modelProbe: Record<string,unknown> | null = null
  const document = {id:'doc',knowledge_base_id:'kb',original_filename:'设计说明.md',source_path:'设计说明.md',status:'ready',chunk_count:1,current_version_no:1,file_size:100,created_at:new Date().toISOString(),updated_at:new Date().toISOString(),folder_id:null}
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url()); const path=url.pathname.replace('/api/v1',''); const method=route.request().method()
    const ok=(body: unknown,status=200)=>route.fulfill({status,contentType:'application/json',body:status===204?'':JSON.stringify(body)})
    if(path==='/auth/me') return ok(user||{detail:'unauthorized'},user?200:401)
    if(path==='/auth/logout') { user=null; return ok(null,204) }
    if(path==='/auth/login') { user={id:'b',username:'bob',display_name:'Bob',role:'user',is_active:true};conversations.length=0; return ok({user}) }
    if(path==='/auth/password') { user=null; return ok(null,204) }
    if(path==='/users'&&method==='GET') return ok(users)
    if(path==='/users'&&method==='POST') { const {password:_,...body}=route.request().postDataJSON();const created={id:'created',...body}; users.push(created); return ok(created,201) }
    if(path==='/knowledge-bases') return ok(bases)
    if(path==='/conversations'&&method==='GET') return ok(conversations)
    if(path==='/conversations'&&method==='POST') { const body=route.request().postDataJSON(); const c={id:'new',knowledge_base_id:body.knowledge_base_id,title:'新会话',created_at:'',updated_at:'',last_message_at:null};conversations.unshift(c); return ok(c,201) }
    if(path==='/conversations/new/messages/stream') return route.fulfill({contentType:'text/event-stream',body:[
      'event: message\ndata: {"message_id":"assistant"}',
      'event: delta\ndata: '+JSON.stringify({content:'私密答案\n```python\n"""docstring"""\n```'}),
      'event: sources\ndata: {"sources":[]}',
      'event: done\ndata: {"status":"completed"}',
    ].join('\n\n')+'\n\n'})
    if(path==='/conversations/new') return ok({...conversations[0],messages:[]})
    if(path==='/ready') return ok({status:'not_ready',checked_at:new Date().toISOString(),components:{database:{status:'error',detail:'数据库连接失败，请检查服务'},...(modelProbe?{model_probe:{status:'fail',profiles:modelProbe.profiles,checked_at:modelProbe.checked_at}}:{})}},503)
    if(path==='/operations/metrics') return ok({
      checked_at:new Date().toISOString(),source:'shared_redis',retention_days:7,
      tasks:{pending:2,running:1},oldest_pending_seconds:10,metrics:{model_first_token_seconds:{observations:2,total:1,mean:0.5},model_stream_seconds:{observations:2,total:8,mean:4}},
      database_pool:{size:5,checkedin:3,checkedout:2,overflow:0},process:{rss_bytes:1024**3},
      disk:{total_bytes:100*1024**3,free_bytes:40*1024**3},memory:{percent:25,scope:'container'},
      backup:{status:'backed_up',last_success_at:new Date().toISOString(),mirror_status:'unconfigured',rpo_target_hours:24,rto_target_minutes:60,rto_verified:false},
      alerts:[{code:'BACKUP_MIRROR_UNVERIFIED',message:'异机备份副本尚未配置或未通过校验'}],
    })
    if(path==='/model-probe') { modelProbe={status:'failed',checked_at:new Date().toISOString(),profiles:{answer:{status:'fail',elapsed_ms:10,error_type:'APIStatusError',http_status:503}}};return ok(modelProbe) }
    if(path.endsWith('/search')) return ok([{chunk_id:'chunk',document_id:'doc',document_name:'A库唯一文档',chunk_index:0,score:1,snippet:'架构结果',content:'架构结果',images:[]}])
    if(path.endsWith('/documents/tag-map')) return ok({doc:[]})
    if(path.endsWith('/documents/page')) { const matches=!url.searchParams.get('query')||document.original_filename.includes(url.searchParams.get('query')!);return ok({items:matches?[document]:[],total:matches?1:0,offset:Number(url.searchParams.get('offset')||0),limit:Number(url.searchParams.get('limit')||50)}) }
    if(path.endsWith('/documents/doc')&&method==='GET')return ok(document)
    if(path.endsWith('/documents')&&method==='GET') return ok([document])
    if(path.endsWith('/documents/batch')) return ok({updated:1})
    if(path.endsWith('/documents/doc/tags')) return ok(method==='GET'?[]:route.request().postDataJSON().tag_ids)
    if(path.endsWith('/folders')&&method==='POST') { const body=route.request().postDataJSON();const folder={id:'folder',name:body.name,path:body.name,parent_id:null}; folders.push(folder);return ok(folder,201) }
    if(path.endsWith('/folders')) return ok(folders)
    if(path.endsWith('/tags')&&method==='POST') {const body=route.request().postDataJSON();const tag={id:'tag',...body}; tags.push(tag);return ok(tag,201)}
    if(path.endsWith('/tags')) return ok(tags)
    if(path.endsWith('/subgraph')) return ok({nodes:[],edges:[]})
    if(path.endsWith('/document-coverage')) return ok({documents:[],summary:{}})
    if(path.endsWith('/index-jobs/page'))return ok({items:[{id:'job',job_type:'code_index',status:'pending',created_at:'',total_items:0,processed_items:0,succeeded_items:0,failed_items:0,attempt_count:0,max_attempts:3,document_ids:['doc'],document_names:['设计说明.md'],current_stage:null,waiting_reason:'等待可用的后台处理进程'}],total:1,offset:0,limit:50})
    if(path.endsWith('/index-jobs')) return ok([{id:'job',job_type:'code_index',status:'pending',created_at:'',total_items:0,processed_items:0,succeeded_items:0,failed_items:0,attempt_count:0,max_attempts:3}])
    if(path.endsWith('/cancel')) return ok({id:'job',status:'cancelled'})
    if(path.endsWith('/chunks')) return ok([{id:'chunk',chunk_index:0,content:'文档正文',token_count:10,graph_status:'pending'}])
    return ok([])
  })
}

test('first question creates a conversation, renders Python code, and logout isolates the next account', async ({page})=>{
  await mockApi(page); await page.goto('/#page=chat&kb=kb')
  await page.getByPlaceholder('输入问题，Enter 发送，Shift+Enter 换行').fill('业务问题')
  await page.getByRole('button',{name:'发送',exact:true}).click()
  await expect(page.getByText('私密答案',{exact:true})).toBeVisible()
  await expect(page.locator('pre code').first()).toHaveText('"""docstring"""')
  await page.getByTitle('退出').click()
  await page.getByLabel('用户名').fill('bob');await page.getByLabel('密码',{exact:true}).fill('TestingPassword123')
  await page.getByRole('button',{name:'登录',exact:true}).click()
  await expect(page.getByText('Bob',{exact:true})).toBeVisible()
  await expect(page.getByText('私密答案',{exact:true})).toHaveCount(0)
})

test('switching knowledge bases clears old search results and persists navigation in URL', async ({page})=>{
  await mockApi(page); await page.goto('/#page=search&kb=kb')
  await page.getByPlaceholder('搜索知识库全文，例如：部署架构 或 权限模型').fill('架构')
  await page.getByRole('button',{name:'搜索',exact:true}).click();await expect(page.getByText('A库唯一文档')).toBeVisible()
  await page.locator('.kb-select select').selectOption('other')
  await expect(page.getByText('A库唯一文档')).toHaveCount(0)
  await expect(page).toHaveURL(/kb=other/)
  await page.reload();await expect(page.locator('.kb-select select')).toHaveValue('other')
  await expect(page.getByRole('button',{name:'搜索',exact:true})).toBeVisible()
  await page.goBack()
  await expect(page.locator('.kb-select select')).toHaveValue('kb')
  await expect(page.getByPlaceholder('搜索知识库全文，例如：部署架构 或 权限模型')).toHaveValue('架构')
  await expect(page.getByText('A库唯一文档')).toBeVisible()
})

test('pending tasks can be cancelled and readiness failures show component diagnostics', async ({page})=>{
  await mockApi(page)
  const renderErrors:string[]=[];page.on('pageerror',error=>renderErrors.push(error.message))
  let metricRequests=0
  await page.route('**/api/v1/operations/metrics',async route=>{
    metricRequests+=1
    if(metricRequests===1)return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({error:{message:'invalid upstream response'}})})
    return route.fallback()
  })
  await page.goto('/#page=jobs&kb=kb')
  await page.locator('.toolbar-actions select').selectOption('pending')
  await expect(page.getByRole('button',{name:'取消',exact:true})).toBeVisible()
  await page.getByRole('button',{name:'运行状态'}).click()
  await expect(page.getByText('数据库连接失败，请检查服务')).toBeVisible()
  await expect(page.locator('.operations-overview [role="alert"]')).toContainText('运行指标响应格式无效')
  await page.getByRole('button',{name:'验证模型连接',exact:true}).click()
  await expect(page.getByText('模型连接验证失败',{exact:true})).toBeVisible()
  await expect(page.getByText('APIStatusError · HTTP 503')).toBeVisible()
  await page.getByRole('button',{name:'刷新概览',exact:true}).click()
  await expect(page.locator('.operations-overview [role="alert"]')).toHaveCount(0)
  await expect(page.locator('.operations-overview')).toContainText('排队中2 项')
  await expect(page.locator('.operations-overview')).toContainText('异机备份未配置')
  expect(renderErrors).toEqual([])
})

test('viewer sees documents but no editing, membership or governance actions', async ({page})=>{
  await mockApi(page,true);await page.goto('/#page=documents&kb=kb')
  await expect(page.getByText('设计说明.md',{exact:true})).toBeVisible()
  await expect(page.getByText('批量上传',{exact:true})).toHaveCount(0)
  await expect(page.getByRole('button',{name:'图谱治理'})).toHaveCount(0)
  await page.getByRole('button',{name:'数据源与成员'}).click()
  await expect(page.getByRole('button',{name:'保存连接器'})).toHaveCount(0)
  await page.getByRole('button',{name:'成员与权限'}).click()
  await expect(page.getByRole('button',{name:'保存成员'})).toHaveCount(0)
})

test('editor can create folders and tags and classify a document', async ({page})=>{
  await mockApi(page);await page.goto('/#page=documents&kb=kb')
  await page.getByRole('button',{name:'文件夹与标签',exact:true}).click()
  await page.getByLabel('名称',{exact:true}).fill('研发文档');await page.getByRole('button',{name:'保存文件夹',exact:true}).click()
  await expect(page.locator('.catalog')).toContainText('研发文档')
  await page.getByRole('button',{name:'新建标签',exact:true}).click()
  await page.getByLabel('名称',{exact:true}).fill('已审核');await page.getByRole('button',{name:'保存标签',exact:true}).click()
  await expect(page.locator('.catalog')).toContainText('已审核')
  await page.getByRole('button',{name:'关闭',exact:true}).click();await page.getByText('设计说明.md',{exact:true}).click()
  await page.getByLabel('文档文件夹',{exact:true}).selectOption('folder')
  await page.getByLabel('文档标签',{exact:true}).selectOption('tag')
  await page.getByRole('button',{name:'保存分类',exact:true}).click()
  await expect(page.getByText('文档分类已保存')).toBeVisible()
})

test('administrator creates a user and sees real probe failure details', async ({page})=>{
  await mockApi(page);await page.goto('/#page=users&kb=kb')
  await page.getByLabel('用户名',{exact:true}).fill('charlie')
  await page.getByLabel('显示名称',{exact:true}).fill('Charlie')
  await page.getByLabel('初始密码',{exact:true}).fill('TestingPassword123')
  await page.getByRole('button',{name:'保存用户',exact:true}).click()
  await expect(page.getByText('Charlie',{exact:true})).toBeVisible()
  await page.getByRole('button',{name:'运行状态',exact:true}).click()
  await page.getByRole('button',{name:'验证模型连接',exact:true}).click()
  await expect(page.getByText('模型连接验证失败',{exact:true})).toBeVisible()
  await expect(page.getByText('APIStatusError · HTTP 503')).toBeVisible()
})

test('changing own password requires matching confirmation and signs out after success', async ({page})=>{
  await mockApi(page);await page.goto('/#page=account&kb=kb')
  await page.getByLabel('当前密码',{exact:true}).fill('old-password')
  await page.getByLabel('新密码',{exact:true}).fill('NewTestingPassword123')
  await page.getByLabel('确认新密码',{exact:true}).fill('WrongTestingPassword123')
  await page.getByRole('button',{name:'保存并重新登录',exact:true}).click()
  await expect(page.getByText('两次输入的新密码不一致')).toBeVisible()
  await page.getByLabel('确认新密码',{exact:true}).fill('NewTestingPassword123')
  await page.getByRole('button',{name:'保存并重新登录',exact:true}).click()
  await expect(page.getByRole('button',{name:'登录',exact:true})).toBeVisible()
})

test('daily navigation preserves a question draft and explicitly chosen answer range',async({page})=>{
  await mockApi(page);await page.goto('/#page=chat&kb=kb')
  await expect(page.locator('.nav-group-label')).toHaveText(['日常使用','知识维护','系统管理'])
  await expect(page.locator('.scope-bar select').first()).toHaveValue('knowledge_base')
  await page.getByPlaceholder('输入问题，Enter 发送，Shift+Enter 换行').fill('尚未发送的业务问题')
  await page.locator('.scope-bar select').first().selectOption('all')
  await page.getByRole('button',{name:'文档管理',exact:true}).click()
  await expect(page.getByText('设计说明.md',{exact:true})).toBeVisible()
  await page.getByRole('button',{name:'知识问答',exact:true}).click()
  await expect(page.getByPlaceholder('输入问题，Enter 发送，Shift+Enter 换行')).toHaveValue('尚未发送的业务问题')
  await expect(page.locator('.scope-bar select').first()).toHaveValue('all')
  await page.locator('.kb-select select').selectOption('other')
  await expect(page.getByPlaceholder('输入问题，Enter 发送，Shift+Enter 换行')).toHaveValue('')
  await expect(page.locator('.scope-bar select').first()).toHaveValue('knowledge_base')
})

test('readable task status links directly to a document detail and search uses server paging',async({page})=>{
  await mockApi(page);await page.goto('/#page=jobs&kb=kb')
  await expect(page.locator('.job-main header strong')).toHaveText('代码分析')
  await expect(page.locator('.job-stage')).toContainText('等待可用的后台处理进程')
  await page.getByRole('button',{name:'查看 设计说明.md',exact:true}).click()
  await expect(page).toHaveURL(/page=documents.*document=doc/)
  await expect(page.locator('.detail-head strong')).toHaveText('设计说明.md')
  await page.getByPlaceholder('按文件名搜索').fill('不存在的名称')
  await expect(page.getByText('没有匹配的文档，请调整搜索或筛选。')).toBeVisible()
  await expect(page.locator('.detail-head strong')).toHaveText('设计说明.md')
})
