import { computed } from 'vue'
import { mount, flushPromises } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import CodeGraphView from '../../src/components/CodeGraphView.vue'
import { knowledgeBaseKey } from '../../src/composables/usePermissions'
import * as api from '../../src/api/client'
vi.mock('../../src/api/client',()=>({getCodeGraphStats:vi.fn(),listCodeSnapshots:vi.fn(),listDataSources:vi.fn(),listIndexJobs:vi.fn(),listConnectorCredentials:vi.fn(),requestCodeIndex:vi.fn()}))
describe('code index progress',()=>{
  it('starts tracking the first task immediately after submission',async()=>{
    vi.mocked(api.getCodeGraphStats).mockResolvedValue({files:0,symbols:0,relations:0,resolved_relations:0} as never)
    vi.mocked(api.listCodeSnapshots).mockResolvedValue([])
    vi.mocked(api.listDataSources).mockResolvedValue([{id:'source',name:'repo',source_type:'git'}] as never)
    vi.mocked(api.listIndexJobs).mockResolvedValue([])
    vi.mocked(api.listConnectorCredentials).mockResolvedValue([])
    vi.mocked(api.requestCodeIndex).mockResolvedValue({id:'first',job_type:'code_index',status:'pending',total_items:0,processed_items:0} as never)
    const wrapper=mount(CodeGraphView,{props:{knowledgeBaseId:'kb'},global:{provide:{[knowledgeBaseKey as symbol]:computed(()=>({capabilities:{can_edit:true}}))}}})
    await flushPromises();expect(wrapper.find('.build-progress').exists()).toBe(false)
    await wrapper.find('.toolbar-card button:not(.check)').trigger('click');await flushPromises()
    expect(api.requestCodeIndex).toHaveBeenCalledWith('kb','source')
    expect(wrapper.find('.build-progress').text()).toContain('等待中')
  })
})
