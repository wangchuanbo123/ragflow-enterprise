import type { GraphProfile, GraphSchema } from './types'

export interface GraphTypeLabel { label: string; definition: string }

const entityDefaults: Record<string, string> = {
  SYSTEM: '系统', MODULE: '模块', INTERFACE: '接口', REQUIREMENT: '需求', TEST_CASE: '测试用例',
  ROLE: '角色', ENVIRONMENT: '环境', DOCUMENT: '文档', VERSION: '版本', TERM: '术语', OTHER: '其他',
  PERSON: '人物', ORGANIZATION: '组织', LOCATION: '地点', CONCEPT: '概念', EVENT: '事件',
  TECHNOLOGY: '技术', PRODUCT: '产品', PROCESS: '流程', DATA: '数据', STANDARD: '标准',
}
const relationDefaults: Record<string, string> = {
  CONTAINS: '包含', PART_OF: '属于', DEPENDS_ON: '依赖', CALLS: '调用', PROVIDES: '提供',
  IMPLEMENTS: '实现', SATISFIES: '满足', VERIFIED_BY: '由其验证', DEPLOYED_ON: '部署于',
  OWNED_BY: '归属', USES: '使用', RELATED_TO: '关联', WORKS_FOR: '任职于',
  LOCATED_IN: '位于', BELONGS_TO: '隶属', CREATED_BY: '由其创建',
}

/** Labels describe the sole Schema; they never select or project a graph. */
export function graphTypeLabels(profiles: GraphProfile[], schema?: GraphSchema) {
  const entities = new Map<string, GraphTypeLabel>()
  const relations = new Map<string, GraphTypeLabel>()
  for (const [name, label] of Object.entries(entityDefaults)) entities.set(name, { label, definition: '' })
  for (const [name, label] of Object.entries(relationDefaults)) relations.set(name, { label, definition: '' })
  function add(target: Map<string, GraphTypeLabel>, details: unknown) {
    if (!Array.isArray(details)) return
    for (const item of details) {
      if (!item || typeof item.name !== 'string') continue
      const name = item.name.trim()
      if (!name) continue
      const current = target.get(name.toUpperCase())
      const explicit = typeof item.label === 'string' ? item.label.trim() : ''
      const definition = typeof item.description === 'string' ? item.description.trim() : ''
      // Older general catalogs use the code itself as a placeholder label.
      const label = (explicit && explicit !== name ? explicit : '') ||
        current?.label || definition.split('\n')[0] || name
      target.set(name.toUpperCase(), { label, definition: definition || current?.definition || '' })
    }
  }
  for (const profile of [...profiles.filter(item => item.key === 'general'), schema?.graph_profiles_json?.general].filter(Boolean)) {
    add(entities, profile!.entity_types); add(relations, profile!.relation_types)
  }
  add(entities, schema?.generation_summary_json?.entity_type_details)
  add(relations, schema?.generation_summary_json?.relation_type_details)
  return { entities, relations }
}
