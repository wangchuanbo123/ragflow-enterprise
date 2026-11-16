"""Production entrypoints must not import optional legacy/model frameworks."""
import os
from pathlib import Path
import subprocess
import sys


def test_server_entrypoints_import_with_legacy_and_local_model_packages_blocked(tmp_path):
    root=Path(__file__).resolve().parents[2]
    script=r'''
import importlib, importlib.abc, pathlib, socket, sys
import dotenv
dotenv.load_dotenv=lambda *args,**kwargs:False
forbidden={'langchain','langchain_community','langchain_openai','langchain_experimental','langgraph',
           'unstructured','torch','transformers','sentence_transformers','chromadb'}
class BlockLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname.split('.')[0] in forbidden:
            raise ImportError('Production attempted to import optional package: '+fullname)
sys.meta_path.insert(0,BlockLegacy())
socket.socket.connect=lambda *args,**kwargs:(_ for _ in ()).throw(AssertionError('Import must not open network connections'))
for path in sorted(pathlib.Path('app').rglob('*.py')):
    parts=path.with_suffix('').parts
    name='.'.join(parts[:-1] if parts[-1]=='__init__' else parts)
    importlib.import_module(name)
for name in ['scripts.run_worker','scripts.run_scheduler','scripts.start_server','scripts.server_backup',
             'rag.providers.llm','rag.providers.embedding','rag.loaders.document_loader','rag.runtime.runtime']:
    importlib.import_module(name)
from app.bootstrap.container import get_provider_container
providers=get_provider_container()
assert providers.llm.get_model() is not None
assert providers.embedding.get_model() is not None
assert not any(name.split('.')[0] in forbidden for name in sys.modules)
print('minimal production imports passed')
'''
    env={key:value for key,value in os.environ.items() if key.upper() in {
        'PATH','PATHEXT','SYSTEMROOT','WINDIR','SYSTEMDRIVE','COMSPEC','TEMP','TMP','TMPDIR',
        'USERPROFILE','APPDATA','LOCALAPPDATA','PROGRAMFILES','PROGRAMFILES(X86)','PYTHONIOENCODING'}}
    env.update({'APP_PROFILE':'server','APP_ROLE':'api','APP_ENV':'test',
        'JWT_SECRET_KEY':'synthetic-minimal-runtime-test-key-at-least-32-bytes',
        'INITIAL_ADMIN_PASSWORD':'SyntheticAdminPassword123',
        'DATABASE_URL':'postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/minimal_import_test',
        'REDIS_URL':'redis://127.0.0.1:1/15','REDIS_KEY_PREFIX':'minimal-import-test',
        'DATA_DIR':str(tmp_path),'LOCAL_OBJECT_STORE_DIR':str(tmp_path/'documents'),
        'LLM_PROVIDER':'openai_compatible','LLM_MODEL':'synthetic-test-model',
        'LLM_API_KEY':'synthetic-not-real','LLM_BASE_URL':'https://models.invalid/v1',
        'EMBEDDING_PROVIDER':'openai_compatible','EMBEDDING_MODEL':'synthetic-embedding',
        'EMBEDDING_API_KEY':'synthetic-not-real','EMBEDDING_BASE_URL':'https://embeddings.invalid/v1',
        'RERANKER_PROVIDER':'none','VECTOR_STORE_PROVIDER':'pgvector','GRAPH_AUTO_BUILD_ENABLED':'false',
        'INDEX_WORKER_ENABLED':'false','CONNECTOR_SCHEDULER_ENABLED':'false','BACKUP_MONTHLY_ENABLED':'false'})
    result=subprocess.run([sys.executable,'-c',script],cwd=root,env=env,capture_output=True,text=True,timeout=45)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'minimal production imports passed' in result.stdout
