<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import * as api from '../api/client'
import type { KnowledgeDocument } from '../types'

const props = defineProps<{ knowledgeBaseId: string; knowledgeBaseName: string }>()
const emit = defineEmits<{ (event:'close'):void; (event:'navigate', page:'documents'|'jobs'|'chat'):void }>()
const documents = ref<KnowledgeDocument[]>([])
const loading = ref(false)
const error = ref('')
const autoGraph = ref(true)
let timer: number | null = null
const readyCount = computed(() => documents.value.filter(item=>item.status==='ready').length)
const processingCount = computed(() => documents.value.filter(item=>['processing','pending'].includes(item.status)).length)
const failedCount = computed(() => documents.value.filter(item=>item.status==='failed').length)
const currentStep = computed(() => !documents.value.length ? 2 : processingCount.value ? 3 : readyCount.value ? 4 : 3)

async function load(): Promise<void> {
  try { documents.value=await api.listDocuments(props.knowledgeBaseId); error.value='' }
  catch(e){ error.value=(e as Error).message }
}
async function upload(event:Event):Promise<void>{
  const input=event.target as HTMLInputElement; const files=Array.from(input.files||[]); if(!files.length)return
  loading.value=true; error.value=''
  try {
    await api.updateGraphAutomation(props.knowledgeBaseId,autoGraph.value,true)
    await api.uploadDocuments(props.knowledgeBaseId,files); input.value=''; await load()
  }catch(e){error.value=(e as Error).message}finally{loading.value=false}
}
onMounted(()=>{void load();timer=window.setInterval(()=>{if(processingCount.value)void load()},3000)})
onBeforeUnmount(()=>{if(timer)window.clearInterval(timer)})
</script>

<template>
  <div class="mask" @click.self="emit('close')"><section class="wizard">
    <header><div><span>快速开始</span><h2>{{knowledgeBaseName}}</h2><p>按照下面步骤准备知识库，完成后即可开始有依据的问答。</p></div><button @click="emit('close')">×</button></header>
    <p v-if="error" class="error">{{error}}</p>
    <div class="steps">
      <article class="done"><i>✓</i><div><b>知识库已创建</b><small>文档、图谱和代码会隔离存放。</small></div></article>
      <article :class="{active:currentStep===2,done:documents.length}"><i>{{documents.length?'✓':'2'}}</i><div><b>添加业务文档</b><small>支持一次选择多个文档，上传后自动解析和构建索引。</small><label v-if="!documents.length" class="upload">{{loading?'正在上传…':'选择文档'}}<input type="file" multiple hidden :disabled="loading" @change="upload" /></label></div></article>
      <article :class="{active:currentStep===3,done:readyCount>0&&!processingCount}"><i>{{readyCount&&!processingCount?'✓':'3'}}</i><div><b>等待知识处理</b><small v-if="processingCount">{{processingCount}} 个文档处理中，预计需要几分钟；可以离开此页面。</small><small v-else-if="failedCount">{{failedCount}} 个文档需要处理，请前往文档管理查看原因。</small><small v-else>系统会依次完成解析、切片、向量化、Schema 和图谱构建。</small><button v-if="documents.length" @click="emit('navigate','jobs')">查看实时进度</button></div></article>
      <article :class="{active:currentStep===4,done:readyCount>0}"><i>{{readyCount?'✓':'4'}}</i><div><b>开始提问</b><small>{{readyCount?`${readyCount} 个文档已可检索。`:'文档就绪后即可获得带来源的回答。'}}</small><button v-if="readyCount" class="primary" @click="emit('navigate','chat')">进入知识问答</button></div></article>
    </div>
    <footer><label><input v-model="autoGraph" type="checkbox" /> 文档完成后自动构建知识图谱</label><button @click="emit('close')">稍后处理</button></footer>
  </section></div>
</template>

<style scoped>
.mask{position:fixed;z-index:35;inset:0;display:grid;place-items:center;padding:20px;background:#10182773;backdrop-filter:blur(3px)}.wizard{width:min(720px,100%);max-height:88vh;overflow:auto;border-radius:18px;background:#fff;box-shadow:0 25px 80px #10182742}.wizard>header{display:flex;padding:22px 24px;color:#fff;background:linear-gradient(135deg,#405fe0,#3c91ef)}.wizard>header div{flex:1}.wizard>header span{font-size:10px;opacity:.8}.wizard h2{margin:4px 0;font-size:22px}.wizard header p{margin:0;font-size:11px;opacity:.85}.wizard header button{border:0;color:#fff;background:transparent;font-size:22px;cursor:pointer}.steps{display:grid;gap:0;padding:18px 24px}.steps article{position:relative;display:flex;gap:13px;padding:13px 10px;color:#8591a2}.steps article:not(:last-child)::after{position:absolute;top:45px;bottom:-8px;left:25px;width:2px;background:#e2e7ef;content:''}.steps i{z-index:1;display:grid;width:32px;height:32px;flex:0 0 32px;place-items:center;border:2px solid #dce3ed;border-radius:50%;background:#fff;font-style:normal;font-weight:700}.steps article.active i{color:#fff;border-color:#4f6ef7;background:#4f6ef7}.steps article.done i{color:#18794e;border-color:#b8dfc7;background:#eaf8f0}.steps b,.steps small{display:block}.steps b{color:#334159;font-size:13px}.steps small{margin-top:4px;line-height:1.5;font-size:10px}.steps button,.upload{display:inline-block;margin-top:8px;padding:6px 10px;border:1px solid #cfdaff;border-radius:7px;color:#4561d8;background:#f4f7ff;cursor:pointer;font-size:10px}.steps .primary,.upload{color:#fff;border-color:#4f6ef7;background:#4f6ef7}.wizard>footer{display:flex;align-items:center;justify-content:space-between;padding:14px 24px;border-top:1px solid #edf1f6;color:#67758a;font-size:10px}.wizard>footer button{padding:7px 10px;border:1px solid #dce3ed;border-radius:7px;background:#fff;cursor:pointer}.error{margin:12px 24px 0;padding:9px;color:#b42318;background:#fff1f0;border-radius:8px;font-size:11px}
</style>
