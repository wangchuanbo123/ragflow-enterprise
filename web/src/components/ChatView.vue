<script setup lang="ts">
import { onMounted } from 'vue'
import { useAuth } from '../composables/useAuth'
import { useConversations } from '../composables/useConversations'
import ConversationSidebar from './ConversationSidebar.vue'
import MessageList from './MessageList.vue'

const { user, logout } = useAuth()
const conv = useConversations()

onMounted(() => {
  conv.loadList()
})

function onSend(content: string): void {
  void conv.send(content)
}
</script>

<template>
  <div class="chat-layout">
    <ConversationSidebar
      :conversations="conv.conversations.value"
      :active-id="conv.activeId.value"
      :loading="conv.loadingList.value"
      @select="conv.select"
      @create="conv.create"
      @rename="conv.rename"
      @delete="conv.remove"
    />

    <div class="chat-area">
      <header class="topbar">
        <span class="topbar-title">
          {{ conv.activeId.value ? '当前会话' : '企业知识库助手' }}
        </span>
        <div class="topbar-right">
          <span class="username">{{ user?.display_name || user?.username }}</span>
          <button class="logout-btn" @click="logout">退出</button>
        </div>
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
  height: 100vh;
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
.topbar-right {
  display: flex;
  align-items: center;
  gap: 10px;
}
.username {
  color: #666;
  font-size: 13px;
}
.logout-btn {
  border: 1px solid #ddd;
  background: #fff;
  border-radius: 6px;
  padding: 5px 12px;
  font-size: 13px;
  cursor: pointer;
  color: #555;
}
.logout-btn:hover {
  background: #f3f4f6;
}
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
