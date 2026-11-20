import dataclasses
from types import SimpleNamespace
from unittest import mock

import pytest

import fixtures.ODF as MUT


SOURCE_DEFAULT_CONFIG = MUT._DEF__FXT_CFG


@pytest.fixture(name='testBoundaries', autouse=True)
def TestBoundaries(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    namedDict = mock.Mock(
        side_effect=lambda *args, **kwargs: SimpleNamespace(**kwargs),
    )
    cephFactory = mock.Mock()
    ceph = cephFactory.return_value
    ceph.EnableTools.return_value = True
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
    mdsFactory = mock.Mock(side_effect=lambda name, ceph: SimpleNamespace(
        name=name, ceph=ceph, Pod=mock.Mock(return_value=('pod', 'node')),
    ))
    nodeFactory = mock.Mock()
    monkeypatch.setattr(MUT, 'NamedDict', namedDict)
    monkeypatch.setattr(MUT, 'Ceph', cephFactory)
    monkeypatch.setattr(MUT, 'sys', sysModule)
    monkeypatch.setattr(MUT, 'logs', logs)
    monkeypatch.setattr(MUT, '_DEF__FXT_CFG', defaults)
    monkeypatch.setattr(MUT, 'MDS', mdsFactory)
    monkeypatch.setattr(MUT, 'Node', nodeFactory)
    return SimpleNamespace(
        namedDict=namedDict, cephFactory=cephFactory, ceph=ceph,
        sysModule=sysModule, cephCfg=cephCfg, final=final,
        defaults=defaults, logs=logs,
        mdsFactory=mdsFactory, nodeFactory=nodeFactory,
    )


def test__DEF__FXT_CFG_UsesNestedCephConfiguration() -> None:
    cfg = SOURCE_DEFAULT_CONFIG

    assert isinstance(cfg.kCfg, str)
    assert (cfg.cliCfg.ocCfg == {})
    assert (cfg.ns == 'openshift-storage')
    assert (cfg.fxtInf.cephCfg.chkHealth is True)
    assert (cfg.fxtInf.cephCfg.timeout == {'toolsReady': '10m'})
    assert (cfg.testPars == {})


@pytest.mark.parametrize(
    'field', ['ceph', 'storage', 'fxtInf', 'testPars', 'cli', 'ns'],
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
            {'ceph', 'storage', 'fxtInf', 'testPars', 'cli', 'ns'}
        )
        assert (context.ceph is testBoundaries.ceph)
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
        testBoundaries.ceph.DisableTools.assert_called_once_with(True)
        testBoundaries.logs.info.assert_called_once_with(
            'The `fxtODF` fixture teardown complete.',
        )
    finally:
        generator.close()


def test_FxtODF_BindsLazyStorageOperationsToTheCephContext(
    testBoundaries: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discoverMDS = mock.Mock()
    monkeypatch.setattr(MUT, 'DiscoverMDS', discoverMDS)
    ocFactory = mock.Mock()
    generator = MUT.FxtODF.__wrapped__(SimpleNamespace(), ocFactory)
    try:
        context = next(generator)
        # Lazy: the fixture setup neither discovers MDS nor queries their
        # states.
        discoverMDS.assert_not_called()
        testBoundaries.ceph.MDSstates.assert_not_called()
        # The bound `Health` is the `Ceph` context's own method.
        assert (context.storage.ceph.Health is testBoundaries.ceph.Health)
        context.storage.ceph.Health()
        assert (testBoundaries.ceph.Health.call_count == 2)
        testBoundaries.ceph.Health.assert_called_with()
        # The bound `Discover` routes to `DiscoverMDS()` with the fixture's
        # Ceph and CLI context, and accepts the file system as an argument.
        discover = context.storage.ceph.MDS.Discover
        discover()
        discoverMDS.assert_called_once_with(
            ceph=testBoundaries.ceph, oc=ocFactory.return_value,
        )
        discover(fsName='fs2')
        discoverMDS.assert_called_with(
            fsName='fs2', ceph=testBoundaries.ceph, oc=ocFactory.return_value,
        )
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
            mock.call(Discover=mock.ANY),
            mock.call(Health=testBoundaries.ceph.Health, MDS=mock.ANY),
            mock.call(ceph=mock.ANY),
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
        assert (next(generator).ceph is testBoundaries.ceph)
        testBoundaries.ceph.Health.assert_not_called()
    finally:
        generator.close()


@pytest.mark.parametrize('toolsOn', [False, True])
def test_FxtODF_RaisesWhenUnhealthyAndRestoresToolbox(
    testBoundaries: SimpleNamespace,
    toolsOn: bool,
) -> None:
    testBoundaries.ceph.EnableTools.return_value = toolsOn
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
    testBoundaries.ceph.DisableTools.assert_called_once_with(toolsOn)


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
    testBoundaries.ceph.DisableTools.assert_called_once_with(False)


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


def MDScontext() -> tuple[mock.Mock, mock.Mock]:
    ceph = mock.Mock()
    ceph.ODFfsName.return_value = 'fs'
    ceph.MDSstates.return_value = {
        'fs-a': 'up:active', 'fs-b': 'up:standby-replay',
    }
    return (ceph, mock.Mock())


def MDSpods(
    testBoundaries: SimpleNamespace,
    pods: dict[str, tuple[str, str]],
) -> None:
    def Make(name: str, ceph: object) -> SimpleNamespace:
        return SimpleNamespace(
            name=name, ceph=ceph, Pod=mock.Mock(return_value=pods[name]),
        )
    testBoundaries.mdsFactory.side_effect = Make


@pytest.mark.parametrize(
    'field', ['fsName', 'active', 'standby', 'activeNode', 'stbyNode'],
)
def test_DiscoverMDS_ReturnsFrozenRolesOfTheODFfileSystem(
    testBoundaries: SimpleNamespace,
    field: str,
) -> None:
    (ceph, oc) = MDScontext()
    MDSpods(testBoundaries, {
        'fs-a': ('mds-a-pod', 'worker-a'),
        'fs-b': ('mds-b-pod', 'worker-b'),
    })
    nodes = {'worker-a': mock.Mock(), 'worker-b': mock.Mock()}
    testBoundaries.nodeFactory.side_effect = lambda name, oc: nodes[name]

    context = MUT.DiscoverMDS(ceph=ceph, oc=oc)
    assert isinstance(context, MUT.MDSdiscovery)
    assert (
        {item.name for item in dataclasses.fields(context)} ==
        {'fsName', 'active', 'standby', 'activeNode', 'stbyNode'}
    )
    assert (context.fsName == 'fs')
    assert (context.active.name == 'fs-a')
    assert (context.standby.name == 'fs-b')
    assert (context.active.ceph is ceph)
    assert (context.activeNode is nodes['worker-a'])
    assert (context.stbyNode is nodes['worker-b'])
    assert (testBoundaries.nodeFactory.call_args_list == [
        mock.call('worker-a', oc=oc),
        mock.call('worker-b', oc=oc),
    ])
    ceph.ODFfsName.assert_called_once_with()
    assert (ceph.MDSstates.call_count == 2)
    ceph.MDSstates.assert_called_with('fs')
    testBoundaries.namedDict.assert_not_called()
    original = getattr(context, field)
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(context, field, object())
    assert (getattr(context, field) is original)


def test_DiscoverMDS_UsesConfiguredFileSystem(
    testBoundaries: SimpleNamespace,
) -> None:
    (ceph, oc) = MDScontext()
    MDSpods(testBoundaries, {
        'fs-a': ('mds-a-pod', 'worker-a'),
        'fs-b': ('mds-b-pod', 'worker-b'),
    })
    testBoundaries.nodeFactory.side_effect = lambda name, oc: mock.Mock()

    context = MUT.DiscoverMDS(fsName='fs2', ceph=ceph, oc=oc)
    assert (context.fsName == 'fs2')
    ceph.ODFfsName.assert_not_called()
    assert (ceph.MDSstates.call_count == 2)
    ceph.MDSstates.assert_called_with('fs2')


def test_DiscoverMDS_RequeriesCurrentRolesOnEveryCall(
    testBoundaries: SimpleNamespace,
) -> None:
    (ceph, oc) = MDScontext()
    ceph.MDSstates.side_effect = [
        {'fs-a': 'up:active', 'fs-b': 'up:standby-replay'},
        {'fs-a': 'up:active', 'fs-b': 'up:standby-replay'},
        {'fs-b': 'up:active', 'fs-a': 'up:standby-replay'},
        {'fs-b': 'up:active', 'fs-a': 'up:standby-replay'},
    ]
    MDSpods(testBoundaries, {
        'fs-a': ('mds-a-pod', 'worker-a'),
        'fs-b': ('mds-b-pod', 'worker-b'),
    })
    testBoundaries.nodeFactory.side_effect = lambda name, oc: mock.Mock()

    first = MUT.DiscoverMDS(ceph=ceph, oc=oc)
    second = MUT.DiscoverMDS(ceph=ceph, oc=oc)
    assert (first.active.name == 'fs-a')
    assert (first.standby.name == 'fs-b')
    assert (second.active.name == 'fs-b')
    assert (second.standby.name == 'fs-a')
    assert (ceph.ODFfsName.call_count == 2)
    assert (ceph.MDSstates.call_count == 4)


def test_DiscoverMDS_RaisesWhenRolesChangeDuringDiscovery(
    testBoundaries: SimpleNamespace,
) -> None:
    (ceph, oc) = MDScontext()
    ceph.MDSstates.side_effect = [
        {'fs-a': 'up:active', 'fs-b': 'up:standby-replay'},
        {'fs-b': 'up:active', 'fs-a': 'up:standby-replay'},
    ]
    MDSpods(testBoundaries, {
        'fs-a': ('mds-a-pod', 'worker-a'),
        'fs-b': ('mds-b-pod', 'worker-b'),
    })
    testBoundaries.nodeFactory.side_effect = lambda name, oc: mock.Mock()

    with pytest.raises(RuntimeError) as raised:
        MUT.DiscoverMDS(ceph=ceph, oc=oc)
    assert (str(raised.value) == (
        'Prerequisites not met: MDS roles of `fs` changed during '
        'discovery; discover again.'
    ))
    assert (ceph.MDSstates.call_count == 2)


def test_DiscoverMDS_RaisesWithoutODFfileSystem(
    testBoundaries: SimpleNamespace,
) -> None:
    (ceph, oc) = MDScontext()
    ceph.ODFfsName.side_effect = RuntimeError('no owned fs')

    with pytest.raises(RuntimeError) as raised:
        MUT.DiscoverMDS(ceph=ceph, oc=oc)
    assert (str(raised.value) == 'Prerequisites not met: no owned fs')
    assert (raised.value.__cause__ is None)
    ceph.MDSstates.assert_not_called()


@pytest.mark.parametrize('states', [
    {},
    {'fs-a': 'up:active'},
    {'fs-b': 'up:standby-replay'},
    {'fs-a': 'up:active', 'fs-b': 'up:standby'},
    {
        'fs-a': 'up:active', 'fs-b': 'up:standby-replay',
        'fs-c': 'up:active',
    },
    {
        'fs-a': 'up:active', 'fs-b': 'up:standby-replay',
        'fs-c': 'up:standby-replay',
    },
])
def test_DiscoverMDS_RaisesWithoutOneActiveAndOneStandbyReplay(
    testBoundaries: SimpleNamespace,
    states: dict[str, str],
) -> None:
    (ceph, oc) = MDScontext()
    ceph.MDSstates.return_value = states

    with pytest.raises(RuntimeError) as raised:
        MUT.DiscoverMDS(ceph=ceph, oc=oc)
    assert (str(raised.value) == (
        'Prerequisites not met: expected one active and one '
        f'standby-replay MDS in `fs`, got: {states}.'
    ))
    testBoundaries.mdsFactory.assert_not_called()


@pytest.mark.parametrize('missing', ['fs-a', 'fs-b'])
def test_DiscoverMDS_RaisesWithoutRunningPod(
    testBoundaries: SimpleNamespace,
    missing: str,
) -> None:
    (ceph, oc) = MDScontext()
    pods = {'fs-a': ('mds-a-pod', 'worker-a'), 'fs-b': ('mds-b-pod', 'w-b')}
    pods[missing] = ('', '')
    MDSpods(testBoundaries, pods)

    with pytest.raises(
        RuntimeError,
        match=f'Prerequisites not met: no Running Pod for MDS `{missing}`.',
    ): MUT.DiscoverMDS(ceph=ceph, oc=oc)
    nodeCalls = 1 if (missing == 'fs-b') else 0
    assert (testBoundaries.nodeFactory.call_count == nodeCalls)
