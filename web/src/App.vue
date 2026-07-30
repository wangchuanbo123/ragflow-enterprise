<script setup lang="ts">
import { onMounted } from 'vue'
import { useAuth } from './composables/useAuth'
import LoginView from './components/LoginView.vue'
import ChatView from './components/ChatView.vue'

const { user, ready, restore } = useAuth()

onMounted(() => {
  restore()
})
</script>

<template>
  <div class="app-root">
    <div v-if="!ready" class="boot">正在加载…</div>
    <LoginView v-else-if="!user" />
    <ChatView v-else />
  </div>
</template>

<style scoped>
.boot {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100vh;
  color: #888;
}
</style>
