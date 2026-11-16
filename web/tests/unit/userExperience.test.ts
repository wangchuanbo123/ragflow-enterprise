import { computed } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import DocumentsView from '../../src/components/DocumentsView.vue'
import MessageList from '../../src/components/MessageList.vue'
import { knowledgeBaseKey } from '../../src/composables/usePermissions'
import { useAuth } from '../../src/composables/useAuth'
import { navigationState,parseNavigation } from '../../src/composables/useNavigation'
import * as api from '../../src/api/client'
import type { KnowledgeDocument,PageResult } from '../../src/types'

vi.mock('../../src/api/client')
const user=(id:string)=>({id,username:id,display_name:id,role:'user',is_active:true})
const record=(status='processing'):KnowledgeDocument=>({id:'doc',knowledge_base_id:'kb',original_filename:'设计说明.pdf',source_path:'/documents/doc.pdf',file_hash:'hash',file_size:100,folder_id:null,status,chunk_count:status==='ready'?3:0,current_version_no:1,metadata_json:null,embedding_provider:'test',embedding_model:'test',error_message:null,created_at:'',updated_at:'',indexed_at:null,processing:{job_id:'job',stage:'vector',status:status==='ready'?'completed':'running',message:status==='ready'?'索引已完成':'生成向量中',waiting_reason:null,updated_at:'',stages:[{name:'parse',status:'completed'},{name:'chunk',status:'completed'},{name:'vector',status:status==='ready'?'completed':'running'}]}})
const page=(document=record(),total=1):PageResult<KnowledgeDocument>=>({items:[document],total,offset:0,limit:50})
const composerProps={messages:[],loading:false,sending:false,conversationId:'conv-a',knowledgeBaseId:'kb',knowledgeBases:[{id:'kb',name:'当前库'},{id:'other',name:'其他库'}],documents:[],folders:[],tags:[],submitMessage:vi.fn().mockResolvedValue(true)}
function documents(){return mount(DocumentsView,{props:{knowledgeBaseId:'kb'},global:{provide:{[knowledgeBaseKey as symbol]:computed(()=>({capabilities:{can_edit:true}}))}}})}

beforeEach(()=>{
  vi.clearAllMocks();window.sessionStorage.clear();useAuth().user.value=user('alice')
  navigationState.value=parseNavigation('#page=documents&kb=kb')
  vi.spyOn(document,'hidden','get').mockReturnValue(false)
  vi.mocked(api.listDocumentPage).mockResolvedValue(page())
  vi.mocked(api.listTags).mockResolvedValue([]);vi.mocked(api.listFolders).mockResolvedValue([])
  vi.mocked(api.listDocumentChunks).mockResolvedValue([]);vi.mocked(api.listDocumentVersions).mockResolvedValue([])
  vi.mocked(api.getDocumentSubgraph).mockResolvedValue({document_id:'doc',nodes:[],edges:[]})
  vi.mocked(api.getDocumentTags).mockResolvedValue([])
})
afterEach(()=>vi.useRealTimers())

describe('document progress and paging',()=>{
  it('refreshes selected details from real stages without hiding the existing table',async()=>{
    vi.useFakeTimers()
    const wrapper=documents();await flushPromises();await wrapper.find('tbody tr').trigger('click');await flushPromises()
    expect(wrapper.findAll('.pipeline .done')).toHaveLength(2)
    expect(wrapper.findAll('.pipeline .active')).toHaveLength(1)
    let finish!:(value:PageResult<KnowledgeDocument>)=>void
    vi.mocked(api.listDocumentPage).mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve}))
    await vi.advanceTimersByTimeAsync(3000)
    expect(wrapper.find('table').exists()).toBe(true)
    vi.mocked(api.listDocumentChunks).mockResolvedValue([{id:'new',chunk_index:0,content:'完成后的片段正文',token_count:5,page:1,section:null,graph_status:'pending',graph_schema_version:0}])
    finish(page(record('ready')));await flushPromises()
    expect(wrapper.findAll('.pipeline .active')).toHaveLength(0)
    expect(wrapper.findAll('.pipeline .done')).toHaveLength(3)
    expect(wrapper.find('aside').text()).toContain('索引已完成')
    expect(wrapper.find('aside').text()).toContain('完成后的片段正文')
    expect(wrapper.find('aside').text()).not.toContain('系统正在后台处理')
    expect(api.listFolders).toHaveBeenCalledTimes(1)
  })
  it('does not guess an active Schema stage when no stage was recorded',async()=>{
    vi.mocked(api.listDocumentPage).mockResolvedValue(page({...record('ready'),processing:null}))
    const wrapper=documents();await flushPromises();await wrapper.find('tbody tr').trigger('click');await flushPromises()
    expect(wrapper.findAll('.pipeline .active')).toHaveLength(0)
    expect(wrapper.find('aside').text()).toContain('尚无详细阶段记录')
  })
  it('sends filters and page offsets to the server, with distinct filtered empty state',async()=>{
    vi.useFakeTimers();vi.mocked(api.listDocumentPage).mockResolvedValue(page(record('ready'),120))
    const wrapper=documents();await flushPromises()
    await wrapper.findAll('.pagination button')[1].trigger('click');await flushPromises()
    expect(api.listDocumentPage).toHaveBeenLastCalledWith('kb',expect.objectContaining({offset:50,limit:50}))
    vi.mocked(api.listDocumentPage).mockResolvedValue({items:[],total:0,offset:0,limit:50})
    await wrapper.find('[placeholder="按文件名搜索"]').setValue('不存在')
    await vi.advanceTimersByTimeAsync(200);await flushPromises()
    expect(api.listDocumentPage).toHaveBeenLastCalledWith('kb',expect.objectContaining({query:'不存在',offset:0}))
    expect(wrapper.text()).toContain('没有匹配的文档')
  })
  it('reports batch transfer progress once and permits cancellation and retry',async()=>{
    let fail!:(error:Error)=>void
    vi.mocked(api.uploadDocumentsWithProgress).mockImplementation((_kb,_files,progress,signal)=>{
      progress?.({loaded:50,total:100})
      return new Promise((_resolve,reject)=>{fail=reject;signal?.addEventListener('abort',()=>reject(new DOMException('cancel','AbortError')))})
    })
    const wrapper=documents();await flushPromises()
    const input=wrapper.find('input[type="file"]')
    Object.defineProperty(input.element,'files',{value:[new File(['x'],'a.pdf'),new File(['y'],'b.pdf')]})
    await input.trigger('change');await flushPromises()
    expect(wrapper.find('.upload-header').text()).toContain('本批文件总传输进度')
    expect(wrapper.find('.upload-header').text()).toContain('50%')
    expect(wrapper.findAll('.upload-status').map(item=>item.text())).toEqual(['等待整批确认','等待整批确认'])
    await wrapper.findAll('button').find(button=>button.text()==='取消本次传输')!.trigger('click');await flushPromises()
    expect(wrapper.text()).toContain('已停止本次传输')
    expect(wrapper.findAll('button').some(button=>button.text().includes('重传失败或取消'))).toBe(true)
    fail?.(new Error('unused'))
  })
})

describe('composer drafts and scope isolation',()=>{
  it('defaults to the current knowledge base and remembers each conversation independently',async()=>{
    const wrapper=mount(MessageList,{props:composerProps as never})
    expect(wrapper.find('.scope-bar select').element.value).toBe('knowledge_base')
    await wrapper.find('textarea').setValue('会话A草稿')
    await wrapper.find('.scope-bar select').setValue('all')
    await wrapper.setProps({conversationId:'conv-b'})
    expect(wrapper.find('textarea').element.value).toBe('')
    expect(wrapper.find('.scope-bar select').element.value).toBe('knowledge_base')
    await wrapper.setProps({conversationId:'conv-a'})
    expect(wrapper.find('textarea').element.value).toBe('会话A草稿')
    expect(wrapper.find('.scope-bar select').element.value).toBe('all')
    wrapper.unmount()
    const remounted=mount(MessageList,{props:composerProps as never})
    expect(remounted.find('textarea').element.value).toBe('会话A草稿')
    useAuth().user.value=user('bob');await flushPromises()
    expect(remounted.find('textarea').element.value).toBe('')
    useAuth().user.value=user('alice');await flushPromises()
    expect(remounted.find('textarea').element.value).toBe('会话A草稿')
  })
  it('keeps a failed question but clears a successfully submitted draft from storage',async()=>{
    const submit=vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true)
    const wrapper=mount(MessageList,{props:{...composerProps,submitMessage:submit} as never})
    await wrapper.find('textarea').setValue('待提交的问题');await wrapper.find('.send-btn').trigger('click');await flushPromises()
    expect(wrapper.find('textarea').element.value).toBe('待提交的问题')
    await wrapper.find('.send-btn').trigger('click');await flushPromises();wrapper.unmount()
    expect(mount(MessageList,{props:composerProps as never}).find('textarea').element.value).toBe('')
  })
})
