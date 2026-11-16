import { computed } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import cytoscape from 'cytoscape'
import GraphView from '../../src/components/GraphView.vue'
import GraphManagementView from '../../src/components/GraphManagementView.vue'
import { knowledgeBaseKey } from '../../src/composables/usePermissions'
import type { GraphFilterPreview, GraphProfile, GraphSchema } from '../../src/types'
import * as api from '../../src/api/client'
import { useAuth } from '../../src/composables/useAuth'

vi.mock('../../src/api/client')
vi.mock('cytoscape',()=>({default:vi.fn()}))
const node=(id:string)=>({id,label:`实体${id}`,entity_type:'SYSTEM',description:'实体描述',mention_count:1})
const edge=(id:string,source:string,target:string)=>({id,source,target,predicate:'DEPENDS_ON',confidence:1,evidence:[{chunk_id:'chunk',quote:'原文证据',confidence:1}]})
function preview(summary='知识库完整有效图谱',filtered=false):GraphFilterPreview{return {nodes:filtered?[node('a')]:[node('a'),node('b')],edges:filtered?[]:[edge('ab','a','b')],summary,warnings:[],plan:{version:1,principles:filtered?[{index:0,status:'applied',summary:'保留指定实体'}]:[]},unapplied_principles:[],schema_fingerprint:'schema',stats:{total_nodes:8,total_edges:10,matched_nodes:filtered?1:8,matched_edges:filtered?0:10,returned_nodes:filtered?1:2,returned_edges:filtered?0:1,truncated:!filtered}}}
function renderGraph(edit=false){return mount(GraphView,{props:{knowledgeBaseId:'kb'},global:{provide:{[knowledgeBaseKey as symbol]:computed(()=>({capabilities:{can_edit:edit,can_read:true}}))}}})}
async function click(wrapper:VueWrapper,label:string){await wrapper.findAll('button').find(button=>button.text()===label)!.trigger('click');await flushPromises()}

beforeEach(()=>{
  vi.clearAllMocks()
  window.sessionStorage.clear();useAuth().user.value=null
  vi.stubGlobal('ResizeObserver',class{observe(){}disconnect(){}})
  vi.mocked(cytoscape).mockImplementation(()=>({destroy:vi.fn(),on:vi.fn(),resize:vi.fn(),fit:vi.fn()} as never))
  vi.mocked(api.getGraphStats).mockResolvedValue({entities:8,relations:10,mentions:12,evidence:10,aliases:0,active_corrections:0,conflict_corrections:0})
  vi.mocked(api.listDocuments).mockResolvedValue([{id:'doc',original_filename:'文档',status:'ready'}] as never)
  vi.mocked(api.listGraphCorrections).mockResolvedValue([]);vi.mocked(api.listGraphCommunities).mockResolvedValue([])
  vi.mocked(api.listGraphProfiles).mockResolvedValue([]);vi.mocked(api.listGraphSchemas).mockResolvedValue([])
  vi.mocked(api.listGraphFilters).mockResolvedValue([]);vi.mocked(api.previewGraphFilter).mockResolvedValue(preview())
})

describe('graph viewing continuity and evidence',()=>{
  it('restores unapplied draft and layout without applying the draft or leaking to another account',async()=>{
    useAuth().user.value={id:'alice',username:'alice',display_name:'Alice',role:'user',is_active:true}
    const wrapper=renderGraph();await flushPromises()
    await click(wrapper,'添加原则');await wrapper.find('[aria-label="过滤原则 1"]').setValue('尚未应用的草稿')
    await wrapper.findAll('.view-toolbar select')[2].setValue('breadthfirst')
    wrapper.unmount();vi.mocked(api.previewGraphFilter).mockClear()
    const restored=renderGraph();await flushPromises()
    expect(restored.find('[aria-label="过滤原则 1"]').element).toHaveProperty('value','尚未应用的草稿')
    expect(restored.findAll('.view-toolbar select')[2].element).toHaveProperty('value','breadthfirst')
    expect(api.previewGraphFilter).toHaveBeenLastCalledWith('kb',expect.objectContaining({principles:[]}),expect.any(AbortSignal))
    expect(restored.find('.pending-principles').exists()).toBe(true)
    restored.unmount();useAuth().user.value={id:'bob',username:'bob',display_name:'Bob',role:'user',is_active:true}
    const other=renderGraph();await flushPromises()
    expect(other.find('[aria-label="过滤原则 1"]').exists()).toBe(false)
  })
  it('lets users explicitly choose a disambiguation candidate without automatically applying it',async()=>{
    const wrapper=renderGraph();await flushPromises();await click(wrapper,'添加原则')
    await wrapper.find('[aria-label="过滤原则 1"]').setValue('排除订单相关实体')
    vi.mocked(api.previewGraphFilter).mockRejectedValueOnce(Object.assign(new Error('名称不明确'),{details:{unapplied_principles:[{index:0,principle:'排除订单相关实体',reason:'存在同名实体',candidates:[{id:'entity-a',label:'订单',value:'entity-a',description:'业务系统'}]}]}}))
    await wrapper.find('.graph-principles form').trigger('submit');await flushPromises()
    await wrapper.find('.ambiguity-candidates button').trigger('click')
    expect(wrapper.find('[aria-label="过滤原则 1"]').element).toHaveProperty('value',expect.stringContaining('排除订单相关实体'))
    expect(wrapper.find('[aria-label="过滤原则 1"]').element).toHaveProperty('value',expect.stringContaining('entity-a'))
    expect(api.previewGraphFilter).toHaveBeenCalledTimes(2)
    expect(wrapper.find('.pending-principles').exists()).toBe(true)
  })
  it('opens relation evidence with the file, page, highlighted quote and surrounding text',async()=>{
    useAuth().user.value={id:'alice',username:'alice',display_name:'Alice',role:'user',is_active:true}
    vi.mocked(api.getChunkDetail).mockResolvedValue({chunk_id:'chunk',document_id:'doc',document_name:'设计说明.pdf',chunk_index:2,page:5,section:'依赖关系',content:'前置说明：原文证据。后置说明',images:[],previous:[{chunk_id:'before',chunk_index:1,content:'前一段上下文',page:4,section:null}],next:[{chunk_id:'after',chunk_index:3,content:'后一段上下文',page:5,section:null}]})
    const wrapper=renderGraph();await flushPromises()
    const cy=vi.mocked(cytoscape).mock.results.at(-1)!.value as {on:ReturnType<typeof vi.fn>}
    const tap=cy.on.mock.calls.find(call=>call[0]==='tap'&&call[1]==='edge')![2]
    tap({target:{id:()=> 'ab'}});await flushPromises()
    await click(wrapper,'查看文件、页码与上下文')
    expect(api.getChunkDetail).toHaveBeenCalledWith('kb','chunk',1)
    expect(wrapper.find('[role="dialog"]').text()).toContain('设计说明.pdf')
    expect(wrapper.find('[role="dialog"]').text()).toContain('第 5 页')
    expect(wrapper.find('[role="dialog"] mark').text()).toBe('原文证据')
    expect(wrapper.find('[role="dialog"]').text()).toContain('前一段上下文')
    expect(wrapper.find('[role="dialog"]').text()).toContain('后一段上下文')
    wrapper.unmount()
    const restored=renderGraph();await flushPromises()
    expect(restored.find('.inspector').text()).toContain('关系证据')
    expect(restored.find('.inspector').text()).toContain('原文证据')
  })
})

describe('knowledge-base graph principles',()=>{
  it('loads the whole knowledge-base graph explicitly and reports its bounded scope',async()=>{
    const wrapper=renderGraph();await flushPromises()
    expect(api.previewGraphFilter).toHaveBeenCalledWith('kb',{principles:[],max_nodes:200,max_edges:400},expect.any(AbortSignal))
    expect(wrapper.text()).toContain('知识库总图谱');expect(wrapper.text()).toContain('匹配 8 / 8 个实体')
    expect(wrapper.text()).toContain('结果已截断');expect(wrapper.find('.pending-principles').exists()).toBe(false)
    expect(wrapper.text()).not.toMatch(/五类图谱|专业视图|行业标准图谱/)
    expect(api.listGraphProfiles).toHaveBeenCalledWith('kb')
  })
  it('sends enabled principles only and marks edits as unapplied',async()=>{
    const wrapper=renderGraph();await flushPromises()
    await click(wrapper,'添加原则');await wrapper.find('[aria-label="过滤原则 1"]').setValue('  只看订单服务  ')
    await click(wrapper,'添加原则');await wrapper.find('[aria-label="过滤原则 2"]').setValue('隐藏文档类实体')
    await wrapper.find('[aria-label="启用原则 2"]').setValue(false)
    expect(wrapper.find('.pending-principles').text()).toContain('仍显示上次成功')
    vi.mocked(api.previewGraphFilter).mockResolvedValueOnce(preview('只保留订单服务',true))
    await wrapper.find('.graph-principles form').trigger('submit');await flushPromises()
    expect(api.previewGraphFilter).toHaveBeenLastCalledWith('kb',expect.objectContaining({principles:['只看订单服务']}),expect.any(AbortSignal))
    expect(wrapper.find('.filter-understanding').text()).toContain('只保留订单服务')
    expect(wrapper.find('.pending-principles').exists()).toBe(false)
    await wrapper.find('[aria-label="删除原则 1"]').trigger('click')
    await wrapper.find('[aria-label="启用原则 1"]').setValue(true)
    expect(wrapper.find('.pending-principles').exists()).toBe(true)
  })
  it('keeps the previous graph and structured explanations after rejected principles, then explicitly clears',async()=>{
    const wrapper=renderGraph();await flushPromises()
    await click(wrapper,'添加原则');await wrapper.find('[aria-label="过滤原则 1"]').setValue('模糊不清的原则')
    vi.mocked(api.previewGraphFilter).mockRejectedValueOnce(Object.assign(new Error('原则无法应用'),{details:{summary:'请明确筛选范围',unapplied_principles:[{index:0,principle:'模糊不清的原则',reason:'没有对应的实体或关系'}]}}))
    await wrapper.find('.graph-principles form').trigger('submit');await flushPromises()
    expect(wrapper.find('.filter-understanding').text()).toContain('知识库完整有效图谱')
    expect(wrapper.find('.pending-principles').text()).toContain('尚未应用')
    expect(wrapper.find('.filter-failure').text()).toContain('没有对应的实体或关系')
    expect(api.previewGraphFilter).toHaveBeenCalledTimes(2)
    await click(wrapper,'清除原则，显示全库')
    expect(api.previewGraphFilter).toHaveBeenLastCalledWith('kb',expect.objectContaining({principles:[]}),expect.any(AbortSignal))
    expect(wrapper.find('.pending-principles').exists()).toBe(false)
  })
  it('allows a viewer to persist and reapply only personal principles, without saving a compiled plan',async()=>{
    const saved={id:'view',name:'订单视图',principles:['只看订单服务'],created_at:'',updated_at:'',schema_fingerprint:'schema'}
    vi.mocked(api.listGraphFilters).mockResolvedValue([saved])
    vi.mocked(api.saveGraphFilter).mockResolvedValue(saved);vi.mocked(api.previewSavedGraphFilter).mockResolvedValue(preview('已按当前资料重新理解',true))
    const wrapper=renderGraph(false);await flushPromises()
    expect(wrapper.find('.corrections-card').exists()).toBe(false)
    await click(wrapper,'添加原则');await wrapper.find('[aria-label="过滤原则 1"]').setValue('只看订单服务')
    await wrapper.find('[aria-label="个人视图名称"]').setValue('订单视图');await click(wrapper,'保存当前原则')
    expect(api.saveGraphFilter).toHaveBeenCalledWith('kb',{name:'订单视图',principles:['只看订单服务']})
    await wrapper.find('[aria-label="已保存的个人视图"]').setValue('view');await click(wrapper,'应用个人视图')
    expect(api.previewSavedGraphFilter).toHaveBeenCalledWith('kb','view',{max_nodes:200,max_edges:400},expect.any(AbortSignal))
    expect(wrapper.text()).toContain('已按当前资料重新理解')
    await click(wrapper,'删除个人视图');expect(api.deleteGraphFilter).toHaveBeenCalledWith('kb','view')
    expect(wrapper.findAll('[aria-label="已保存的个人视图"] option')).toHaveLength(1)
  })
  it('never broadens entity expansion beyond the last applied filter',async()=>{
    vi.mocked(api.searchGraphEntities).mockResolvedValue([{id:'a',canonical_name:'实体a',entity_type:'SYSTEM',description:'',aliases:[]},{id:'outside',canonical_name:'范围外实体',entity_type:'SYSTEM',description:'',aliases:[]}])
    vi.mocked(api.getEntitySubgraph).mockResolvedValue({seed_entity_ids:['a'],nodes:[node('a'),node('b'),node('outside')],edges:[edge('ab','a','b'),edge('hidden','a','outside')],offset:0,next_offset:null})
    const wrapper=renderGraph();await flushPromises()
    await wrapper.find('.graph-toolbar input').setValue('实体');await wrapper.find('.graph-toolbar form').trigger('submit');await flushPromises()
    expect(wrapper.findAll('.entity-result')).toHaveLength(1)
    await click(wrapper,'从此实体展开子图')
    const rendered=vi.mocked(cytoscape).mock.calls.at(-1)![0] as {elements:Array<{data:{id:string}}>}
    expect(rendered.elements.map(item=>item.data.id)).toEqual(['a','b','ab'])
  })
  it('refreshes corrections with the last applied rules instead of silently applying draft edits',async()=>{
    const wrapper=renderGraph(true);await flushPromises()
    await click(wrapper,'添加原则');await wrapper.find('[aria-label="过滤原则 1"]').setValue('尚未应用的新原则')
    await click(wrapper,'检查冲突')
    expect(api.previewGraphFilter).toHaveBeenLastCalledWith('kb',{principles:[],max_nodes:200,max_edges:400},expect.any(AbortSignal))
    expect(wrapper.find('.pending-principles').exists()).toBe(true)
    expect(wrapper.find('[aria-label="过滤原则 1"]').element).toHaveProperty('value','尚未应用的新原则')
  })
  it('discards a late filter response from the previous knowledge base',async()=>{
    const wrapper=renderGraph();await flushPromises()
    let finish!:(value:GraphFilterPreview)=>void
    vi.mocked(api.previewGraphFilter).mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve}))
    await click(wrapper,'添加原则');await wrapper.find('[aria-label="过滤原则 1"]').setValue('旧知识库原则')
    await wrapper.find('.graph-principles form').trigger('submit');await flushPromises()
    vi.mocked(api.previewGraphFilter).mockResolvedValue(preview('新知识库图谱',true))
    await wrapper.setProps({knowledgeBaseId:'new-kb'});await flushPromises()
    finish(preview('旧请求结果'));await flushPromises()
    expect(wrapper.find('.filter-understanding').text()).toContain('新知识库图谱')
    expect(wrapper.text()).not.toContain('旧请求结果')
  })
  it('keeps the loaded document title and graph when switching to another document fails',async()=>{
    vi.mocked(api.listDocuments).mockResolvedValue([{id:'first',original_filename:'已加载文档.md',status:'ready'},{id:'second',original_filename:'请求失败文档.md',status:'ready'}] as never)
    vi.mocked(api.listDocumentChunks).mockResolvedValue([])
    vi.mocked(api.getDocumentSubgraph).mockResolvedValueOnce({document_id:'first',nodes:[node('a')],edges:[]}).mockRejectedValueOnce(new Error('文档读取失败'))
    const wrapper=renderGraph();await flushPromises()
    await wrapper.find('.graph-toolbar select').setValue('first');await flushPromises()
    expect(wrapper.find('.canvas-heading strong').text()).toBe('已加载文档.md · 当前图谱范围')
    await wrapper.find('.graph-toolbar select').setValue('second');await flushPromises()
    expect(wrapper.find('.graph-toolbar select').element).toHaveProperty('value','second')
    expect(wrapper.find('.canvas-heading strong').text()).toBe('已加载文档.md · 当前图谱范围')
    expect(wrapper.find('.error').text()).toBe('文档读取失败')
    const rendered=vi.mocked(cytoscape).mock.calls.at(-1)![0] as {elements:Array<{data:{id:string}}>}
    expect(rendered.elements.map(item=>item.data.id)).toEqual(['a'])
  })
  it('uses general catalog and active Schema labels while keeping unknown types unchanged',async()=>{
    vi.mocked(api.listGraphProfiles).mockResolvedValue([{key:'general',entity_types:[{name:'SERVICE',label:'服务',color:'#000'}],relation_types:[{name:'CUSTOM_LINK',label:'目录关系'}]}] as GraphProfile[])
    vi.mocked(api.listGraphSchemas).mockResolvedValue([{graph_kind:'general',status:'active',generation_summary_json:{
      entity_type_details:[{name:'SERVICE',label:'业务服务',description:'知识库中提供业务能力的服务。'},{name:'CUSTOM_KIND',description:'自定义领域对象'}],
      relation_type_details:[{name:'CUSTOM_LINK',label:'支持',description:'服务提供该对象所需的能力。'}],
    }}] as GraphSchema[])
    const graph=preview();graph.nodes=[{...node('a'),entity_type:'SERVICE'},{...node('b'),entity_type:'CUSTOM_KIND'},{...node('c'),entity_type:'UNDEFINED_KIND'}];graph.edges=[{...edge('ab','a','b'),predicate:'CUSTOM_LINK'}]
    vi.mocked(api.previewGraphFilter).mockResolvedValue(graph)
    const wrapper=renderGraph();await flushPromises()
    expect(wrapper.find('.type-legend').text()).toContain('业务服务');expect(wrapper.find('.type-legend').text()).toContain('自定义领域对象');expect(wrapper.find('.type-legend').text()).toContain('UNDEFINED_KIND')
    expect(wrapper.find('.type-legend span[title="知识库中提供业务能力的服务。"]').exists()).toBe(true)
    expect(wrapper.find('.view-toolbar').text()).toContain('支持')
    const rendered=vi.mocked(cytoscape).mock.calls.at(-1)![0] as {elements:Array<{data:{id:string;label:string}}>}
    expect(rendered.elements.find(item=>item.data.id==='ab')?.data.label).toBe('支持')
    expect(wrapper.find('.graph-catalog').exists()).toBe(false)
  })
  it('keeps readable built-in labels if optional catalog or Schema lookup fails',async()=>{
    vi.mocked(api.listGraphProfiles).mockRejectedValue(new Error('目录暂不可用'));vi.mocked(api.listGraphSchemas).mockRejectedValue(new Error('Schema 暂不可用'))
    const graph=preview();graph.nodes=[{...node('a'),entity_type:'PERSON'},node('b')];graph.edges=[{...edge('ab','a','b'),predicate:'WORKS_FOR'}]
    vi.mocked(api.previewGraphFilter).mockResolvedValue(graph)
    const wrapper=renderGraph();await flushPromises()
    expect(wrapper.find('.type-legend').text()).toContain('人物');expect(wrapper.find('.type-legend').text()).toContain('系统')
    expect(wrapper.find('.view-toolbar').text()).toContain('任职于')
    expect(wrapper.find('.filter-understanding').exists()).toBe(true)
    expect(wrapper.find('.error').exists()).toBe(false)
  })
})

it('keeps one global Schema with drafts and always creates general versions',async()=>{
  const schema={id:'active',graph_kind:'general',version:1,status:'active',entity_types_json:['SYSTEM'],relation_types_json:['DEPENDS_ON'],generation_method:'manual',generation_summary_json:{},source_document_ids_json:[],extraction_prompt:null} as GraphSchema
  vi.mocked(api.listGraphSchemas).mockResolvedValue([schema]);vi.mocked(api.getGraphQuality).mockResolvedValue(null as never)
  vi.mocked(api.getGraphAutomation).mockResolvedValue({auto_build_enabled:true,auto_schema_generation_enabled:true})
  for(const fn of [api.listGraphReviews,api.listEntityMerges,api.listRelationMerges,api.listFolders,api.listTags])vi.mocked(fn).mockResolvedValue([] as never)
  for(const fn of [api.getEntityVectorState,api.getRelationVectorState,api.getGraphCommunityState])vi.mocked(fn).mockResolvedValue(null as never)
  vi.mocked(api.getDocumentGraphCoverage).mockResolvedValue({summary:{documents:0,chunks:0,processed_chunks:0,fact_chunks:0,processing_rate:0,fact_coverage_rate:0},documents:[]})
  vi.mocked(api.getDocumentTagMap).mockResolvedValue({})
  const wrapper=mount(GraphManagementView,{props:{knowledgeBaseId:'kb'}});await flushPromises()
  await click(wrapper,'Schema 管理')
  expect(wrapper.text()).toContain('全库只有一个活动 Schema')
  expect(wrapper.find('.profile-schema-builder').exists()).toBe(false)
  expect(wrapper.find('.schema-layout select').exists()).toBe(false)
  await wrapper.find('.schema-layout form').trigger('submit');await flushPromises()
  expect(api.createGraphSchema).toHaveBeenCalledWith('kb',expect.objectContaining({graph_kind:'general'}))
  expect(vi.mocked(api.createGraphSchema).mock.calls[0][1]).not.toHaveProperty('graph_profiles')
})
