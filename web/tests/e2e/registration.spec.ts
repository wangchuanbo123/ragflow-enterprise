import { expect, test, type Page } from '@playwright/test'
import type { User } from '../../src/types'

const applicantPassword = 'Applicant-pass-123'
const adminPassword = 'Admin-login-123'
const admin: User = { id: 'admin', username: 'admin', display_name: '管理员', role: 'admin', is_active: true, registration_status: 'approved' }

async function registrationApi(page: Page, startAsAdmin = false, pending = false) {
  let session: User | null = startAsAdmin ? admin : null
  let applicant: User | null = pending ? { id: 'applicant', username: 'alice', display_name: '小明', role: 'user', is_active: false, registration_status: 'pending', created_at: '2026-11-18T01:00:00Z' } : null
  const registrations: unknown[] = []
  const reviews: unknown[] = []
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1', '')
    const method = route.request().method()
    const ok = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    const fail = (code: string, message: string, status: number) => ok({ error: { code, message } }, status)
    if (path === '/auth/me') return session ? ok(session) : fail('UNAUTHORIZED', '未登录', 401)
    if (path === '/auth/register' && method === 'POST') {
      const body = route.request().postDataJSON()
      registrations.push(body)
      applicant = { id: 'applicant', username: body.username, display_name: body.display_name || body.username, role: 'user', is_active: false, registration_status: 'pending', created_at: '2026-11-18T01:00:00Z' }
      return ok({ message: '注册申请已提交，请等待管理员审核；审核通过后即可登录。', registration_status: 'pending' }, 202)
    }
    if (path === '/auth/login' && method === 'POST') {
      const body = route.request().postDataJSON()
      if (body.username === 'admin' && body.password === adminPassword) { session = admin; return ok({ user: admin }) }
      if (!applicant || body.username !== applicant.username || body.password !== applicantPassword) return fail('BAD_REQUEST', '用户名或密码错误', 400)
      if (applicant.registration_status === 'pending') return fail('REGISTRATION_PENDING', '注册申请正在等待管理员审核，暂时无法登录', 403)
      if (applicant.registration_status === 'rejected') return fail('REGISTRATION_REJECTED', '注册申请未通过审核，请联系管理员', 403)
      session = applicant
      return ok({ user: applicant })
    }
    if (path === '/auth/logout') { session = null; return ok({ message: '已退出' }) }
    if (path === '/users') return session?.role === 'admin' ? ok([admin, ...(applicant ? [applicant] : [])]) : fail('FORBIDDEN', '仅管理员可管理用户', 403)
    if (path === '/users/applicant/registration-review' && method === 'POST') {
      if (session?.role !== 'admin') return fail('FORBIDDEN', '仅管理员可管理用户', 403)
      const body = route.request().postDataJSON()
      reviews.push(body)
      applicant = { ...applicant!, registration_status: body.decision, is_active: body.decision === 'approved', registration_reviewed_by: 'admin', registration_reviewed_at: '2026-11-18T02:00:00Z', registration_review_note: body.note || '' }
      return ok(applicant)
    }
    if (path === '/knowledge-bases') return ok([])
    return ok([])
  })
  return { registrations, reviews }
}

async function login(page: Page, username: string, password: string) {
  await page.getByLabel('用户名', { exact: true }).fill(username)
  await page.getByLabel('密码', { exact: true }).fill(password)
  await page.locator('button[type="submit"]').click()
}

test('registration requires administrator approval before the applicant can sign in', async ({ page }, testInfo) => {
  const captured = await registrationApi(page)
  await page.goto('/')
  await page.getByRole('button', { name: '申请注册', exact: true }).click()
  await page.getByLabel('用户名', { exact: true }).fill('alice')
  await page.getByLabel('显示名称', { exact: true }).fill('小明')
  await page.getByLabel('密码', { exact: true }).fill(applicantPassword)
  await page.getByLabel('确认密码', { exact: true }).fill(applicantPassword)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({ path: testInfo.outputPath('registration-mobile.png'), fullPage: true })
  await page.getByRole('button', { name: '提交注册申请', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('等待管理员审核')
  await expect(page.locator('.workspace')).toHaveCount(0)
  expect(captured.registrations).toEqual([{ username: 'alice', display_name: '小明', password: applicantPassword }])

  await login(page, 'alice', applicantPassword)
  await expect(page.getByRole('alert')).toContainText('等待管理员审核')
  await login(page, 'admin', adminPassword)
  await page.setViewportSize({ width: 1440, height: 1080 })
  await page.locator('.main-nav').getByRole('button', { name: '用户管理', exact: true }).click()
  await expect(page.locator('.registration-requests')).toContainText('小明')
  await expect(page.locator('.pending-count')).toHaveText('1')
  await page.screenshot({ path: testInfo.outputPath('registration-review-desktop.png'), fullPage: true })
  await page.getByLabel('alice 的审核备注').fill('团队成员，批准注册')
  await page.getByRole('button', { name: '审核通过', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('用户现在可以登录')
  await expect(page.locator('.pending-count')).toHaveText('0')
  expect(captured.reviews).toEqual([{ decision: 'approved', note: '团队成员，批准注册' }])

  await page.getByTitle('退出', { exact: true }).click()
  await login(page, 'alice', applicantPassword)
  await expect(page.locator('.account-name strong')).toHaveText('小明')
  await expect(page.locator('.main-nav').getByRole('button', { name: '用户管理', exact: true })).toHaveCount(0)
})

test('a rejected registration remains unavailable and records the review note', async ({ page }) => {
  const captured = await registrationApi(page, true, true)
  await page.goto('/#page=users')
  await expect(page.locator('.pending-count')).toHaveText('1')
  await page.getByLabel('alice 的审核备注').fill('请联系团队负责人核实身份')
  await page.getByRole('button', { name: '拒绝申请', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('已拒绝 alice')
  const account = page.locator('.account-list article').filter({ hasText: '@alice' })
  await expect(account).toContainText('注册已拒绝')
  await expect(account).toContainText('请联系团队负责人核实身份')
  await expect(account.getByRole('button', { name: '编辑', exact: true })).toHaveCount(0)
  expect(captured.reviews).toEqual([{ decision: 'rejected', note: '请联系团队负责人核实身份' }])
  await page.getByTitle('退出', { exact: true }).click()
  await login(page, 'alice', applicantPassword)
  await expect(page.getByRole('alert')).toContainText('未通过审核')
  await expect(page.locator('.workspace')).toHaveCount(0)
})
