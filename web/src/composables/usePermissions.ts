import { computed, inject, type ComputedRef, type InjectionKey } from 'vue'
import type { KnowledgeBase } from '../types'

export const knowledgeBaseKey: InjectionKey<ComputedRef<KnowledgeBase | undefined>> = Symbol('knowledgeBase')

export function usePermissions() {
  const knowledgeBase = inject(knowledgeBaseKey, computed(() => undefined))
  return {
    canEdit: computed(() => knowledgeBase.value?.capabilities?.can_edit === true),
    canManageMembers: computed(() => knowledgeBase.value?.capabilities?.can_manage_members === true),
  }
}
