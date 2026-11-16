import { computed } from 'vue'
import { mount, flushPromises } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import AnswerContent from '../../src/components/AnswerContent.vue'
import MessageList from '../../src/components/MessageList.vue'
import JobsView from '../../src/components/JobsView.vue'
import { knowledgeBaseKey } from '../../src/composables/usePermissions'
import * as api from '../../src/api/client'
vi.mock('../../src/api/client', () => ({ listIndexJobPage: vi.fn(), cancelIndexJob: vi.fn() }))
const composerProps = { messages: [], loading: false, sending: false, knowledgeBaseId: 'kb', knowledgeBases: [], documents: [], folders: [], tags: [], submitMessage: vi.fn().mockResolvedValue(true) }

describe('answer rendering and actions', () => {
  it.each(['/* block comment */', '"""Python docstring"""', "'''Python docstring'''"] )('renders code safely: %s', content => {
    const wrapper = mount(AnswerContent, { props: { content: '```python\n'+content+'\n```' } })
    expect(wrapper.find('pre').text()).toBe(content)
  })
  it('preserves the draft when the first request fails', async () => {
    const wrapper = mount(MessageList, { props: { ...composerProps, submitMessage: vi.fn().mockResolvedValue(false) } })
    await wrapper.find('textarea').setValue('keep this question'); await wrapper.find('.send-btn').trigger('click'); await flushPromises()
    expect(wrapper.find('textarea').element.value).toBe('keep this question')
  })
  it('does not submit while the user is composing Chinese text', async () => {
    const submit = vi.fn(); const wrapper = mount(MessageList, { props: { ...composerProps, submitMessage: submit } })
    await wrapper.find('textarea').setValue('中文'); await wrapper.find('textarea').trigger('keydown', { key: 'Enter', isComposing: true })
    expect(submit).not.toHaveBeenCalled()
  })
  it('resolves citations only within the answer that was clicked', async () => {
    const source = { citation_id: '[来源1]', source: 'doc', preview: 'evidence' }
    const messages = ['a','b'].map(id=>({ id, role:'assistant', content:'见[来源1]', status:'completed', sources:[source], created_at:'' }))
    const wrapper = mount(MessageList, { attachTo: document.body, props: { ...composerProps, messages } })
    await wrapper.findAll('.inline.citation')[1].trigger('click'); await flushPromises()
    expect(wrapper.find('[data-source-key="1-0"]').classes()).toContain('flash')
    expect(wrapper.find('[data-source-key="0-0"]').classes()).not.toContain('flash')
  })
  it('shows pending jobs in the queue filter and allows editors to cancel', async () => {
    vi.mocked(api.listIndexJobPage).mockResolvedValue({items:[{ id:'pending',job_type:'code_index',status:'pending',total_items:0,processed_items:0,created_at:'' }],total:1,offset:0,limit:50} as never)
    const wrapper = mount(JobsView, {props:{knowledgeBaseId:'kb'}, global: { provide: { [knowledgeBaseKey as symbol]: computed(()=>({ capabilities:{can_edit:true} })) } } })
    await flushPromises(); await wrapper.find('select').setValue('pending')
    expect(wrapper.text()).toContain('代码分析'); expect(wrapper.find('.job-actions').text()).toContain('取消')
  })
})
