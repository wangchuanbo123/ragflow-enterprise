import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import LoginView from '../../src/components/LoginView.vue'
import UsersView from '../../src/components/UsersView.vue'
import { useAuth } from '../../src/composables/useAuth'
import type { User } from '../../src/types'
import * as api from '../../src/api/client'

vi.mock('../../src/api/client')

const securePassword = 'New-password-123'
const account = (status: 'pending' | 'approved' | 'rejected' = 'pending'): User => ({
  id: 'new-user', username: 'new-user', display_name: '新用户', role: 'user',
  is_active: status === 'approved', registration_status: status,
  created_at: '2026-11-18T01:00:00Z', registration_reviewed_by: null,
  registration_reviewed_at: null, registration_review_note: '',
})

function button(wrapper: VueWrapper, text: string) {
  const found = wrapper.findAll('button').find(item => item.text() === text)
  expect(found, `button ${text}`).toBeDefined()
  return found!
}

function input(wrapper: VueWrapper, name: string) {
  const found = wrapper.findAll('label').find(item => item.text().trim().startsWith(name))
  expect(found, `field ${name}`).toBeDefined()
  return found!.find('input')
}

async function registrationForm(wrapper: VueWrapper, password = securePassword, confirmation = password) {
  await button(wrapper, '申请注册').trigger('click')
  await input(wrapper, '用户名').setValue('new-user')
  await input(wrapper, '显示名称').setValue('新用户')
  await input(wrapper, '密码').setValue(password)
  await input(wrapper, '确认密码').setValue(confirmation)
}

beforeEach(() => {
  vi.resetAllMocks()
  const auth = useAuth()
  auth.user.value = null
  auth.state.loading = false
  auth.state.error = ''
  vi.mocked(api.registerUser).mockResolvedValue({ message: '注册申请已提交，等待管理员审核，通过后即可登录。', registration_status: 'pending' })
  vi.mocked(api.listUsers).mockResolvedValue([])
})

describe('registration application', () => {
  it('submits a pending application and returns to login without creating a session', async () => {
    const wrapper = mount(LoginView)
    await registrationForm(wrapper)
    await wrapper.find('form').trigger('submit'); await flushPromises()

    expect(api.registerUser).toHaveBeenCalledWith({ username: 'new-user', display_name: '新用户', password: securePassword })
    expect(api.login).not.toHaveBeenCalled()
    expect(useAuth().user.value).toBeNull()
    expect(wrapper.text()).toContain('等待管理员审核')
    expect(wrapper.find('[type="submit"]').text()).toBe('登录')
    expect(input(wrapper, '用户名').element).toHaveProperty('value', 'new-user')
    expect(input(wrapper, '密码').element).toHaveProperty('value', '')
  })

  it.each([
    ['short1', 'short1', '密码'],
    ['letters-only-password', 'letters-only-password', '字母和数字'],
    [securePassword, 'Different-password-456', '一致'],
  ])('rejects invalid passwords before sending an application: %s', async (password, confirmation, message) => {
    const wrapper = mount(LoginView)
    await registrationForm(wrapper, password, confirmation)
    await wrapper.find('form').trigger('submit'); await flushPromises()

    expect(api.registerUser).not.toHaveBeenCalled()
    expect(wrapper.find('[role="alert"]').text()).toContain(message)
  })

  it('keeps a failed application editable and allows a retry', async () => {
    vi.mocked(api.registerUser).mockRejectedValueOnce(new Error('用户名已存在'))
    const wrapper = mount(LoginView)
    await registrationForm(wrapper)
    await wrapper.find('form').trigger('submit'); await flushPromises()
    expect(wrapper.find('[role="alert"]').text()).toContain('用户名已存在')
    expect(wrapper.find('[type="submit"]').attributes('disabled')).toBeUndefined()

    await input(wrapper, '用户名').setValue('another-user')
    await input(wrapper, '密码').setValue(securePassword)
    await input(wrapper, '确认密码').setValue(securePassword)
    await wrapper.find('form').trigger('submit'); await flushPromises()
    expect(api.registerUser).toHaveBeenCalledTimes(2)
    expect(api.registerUser).toHaveBeenLastCalledWith(expect.objectContaining({ username: 'another-user' }))
    expect(wrapper.text()).toContain('等待管理员审核')
  })

  it('prevents duplicate applications and mode changes while submission is in progress', async () => {
    let finish!: (result: Awaited<ReturnType<typeof api.registerUser>>) => void
    vi.mocked(api.registerUser).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const wrapper = mount(LoginView)
    await registrationForm(wrapper)
    await wrapper.find('form').trigger('submit')
    await wrapper.find('form').trigger('submit')

    expect(api.registerUser).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[type="submit"]').attributes('disabled')).toBeDefined()
    expect(button(wrapper, '申请注册').attributes('disabled')).toBeDefined()
    expect(button(wrapper, '登录').attributes('disabled')).toBeDefined()
    finish({ message: '注册申请已提交，等待管理员审核，通过后即可登录。', registration_status: 'pending' })
    await flushPromises()
    expect(wrapper.find('[type="submit"]').text()).toBe('登录')
  })

  it.each([
    ['REGISTRATION_PENDING', '注册申请待管理员审核，通过后即可登录'],
    ['REGISTRATION_REJECTED', '注册申请未通过审核，请联系管理员'],
  ])('shows the review status when login is blocked: %s', async (code, message) => {
    vi.mocked(api.login).mockRejectedValue(Object.assign(new Error(message), { code, status: 403 }))
    const wrapper = mount(LoginView)
    await input(wrapper, '用户名').setValue('new-user')
    await input(wrapper, '密码').setValue(securePassword)
    await wrapper.find('form').trigger('submit'); await flushPromises()
    expect(wrapper.find('[role="alert"]').text()).toContain(message)
    expect(useAuth().user.value).toBeNull()
  })
})

describe('administrator registration review', () => {
  it('approves a pending application and refreshes the account status', async () => {
    vi.mocked(api.listUsers).mockResolvedValueOnce([account()]).mockResolvedValue([account('approved')])
    vi.mocked(api.reviewUserRegistration).mockResolvedValue(account('approved'))
    const wrapper = mount(UsersView); await flushPromises()
    expect(wrapper.find('.registration-requests').text()).toContain('new-user')

    await button(wrapper, '审核通过').trigger('click'); await flushPromises()
    expect(api.reviewUserRegistration).toHaveBeenCalledWith('new-user', expect.objectContaining({ decision: 'approved' }))
    expect(api.listUsers).toHaveBeenCalledTimes(2)
    expect(wrapper.findAll('.registration-requests article')).toHaveLength(0)
    expect(wrapper.text()).toContain('启用')
  })

  it('sends the rejection note and leaves the rejected account unavailable for ordinary editing', async () => {
    vi.mocked(api.listUsers).mockResolvedValueOnce([account()]).mockResolvedValue([{ ...account('rejected'), registration_review_note: '请补充身份信息' }])
    vi.mocked(api.reviewUserRegistration).mockResolvedValue(account('rejected'))
    const wrapper = mount(UsersView); await flushPromises()
    await wrapper.find('[aria-label="new-user 的审核备注"]').setValue('请补充身份信息')
    await button(wrapper, '拒绝申请').trigger('click'); await flushPromises()

    expect(api.reviewUserRegistration).toHaveBeenCalledWith('new-user', { decision: 'rejected', note: '请补充身份信息' })
    expect(wrapper.text()).toContain('已拒绝')
    expect(wrapper.findAll('article button').some(item => item.text() === '编辑')).toBe(false)
  })

  it('retains a failed review for retry and disables repeated decisions during the request', async () => {
    vi.mocked(api.listUsers).mockResolvedValue([account()])
    let fail!: (error: Error) => void
    vi.mocked(api.reviewUserRegistration).mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject }))
    const wrapper = mount(UsersView); await flushPromises()
    const approve = button(wrapper, '审核通过'), reject = button(wrapper, '拒绝申请')
    await approve.trigger('click')
    expect(approve.attributes('disabled')).toBeDefined()
    expect(reject.attributes('disabled')).toBeDefined()
    fail(new Error('审核保存失败')); await flushPromises()

    expect(wrapper.find('[role="alert"]').text()).toContain('审核保存失败')
    expect(wrapper.find('.registration-requests').text()).toContain('new-user')
    expect(button(wrapper, '审核通过').attributes('disabled')).toBeUndefined()
    vi.mocked(api.reviewUserRegistration).mockResolvedValue(account('approved'))
    vi.mocked(api.listUsers).mockResolvedValue([account('approved')])
    await button(wrapper, '审核通过').trigger('click'); await flushPromises()
    expect(api.reviewUserRegistration).toHaveBeenCalledTimes(2)
    expect(wrapper.findAll('.registration-requests article')).toHaveLength(0)
  })

  it('allows existing approved accounts to be edited while separating unreviewed accounts', async () => {
    const legacy = { id: 'old-user', username: 'old-user', display_name: '已有账号', role: 'user', is_active: true }
    vi.mocked(api.listUsers).mockResolvedValue([account(), { ...account('rejected'), id: 'rejected', username: 'rejected' }, legacy])
    const wrapper = mount(UsersView); await flushPromises()
    expect(wrapper.findAll('article button').filter(item => item.text() === '编辑')).toHaveLength(1)
    await button(wrapper, '编辑').trigger('click')
    expect(input(wrapper, '用户名').element).toHaveProperty('value', 'old-user')
    expect(input(wrapper, '用户名').attributes('disabled')).toBeDefined()
  })
})
