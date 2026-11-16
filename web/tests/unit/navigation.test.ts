import { describe, expect, it } from 'vitest'
import { navigate, navigationState, parseNavigation } from '../../src/composables/useNavigation'
describe('shareable navigation',()=>{
  it('validates page names and preserves encoded user queries',()=>{
    expect(parseNavigation('#page=unexpected').page).toBe('chat')
    expect(parseNavigation('#page=search&kb=a&query=%E4%B8%AD%E6%96%87').query).toBe('中文')
  })
  it('stores only the parameters relevant to the selected page',()=>{
    navigate({page:'chat',kb:'a',conversation:'private',query:'stale'})
    expect(window.location.hash).toContain('conversation=private')
    navigate({page:'search',query:'架构'})
    expect(window.location.hash).not.toContain('conversation=')
    expect(navigationState.value.query).toBe('架构')
  })
  it('preserves a task ID query in a shareable task-center URL',()=>{
    navigate({page:'jobs',kb:'kb',query:'shadow-job'})
    expect(parseNavigation(window.location.hash)).toMatchObject({page:'jobs',kb:'kb',query:'shadow-job'})
    navigate({page:'documents',query:'shadow-job'})
    expect(window.location.hash).not.toContain('query=')
  })
})
