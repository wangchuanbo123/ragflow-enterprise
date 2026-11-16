"""Provider wire contracts via official SDK and MockTransport; no model calls."""
import json

import httpx
import pytest

from rag.providers import llm, embedding


def chat_provider(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = llm.OpenAICompatibleLLMProvider('test-model', 'secret', 'https://models.invalid/v1')
    provider._model = llm.ChatOpenAI(model='test-model', api_key='secret', base_url=provider.base_url,
                                   max_retries=0, http_client=client)
    return provider, client


def test_openai_generate_uses_official_wire_contract_and_retains_usage(monkeypatch):
    usage=[]
    monkeypatch.setattr(llm,'record_usage',usage.append)
    def handler(request):
        assert request.url.path=='/v1/chat/completions'
        assert request.headers['authorization']=='Bearer secret'
        assert json.loads(request.content)=={'model':'test-model','messages':[{'role':'user','content':'问题'}],'temperature':0}
        return httpx.Response(200,json={'id':'reply','object':'chat.completion','created':1,'model':'test-model',
            'choices':[{'index':0,'message':{'role':'assistant','content':'有证据的答案'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':12,'completion_tokens':7,'total_tokens':19}})
    provider,client=chat_provider(handler)
    try:
        assert provider.generate('问题')=='有证据的答案'
        assert usage[-1]['prompt_tokens']==12 and usage[-1]['completion_tokens']==7
        assert provider.get_model().invoke('问题').content=='有证据的答案'
    finally:
        client.close()


def test_openai_stream_delivers_incremental_text_and_final_usage(monkeypatch):
    usage=[]
    monkeypatch.setattr(llm,'record_usage',usage.append)
    def handler(request):
        assert json.loads(request.content)['stream_options']=={'include_usage':True}
        frames=[{'id':'r','choices':[{'index':0,'delta':{'content':'一'},'finish_reason':None}]},
                {'id':'r','choices':[{'index':0,'delta':{'content':'二'},'finish_reason':None}]},
                {'id':'r','choices':[{'index':0,'delta':{},'finish_reason':'stop'}]},
                {'id':'r','choices':[],'usage':{'prompt_tokens':2,'completion_tokens':3,'total_tokens':5}}]
        return httpx.Response(200,headers={'content-type':'text/event-stream'},text=''.join('data: '+json.dumps(frame)+'\n\n' for frame in frames)+'data: [DONE]\n\n')
    provider,client=chat_provider(handler)
    try:
        assert list(provider.stream('问题'))==['一','二']
        assert usage[-1]['total_tokens']==5
    finally:
        client.close()


def test_openai_stream_close_releases_upstream_http_stream():
    class TrackingStream(httpx.SyncByteStream):
        closed=False
        def __iter__(self):
            yield b'data: {"id":"r","choices":[{"index":0,"delta":{"content":"first"},"finish_reason":null}]}\n\n'
            yield b'data: {"id":"r","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        def close(self):
            self.closed=True
    upstream=TrackingStream()
    provider,client=chat_provider(lambda _request:httpx.Response(200,headers={'content-type':'text/event-stream'},stream=upstream))
    try:
        stream=provider.stream('question')
        assert next(stream)=='first'
        stream.close()
        assert upstream.closed
    finally:
        client.close()


def test_openai_truncated_generation_is_reported_as_failure():
    provider,client=chat_provider(lambda _request:httpx.Response(200,headers={'content-type':'text/event-stream'},
        text='data: {"id":"r","choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}\n\n'))
    try:
        with pytest.raises(RuntimeError,match='before completion'):
            list(provider.stream('q'))
    finally:
        client.close()


def test_ollama_generate_and_stream_preserve_options_usage_and_cleanup(monkeypatch):
    usage=[]
    monkeypatch.setattr(llm,'record_usage',usage.append)
    def handler(request):
        assert request.url.path=='/api/generate'
        payload=json.loads(request.content)
        assert payload['model']=='local-model' and payload['prompt']=='question' and payload['options']=={'num_predict':32}
        if payload['stream']:
            return httpx.Response(200,text='{"response":"hello","done":false}\n{"response":" world","done":true,"prompt_eval_count":2,"eval_count":4}\n')
        return httpx.Response(200,json={'response':'hello world','done':True,'prompt_eval_count':2,'eval_count':4})
    client=httpx.Client(transport=httpx.MockTransport(handler))
    provider=llm.OllamaLLMProvider('local-model','http://local.invalid')
    provider._model=llm.Ollama(model='local-model',base_url=provider.base_url,http_client=client)
    provider.get_model().num_predict=32
    try:
        assert provider.generate('question')=='hello world'
        assert list(provider.stream('question'))==['hello',' world']
        assert usage[-1]=={'input_tokens':2,'output_tokens':4}
    finally:
        provider.get_model().close()
    assert client.is_closed


def test_ollama_incomplete_ndjson_is_not_silently_accepted():
    client=httpx.Client(transport=httpx.MockTransport(lambda _request:httpx.Response(200,text='{"response":"partial","done":false}\n')))
    model=llm.Ollama(model='local',base_url='http://local.invalid',http_client=client)
    try:
        with pytest.raises(RuntimeError,match='before completion'):
            list(model.stream('question'))
    finally:
        model.close()


def test_openai_embeddings_send_raw_strings_and_restore_response_order():
    def handler(request):
        assert request.url.path=='/v1/embeddings'
        payload=json.loads(request.content)
        assert payload=={'model':'bge-m3','input':['中文原文','代码: a()'],'encoding_format':'float'}
        return httpx.Response(200,json={'data':[{'index':1,'embedding':[0.,1.]},{'index':0,'embedding':[1.,0.]}],'model':'bge-m3','usage':{'prompt_tokens':2,'total_tokens':2}})
    client=httpx.Client(transport=httpx.MockTransport(handler))
    model=embedding.OpenAIEmbeddings(model='bge-m3',api_key='secret',base_url='https://embed.invalid/v1',http_client=client)
    try:
        assert model.embed_documents(['中文原文','代码: a()'])==[[1.,0.],[0.,1.]]
        assert model.embed_documents([])==[]
        assert model.base_url=='https://embed.invalid/v1' and model.dimensions is None
    finally:
        model.close()


@pytest.mark.parametrize('data',[
    [{'index':0,'embedding':[1.,0.]}],
    [{'index':0,'embedding':[1.,0.]},{'index':0,'embedding':[0.,1.]}],
    [{'index':0,'embedding':[]},{'index':1,'embedding':[]}],
    [{'index':0,'embedding':[1.]},{'index':1,'embedding':[0.,1.]}],
])
def test_embeddings_reject_missing_duplicate_empty_or_inconsistent_vectors(data):
    client=httpx.Client(transport=httpx.MockTransport(lambda _request:httpx.Response(200,json={'data':data,'model':'m','usage':{'prompt_tokens':2,'total_tokens':2}})))
    model=embedding.OpenAIEmbeddings(model='m',api_key='secret',base_url='https://embed.invalid/v1',http_client=client)
    try:
        with pytest.raises(ValueError):
            model.embed_documents(['a','b'])
    finally:
        model.close()
