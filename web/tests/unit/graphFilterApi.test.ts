import { afterEach, expect, it, vi } from 'vitest'
import { ApiRequestError, previewGraphFilter, previewSavedGraphFilter, deleteGraphFilter } from '../../src/api/client'

afterEach(()=>vi.unstubAllGlobals())
it('preserves structured unsupported-principle explanations from the actual API envelope',async()=>{
  const details={summary:'需要更明确的原则',unapplied_principles:[{index:0,principle:'随便看看',reason:'含义不明确'}],warnings:['未改变图谱'],plan:{version:1,principles:[]}}
  const fetch=vi.fn().mockResolvedValue(new Response(JSON.stringify({error:{code:'GRAPH_FILTER_UNSUPPORTED',message:'原则未应用',details}}),{status:422}))
  vi.stubGlobal('fetch',fetch)
  const error=await previewGraphFilter('kb',{principles:['随便看看'],max_nodes:200,max_edges:400}).catch(error=>error)
  expect(error).toBeInstanceOf(ApiRequestError);expect(error).toMatchObject({status:422,code:'GRAPH_FILTER_UNSUPPORTED',details})
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({principles:['随便看看'],max_nodes:200,max_edges:400})
})
it('recompiles saved personal views through their preview endpoint and handles DELETE 204',async()=>{
  const fetch=vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({summary:'重新理解'}),{status:200})).mockResolvedValueOnce(new Response(null,{status:204}))
  vi.stubGlobal('fetch',fetch)
  await previewSavedGraphFilter('kb','mine',{max_nodes:250,max_edges:500})
  await expect(deleteGraphFilter('kb','mine')).resolves.toBeUndefined()
  expect(fetch.mock.calls[0][0]).toBe('/api/v1/knowledge-bases/kb/knowledge-graph/filters/mine/preview')
  expect(fetch.mock.calls[1][1].method).toBe('DELETE')
})
