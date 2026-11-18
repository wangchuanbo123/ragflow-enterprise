import { computed } from 'vue'
import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import CodeGraphView from '../../src/components/CodeGraphView.vue'
import { knowledgeBaseKey } from '../../src/composables/usePermissions'
import { navigationState, parseNavigation } from '../../src/composables/useNavigation'
import type { CodeSubgraph, CodeSymbol } from '../../src/types'
import * as api from '../../src/api/client'

vi.mock('../../src/api/client')
const symbol = (id: string): CodeSymbol => ({ id, snapshot_id: 'snapshot', file_id: 'file', path: 'order.py', language: 'python', kind: 'function', name: id, qualified_name: `order.${id}`, signature: null, start_line: 1, end_line: 3, source_excerpt: `def ${id}(): pass`, functional_summary: null, metadata: {} })
const render = (edit = true) => mount(CodeGraphView, { props: { knowledgeBaseId: 'kb' }, global: { provide: { [knowledgeBaseKey as symbol]: computed(() => ({ capabilities: { can_edit: edit } })) } } })

beforeEach(() => {
  vi.resetAllMocks()
  navigationState.value = parseNavigation('#page=code-graph&kb=kb')
  vi.mocked(api.getCodeGraphStats).mockResolvedValue({ active_snapshot_id: 'snapshot', revision: 'main@123', files: 1, symbols: 2, relations: 1, resolved_relations: 1 })
  vi.mocked(api.searchCodeSymbols).mockResolvedValue([symbol('first'), symbol('second')])
  vi.mocked(api.getCodeSubgraph).mockResolvedValue({ nodes: [], edges: [] })
})

describe('code graph browsing', () => {
  it('opens code governance without loading management data', async () => {
    const wrapper = render(); await flushPromises()
    expect(api.listDataSources).not.toHaveBeenCalled()
    expect(api.listConnectorCredentials).not.toHaveBeenCalled()
    expect(api.listIndexJobs).not.toHaveBeenCalled()
    await wrapper.find('.manage-code').trigger('click')
    expect(navigationState.value).toMatchObject({ page: 'graph-management', kb: 'kb', graphKind: 'code' })
    expect(window.location.hash).toContain('graph=code')
  })

  it('keeps symbol search available to viewers and hides the governance entry', async () => {
    const wrapper = render(false); await flushPromises()
    expect(wrapper.find('.manage-code').exists()).toBe(false)
    await wrapper.find('input').setValue('first')
    await wrapper.find('form').trigger('submit'); await flushPromises()
    expect(api.searchCodeSymbols).toHaveBeenCalledWith('kb', 'first')
    expect(wrapper.findAll('.symbol')).toHaveLength(2)
  })

  it('ignores a late relation response after selecting another symbol', async () => {
    const wrapper = render(); await flushPromises()
    await wrapper.find('input').setValue('order')
    await wrapper.find('form').trigger('submit'); await flushPromises()
    let finish!: (result: CodeSubgraph) => void
    vi.mocked(api.getCodeSubgraph).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    await wrapper.findAll('.symbol')[0].trigger('click')
    await wrapper.findAll('.symbol')[1].trigger('click'); await flushPromises()
    finish({ nodes: [], edges: [{ id: 'old', source: 'first', source_name: '过时的调用', target: 'other', target_name: 'other', relation_type: 'CALLS', confidence: 1, resolution_status: 'resolved', path: 'old.py', start_line: 1, end_line: 1 }] })
    await flushPromises()
    expect(wrapper.find('.detail-card header').text()).toContain('order.second')
    expect(wrapper.find('.detail-card').text()).not.toContain('过时的调用')
  })

  it('discards late search results after switching knowledge bases', async () => {
    const wrapper = render(); await flushPromises()
    let finish!: (result: CodeSymbol[]) => void
    vi.mocked(api.searchCodeSymbols).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    await wrapper.find('input').setValue('old')
    await wrapper.find('form').trigger('submit')
    await wrapper.setProps({ knowledgeBaseId: 'other' })
    finish([symbol('old')]); await flushPromises()
    expect(wrapper.findAll('.symbol')).toHaveLength(0)
    expect(wrapper.find('input').element).toHaveProperty('value', '')
    expect(api.getCodeGraphStats).toHaveBeenLastCalledWith('other')
  })
})
