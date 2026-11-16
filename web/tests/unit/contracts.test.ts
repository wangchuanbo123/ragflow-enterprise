import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ReadinessView from '../../src/components/ReadinessView.vue'
import UsersView from '../../src/components/UsersView.vue'
import { useAuth } from '../../src/composables/useAuth'
import { passwordError } from '../../src/passwordValidation'
import * as api from '../../src/api/client'
import type { Readiness } from '../../src/types'
vi.mock('../../src/api/client',()=>({getReadiness:vi.fn(),probeModels:vi.fn(),listUsers:vi.fn()}))

beforeEach(()=>{useAuth().user.value={id:'admin',username:'admin',display_name:'Admin',role:'admin',is_active:true}})

const checked_at='2026-11-05T02:00:00Z'
function ready(verified:boolean,failed=false):Readiness {
  return {status:failed?'not_ready':'ready',checked_at,model_inference_verified:verified,components:{
    model_profiles:{status:'ok',profiles:{answer:{status:'ok',verification:'configuration_only'}}},
    ...((verified||failed)?{model_probe:{status:failed?'fail':'ok',checked_at,profiles:{answer:{status:failed?'fail':'ok',elapsed_ms:10,...(failed?{error_type:'HTTPStatusError',http_status:402}:{})}}}}:{}),
  }}
}
describe('actual backend response contracts',()=>{
  it('restores cached model-probe details from ready.components on page entry',async()=>{
    vi.mocked(api.getReadiness).mockResolvedValue(ready(false,true))
    const wrapper=mount(ReadinessView);await flushPromises()
    expect(wrapper.text()).toContain('模型连接验证失败')
    expect(wrapper.text()).toContain('HTTPStatusError · HTTP 402')
  })
  it('refreshes readiness after a successful POST probe so the summary is current',async()=>{
    vi.mocked(api.getReadiness).mockResolvedValueOnce(ready(false)).mockResolvedValueOnce(ready(true))
    vi.mocked(api.probeModels).mockResolvedValue({status:'verified',checked_at,profiles:{answer:{status:'ok',elapsed_ms:10}}})
    const wrapper=mount(ReadinessView);await flushPromises()
    expect(wrapper.text()).toContain('模型推理尚未验证')
    await wrapper.findAll('button').find(button=>button.text()==='验证模型连接')!.trigger('click');await flushPromises()
    expect(api.getReadiness).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('模型推理已验证')
    expect(wrapper.text()).toContain('模型连接验证通过')
    expect(wrapper.text()).not.toContain('模型推理尚未验证')
  })
  it('keeps account form lengths and username alphabet within the ASGI schemas',async()=>{
    vi.mocked(api.listUsers).mockResolvedValue([])
    const wrapper=mount(UsersView);await flushPromises()
    const input=wrapper.findAll('input')
    expect(input[1].attributes('maxlength')).toBe('64')
    expect(input[0].attributes('pattern')).toBe('[A-Za-z0-9_.@-]+')
  })
  it('enforces the server password character, byte and mixed-content constraints',()=>{
    expect(passwordError('ContractPassword123')).toBe('')
    expect(passwordError('letters-only-password')).toContain('字母和数字')
    expect(passwordError('Short1')).not.toBe('')
    expect(passwordError('密'.repeat(24)+'a1')).toContain('72 字节')
  })
})
