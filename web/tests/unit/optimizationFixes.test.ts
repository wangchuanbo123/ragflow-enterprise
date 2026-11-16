import { computed } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import MessageList from '../../src/components/MessageList.vue'
import DocumentsView from '../../src/components/DocumentsView.vue'
import OnboardingWizard from '../../src/components/OnboardingWizard.vue'
import JobsView from '../../src/components/JobsView.vue'
import { knowledgeBaseKey } from '../../src/composables/usePermissions'
import { navigate, navigationState } from '../../src/composables/useNavigation'
import { useAuth } from '../../src/composables/useAuth'
import { documentUploadError, pageLabel } from '../../src/documentPresentation'
import * as api from '../../src/api/client'

vi.mock('../../src/api/client')
const doc={id:'doc',knowledge_base_id:'kb',original_filename:'manual.pdf',source_path:'manual.pdf',status:'ready',chunk_count:520,current_version_no:1,file_size:50,created_at:'',updated_at:''}
const composer={messages:[],loading:false,sending:false,knowledgeBaseId:'kb',knowledgeBases:[{id:'kb',name:'当前库'}],documents:[doc],folders:[],tags:[],submitMessage:vi.fn().mockResolvedValue(true)}
const renderDocuments=()=>mount(DocumentsView,{props:{knowledgeBaseId:'kb'},global:{provide:{[knowledgeBaseKey as symbol]:computed(()=>({capabilities:{can_edit:true}}))}}})

beforeEach(()=>{
  vi.clearAllMocks();window.sessionStorage.clear();useAuth().user.value={id:'reviewer',username:'reviewer',display_name:'Reviewer',role:'user',is_active:true}
  navigate({page:'documents',kb:'kb',document:'',chunk:''},true)
  vi.mocked(api.listDocumentPage).mockResolvedValue({items:[doc],total:1,offset:0,limit:50} as never)
  vi.mocked(api.listDocuments).mockResolvedValue([])
  vi.mocked(api.listTags).mockResolvedValue([]);vi.mocked(api.listFolders).mockResolvedValue([])
  vi.mocked(api.listDocumentChunks).mockResolvedValue([]);vi.mocked(api.listDocumentVersions).mockResolvedValue([])
  vi.mocked(api.getDocumentTags).mockResolvedValue([]);vi.mocked(api.getDocumentSubgraph).mockResolvedValue({document_id:'doc',nodes:[],edges:[]})
  vi.mocked(api.getGraphAutomation).mockResolvedValue({auto_build_enabled:false,auto_schema_generation_enabled:false} as never)
  vi.mocked(api.rebuildKnowledgeBaseIndex).mockResolvedValue({job_id:'shadow-job',status:'accepted'})
  vi.mocked(api.listIndexJobPage).mockResolvedValue({items:[],total:0,offset:0,limit:50})
})

describe('whole knowledge-base index recovery',()=>{
  const rebuildButton=(wrapper:ReturnType<typeof renderDocuments>)=>wrapper.findAll('.actions button').find(button=>button.text()==='重建全库索引')!
  it('offers the recovery action only to editors and owners',async()=>{
    const editor=renderDocuments();await flushPromises()
    expect(rebuildButton(editor).exists()).toBe(true)
    const viewer=mount(DocumentsView,{props:{knowledgeBaseId:'kb'},global:{provide:{[knowledgeBaseKey as symbol]:computed(()=>({capabilities:{can_edit:false}}))}}})
    await flushPromises()
    expect(viewer.text()).not.toContain('重建全库索引')
  })
  it('requires confirmation before creating a shadow rebuild task',async()=>{
    const confirm=vi.spyOn(window,'confirm').mockReturnValue(false)
    const wrapper=renderDocuments();await flushPromises();await rebuildButton(wrapper).trigger('click');await flushPromises()
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('从已保存的原始文件重新解析'))
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('构建影子索引，全部成功后再切换'))
    expect(api.rebuildKnowledgeBaseIndex).not.toHaveBeenCalled()
  })
  it('submits one genuine full-library rebuild and links to the exact task',async()=>{
    vi.spyOn(window,'confirm').mockReturnValue(true)
    const wrapper=renderDocuments();await flushPromises();await rebuildButton(wrapper).trigger('click');await flushPromises()
    expect(api.rebuildKnowledgeBaseIndex).toHaveBeenCalledExactlyOnceWith('kb')
    expect(api.reindexDocument).not.toHaveBeenCalled();expect(api.batchDocuments).not.toHaveBeenCalled()
    expect(wrapper.find('.rebuild-task').text()).toContain('shadow-job')
    await wrapper.find('.rebuild-task button').trigger('click')
    expect(navigationState.value).toMatchObject({page:'jobs',kb:'kb',query:'shadow-job'})
    const jobs=mount(JobsView,{props:{knowledgeBaseId:'kb'}});await flushPromises()
    expect(jobs.find('[aria-label="搜索任务"]').element.value).toBe('shadow-job')
    expect(api.listIndexJobPage).toHaveBeenLastCalledWith('kb',expect.objectContaining({query:'shadow-job'}))
  })
  it('reports backend rejection without showing a task that was never created',async()=>{
    vi.spyOn(window,'confirm').mockReturnValue(true)
    vi.mocked(api.rebuildKnowledgeBaseIndex).mockRejectedValueOnce(new Error('仍有索引任务运行，请完成后重试'))
    const wrapper=renderDocuments();await flushPromises();await rebuildButton(wrapper).trigger('click');await flushPromises()
    expect(wrapper.find('.error-banner').text()).toContain('仍有索引任务运行')
    expect(wrapper.find('.rebuild-task').exists()).toBe(false)
    expect(rebuildButton(wrapper).attributes('disabled')).toBeUndefined()
  })
  it('does not display a previous knowledge-base response after switching away and back',async()=>{
    vi.spyOn(window,'confirm').mockReturnValue(true)
    let resolve!:(value:{job_id:string;status:'accepted'})=>void
    vi.mocked(api.rebuildKnowledgeBaseIndex).mockImplementationOnce(()=>new Promise(done=>{resolve=done}))
    const wrapper=renderDocuments();await flushPromises();await rebuildButton(wrapper).trigger('click')
    await wrapper.setProps({knowledgeBaseId:'other'});await wrapper.setProps({knowledgeBaseId:'kb'});await flushPromises()
    resolve({job_id:'stale-job',status:'accepted'});await flushPromises()
    expect(wrapper.find('.rebuild-task').exists()).toBe(false)
  })
  it('does not import another knowledge-base or search page query into the task center',async()=>{
    navigate({page:'search',kb:'kb',query:'private-search'},true)
    const jobs=mount(JobsView,{props:{knowledgeBaseId:'kb'}});await flushPromises()
    expect(jobs.find('[aria-label="搜索任务"]').element.value).toBe('')
    navigate({page:'jobs',kb:'other',query:'other-job'},true);await flushPromises()
    expect(jobs.find('[aria-label="搜索任务"]').element.value).toBe('')
  })
})

describe('explicit scope boundaries',()=>{
  it.each(['document','folder','tag'])('blocks buttons, Enter and suggestions with incomplete %s scope',async(kind)=>{
    const submit=vi.fn()
    const wrapper=mount(MessageList,{props:{...composer,submitMessage:submit,messages:[{id:'answer',role:'assistant',content:'完成',status:'completed',created_at:''}]} as never})
    await wrapper.find('.scope-bar select').setValue(kind)
    await wrapper.find('textarea').setValue('仅查询选定范围')
    expect(wrapper.find('.send-btn').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[role="alert"]').text()).toContain('请先选择')
    await wrapper.find('textarea').trigger('keydown',{key:'Enter'})
    await wrapper.find('.suggestions button').trigger('click')
    expect(submit).not.toHaveBeenCalled()
    expect(wrapper.find('textarea').element.value).toBe('仅查询选定范围')
  })
  it('sends the explicit selection kind with the selected document',async()=>{
    const submit=vi.fn().mockResolvedValue(true)
    const wrapper=mount(MessageList,{props:{...composer,submitMessage:submit} as never})
    await wrapper.find('.scope-bar select').setValue('document')
    await wrapper.findAll('.scope-bar select')[1].setValue('doc')
    await wrapper.find('textarea').setValue('查询');await wrapper.find('.send-btn').trigger('click')
    expect(submit).toHaveBeenCalledWith(expect.objectContaining({scope:{selection_kind:'document',knowledge_base_ids:['kb'],document_ids:['doc']}}))
  })
})

describe('upload feedback and validation',()=>{
  it('reports all server-rejected files without hiding the next upload action',async()=>{
    vi.mocked(api.uploadDocuments).mockResolvedValue({job_id:null,document_ids:[],accepted:0,rejected:[{filename:'bad.pdf',error:'PDF无法解析'}],status:'partial'})
    const wrapper=mount(OnboardingWizard,{props:{knowledgeBaseId:'kb',knowledgeBaseName:'测试库'}});await flushPromises()
    const input=wrapper.find('input[type="file"]');Object.defineProperty(input.element,'files',{value:[new File(['bad'],'bad.pdf')]})
    await input.trigger('change');await flushPromises()
    expect(wrapper.find('.upload-results').text()).toContain('已提交 0 个文档')
    expect(wrapper.text()).toContain('bad.pdf：PDF无法解析')
    expect(wrapper.find('input[type="file"]').exists()).toBe(true)
    expect(api.updateGraphAutomation).toHaveBeenCalledWith('kb',false,false)
  })
  it('shows partial rejection and a concrete task entry',async()=>{
    vi.mocked(api.uploadDocuments).mockResolvedValue({job_id:'job',document_ids:['doc'],accepted:1,rejected:[{filename:'bad.pdf',error:'文件损坏'}],status:'partial'})
    const wrapper=mount(OnboardingWizard,{props:{knowledgeBaseId:'kb',knowledgeBaseName:'测试库'}});await flushPromises()
    const input=wrapper.find('input[type="file"]');Object.defineProperty(input.element,'files',{value:[new File(['ok'],'ok.pdf'),new File(['bad'],'bad.pdf')]})
    await input.trigger('change');await flushPromises()
    expect(wrapper.text()).toContain('已提交 1 个文档')
    await wrapper.find('.upload-results button').trigger('click')
    expect(wrapper.emitted('navigate')).toEqual([['jobs']])
  })
  it('rejects screenshots locally and uploads only supported files from a mixed batch',async()=>{
    vi.mocked(api.uploadDocumentsWithProgress).mockResolvedValue({job_id:'job',document_ids:['doc'],accepted:1,rejected:[],status:'accepted'})
    const wrapper=renderDocuments();await flushPromises()
    const image=new File(['png'],'screenshot.png'),pdf=new File(['pdf'],'manual.pdf')
    const input=wrapper.find('input[type="file"]');Object.defineProperty(input.element,'files',{value:[image,pdf]})
    await input.trigger('change');await flushPromises()
    expect(api.uploadDocumentsWithProgress).toHaveBeenCalledWith('kb',[pdf],expect.any(Function),expect.any(AbortSignal))
    expect(wrapper.find('.upload-error').text()).toContain('截图请先转为 PDF')
    expect(wrapper.findAll('.upload-status').map(item=>item.text())).toEqual(['失败','已提交'])
    expect(documentUploadError(new File([],'empty.md'))).toContain('为空')
  })
})

describe('long-document evidence navigation',()=>{
  it('shows parsing diagnostics and submits recommended OCR pages using internal zero-based coordinates',async()=>{
    vi.mocked(api.listDocumentPage).mockResolvedValue({items:[{...doc,metadata_json:{parse_summary:{page_count:4,ocr_recommended_pages:[0,2],ocr_pages:[1],table_pages:[3],native_text_characters:140,preview:'解析出的正文预览'}}}],total:1,offset:0,limit:50} as never)
    const wrapper=renderDocuments();await flushPromises();await wrapper.find('tbody tr').trigger('click');await flushPromises()
    expect(wrapper.find('.parse-diagnostics').text()).toContain('第 1、3 页可能包含扫描正文')
    expect(wrapper.find('.parse-diagnostics').text()).toContain('已 OCR：第 2 页')
    expect(wrapper.find('.parse-preview').text()).toBe('解析出的正文预览')
    await wrapper.findAll('.ocr-actions button')[0].trigger('click');await flushPromises()
    expect(api.reindexDocument).toHaveBeenCalledWith('kb','doc',{force_ocr:false,ocr_pages:[0,1,2]})
    await wrapper.findAll('.ocr-actions button')[1].trigger('click');await flushPromises()
    expect(api.reindexDocument).toHaveBeenLastCalledWith('kb','doc',{force_ocr:true,ocr_pages:null})
  })
  it('explains the local OCR page limit before allowing an oversized force request',async()=>{
    vi.mocked(api.listDocumentPage).mockResolvedValue({items:[{...doc,metadata_json:{parse_summary:{page_count:80,ocr_recommended_pages:[],preview:'正文'}}}],total:1,offset:0,limit:50} as never)
    const wrapper=renderDocuments();await flushPromises();await wrapper.find('tbody tr').trigger('click');await flushPromises()
    expect(wrapper.find('.ocr-actions button').attributes('disabled')).toBeDefined()
    expect(wrapper.find('.ocr-actions').text()).toContain('每文档最多处理 40 页')
  })
  it('loads later chunk pages instead of stopping at the first 200 chunks',async()=>{
    const wrapper=renderDocuments();await flushPromises();await wrapper.find('tbody tr').trigger('click');await flushPromises()
    expect(wrapper.find('.chunk-pagination').text()).toContain('共 520 个片段')
    await wrapper.find('[aria-label="片段页码"]').setValue(8)
    await wrapper.findAll('.chunk-pagination button').find(button=>button.text()==='跳转')!.trigger('click');await flushPromises()
    expect(api.listDocumentChunks).toHaveBeenLastCalledWith('kb','doc',{offset:350,limit:50})
  })
  it('opens a linked citation beyond chunk 200 and highlights its original content',async()=>{
    vi.mocked(api.getChunkDetail).mockResolvedValue({chunk_id:'late',document_id:'doc',chunk_index:417,page:7,content:'后半部引用原文',previous:[],next:[],images:[]} as never)
    navigate({page:'documents',kb:'kb',document:'doc',chunk:'late'},true)
    const wrapper=renderDocuments();await flushPromises()
    expect(api.getChunkDetail).toHaveBeenCalledTimes(1)
    expect(api.listDocumentChunks).toHaveBeenLastCalledWith('kb','doc',{offset:400,limit:50})
    expect(wrapper.find('.linked-chunk').text()).toContain('后半部引用原文')
    expect(wrapper.find('.linked-chunk').text()).toContain('第 8 页')
  })
})

describe('page labels and answer quality',()=>{
  it('displays zero-based PDF source pages consistently as one-based page labels',()=>{
    expect(pageLabel(0)).toBe('第 1 页');expect(pageLabel('9')).toBe('第 10 页');expect(pageLabel(null)).toBe('')
    const wrapper=mount(MessageList,{props:{...composer,messages:[{id:'a',role:'assistant',content:'来源',status:'completed',created_at:'',sources:[{source:'manual.pdf',preview:'原文',page:0}]}]} as never})
    expect(wrapper.find('.source-location').text()).toBe('第 1 页')
    expect(wrapper.text()).not.toContain('第 0 页')
  })
  it('distinguishes unavailable retrieval and invalid citation IDs from verified evidence',()=>{
    const route={confidence:0.5,reasons:[],selected_channels:['text'],evidence_status:'unavailable',citation_validation:{status:'invalid',invalid_ids:['来源99']},retrieval_diagnostics:[{channel:'bm25',status:'degraded',code:'capacity_exceeded',count:0,elapsed_ms:2,detail:'关键词索引超过容量'}]}
    const wrapper=mount(MessageList,{props:{...composer,messages:[{id:'a',role:'assistant',content:'回答',status:'completed',created_at:'',route}]} as never})
    expect(wrapper.text()).toContain('部分检索服务已降级')
    expect(wrapper.text()).toContain('检索服务不可用')
    expect(wrapper.text()).toContain('无效引用编号：来源99')
    expect(wrapper.text()).toContain('关键词索引超过容量')
    expect(wrapper.text()).not.toContain('引用编号已校验')
  })
})
