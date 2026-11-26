import dataclasses
import importlib.util
import os
from types import ModuleType, SimpleNamespace
from unittest import mock

import pytest

import fixtures.ODF as MUT


def LoadModule(
    kubeConfig: str | None,
    expandUser: mock.Mock,
) -> ModuleType:
    '''
    Import a fresh, isolated `fixtures.ODF` module copy.

    The import-time default configuration resolves `kCfg` from the `KUBECONFIG`
    environment variable and, as the fallback, from home-directory expansion.
    Both are controlled here, so the imported defaults never depend on the
    host environment. Every other collaborator stays real: the module body
    performs no other external access.
    '''
    environment = {
        key: value for (key, value) in os.environ.items()
        if (key != 'KUBECONFIG')
    }
    if (kubeConfig is not None): environment['KUBECONFIG'] = kubeConfig
    spec = importlib.util.spec_from_file_location(
        'ODF__controlledImport', MUT.__file__,
    )
    module = importlib.util.module_from_spec(spec)
    with (
        mock.patch.dict(os.environ, environment, clear=True),
        mock.patch('os.path.expanduser', expandUser),
    ):
        spec.loader.exec_module(module)
    return module


@pytest.fixture(name='testBoundaries', autouse=True)
def TestBoundaries(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    namedDict = mock.Mock(
        side_effect=lambda *args, **kwargs: SimpleNamespace(**kwargs),
    )
    cephFactory = mock.Mock()
    ceph = cephFactory.return_value
    ceph.EnableTools.return_value = ('synthetic-sc', 'synthetic-uid')
    ceph.Health.return_value = ('HEALTH_OK', set())
    sysModule = SimpleNamespace(
        exc_info=mock.Mock(return_value=(None, None, None)),
    )
    cephCfg = SimpleNamespace(
        chkHealth=True,
        timeout=SimpleNamespace(toolsReady='7m'),
    )
    final = SimpleNamespace(
        kCfg='/synthetic/config',
        cliCfg=SimpleNamespace(ocCfg={}),
        ns='synthetic-ns',
        fxtInf=SimpleNamespace(cephCfg=cephCfg),
        testPars=SimpleNamespace(),
    )
    defaults = mock.MagicMock()
    defaults.__or__.return_value = final
    logs = mock.Mock()
    monkeypatch.setattr(MUT, 'NamedDict', namedDict)
    monkeypatch.setattr(MUT, 'Ceph', cephFactory)
    monkeypatch.setattr(MUT, 'sys', sysModule)
    monkeypatch.setattr(MUT, 'logs', logs)
    monkeypatch.setattr(MUT, '_DEF__FXT_CFG', defaults)
    return SimpleNamespace(
        namedDict=namedDict, cephFactory=cephFactory, ceph=ceph,
        sysModule=sysModule, cephCfg=cephCfg, final=final,
        defaults=defaults, logs=logs,
    )


def test__DEF__FXT_CFG_UsesNestedCephConfiguration() -> None:
    cfg = LoadModule('/synthetic/env/config', mock.Mock())._DEF__FXT_CFG

    assert (cfg.kCfg == '/synthetic/env/config')
    assert (cfg.cliCfg.ocCfg == {})
    assert (cfg.ns == 'openshift-storage')
    assert (cfg.fxtInf.cephCfg.chkHealth is True)
    assert (cfg.fxtInf.cephCfg.timeout == {'toolsReady': '10m'})
    assert (cfg.testPars == {})


def test__DEF__FXT_CFG_PrefersKubeconfigEnvVariable() -> None:
    expandUser = mock.Mock(return_value='/synthetic/home/.kube/config')
    module = LoadModule('/synthetic/env/config', expandUser)

    assert (module._DEF__FXT_CFG.kCfg == '/synthetic/env/config')
    expandUser.assert_not_called()


@pytest.mark.parametrize('kubeConfig', [None, ''])
def test__DEF__FXT_CFG_FallsBackToExpandedHomePath(
    kubeConfig: str | None,
) -> None:
    expandUser = mock.Mock(return_value='/synthetic/home/.kube/config')
    module = LoadModule(kubeConfig, expandUser)

    assert (module._DEF__FXT_CFG.kCfg == '/synthetic/home/.kube/config')
    expandUser.assert_called_once_with('~/.kube/config')


@pytest.mark.parametrize(
    'field', ['cli', 'ns', 'fxtInf', 'testPars', 'storage'],
)
def test_FxtODF_YieldsFrozenObjectAndRestoresToolbox(
    testBoundaries: SimpleNamespace,
    field: str,
) -> None:
    testBoundaries.final.cliCfg.ocCfg = {
        'context': 'oc-context', 'kubeconfig': '/ignored',
    }
    request = SimpleNamespace(param={'ns': 'custom-ns'})
    ocFactory = mock.Mock()
    generator = MUT.FxtODF.__wrapped__(request, ocFactory)
    try:
        context = next(generator)
        assert isinstance(context, MUT.FxtObjODF)
        assert (
            {item.name for item in dataclasses.fields(context)} ==
            {'cli', 'ns', 'fxtInf', 'testPars', 'storage'}
        )
        assert isinstance(context.storage, MUT.ODFstorage)
        assert (context.storage.ceph is testBoundaries.ceph)
        assert (context.fxtInf is testBoundaries.final.fxtInf)
        assert (context.testPars is testBoundaries.final.testPars)
        assert (context.cli.oc is ocFactory.return_value)
        assert (context.ns == 'synthetic-ns')
        testBoundaries.namedDict.assert_any_call(ns='custom-ns')
        testBoundaries.defaults.__or__.assert_called_once()
        ocFactory.assert_called_once_with(
            ns='synthetic-ns', context='oc-context',
            kubeconfig='/synthetic/config',
        )
        testBoundaries.cephFactory.assert_called_once_with(
            'synthetic-ns', oc=ocFactory.return_value,
        )
        testBoundaries.ceph.EnableTools.assert_called_once_with(timeout='7m')
        testBoundaries.ceph.Health.assert_called_once_with()
        testBoundaries.ceph.DisableTools.assert_not_called()
        original = getattr(context, field)
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(context, field, object())
        assert (getattr(context, field) is original)
        with pytest.raises(StopIteration):
            next(generator)
        testBoundaries.ceph.DisableTools.assert_called_once_with(
            ('synthetic-sc', 'synthetic-uid'),
        )
        testBoundaries.logs.info.assert_called_once_with(
            'The `fxtODF` fixture teardown complete.',
        )
    finally:
        generator.close()


def test_FxtODF_YieldsTheClusterStorageAndKeepsDiscoveryLazy(
    testBoundaries: SimpleNamespace,
) -> None:
    ocFactory = mock.Mock()
    generator = MUT.FxtODF.__wrapped__(SimpleNamespace(), ocFactory)
    try:
        context = next(generator)
        # The storage holds the fixture's `Ceph` instance, so its health and
        # MDS operations are the cluster's own.
        assert isinstance(context.storage, MUT.ODFstorage)
        assert (context.storage.ceph is testBoundaries.ceph)
        # Lazy: the fixture setup queries neither the MDS states nor the ODF
        # file system.
        testBoundaries.ceph.GetMDSstates.assert_not_called()
        testBoundaries.ceph.GetODFfsName.assert_not_called()
    finally:
        generator.close()


@pytest.mark.parametrize('hasRequestParam', [False, True])
def test_FxtODF_UsesDefaultConfigurationWithAbsentOrEmptyRequestParam(
    testBoundaries: SimpleNamespace,
    hasRequestParam: bool,
) -> None:
    request = SimpleNamespace(param={}) if hasRequestParam else (
        SimpleNamespace()
    )
    ocFactory = mock.Mock()
    generator = MUT.FxtODF.__wrapped__(request, ocFactory)
    try:
        next(generator)
        assert (testBoundaries.namedDict.call_args_list == [
            mock.call(),
            mock.call(oc=ocFactory.return_value),
        ])
        ocFactory.assert_called_once_with(
            ns='synthetic-ns', kubeconfig='/synthetic/config',
        )
    finally:
        generator.close()


def test_FxtODF_SkipsHealthCheckWhenDisabled(
    testBoundaries: SimpleNamespace,
) -> None:
    testBoundaries.cephCfg.chkHealth = False
    testBoundaries.ceph.Health.return_value = ('HEALTH_ERR', {'X'})
    generator = MUT.FxtODF.__wrapped__(SimpleNamespace(), mock.Mock())
    try:
        assert (next(generator).storage.ceph is testBoundaries.ceph)
        testBoundaries.ceph.Health.assert_not_called()
    finally:
        generator.close()


@pytest.mark.parametrize('toolsOwner', [None, ('synthetic-sc', 'synthetic-uid')])
def test_FxtODF_RaisesWhenUnhealthyAndRestoresToolbox(
    testBoundaries: SimpleNamespace,
    toolsOwner: object,
) -> None:
    testBoundaries.ceph.EnableTools.return_value = toolsOwner
    testBoundaries.ceph.Health.return_value = (
        'HEALTH_WARN', {'MDS_SLOW_METADATA_IO', 'FS_DEGRADED'},
    )
    error = RuntimeError('primary')
    testBoundaries.sysModule.exc_info.return_value = (
        RuntimeError, error, None,
    )
    generator = MUT.FxtODF.__wrapped__(SimpleNamespace(), mock.Mock())

    with pytest.raises(
        RuntimeError,
        match=(
            r"Ceph is not `HEALTH_OK`: `HEALTH_WARN` "
            r"\['FS_DEGRADED', 'MDS_SLOW_METADATA_IO'\]."
        ),
    ): next(generator)
    testBoundaries.ceph.DisableTools.assert_called_once_with(toolsOwner)


def test_FxtODF_RaisesWhenToolboxUnavailable(
    testBoundaries: SimpleNamespace,
) -> None:
    error = RuntimeError('toolbox unavailable')
    testBoundaries.ceph.EnableTools.side_effect = error
    testBoundaries.sysModule.exc_info.return_value = (
        RuntimeError, error, None,
    )
    generator = MUT.FxtODF.__wrapped__(SimpleNamespace(), mock.Mock())

    with pytest.raises(RuntimeError) as raised: next(generator)
    assert (raised.value is error)
    testBoundaries.ceph.Health.assert_not_called()
    testBoundaries.ceph.DisableTools.assert_called_once_with(None)


def test_FxtODF_RaisesTeardownErrorWithoutPrimaryException(
    testBoundaries: SimpleNamespace,
) -> None:
    error = RuntimeError('patch failed')
    testBoundaries.ceph.DisableTools.side_effect = error
    generator = MUT.FxtODF.__wrapped__(SimpleNamespace(), mock.Mock())
    next(generator)

    with pytest.raises(RuntimeError) as raised: next(generator)
    assert (raised.value is error)
    testBoundaries.logs.error.assert_called_once_with(
        'Could not disable Ceph toolbox: %s', error,
    )
    testBoundaries.logs.info.assert_not_called()


def test_FxtODF_PreservesPrimaryExceptionOverTeardownError(
    testBoundaries: SimpleNamespace,
) -> None:
    testBoundaries.ceph.DisableTools.side_effect = RuntimeError('patch')
    primary = AssertionError('test failed')
    testBoundaries.sysModule.exc_info.return_value = (
        AssertionError, primary, None,
    )
    generator = MUT.FxtODF.__wrapped__(SimpleNamespace(), mock.Mock())
    next(generator)

    with pytest.raises(AssertionError) as raised: generator.throw(primary)
    assert (raised.value is primary)
    testBoundaries.logs.error.assert_called_once()
    testBoundaries.logs.info.assert_called_once_with(
        'The `fxtODF` fixture teardown complete.',
    )
