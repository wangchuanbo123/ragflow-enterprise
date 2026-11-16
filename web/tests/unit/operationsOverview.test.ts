import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ReadinessView from '../../src/components/ReadinessView.vue'
import { useAuth } from '../../src/composables/useAuth'
import * as api from '../../src/api/client'
import * as operations from '../../src/api/operations'
import type { OperationsOverview } from '../../src/types/operations'

vi.mock('../../src/api/client')
vi.mock('../../src/api/operations')
const checkedAt = '2026-11-06T10:00:00Z'
const admin = (id = 'admin') => ({ id, username: id, display_name: id, role: 'admin', is_active: true })
function snapshot(): OperationsOverview {
  return {
    checked_at: checkedAt, source: 'shared_redis', retention_days: 7,
    metrics: {
      model_first_token_seconds: { observations: 4, total: 8, mean: 2 },
      model_stream_seconds: { observations: 5, total: 40, mean: 8 },
    },
    tasks: { pending: 12, running: 3 }, oldest_pending_seconds: 180,
    database_pool: { size: 10, checkedin: 6, checkedout: 4, overflow: 0 },
    process: { rss_bytes: 1024 ** 3 }, disk: { total_bytes: 100 * 1024 ** 3, free_bytes: 20 * 1024 ** 3 },
    memory: { percent: 51.25, scope: 'container' },
    backup: { status: 'backed_up', last_success_at: checkedAt, mirror_status: 'unconfigured', rpo_target_hours: 24, rto_target_minutes: 60, rto_verified: false },
    alerts: [{ code: 'BACKUP_MIRROR_UNVERIFIED', message: 'untranslated message' }, { code: 'RECENT_TASK_FAILURES', message: '', count: 2 }],
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  useAuth().user.value = admin()
  vi.mocked(api.getReadiness).mockResolvedValue({ status: 'ready', checked_at: checkedAt, model_inference_verified: false, components: { database: { status: 'ok' } } })
  vi.mocked(operations.getOperationsOverview).mockResolvedValue(snapshot())
})

describe('administrator operations overview', () => {
  it('shows live tasks, precise metric scope, resources, backup and Chinese alerts', async () => {
    const wrapper = mount(ReadinessView); await flushPromises()
    const panel = wrapper.find('.operations-overview')
    expect(panel.text()).toContain('排队中12 项')
    expect(panel.text()).toContain('运行中3 项')
    expect(panel.text()).toContain('最长等待3.0 分钟')
    expect(panel.text()).toContain('最近 7 个 UTC 自然日（含今天）')
    expect(panel.text()).toContain('算术均值')
    expect(panel.text()).toContain('流式首字均值2.0 秒')
    expect(panel.text()).toContain('流式总时均值8.0 秒')
    expect(panel.text()).toContain('非流式总时均值暂无样本')
    expect(panel.text()).toContain('基础池大小10')
    expect(panel.text()).toContain('使用中 / 空闲4 / 6')
    expect(panel.text()).toContain('当前容器内存51.3%')
    expect(panel.text()).toContain('20.00 GiB / 100.00 GiB')
    expect(panel.text()).toContain(new Date(checkedAt).toLocaleString())
    expect(panel.text()).toContain('异机备份未配置')
    expect(panel.text()).toContain('尚未通过恢复演练验证')
    expect(panel.text()).toContain('异机备份副本尚未配置或未通过校验')
    expect(panel.text()).toContain('最近 15 分钟存在失败或部分完成的任务（2 项）')
    expect(panel.text()).not.toContain('BACKUP_MIRROR_UNVERIFIED')
  })

  it('does not request administrator metrics for an ordinary member', async () => {
    useAuth().user.value = { ...admin(), role: 'user' }
    const wrapper = mount(ReadinessView); await flushPromises()
    expect(operations.getOperationsOverview).not.toHaveBeenCalled()
    expect(wrapper.find('.operations-overview').exists()).toBe(false)
    expect(wrapper.text()).toContain('基础服务检查通过')
  })

  it('isolates a metric failure from readiness and supports its own retry', async () => {
    vi.mocked(operations.getOperationsOverview).mockRejectedValueOnce(new Error('指标服务繁忙'))
    const wrapper = mount(ReadinessView); await flushPromises()
    expect(wrapper.text()).toContain('基础服务检查通过')
    expect(wrapper.find('.operations-overview [role="alert"]').text()).toContain('指标服务繁忙')
    expect(wrapper.find('.summary').text()).toContain('模型推理尚未验证')
    await wrapper.find('.operations-head button').trigger('click'); await flushPromises()
    expect(operations.getOperationsOverview).toHaveBeenCalledTimes(2)
    expect(api.getReadiness).toHaveBeenCalledTimes(1)
    expect(wrapper.find('.operations-overview [role="alert"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('排队中12 项')
  })

  it('keeps metrics available even when the independent readiness request fails', async () => {
    vi.mocked(api.getReadiness).mockRejectedValueOnce(new Error('就绪检查暂不可用'))
    const wrapper = mount(ReadinessView); await flushPromises()
    expect(wrapper.text()).toContain('就绪检查暂不可用')
    expect(wrapper.find('.operations-overview').text()).toContain('排队中12 项')
  })

  it('labels local fallback accumulation, missing samples and unavailable backup honestly', async () => {
    const value = snapshot()
    value.source = 'process_fallback'; value.metrics = {}; value.backup = { status: 'not_recorded' }
    value.database_pool.overflow = -5; value.memory.scope = 'host'; value.alerts = []
    vi.mocked(operations.getOperationsOverview).mockResolvedValue(value)
    const wrapper = mount(ReadinessView); await flushPromises()
    const panel = wrapper.find('.operations-overview')
    expect(panel.text()).toContain('共享指标暂不可用；当前进程累计')
    expect(panel.text()).toContain('不代表完整 7 日')
    expect(panel.text()).toContain('流式首字均值暂无样本')
    expect(panel.text()).toContain('溢出连接0')
    expect(panel.text()).toContain('主机内存')
    expect(panel.text()).toContain('尚无成功记录')
    expect(panel.text()).toContain('异机副本尚未验证')
    expect(panel.text()).toContain('当前未触发运行告警')
  })

  it('cancels and discards a prior administrator response after an account change', async () => {
    let finish!: (value: OperationsOverview) => void
    vi.mocked(operations.getOperationsOverview).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const wrapper = mount(ReadinessView); await flushPromises()
    const signal = vi.mocked(operations.getOperationsOverview).mock.calls[0][0]!
    useAuth().user.value = { ...admin('member'), role: 'user' }; await flushPromises()
    expect(signal.aborted).toBe(true)
    expect(wrapper.find('.operations-overview').exists()).toBe(false)
    useAuth().user.value = admin('second-admin'); await flushPromises()
    const stale = snapshot(); stale.tasks.pending = 9999
    finish(stale); await flushPromises()
    expect(wrapper.find('.operations-overview').text()).toContain('排队中12 项')
    expect(wrapper.text()).not.toContain('9999')
    expect(operations.getOperationsOverview).toHaveBeenCalledTimes(2)
  })

  it('aborts an in-flight metric request when the view is unmounted', async () => {
    vi.mocked(operations.getOperationsOverview).mockImplementationOnce(() => new Promise(() => {}))
    const wrapper = mount(ReadinessView); await flushPromises()
    const signal = vi.mocked(operations.getOperationsOverview).mock.calls[0][0]!
    wrapper.unmount()
    expect(signal.aborted).toBe(true)
  })

  it('retains the timestamp and identifies stale data if a refresh fails', async () => {
    const wrapper = mount(ReadinessView); await flushPromises()
    vi.mocked(operations.getOperationsOverview).mockRejectedValueOnce(new Error('暂不可用'))
    await wrapper.find('.operations-head button').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('以下为上次成功获取的数据')
    expect(wrapper.text()).toContain('排队中12 项')
  })
})
