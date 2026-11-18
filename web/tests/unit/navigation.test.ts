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
  it('restores the code governance section from a shareable URL',()=>{
    navigate({page:'graph-management',kb:'kb',graphKind:'code'})
    expect(window.location.hash).toContain('graph=code')
    expect(parseNavigation(window.location.hash)).toMatchObject({page:'graph-management',kb:'kb',graphKind:'code'})
    navigate({graphKind:'document'})
    expect(window.location.hash).not.toContain('graph=')
    expect(navigationState.value.graphKind).toBe('document')
  })
  it('defaults invalid and unrelated graph parameters to document governance',()=>{
    expect(parseNavigation('#page=graph-management&graph=unexpected').graphKind).toBe('document')
    expect(parseNavigation('#page=graph-management').graphKind).toBe('document')
    expect(parseNavigation('#page=search&graph=code').graphKind).toBe('document')
  })
  it('clears the governance section when leaving the page or changing knowledge bases',()=>{
    navigate({page:'graph-management',kb:'a',graphKind:'code'})
    navigate({page:'code-graph'})
    expect(window.location.hash).not.toContain('graph=')
    expect(navigationState.value.graphKind).toBe('document')
    navigate({page:'graph-management'})
    expect(navigationState.value.graphKind).toBe('document')
    navigate({graphKind:'code'})
    navigate({kb:'b'})
    expect(navigationState.value).toMatchObject({kb:'b',graphKind:'document'})
    navigate({kb:'c',graphKind:'code'})
    expect(navigationState.value).toMatchObject({kb:'c',graphKind:'code'})
  })
})
