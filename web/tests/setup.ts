import { afterEach, vi } from 'vitest'
import { enableAutoUnmount } from '@vue/test-utils'
enableAutoUnmount(afterEach)
Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
if (!window.requestAnimationFrame) window.requestAnimationFrame = callback => window.setTimeout(() => callback(performance.now()), 0)
if (!window.cancelAnimationFrame) window.cancelAnimationFrame = id => window.clearTimeout(id)
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })
