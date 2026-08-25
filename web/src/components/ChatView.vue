<script setup lang="ts">
import { computed, onMounted, watch } from 'vue'
import type { RetrievalStrategy } from '../types'
import { useConversations } from '../composables/useConversations'
import ConversationSidebar from './ConversationSidebar.vue'
import MessageList from './MessageList.vue'

const props = defineProps<{ knowledgeBaseId: string }>()
const conv = useConversations()
const visibleConversations = computed(() =>
  conv.conversations.value.filter((item) => item.knowledge_base_id === props.knowledgeBaseId),
)

onMounted(() => {
  conv.loadList()
})

function onSend(payload: { content: string; strategy: RetrievalStrategy }): void {
  void conv.send(payload.content, payload.strategy)
}

function onCreate(): void {
  void conv.create(props.knowledgeBaseId)
}

watch(() => props.knowledgeBaseId, () => {
  const active = conv.conversations.value.find((item) => item.id === conv.activeId.value)
  if (active?.knowledge_base_id !== props.knowledgeBaseId) {
    conv.activeId.value = null
    conv.resetDetail()
  }
})
</script>

<template>
  <div class="chat-layout">
    <ConversationSidebar
      :conversations="visibleConversations"
      :active-id="conv.activeId.value"
      :loading="conv.loadingList.value"
      @select="conv.select"
      @create="onCreate"
      @rename="conv.rename"
      @delete="conv.remove"
    />

    <div class="chat-area">
      <header class="topbar">
        <span class="topbar-title">
          {{ conv.activeId.value ? '当前会话' : '企业知识库助手' }}
        </span>
        <span class="topbar-hint">同步问答与流式问答使用同一检索工作流</span>
      </header>

      <p v-if="conv.errorMsg.value" class="global-error">{{ conv.errorMsg.value }}</p>

      <MessageList
        :messages="conv.messages.value"
        :loading="conv.loadingDetail.value"
        :sending="conv.sending.value"
        @retry="onSend"
      />
    </div>
  </div>
</template>

<style scoped>
.chat-layout {
  display: flex;
  height: 100%;
  overflow: hidden;
}
.chat-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: #fff;
}
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
  border-bottom: 1px solid #e5e7eb;
  background: #fff;
}
.topbar-title {
  font-weight: 600;
  color: #333;
  font-size: 15px;
}
.topbar-hint { color: #9aa4b2; font-size: 11px; }
.global-error {
  margin: 0;
  padding: 8px 16px;
  background: #fff3f3;
  color: #d33;
  font-size: 13px;
  border-bottom: 1px solid #ffd6d6;
}

@media (max-width: 640px) {
  .topbar-title {
    font-size: 13px;
  }
}
</style>
