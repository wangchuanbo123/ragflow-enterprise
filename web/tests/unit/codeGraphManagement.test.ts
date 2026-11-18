import { computed, defineComponent, h, KeepAlive, ref, type Ref } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import CodeGraphManagementView from '../../src/components/CodeGraphManagementView.vue'
import { knowledgeBaseKey } from '../../src/composables/usePermissions'
import * as api from '../../src/api/client'
import type { CodeGraphStats, CodeSnapshot, CodeSnapshotDiff, CodeVectorState, IndexJob } from '../../src/types'

vi.mock('../../src/api/client')

const snapshot = (id: string, source = 'source'): CodeSnapshot => ({
  id, data_source_id: source, revision: `revision-${id}`, parser_version: 'parser-v1',
  status: 'active', language_stats_json: null, error_message: null,
  created_at: '2026-11-18T00:00:00Z', activated_at: '2026-11-18T00:00:00Z',
})
const stats = (id = 'current'): CodeGraphStats => ({
  active_snapshot_id: id, revision: `revision-${id}`, files: 3, symbols: 8,
  relations: 5, resolved_relations: 4,
})
const vector = (id = 'current'): CodeVectorState => ({
  snapshot_id: id, index_name: 'code-index', embedding_fingerprint: 'model',
  symbol_fingerprint: 'symbols', symbol_count: 8, status: 'ready', updated_at: null,
})
const job = (type = 'code_index', status = 'pending'): IndexJob => ({
  id: `${type}-job`, job_type: type, status, total_items: 3, processed_items: status === 'completed' ? 3 : 0,
  succeeded_items: status === 'completed' ? 3 : 0, failed_items: 0, error_message: null,
  attempt_count: 0, max_attempts: 3, cancel_requested: false, created_at: '', started_at: null, finished_at: null,
})
const diff: CodeSnapshotDiff = {
  base_snapshot_id: 'old', target_snapshot_id: 'current', added_files: ['new-file.py'],
  modified_files: [], deleted_files: [], unchanged_files: 2,
}

function render(edit: boolean | Ref<boolean> = true) {
  return mount(CodeGraphManagementView, {
    props: { knowledgeBaseId: 'kb' },
    global: { provide: { [knowledgeBaseKey as symbol]: computed(() => ({ capabilities: { can_edit: typeof edit === 'boolean' ? edit : edit.value } })) } },
  })
}

async function click(wrapper: VueWrapper, label: string) {
  const button = wrapper.findAll('button').find(item => item.text() === label)
  expect(button, `button ${label}`).toBeDefined()
  await button!.trigger('click')
  await flushPromises()
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api.getCodeGraphStats).mockResolvedValue(stats())
  vi.mocked(api.listCodeSnapshots).mockResolvedValue([snapshot('current')])
  vi.mocked(api.getCodeVectorState).mockResolvedValue(vector())
  vi.mocked(api.listIndexJobs).mockResolvedValue([])
  vi.mocked(api.getIndexJob).mockResolvedValue(job('code_index', 'running'))
  vi.mocked(api.listDataSources).mockResolvedValue([{ id: 'source', name: '订单服务', source_type: 'git' }] as never)
  vi.mocked(api.listConnectorCredentials).mockResolvedValue([])
  vi.mocked(api.getCodeSnapshotIssues).mockResolvedValue([])
  vi.mocked(api.getCodeSnapshotDiff).mockResolvedValue(diff)
})

afterEach(() => vi.useRealTimers())

describe('code graph governance', () => {
  it('preserves a code-source draft and pauses polling while its KeepAlive view is inactive', async () => {
    vi.useFakeTimers()
    vi.mocked(api.listIndexJobs).mockResolvedValue([job('code_index', 'running')])
    const visible = ref(true)
    const DocumentPanel = defineComponent({ render: () => h('p', '文档图谱治理') })
    const Host = defineComponent({
      setup: () => () => h(KeepAlive, null, {
        default: () => visible.value
          ? h(CodeGraphManagementView, { knowledgeBaseId: 'kb' })
          : h(DocumentPanel),
      }),
    })
    const wrapper = mount(Host, {
      global: { provide: { [knowledgeBaseKey as symbol]: computed(() => ({ capabilities: { can_edit: true } })) } },
    })
    await flushPromises()
    await wrapper.find('[placeholder="例如：订单服务"]').setValue('尚未保存的订单服务')
    const readsBeforeHiding = vi.mocked(api.listIndexJobs).mock.calls.length

    visible.value = false
    await flushPromises()
    expect(wrapper.text()).toContain('文档图谱治理')
    await vi.advanceTimersByTimeAsync(10000)
    expect(api.listIndexJobs).toHaveBeenCalledTimes(readsBeforeHiding)

    visible.value = true
    await flushPromises()
    expect(api.listIndexJobs).toHaveBeenCalledTimes(readsBeforeHiding + 1)
    expect(wrapper.find('[placeholder="例如：订单服务"]').element).toHaveProperty('value', '尚未保存的订单服务')
    expect(api.listConnectorCredentials).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(2500)
    await flushPromises()
    expect(api.listIndexJobs).toHaveBeenCalledTimes(readsBeforeHiding + 2)
    wrapper.unmount()
  })

  it('tracks the first submitted build immediately and polls without rereading credentials', async () => {
    vi.useFakeTimers()
    vi.mocked(api.requestCodeIndex).mockResolvedValue(job())
    const wrapper = render()
    await flushPromises()
    expect(wrapper.find('.build-progress').exists()).toBe(false)

    await click(wrapper, '创建索引任务')
    expect(api.requestCodeIndex).toHaveBeenCalledWith('kb', 'source')
    expect(wrapper.find('.build-progress').text()).toMatch(/等待|排队/)
    vi.mocked(api.listIndexJobs).mockResolvedValue([job('code_index', 'running')])
    await vi.advanceTimersByTimeAsync(2500)
    await flushPromises()
    expect(api.listIndexJobs).toHaveBeenCalledTimes(2)
    expect(api.listConnectorCredentials).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('loads parsing issues with a single snapshot and no version comparison', async () => {
    vi.mocked(api.getCodeSnapshotIssues).mockResolvedValue([
      { path: 'broken.py', language: 'python', status: 'failed', errors: ['无法解析函数定义'] },
    ])
    const wrapper = render()
    await flushPromises()

    expect(api.getCodeSnapshotIssues).toHaveBeenCalledWith('kb', 'current')
    expect(wrapper.find('.issues').text()).toContain('broken.py')
    expect(wrapper.find('.issues').text()).toContain('无法解析函数定义')
    expect(api.getCodeSnapshotDiff).not.toHaveBeenCalled()
    const compare = wrapper.findAll('button').find(item => item.text() === '比较版本')!
    expect(compare.attributes('disabled')).toBeDefined()
  })

  it('limits comparison to one source and clears displayed or late differences after selection changes', async () => {
    vi.mocked(api.listCodeSnapshots).mockResolvedValue([
      snapshot('current'), snapshot('old'), snapshot('other', 'other-source'),
    ])
    const wrapper = render()
    await flushPromises()
    await wrapper.find('[aria-label="目标快照"]').setValue('current')
    const base = wrapper.find('[aria-label="基准快照"]')
    expect(base.findAll('option').map(option => option.attributes('value'))).not.toContain('other')
    await base.setValue('old')
    await click(wrapper, '比较版本')
    expect(api.getCodeSnapshotDiff).toHaveBeenCalledWith('kb', 'current', 'old')
    expect(wrapper.find('.diff-grid').exists()).toBe(true)

    let finish!: (value: CodeSnapshotDiff) => void
    vi.mocked(api.getCodeSnapshotDiff).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    await wrapper.findAll('button').find(item => item.text() === '比较版本')!.trigger('click')
    await wrapper.find('[aria-label="目标快照"]').setValue('other')
    finish(diff)
    await flushPromises()
    expect(wrapper.find('.diff-grid').exists()).toBe(false)
    expect(wrapper.find('[aria-label="基准快照"]').element).toHaveProperty('value', '')
  })

  it('rebuilds a stale vector index, tracks its task and refreshes state on completion', async () => {
    vi.useFakeTimers()
    vi.mocked(api.getCodeVectorState).mockResolvedValue(vector('previous'))
    vi.mocked(api.rebuildCodeVectors).mockResolvedValue(job('code_vector_rebuild'))
    const wrapper = render()
    await flushPromises()
    expect(wrapper.find('.vector-state').text()).toMatch(/落后|过期|不同步/)

    await click(wrapper, '重建代码索引')
    expect(api.rebuildCodeVectors).toHaveBeenCalledWith('kb')
    expect(wrapper.find('.vector-progress').text()).toMatch(/等待|排队/)
    vi.mocked(api.listIndexJobs).mockResolvedValue([job('code_vector_rebuild', 'completed')])
    vi.mocked(api.getCodeVectorState).mockResolvedValue(vector())
    await vi.advanceTimersByTimeAsync(2500)
    await flushPromises()

    expect(api.getCodeGraphStats).toHaveBeenCalledTimes(2)
    expect(api.getCodeVectorState).toHaveBeenCalledTimes(2)
    expect(wrapper.find('.vector-state').text()).not.toMatch(/落后|过期|不同步/)
    expect(api.getCodeSnapshotIssues).toHaveBeenCalledTimes(2)
    expect(api.listConnectorCredentials).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('keeps available graph and source information when the vector state cannot be read', async () => {
    vi.mocked(api.getCodeVectorState).mockRejectedValue(new Error('代码索引暂不可用'))
    const wrapper = render()
    await flushPromises()

    expect(wrapper.find('.error').text()).toContain('代码索引暂不可用')
    expect(wrapper.text()).toContain('revision-current')
    expect(wrapper.find('[aria-label="代码数据源"]').text()).toContain('订单服务')
    expect(api.getCodeSnapshotIssues).toHaveBeenCalledWith('kb', 'current')
  })

  it('does not expose write controls or read credentials without edit permission', async () => {
    const wrapper = render(false)
    await flushPromises()

    expect(api.listConnectorCredentials).not.toHaveBeenCalled()
    const labels = wrapper.findAll('button').map(button => button.text())
    expect(labels).not.toContain('创建索引任务')
    expect(labels).not.toContain('检查连接')
    expect(labels).not.toContain('重建代码索引')
    expect(wrapper.find('input[type="file"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('revision-current')
  })

  it('ignores issues from a previous knowledge base when an old request finishes late', async () => {
    let finish!: (value: Awaited<ReturnType<typeof api.getCodeSnapshotIssues>>) => void
    vi.mocked(api.getCodeSnapshotIssues).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const wrapper = render()
    await flushPromises()
    vi.mocked(api.getCodeGraphStats).mockResolvedValue(stats('new-current'))
    vi.mocked(api.listCodeSnapshots).mockResolvedValue([snapshot('new-current')])
    vi.mocked(api.getCodeVectorState).mockResolvedValue(vector('new-current'))
    vi.mocked(api.getCodeSnapshotIssues).mockResolvedValue([
      { path: 'new-kb.py', language: 'python', status: 'failed', errors: ['新的解析问题'] },
    ])
    await wrapper.setProps({ knowledgeBaseId: 'other-kb' })
    await flushPromises()
    finish([{ path: 'old-kb.py', language: 'python', status: 'failed', errors: ['旧问题'] }])
    await flushPromises()

    expect(api.getCodeSnapshotIssues).toHaveBeenLastCalledWith('other-kb', 'new-current')
    expect(wrapper.find('.issues').text()).toContain('new-kb.py')
    expect(wrapper.text()).not.toContain('old-kb.py')
  })

  it('does not schedule background work after unmount while its initial request is in flight', async () => {
    vi.useFakeTimers()
    let finish!: (value: CodeGraphStats) => void
    vi.mocked(api.getCodeGraphStats).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    vi.mocked(api.listIndexJobs).mockResolvedValue([job()])
    const wrapper = render()
    await flushPromises()
    wrapper.unmount()
    finish(stats())
    await flushPromises()
    await vi.advanceTimersByTimeAsync(5000)
    expect(api.listIndexJobs).toHaveBeenCalledTimes(1)
    expect(api.getCodeSnapshotIssues).not.toHaveBeenCalled()
  })

  it('waits for the current poll to finish before starting another one', async () => {
    vi.useFakeTimers()
    vi.mocked(api.listIndexJobs).mockResolvedValueOnce([job()])
    let finish!: (value: IndexJob[]) => void
    vi.mocked(api.listIndexJobs).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const wrapper = render()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(2500)
    expect(api.listIndexJobs).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(10000)
    expect(api.listIndexJobs).toHaveBeenCalledTimes(2)
    finish([job('code_index', 'completed')])
    await flushPromises()
    wrapper.unmount()
  })

  it('refreshes once more after task completion when parallel reads returned the previous committed state', async () => {
    vi.useFakeTimers()
    vi.mocked(api.getCodeGraphStats).mockResolvedValueOnce(stats('before')).mockResolvedValueOnce(stats('before')).mockResolvedValue(stats('after'))
    vi.mocked(api.listCodeSnapshots).mockResolvedValueOnce([snapshot('before')]).mockResolvedValueOnce([snapshot('before')]).mockResolvedValue([snapshot('after')])
    vi.mocked(api.getCodeVectorState).mockResolvedValueOnce(vector('before')).mockResolvedValueOnce(vector('before')).mockResolvedValue(vector('after'))
    vi.mocked(api.listIndexJobs).mockResolvedValueOnce([job('code_index', 'running')]).mockResolvedValue([job('code_index', 'completed')])
    const wrapper = render()
    await flushPromises()

    await vi.advanceTimersByTimeAsync(2500)
    await flushPromises()
    expect(wrapper.find('.build-progress').text()).toContain('已完成')
    expect(wrapper.text()).toContain('revision-before')
    await vi.advanceTimersByTimeAsync(2500)
    await flushPromises()
    expect(wrapper.text()).toContain('revision-after')
    expect(wrapper.find('.vector-state').text()).toContain('已与当前快照同步')
    expect(api.getCodeSnapshotIssues).toHaveBeenLastCalledWith('kb', 'after')
    expect(api.getCodeGraphStats).toHaveBeenCalledTimes(3)

    await vi.advanceTimersByTimeAsync(10000)
    expect(api.getCodeGraphStats).toHaveBeenCalledTimes(3)
    wrapper.unmount()
  })

  it('preserves a failed completion refresh error and recovers through a manual refresh', async () => {
    vi.useFakeTimers()
    vi.mocked(api.listIndexJobs).mockResolvedValueOnce([job('code_index', 'running')]).mockResolvedValue([job('code_index', 'completed')])
    vi.mocked(api.getCodeGraphStats).mockResolvedValueOnce(stats()).mockResolvedValueOnce(stats()).mockRejectedValueOnce(new Error('完成后状态读取失败')).mockResolvedValue(stats('recovered'))
    const wrapper = render()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(2500)
    await flushPromises()
    await vi.advanceTimersByTimeAsync(2500)
    await flushPromises()

    expect(wrapper.find('.error').text()).toContain('完成后状态读取失败')
    expect(wrapper.text()).toContain('revision-current')
    await click(wrapper, '刷新治理状态')
    expect(wrapper.find('.error').exists()).toBe(false)
    expect(wrapper.text()).toContain('revision-recovered')
    expect(api.getCodeGraphStats).toHaveBeenCalledTimes(4)
    await vi.advanceTimersByTimeAsync(10000)
    expect(api.getCodeGraphStats).toHaveBeenCalledTimes(4)
    wrapper.unmount()
  })

  it('does not start an index task if edit permission is withdrawn while creating its source', async () => {
    const canEdit = ref(true)
    let finish!: (value: Awaited<ReturnType<typeof api.createDataSource>>) => void
    vi.mocked(api.createDataSource).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const wrapper = render(canEdit)
    await flushPromises()
    await wrapper.find('[placeholder="例如：订单服务"]').setValue('新仓库')
    await wrapper.find('[placeholder="https://git.example.com/team/order-service.git"]').setValue('https://git.example.com/new.git')
    await wrapper.find('.source-builder form').trigger('submit')
    await flushPromises()
    expect(api.createDataSource).toHaveBeenCalledTimes(1)

    canEdit.value = false
    await flushPromises()
    finish({ id: 'new-source', name: '新仓库', source_type: 'git' } as never)
    await flushPromises()
    expect(api.requestCodeIndex).not.toHaveBeenCalled()
    expect(wrapper.find('.source-tools').exists()).toBe(false)
    expect(wrapper.text()).toContain('代码源已保存')
  })

  it('looks up a tracked task omitted from the recent list and refreshes once after it completes', async () => {
    vi.useFakeTimers()
    vi.mocked(api.listIndexJobs).mockResolvedValueOnce([job('code_index', 'running')]).mockResolvedValue([])
    vi.mocked(api.getIndexJob).mockResolvedValue(job('code_index', 'completed'))
    const wrapper = render()
    await flushPromises()
    expect(wrapper.find('.build-progress').text()).toContain('构建中')

    await vi.advanceTimersByTimeAsync(2500)
    await flushPromises()
    expect(api.getIndexJob).toHaveBeenCalledWith('kb', 'code_index-job')
    expect(wrapper.find('.build-progress').text()).toContain('已完成')
    await vi.advanceTimersByTimeAsync(2500)
    await flushPromises()
    expect(api.getCodeGraphStats).toHaveBeenCalledTimes(3)
    expect(api.getIndexJob).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(10000)
    expect(api.getCodeGraphStats).toHaveBeenCalledTimes(3)
    wrapper.unmount()
  })

  it('retains a missing task after its lookup fails and recovers its completion on the next poll', async () => {
    vi.useFakeTimers()
    vi.mocked(api.listIndexJobs).mockResolvedValueOnce([job('code_index', 'running')]).mockResolvedValue([])
    vi.mocked(api.getIndexJob).mockRejectedValueOnce(new Error('任务专用查询暂不可用')).mockResolvedValue(job('code_index', 'completed'))
    const wrapper = render()
    await flushPromises()

    await vi.advanceTimersByTimeAsync(2500)
    await flushPromises()
    expect(wrapper.find('.error').text()).toContain('任务专用查询暂不可用')
    expect(wrapper.find('.build-progress').text()).toContain('构建中')
    await vi.advanceTimersByTimeAsync(2500)
    await flushPromises()
    expect(api.getIndexJob).toHaveBeenCalledTimes(2)
    expect(wrapper.find('.error').exists()).toBe(false)
    expect(wrapper.find('.build-progress').text()).toContain('已完成')
    await vi.advanceTimersByTimeAsync(2500)
    await flushPromises()
    expect(api.getCodeGraphStats).toHaveBeenCalledTimes(4)
    await vi.advanceTimersByTimeAsync(10000)
    expect(api.getCodeGraphStats).toHaveBeenCalledTimes(4)
    wrapper.unmount()
  })
})
