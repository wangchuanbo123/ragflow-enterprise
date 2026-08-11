<script setup lang="ts">
import { onMounted } from 'vue'
import { useAuth } from './composables/useAuth'
import LoginView from './components/LoginView.vue'
import WorkspaceView from './components/WorkspaceView.vue'

const { user, ready, restore } = useAuth()
onMounted(restore)
</script>

<template>
  <div class="app-root">
    <div v-if="!ready" class="boot">正在加载工作台…</div>
    <LoginView v-else-if="!user" />
    <WorkspaceView v-else />
  </div>
</template>

<style scoped>
.boot { display: grid; place-items: center; height: 100vh; color: #64748b; }
</style>
