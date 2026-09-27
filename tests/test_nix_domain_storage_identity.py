"""Root-only updates must select their matching pool generation."""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def storage(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        'storage_identity', ROOT / 'lifecycle/runtime/tools/scripts/nix-domain-storage.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    args = SimpleNamespace(domain='usr-home', source='/closure', artifact='/closure',
        system='/system', root='/new-root', kernel='kernel', pool_root=str(tmp_path),
        mountpoint='/nix/store', verify_mode='incremental', view_backend='erofs-objectmap', device='/dev/xvde', blob_device='/dev/xvdf')
    current = dict(system=args.system, root_source='/old-root', kernel_name=args.kernel,
        source=args.artifact, approval_source=args.source, view_backend=args.view_backend,
        blob_device=args.blob_device, generation=1)
    staged = dict(current, root_source=args.root, generation=2)
    vm = SimpleNamespace(features={'airlock-store-source':args.source,
        'airlock-store-mode':'shared-closure', 'airlock-store-view-backend':args.view_backend,
        'airlock-store-generation':'1'}, volumes={}, is_running=lambda:False)
    monkeypatch.setattr(helper, 'load_qubes', lambda:None)
    monkeypatch.setattr(helper, 'load_domain', lambda *_:(SimpleNamespace(close=lambda:None),vm))
    monkeypatch.setattr(helper, 'shared_volume_needs_mutation', lambda *_:False)
    monkeypatch.setattr(helper, 'feature_admission_for_current_store', lambda *_:current)
    monkeypatch.setattr(helper, 'load_generation_admission', lambda *_:staged)
    monkeypatch.setattr(helper, 'validate_shared_store_frontends', lambda *_:None)
    monkeypatch.setattr(helper, 'run_engine', lambda *_:pytest.fail('must use staged generation'))
    calls=[]
    async def loaded(_qubes, _args, admission):
        calls.append(admission['generation'])
        return admission
    async def restart(needed, operation):
        assert needed
        return await operation()
    monkeypatch.setattr(helper, 'ensure_shared_store_loaded', loaded)
    monkeypatch.setattr(helper, 'with_qubesd_restart', restart)
    path=tmp_path/'staged.json';path.write_text(json.dumps({'usr-home':2}))
    monkeypatch.setenv('AIRLOCK_STAGED_GENERATIONS', str(path))
    return helper,args,current,staged,vm,calls


@pytest.mark.parametrize('old,new', [('/small-root','/large-root'),('/large-root','/small-root')])
def test_root_change_selects_staged_generation(storage, old, new):
    helper,args,current,staged,vm,calls=storage
    current['root_source']=old
    args.root=staged['root_source']=new
    result=asyncio.run(helper.ensure_shared_store(args))
    assert result['root_source']==new
    assert calls==[2]


def test_running_guest_rejects_root_identity_change(storage, capsys):
    helper,args,current,staged,vm,calls=storage
    vm.is_running=lambda:True
    with pytest.raises(SystemExit):
        asyncio.run(helper.ensure_shared_store(args))
    assert 'domain is running' in capsys.readouterr().err
    assert calls==[]


def test_equivalent_new_admission_does_not_rebind_running_guest(storage):
    helper,args,current,staged,vm,calls=storage
    current['root_source']=args.root
    vm.is_running=lambda:True
    result=asyncio.run(helper.ensure_shared_store(args))
    assert result['generation']=='1'
    assert calls==[]


def test_bad_staged_identity_is_checked_before_unchanged_store_return(storage, capsys):
    helper,args,current,staged,vm,calls=storage
    current['root_source']=args.root
    staged['root_source']='/wrong-root'
    with pytest.raises(SystemExit):
        asyncio.run(helper.ensure_shared_store(args))
    assert 'staged generation identity mismatch' in capsys.readouterr().err
    assert calls==[]


def test_without_staging_root_change_does_not_reuse_old_admission(storage, monkeypatch):
    helper,args,current,staged,vm,calls=storage
    monkeypatch.delenv('AIRLOCK_STAGED_GENERATIONS')
    monkeypatch.setattr(helper,'run_engine',lambda *_:staged)
    assert asyncio.run(helper.ensure_shared_store(args))['root_source']==args.root
    assert calls==[2]


@pytest.mark.parametrize('field,value', [('system','/other-system'),('kernel_name','other-kernel')])
def test_boot_identity_changes_require_matching_generation(storage, field, value):
    helper,args,current,staged,vm,calls=storage
    current['root_source']=args.root
    current[field]=value
    asyncio.run(helper.ensure_shared_store(args))
    assert calls==[2]
