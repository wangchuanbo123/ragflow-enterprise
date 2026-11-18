import { expect, test, type Page } from '@playwright/test'

async function governanceApi(page: Page, editor = true) {
  const requests: string[] = []
  const snapshot = {
    id: 'snapshot', knowledge_base_id: 'kb', data_source_id: 'source', revision: 'commit-1',
    content_hash: 'content', parser_version: 'parser-1', status: 'active',
    language_stats_json: { files_parsed: 2 }, error_message: null,
    created_at: '2026-11-18T01:00:00Z', activated_at: '2026-11-18T01:00:00Z',
  }
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1', '')
    requests.push(path)
    const ok = (body: unknown) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    if (path === '/auth/me') return ok({ id: 'user', username: editor ? 'editor' : 'viewer', display_name: '测试用户', role: 'user', is_active: true })
    if (path === '/knowledge-bases') return ok([{
      id: 'kb', name: '代码知识库', slug: 'kb', description: '图谱治理测试', is_active: true, is_default: true,
      current_user_role: editor ? 'editor' : 'viewer',
      capabilities: { can_read: true, can_edit: editor, can_manage_members: false, can_archive: false },
    }])
    if (path.endsWith('/code-graph/stats')) return ok({ active_snapshot_id: 'snapshot', revision: 'commit-1', files: 2, symbols: 4, relations: 3, resolved_relations: 2 })
    if (path.endsWith('/code-graph/snapshots')) return ok([snapshot])
    if (path.endsWith('/code-graph/snapshots/snapshot/issues')) return ok([{ path: 'src/broken.py', language: 'python', status: 'failed', errors: ['第 5 行语法错误'] }])
    if (path.endsWith('/code-graph/vector')) return ok({ snapshot_id: 'snapshot', index_name: 'code-index', embedding_fingerprint: 'embedding', symbol_fingerprint: 'symbols', symbol_count: 4, status: 'ready', updated_at: '2026-11-18T01:00:00Z' })
    if (path.endsWith('/sources')) return ok([{ id: 'source', name: '订单服务', source_type: 'git', config: { location: 'https://example.invalid/repo.git' }, health_status: 'healthy' }])
    if (path.endsWith('/knowledge-graph/quality')) return ok(null)
    if (path.endsWith('/knowledge-graph/automation')) return ok({ auto_build_enabled: true, auto_schema_generation_enabled: true })
    if (path.endsWith('/knowledge-graph/coverage')) return ok({ summary: { documents: 1, chunks: 1, processed_chunks: 1, fact_chunks: 1, processing_rate: 1, fact_coverage_rate: 1 }, documents: [] })
    if (path.endsWith('/knowledge-graph/entity-vector')) return ok({ status: 'ready', entity_count: 1 })
    if (path.endsWith('/knowledge-graph/relation-vector')) return ok({ status: 'ready', relation_count: 1 })
    if (path.endsWith('/knowledge-graph/community-status')) return ok({ status: 'ready', communities: 0, entities: 1 })
    if (path.endsWith('/documents/tag-map')) return ok({})
    if (path.endsWith('/documents')) return ok([{ id: 'doc', original_filename: '代码说明.md', status: 'ready' }])
    return ok([])
  })
  return requests
}

test('an editor opens code governance from the adjacent code graph navigation and keeps it after refresh', async ({ page }, testInfo) => {
  await governanceApi(page)
  await page.goto('/#page=code-graph&kb=kb')
  await expect(page.getByRole('button', { name: '管理代码图谱', exact: true })).toBeVisible()
  const labels = await page.locator('.main-nav nav button').evaluateAll(buttons => buttons.map(button => button.getAttribute('aria-label')))
  expect(labels.indexOf('代码图谱')).toBe(labels.indexOf('知识图谱') + 1)

  await page.getByRole('button', { name: '管理代码图谱', exact: true }).click()
  await expect(page).toHaveURL(/page=graph-management.*graph=code/)
  await expect(page.getByRole('tab', { name: '代码图谱', exact: true })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('heading', { name: '代码图谱治理', exact: true })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('tab', { name: '代码图谱', exact: true })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('heading', { name: '代码图谱治理', exact: true })).toBeVisible()
  await page.setViewportSize({ width: 1440, height: 1080 })
  await page.screenshot({ path: testInfo.outputPath('code-governance-desktop.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({ path: testInfo.outputPath('code-governance-mobile.png'), fullPage: true })
  await page.locator('#code-governance-panel').hover()
  await page.mouse.wheel(0, 4000)
  await expect(page.getByRole('heading', { name: '代码快照历史', exact: true })).toBeInViewport()
  await page.screenshot({ path: testInfo.outputPath('code-governance-mobile-history.png'), fullPage: true })
})

test('a single code snapshot exposes parsing issues without comparing versions', async ({ page }) => {
  const requests = await governanceApi(page)
  await page.goto('/#page=graph-management&kb=kb&graph=code')
  await expect(page.getByLabel('问题快照', { exact: true })).toHaveValue('snapshot')
  await expect(page.locator('.issues-section')).toContainText('src/broken.py')
  await expect(page.locator('.issues-section')).toContainText('第 5 行语法错误')
  await expect(page.getByRole('button', { name: '比较版本', exact: true })).toBeDisabled()
  expect(requests.filter(path => path.endsWith('/issues'))).toHaveLength(1)
  expect(requests.some(path => path.endsWith('/diff'))).toBe(false)
})

test('governance tabs update the URL and browser back restores the previous section', async ({ page }) => {
  await governanceApi(page)
  await page.goto('/#page=graph-management&kb=kb&graph=code')
  await expect(page.getByRole('heading', { name: '代码图谱治理', exact: true })).toBeVisible()
  await page.getByRole('tab', { name: '文档图谱', exact: true }).click()
  await expect(page.getByRole('tab', { name: '文档图谱', exact: true })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('button', { name: 'Schema 管理', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Schema 管理', exact: true }).click()
  await page.getByPlaceholder('领域定义、边界、反例等').fill('保留尚未保存的领域规则')
  await expect(page).not.toHaveURL(/graph=code/)
  await expect(page.getByRole('heading', { name: '代码图谱治理', exact: true })).toHaveCount(0)

  await page.goBack()
  await expect(page).toHaveURL(/graph=code/)
  await expect(page.getByRole('tab', { name: '代码图谱', exact: true })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('heading', { name: '代码图谱治理', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Schema 管理', exact: true })).toHaveCount(0)
  await page.getByRole('tab', { name: '文档图谱', exact: true }).click()
  await expect(page.getByPlaceholder('领域定义、边界、反例等')).toHaveValue('保留尚未保存的领域规则')
})

test('a viewer can browse the code graph without management entry points', async ({ page }) => {
  await governanceApi(page, false)
  await page.goto('/#page=code-graph&kb=kb')
  await expect(page.locator('.stats')).toContainText('commit-1')
  await expect(page.locator('.main-nav').getByRole('button', { name: '代码图谱', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '管理代码图谱', exact: true })).toHaveCount(0)
  await expect(page.locator('.main-nav').getByRole('button', { name: '图谱治理', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '创建索引任务', exact: true })).toHaveCount(0)
})
