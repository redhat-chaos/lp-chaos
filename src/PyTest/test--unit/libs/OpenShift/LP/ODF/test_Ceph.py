import dataclasses
import json
from types import SimpleNamespace
from unittest import mock

import pytest

import libs.OpenShift.LP.ODF.Ceph as MUT
from libs.OpenShift.OCP.CLI.OC import OCcli
from libs.OpenShift.OCP.Infra.Node import Node
from libs.Utils.CmdExec import CmdRes
import Utils


@pytest.fixture(name='cephBoundaries', autouse=True)
def CephBoundaries(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    ocFactory = mock.Mock()
    monkeypatch.setattr(MUT, 'OCcli', ocFactory)
    return SimpleNamespace(ocFactory=ocFactory)


@pytest.fixture(name='cephCluster')
def CephCluster() -> SimpleNamespace:
    oc = mock.create_autospec(OCcli, instance=True)
    return SimpleNamespace(ceph=MUT.Ceph('odf-ns', oc=oc), oc=oc)


def CommandResult(
    success: bool,
    stdout: str | None = '',
    stderr: str = '',
) -> CmdRes:
    return CmdRes(stdout, stderr, 0 if success else 1, success)


def StgClusterResult(spec: dict | None = None) -> CmdRes:
    stgCluster = {
        'metadata': {'name': 'ocs-storagecluster', 'uid': 'sc-uid-1'},
    }
    if (spec is not None): stgCluster['spec'] = spec
    return CommandResult(True, json.dumps({'items': [stgCluster]}))


def PodItem(
    name: str,
    node: str | None = 'worker-a',
    ready: str | None = 'True',
    deleting: bool = False,
) -> dict:
    pod = {'metadata': {'name': name}}
    if deleting: pod['metadata']['deletionTimestamp'] = '2026-11-18T00:00:00Z'
    if (node is not None): pod['spec'] = {'nodeName': node}
    if (ready is not None):
        pod['status'] = {'conditions': [
            {'type': 'PodScheduled', 'status': 'True'},
            {'type': 'Ready', 'status': ready},
        ]}
    return pod


def NodeItem(name: str, ready: str | None = 'True') -> dict:
    node = {'metadata': {'name': name}}
    if (ready is not None):
        node['status'] = {'conditions': [
            {'type': 'Ready', 'status': ready},
        ]}
    return node


def test_Init_UsesSuppliedClient(
    cephCluster: SimpleNamespace,
    cephBoundaries: SimpleNamespace,
) -> None:
    assert (cephCluster.ceph.namespace == 'odf-ns')
    assert (cephCluster.ceph.oc is cephCluster.oc)
    cephBoundaries.ocFactory.assert_not_called()


def test_Init_CreatesDefaultClient(cephBoundaries: SimpleNamespace) -> None:
    ceph = MUT.Ceph()

    assert (ceph.namespace == 'openshift-storage')
    cephBoundaries.ocFactory.assert_called_once_with(n='openshift-storage')
    assert (ceph.oc is cephBoundaries.ocFactory.return_value)


def test_Init_BindsTheMDSlayer(cephCluster: SimpleNamespace) -> None:
    assert (isinstance(cephCluster.ceph.MDS, MUT.MDS))
    assert (cephCluster.ceph.MDS.ceph is cephCluster.ceph)


@pytest.mark.parametrize(('spec', 'expected'), [
    (None, False),
    ({}, False),
    ({'enableCephTools': False}, False),
    ({'enableCephTools': 'true'}, False),
    ({'enableCephTools': True}, True),
])
def test__StorageCluster_ReturnsNameAndToolsState(
    cephCluster: SimpleNamespace,
    spec: dict | None,
    expected: bool,
) -> None:
    cephCluster.oc.Run.return_value = StgClusterResult(spec)

    assert (
        cephCluster.ceph._StorageCluster() ==
        ('ocs-storagecluster', 'sc-uid-1', expected)
    )
    cephCluster.oc.Run.assert_called_once_with(
        'get',
        'StorageCluster',
        n='odf-ns',
        o='json',
    )


@pytest.mark.parametrize('result', [
    CommandResult(False, json.dumps({'items': [{'metadata': {'name': 'x'}}]})),
    CommandResult(True, 'not JSON'),
    CommandResult(True, json.dumps({})),
    CommandResult(True, json.dumps({'items': []})),
    CommandResult(True, json.dumps({'items': [{}]})),
    CommandResult(True, json.dumps({'items': None})),
])
def test__StorageCluster_RaisesWithoutStorageCluster(
    cephCluster: SimpleNamespace,
    result: CmdRes,
) -> None:
    cephCluster.oc.Run.return_value = result

    with pytest.raises(
        RuntimeError,
        match='No `StorageCluster` found in Namespace `odf-ns`.',
    ): cephCluster.ceph._StorageCluster()


@pytest.mark.parametrize('enabled', [False, True])
def test__SetToolsEnabled_PatchesStorageCluster(
    cephCluster: SimpleNamespace,
    enabled: bool,
) -> None:
    cephCluster.oc.Run.return_value = CommandResult(True)

    cephCluster.ceph._SetToolsEnabled('ocs-storagecluster', 'sc-uid-1', enabled)

    cephCluster.oc.Run.assert_called_once_with(
        'patch',
        'StorageCluster/ocs-storagecluster',
        n='odf-ns',
        type='json',
        p=json.dumps([
            {'op': 'test', 'path': '/metadata/uid', 'value': 'sc-uid-1'},
            {'op': 'add', 'path': '/spec/enableCephTools', 'value': enabled},
        ]),
    )


@pytest.mark.parametrize('enabled', [False, True])
def test__SetToolsEnabled_RaisesWhenPatchFails(
    cephCluster: SimpleNamespace,
    enabled: bool,
) -> None:
    cephCluster.oc.Run.return_value = CommandResult(False)

    with pytest.raises(
        RuntimeError,
        match=(
            f'Failed to {"enable" if enabled else "disable"} Ceph toolbox '
            'on `StorageCluster/ocs-storagecluster`.'
        ),
    ): cephCluster.ceph._SetToolsEnabled(
        'ocs-storagecluster', 'sc-uid-1', enabled,
    )


@pytest.mark.parametrize('timeout', [None, '5m'])
def test_EnableTools_PatchesThenWaitsWhenDisabled(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
    timeout: str | None,
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, '_StorageCluster')
    MUT.Ceph._StorageCluster.return_value = (
        'ocs-storagecluster', 'sc-uid-1', False,
    )
    cephCluster.oc.Run.return_value = CommandResult(True)
    caplog.set_level('INFO')
    result = (
        cephCluster.ceph.EnableTools() if (timeout is None)
        else cephCluster.ceph.EnableTools(timeout=timeout)
    )

    assert (result == ('ocs-storagecluster', 'sc-uid-1'))
    expTO = '10m' if (timeout is None) else timeout
    assert (cephCluster.oc.Run.call_args_list == [
        mock.call(
            'patch',
            'StorageCluster/ocs-storagecluster',
            n='odf-ns',
            type='json',
            p=json.dumps([
                {'op': 'test', 'path': '/metadata/uid', 'value': 'sc-uid-1'},
                {'op': 'add', 'path': '/spec/enableCephTools', 'value': True},
            ]),
        ),
        mock.call(
            'wait',
            'Deployment/rook-ceph-tools',
            n='odf-ns',
            **{'for': 'create'}, timeout=expTO,
        ),
        mock.call(
            'wait',
            'Deployment/rook-ceph-tools',
            n='odf-ns',
            **{'for': 'condition=Available'}, timeout=expTO,
        ),
    ])
    assert (
        'Enabled Ceph toolbox on `StorageCluster/ocs-storagecluster`.'
        in caplog.text
    )


def test_EnableTools_OnlyWaitsWhenEnabled(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, '_StorageCluster')
    MUT.Ceph._StorageCluster.return_value = (
        'ocs-storagecluster', 'sc-uid-1', True,
    )
    cephCluster.oc.Run.return_value = CommandResult(True)

    assert (cephCluster.ceph.EnableTools() is None)
    assert ([
        call.args[0] for call in cephCluster.oc.Run.call_args_list
    ] == ['wait', 'wait'])


def test_EnableTools_RaisesWhenPatchFails(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, '_StorageCluster')
    MUT.Ceph._StorageCluster.return_value = (
        'ocs-storagecluster', 'sc-uid-1', False,
    )
    cephCluster.oc.Run.return_value = CommandResult(False)

    with pytest.raises(
        RuntimeError,
        match=(
            'Failed to enable Ceph toolbox on '
            '`StorageCluster/ocs-storagecluster`.'
        ),
    ): cephCluster.ceph.EnableTools()
    cephCluster.oc.Run.assert_called_once()


@pytest.mark.parametrize(('waitResults', 'failedCond'), [
    ([False], 'create'),
    ([True, False], 'condition=Available'),
])
def test_EnableTools_RaisesWithoutDisablingPreEnabledToolbox(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
    waitResults: list[bool],
    failedCond: str,
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, '_StorageCluster')
    Utils.AutoPatch(monkeypatch, MUT.Ceph, 'DisableTools')
    MUT.Ceph._StorageCluster.return_value = (
        'ocs-storagecluster', 'sc-uid-1', True,
    )
    cephCluster.oc.Run.side_effect = [
        CommandResult(success) for success in waitResults
    ]

    with pytest.raises(
        RuntimeError,
        match=(
            'Ceph toolbox is not available within '
            rf'`10m` \(`{failedCond}`\).'
        ),
    ): cephCluster.ceph.EnableTools()
    assert (cephCluster.oc.Run.call_count == len(waitResults))
    MUT.Ceph.DisableTools.assert_not_called()


@pytest.mark.parametrize('waitResults', [[False], [True, False]])
@pytest.mark.parametrize('disableFails', [False, True])
def test_EnableTools_DisablesOwnToolboxBeforeRaising(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
    waitResults: list[bool],
    disableFails: bool,
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, '_StorageCluster')
    Utils.AutoPatch(monkeypatch, MUT.Ceph, 'DisableTools')
    MUT.Ceph._StorageCluster.return_value = (
        'ocs-storagecluster', 'sc-uid-1', False,
    )
    disableError = RuntimeError('patch failed')
    if disableFails: MUT.Ceph.DisableTools.side_effect = disableError
    cephCluster.oc.Run.side_effect = [CommandResult(True)] + [
        CommandResult(success) for success in waitResults
    ]

    with pytest.raises(
        RuntimeError, match='Ceph toolbox is not available within',
    ): cephCluster.ceph.EnableTools()
    MUT.Ceph.DisableTools.assert_called_once_with(
        cephCluster.ceph, ('ocs-storagecluster', 'sc-uid-1'),
    )
    assert (
        ('Could not disable Ceph toolbox: patch failed' in caplog.text) is
        disableFails
    )


@pytest.mark.parametrize('error', [KeyboardInterrupt(), OSError('E2BIG')])
@pytest.mark.parametrize('toolsOn', [False, True])
def test_EnableTools_RollsBackOwnToolboxWhenAWaitRaises(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
    error: BaseException,
    toolsOn: bool,
) -> None:
    # A raise from a wait (e.g. Ctrl-C during a long wait) must still roll
    # back a toolbox this call enabled: the caller never gets an owner.
    Utils.AutoPatch(monkeypatch, MUT.Ceph, '_StorageCluster')
    Utils.AutoPatch(monkeypatch, MUT.Ceph, 'DisableTools')
    MUT.Ceph._StorageCluster.return_value = (
        'ocs-storagecluster', 'sc-uid-1', toolsOn,
    )
    cephCluster.oc.Run.side_effect = (
        [error] if toolsOn else [CommandResult(True), error]
    )

    with pytest.raises(type(error)) as raised: cephCluster.ceph.EnableTools()
    assert (raised.value is error)
    if toolsOn: MUT.Ceph.DisableTools.assert_not_called()
    else:
        MUT.Ceph.DisableTools.assert_called_once_with(
            cephCluster.ceph, ('ocs-storagecluster', 'sc-uid-1'),
        )


@pytest.mark.parametrize('error', [KeyboardInterrupt(), OSError('E2BIG')])
def test_EnableTools_RollsBackWhenTheEnablePatchIsInterrupted(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
    error: BaseException,
) -> None:
    # The patch may have been committed by the API server before the
    # interrupt: its result is unknown, so the rollback must run.
    Utils.AutoPatch(monkeypatch, MUT.Ceph, '_StorageCluster')
    Utils.AutoPatch(monkeypatch, MUT.Ceph, 'DisableTools')
    MUT.Ceph._StorageCluster.return_value = (
        'ocs-storagecluster', 'sc-uid-1', False,
    )
    cephCluster.oc.Run.side_effect = [error]

    with pytest.raises(type(error)) as raised: cephCluster.ceph.EnableTools()
    assert (raised.value is error)
    cephCluster.oc.Run.assert_called_once()
    MUT.Ceph.DisableTools.assert_called_once_with(
        cephCluster.ceph, ('ocs-storagecluster', 'sc-uid-1'),
    )


def test_DisableTools_SkipsWhenAlreadyDisabled(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
) -> None:
    # E.g. a rollback after an enable patch that failed or never landed:
    # there is nothing to undo, so no patch is sent.
    Utils.AutoPatch(monkeypatch, MUT.Ceph, '_StorageCluster')
    MUT.Ceph._StorageCluster.return_value = (
        'ocs-storagecluster', 'sc-uid-1', False,
    )

    cephCluster.ceph.DisableTools(('ocs-storagecluster', 'sc-uid-1'))

    cephCluster.oc.Run.assert_not_called()


def test_DisableTools_SkipsWhenNotEnabledByCaller(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, '_StorageCluster')

    assert (cephCluster.ceph.DisableTools(None) is None)
    MUT.Ceph._StorageCluster.assert_not_called()
    cephCluster.oc.Run.assert_not_called()


@pytest.mark.parametrize(('name', 'uid'), [
    ('ocs-storagecluster', 'recreated-sc-uid'),
    ('other-storagecluster', 'sc-uid-1'),
])
def test_DisableTools_SkipsWhenStorageClusterWasReplaced(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
    name: str,
    uid: str,
) -> None:
    # The cleanup must not disable the toolbox of a `StorageCluster`
    # created after the enable, even when it reuses the name.
    Utils.AutoPatch(monkeypatch, MUT.Ceph, '_StorageCluster')
    MUT.Ceph._StorageCluster.return_value = (name, uid, True)
    cephCluster.oc.Run.return_value = CommandResult(True)
    caplog.set_level('INFO')

    cephCluster.ceph.DisableTools(('ocs-storagecluster', 'sc-uid-1'))

    cephCluster.oc.Run.assert_not_called()
    assert (
        'Not disabling Ceph toolbox: `StorageCluster/'
        f'{name}` was replaced since the enable.' in caplog.text
    )


def test_DisableTools_PatchesWhenEnabledByCaller(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, '_StorageCluster')
    MUT.Ceph._StorageCluster.return_value = (
        'ocs-storagecluster', 'sc-uid-1', True,
    )
    cephCluster.oc.Run.return_value = CommandResult(True)
    caplog.set_level('INFO')
    cephCluster.ceph.DisableTools(('ocs-storagecluster', 'sc-uid-1'))

    cephCluster.oc.Run.assert_called_once_with(
        'patch',
        'StorageCluster/ocs-storagecluster',
        n='odf-ns',
        type='json',
        p=json.dumps([
            {'op': 'test', 'path': '/metadata/uid', 'value': 'sc-uid-1'},
            {'op': 'add', 'path': '/spec/enableCephTools', 'value': False},
        ]),
    )
    assert (
        'Disabled Ceph toolbox on `StorageCluster/ocs-storagecluster`.'
        in caplog.text
    )


def test_DisableTools_RaisesWhenPatchFails(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, '_StorageCluster')
    MUT.Ceph._StorageCluster.return_value = (
        'ocs-storagecluster', 'sc-uid-1', True,
    )
    cephCluster.oc.Run.return_value = CommandResult(False)

    with pytest.raises(
        RuntimeError,
        match=(
            'Failed to disable Ceph toolbox on '
            '`StorageCluster/ocs-storagecluster`.'
        ),
    ): cephCluster.ceph.DisableTools(('ocs-storagecluster', 'sc-uid-1'))


@pytest.mark.parametrize(('pods', 'nodes', 'expected'), [
    ([PodItem('tools-1')], [NodeItem('worker-a')], 'tools-1'),
    (
        [PodItem('tools-1', node='worker-b'), PodItem('tools-2')],
        [NodeItem('worker-b', ready='Unknown'), NodeItem('worker-a')],
        'tools-2',
    ),
    (
        [PodItem('tools-1', deleting=True), PodItem('tools-2')],
        [NodeItem('worker-a')],
        'tools-2',
    ),
    (
        [PodItem('tools-1', ready='False'), PodItem('tools-2')],
        [NodeItem('worker-a')],
        'tools-2',
    ),
    (
        [PodItem('tools-1', ready=None), PodItem('tools-2')],
        [NodeItem('worker-a')],
        'tools-2',
    ),
])
@pytest.mark.parametrize('cmdTO', [None, 30])
def test__ToolsPod_ReturnsFirstReadyPodOnAReadyNode(
    cephCluster: SimpleNamespace,
    pods: list[dict],
    nodes: list[dict],
    expected: str,
    cmdTO: int | None,
) -> None:
    cephCluster.oc.Run.side_effect = [
        CommandResult(True, json.dumps({'items': pods})),
        CommandResult(True, json.dumps({'items': nodes})),
    ]
    result = (
        cephCluster.ceph._ToolsPod() if (cmdTO is None)
        else cephCluster.ceph._ToolsPod(cmdTO=cmdTO)
    )

    assert (result == expected)
    expTO = 60 if (cmdTO is None) else cmdTO
    assert (cephCluster.oc.Run.call_args_list == [
        mock.call(
            'get',
            'Pod',
            n='odf-ns',
            l='app=rook-ceph-tools',
            o='json',
            spPars={'timeout': expTO},
        ),
        mock.call(
            'get',
            'Node',
            o='json',
            spPars={'timeout': expTO},
        ),
    ])


@pytest.mark.parametrize(('pods', 'nodes'), [
    ([PodItem('tools-1')], [NodeItem('worker-a', ready='Unknown')]),
    ([PodItem('tools-1', node='worker-b')], [NodeItem('worker-a')]),
    ([PodItem('tools-1', node=None)], [NodeItem('worker-a')]),
    ([PodItem('tools-1')], [{}]),
    ([PodItem('tools-1')], [{'metadata': {}}]),
])
def test__ToolsPod_ReturnsEmptyWithoutPodOnAReadyNode(
    cephCluster: SimpleNamespace,
    pods: list[dict],
    nodes: list[dict],
) -> None:
    # A Pod on a `NotReady` Node is frozen at its last conditions: it still
    # reports `Ready`, but `oc exec` into it cannot run, so it is skipped.
    cephCluster.oc.Run.side_effect = [
        CommandResult(True, json.dumps({'items': pods})),
        CommandResult(True, json.dumps({'items': nodes})),
    ]

    assert (cephCluster.ceph._ToolsPod() == '')
    assert (len(cephCluster.oc.Run.call_args_list) == 2)


@pytest.mark.parametrize('pods', [
    [],
    [PodItem('tools-1', ready='False')],
    [PodItem('tools-1', deleting=True)],
])
def test__ToolsPod_SkipsNodeLookupWithoutReadyPod(
    cephCluster: SimpleNamespace,
    pods: list[dict],
) -> None:
    cephCluster.oc.Run.return_value = CommandResult(
        True, json.dumps({'items': pods}),
    )

    assert (cephCluster.ceph._ToolsPod() == '')
    cephCluster.oc.Run.assert_called_once_with(
        'get',
        'Pod',
        n='odf-ns',
        l='app=rook-ceph-tools',
        o='json',
        spPars={'timeout': 60},
    )


@pytest.mark.parametrize('result', [
    CommandResult(False, json.dumps({'items': [PodItem('tools-1')]})),
    CommandResult(True, 'not JSON'),
    CommandResult(True, json.dumps({})),
    CommandResult(True, json.dumps([])),
])
def test__ToolsPod_ReturnsEmptyOnPodLookupFailure(
    cephCluster: SimpleNamespace,
    result: CmdRes,
) -> None:
    cephCluster.oc.Run.return_value = result

    assert (cephCluster.ceph._ToolsPod() == '')


@pytest.mark.parametrize('nodesResult', [
    CommandResult(False, json.dumps({'items': [NodeItem('worker-a')]})),
    CommandResult(True, 'not JSON'),
    CommandResult(True, json.dumps({})),
])
def test__ToolsPod_ReturnsEmptyOnNodeLookupFailure(
    cephCluster: SimpleNamespace,
    nodesResult: CmdRes,
) -> None:
    cephCluster.oc.Run.side_effect = [
        CommandResult(True, json.dumps({'items': [PodItem('tools-1')]})),
        nodesResult,
    ]

    assert (cephCluster.ceph._ToolsPod() == '')
    assert (len(cephCluster.oc.Run.call_args_list) == 2)


@pytest.mark.parametrize(('stdout', 'expected'), [
    ('{"status": "HEALTH_OK"}', {'status': 'HEALTH_OK'}),
    ('[1, 2]', [1, 2]),
    ('', None),
    (' \n', None),
    (None, None),
])
@pytest.mark.parametrize('cmdTO', [None, 30])
def test_Run_ExecsCephInToolsPodWithJSONOutput(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
    stdout: str | None,
    expected: object,
    cmdTO: int | None,
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, '_ToolsPod')
    MUT.Ceph._ToolsPod.return_value = 'tools-1'
    cephCluster.oc.Run.return_value = CommandResult(True, stdout)
    result = (
        cephCluster.ceph.Run('fs', 'dump') if (cmdTO is None)
        else cephCluster.ceph.Run('fs', 'dump', cmdTO=cmdTO)
    )

    assert (result == expected)
    MUT.Ceph._ToolsPod.assert_called_once_with(
        cephCluster.ceph, 60 if (cmdTO is None) else cmdTO,
    )
    cephCluster.oc.Run.assert_called_once_with(
        'exec',
        'tools-1',
        '--', 'ceph', 'fs', 'dump', '--format', 'json',
        n='odf-ns',
        spPars={'timeout': 60 if (cmdTO is None) else cmdTO},
    )


@pytest.mark.parametrize('cmdTO', [0, -1, 1.5, '60', True, None])
def test_Run_RejectsInvalidTimeout(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
    cmdTO: object,
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, '_ToolsPod')

    with pytest.raises(
        ValueError, match='The `cmdTO` must be a positive integer.',
    ): cephCluster.ceph.Run('health', cmdTO=cmdTO)
    MUT.Ceph._ToolsPod.assert_not_called()
    cephCluster.oc.Run.assert_not_called()


def test_Run_RaisesWithoutReadyToolsPod(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, '_ToolsPod')
    MUT.Ceph._ToolsPod.return_value = ''

    with pytest.raises(
        RuntimeError, match='No Ready Ceph toolbox Pod found.',
    ): cephCluster.ceph.Run('health')
    cephCluster.oc.Run.assert_not_called()


@pytest.mark.parametrize(('result', 'message'), [
    (CommandResult(False, '{}'), 'Ceph command `mds fail x` failed.'),
    (
        CommandResult(True, 'not JSON'),
        'Ceph command `mds fail x` returned invalid JSON.',
    ),
])
def test_Run_RaisesOnFailureOrInvalidJSON(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
    result: CmdRes,
    message: str,
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, '_ToolsPod')
    MUT.Ceph._ToolsPod.return_value = 'tools-1'
    cephCluster.oc.Run.return_value = result

    with pytest.raises(RuntimeError, match=message):
        cephCluster.ceph.Run('mds', 'fail', 'x')


@pytest.mark.parametrize(('health', 'expected'), [
    ({'status': 'HEALTH_OK'}, ('HEALTH_OK', set())),
    ({'status': 'HEALTH_OK', 'checks': {}}, ('HEALTH_OK', set())),
    (
        {
            'status': 'HEALTH_WARN',
            'checks': {'FS_DEGRADED': {}, 'MDS_SLOW_METADATA_IO': {}},
        },
        ('HEALTH_WARN', {'FS_DEGRADED', 'MDS_SLOW_METADATA_IO'}),
    ),
    (
        {
            'status': 'HEALTH_OK',
            'checks': {
                'AUTH_INSECURE_KEYS_ALLOWED': {'muted': True},
                'AUTH_INSECURE_CLIENT_KEY_TYPE': {'muted': True},
            },
        },
        ('HEALTH_OK', set()),
    ),
    (
        {
            'status': 'HEALTH_WARN',
            'checks': {
                'AUTH_INSECURE_KEYS_ALLOWED': {'muted': True},
                'FS_DEGRADED': {'muted': False},
                'MDS_SLOW_METADATA_IO': {},
            },
        },
        ('HEALTH_WARN', {'FS_DEGRADED', 'MDS_SLOW_METADATA_IO'}),
    ),
])
def test_Health_ReturnsStatusAndUnmutedChecks(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
    health: dict,
    expected: tuple[str, set[str]],
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, 'Run')
    MUT.Ceph.Run.return_value = health

    assert (cephCluster.ceph.Health() == expected)
    MUT.Ceph.Run.assert_called_once_with(cephCluster.ceph, 'health')


@pytest.mark.parametrize('health', [
    None, {}, [], {'checks': {}},
    {'status': None, 'checks': {}},
    {'status': 'HEALTH_WARN', 'checks': {'FS_DEGRADED': None}},
    {'status': 'HEALTH_WARN', 'checks': ['FS_DEGRADED']},
])
def test_Health_RaisesWithoutValidStatusOrChecks(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
    health: object,
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, 'Run')
    MUT.Ceph.Run.return_value = health

    with pytest.raises(
        RuntimeError, match='Ceph health has no valid status or checks.',
    ): cephCluster.ceph.Health()


FS_DUMP = {'filesystems': [
    {'mdsmap': {'fs_name': 'fs1', 'info': {
        '1101': {'gid': 1101, 'name': 'fs1-a', 'state': 'up:active'},
        '1102': {'gid': 1102, 'name': 'fs1-b', 'state': 'up:standby-replay'},
    }}},
    {'mdsmap': {'fs_name': 'fs0', 'info': {}}},
    {'mdsmap': {'fs_name': 'fs2', 'info': {
        '1103': {'gid': 1103, 'name': 'fs2-a', 'state': 'up:replay'},
    }}},
]}


def CephFS(name: str, owners: list[tuple[str, str]] | None) -> dict:
    meta: dict = {'name': name}
    if (owners is not None):
        meta['ownerReferences'] = [
            {'kind': kind, 'name': owner, 'uid': 'sc-uid-1'}
            for (kind, owner) in owners
        ]
    return {'metadata': meta}


def test_GetODFfsName_ReturnsCephFilesystemOwnedByStorageCluster(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, '_StorageCluster')
    MUT.Ceph._StorageCluster.return_value = (
        'ocs-storagecluster', 'sc-uid-1', False,
    )
    cephCluster.oc.Run.return_value = CommandResult(True, json.dumps({
        'items': [
            CephFS('user-fs', None),
            CephFS('other-fs', [('StorageCluster', 'other-sc')]),
            CephFS('cfg-fs', [('ConfigMap', 'ocs-storagecluster')]),
            {'metadata': {'name': 'stale-fs', 'ownerReferences': [
                {
                    'kind': 'StorageCluster', 'name': 'ocs-storagecluster',
                    'uid': 'recreated-sc-old-uid',
                },
            ]}},
            CephFS('ocs-storagecluster-cephfilesystem', [
                ('Other', 'x'), ('StorageCluster', 'ocs-storagecluster'),
            ]),
        ],
    }))

    assert (
        cephCluster.ceph.GetODFfsName() ==
        'ocs-storagecluster-cephfilesystem'
    )
    cephCluster.oc.Run.assert_called_once_with(
        'get',
        'CephFilesystem',
        n='odf-ns',
        o='json',
    )


@pytest.mark.parametrize(('result', 'owned'), [
    (CommandResult(False, json.dumps({'items': []})), None),
    (CommandResult(True, 'not JSON'), None),
    (CommandResult(True, json.dumps({})), None),
    (CommandResult(True, json.dumps({'items': [{}]})), None),
    (CommandResult(True, json.dumps({'items': [
        {'metadata': {'name': 'fs', 'ownerReferences': [None]}},
    ]})), None),
    (CommandResult(True, json.dumps({'items': []})), []),
    (CommandResult(True, json.dumps({'items': [
        CephFS('user-fs', None),
    ]})), []),
    (CommandResult(True, json.dumps({'items': [
        CephFS('fs1', [('StorageCluster', 'ocs-storagecluster')]),
        CephFS('fs2', [('StorageCluster', 'ocs-storagecluster')]),
    ]})), ['fs1', 'fs2']),
])
def test_GetODFfsName_RaisesWithoutExactlyOneOwnedCephFilesystem(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
    result: CmdRes,
    owned: list[str] | None,
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, '_StorageCluster')
    MUT.Ceph._StorageCluster.return_value = (
        'ocs-storagecluster', 'sc-uid-1', False,
    )
    cephCluster.oc.Run.return_value = result

    with pytest.raises(RuntimeError) as raised:
        cephCluster.ceph.GetODFfsName()
    assert (str(raised.value) == (
        'Expected one `CephFilesystem` owned by `StorageCluster` '
        f'`ocs-storagecluster`, got: {owned}.'
    ))


def test__FSmaps_MapsFileSystemNamesToMDSmaps(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, 'Run')
    MUT.Ceph.Run.return_value = FS_DUMP

    assert (sorted(cephCluster.ceph._FSmaps()) == ['fs0', 'fs1', 'fs2'])
    MUT.Ceph.Run.assert_called_once_with(cephCluster.ceph, 'fs', 'dump')


@pytest.mark.parametrize(('fsName', 'states'), [
    ('fs1', {'fs1-a': 'up:active', 'fs1-b': 'up:standby-replay'}),
    ('fs0', {}),
    ('fs2', {'fs2-a': 'up:replay'}),
])
def test_GetMDSstates_MapsDaemonNamesToStatesOfOneFileSystem(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
    fsName: str,
    states: dict[str, str],
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, 'Run')
    MUT.Ceph.Run.return_value = FS_DUMP

    assert (cephCluster.ceph.GetMDSstates(fsName) == states)
    MUT.Ceph.Run.assert_called_once_with(cephCluster.ceph, 'fs', 'dump')


@pytest.mark.parametrize(('fsName', 'mdsMap'), [
    ('fs1', {
        'fs1-a': ('up:active', '1101'),
        'fs1-b': ('up:standby-replay', '1102'),
    }),
    ('fs0', {}),
    ('fs2', {'fs2-a': ('up:replay', '1103')}),
])
def test_GetMDSmap_MapsDaemonNamesToStatesAndInstanceGIDs(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
    fsName: str,
    mdsMap: dict[str, tuple[str, str]],
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, 'Run')
    MUT.Ceph.Run.return_value = FS_DUMP

    assert (cephCluster.ceph.GetMDSmap(fsName) == mdsMap)
    MUT.Ceph.Run.assert_called_once_with(cephCluster.ceph, 'fs', 'dump')


def test_GetMDSmap_UsesTheMapKeyWithoutGidField(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, 'Run')
    MUT.Ceph.Run.return_value = {'filesystems': [
        {'mdsmap': {'fs_name': 'fs1', 'info': {
            'rank-0': {'name': 'fs1-a', 'state': 'up:active'},
        }}},
    ]}

    assert (cephCluster.ceph.GetMDSmap('fs1') == {
        'fs1-a': ('up:active', 'rank-0'),
    })


def test_GetMDSstates_RaisesForUnknownFileSystem(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, 'Run')
    MUT.Ceph.Run.return_value = FS_DUMP

    with pytest.raises(
        RuntimeError, match='No CephFS file system `fs9`.',
    ): cephCluster.ceph.GetMDSstates('fs9')


@pytest.mark.parametrize('fsDump', [
    None,
    {},
    {'filesystems': [{}]},
    {'filesystems': [{'mdsmap': {}}]},
    {'filesystems': [{'mdsmap': {'info': {}}}]},
    {'filesystems': [{'mdsmap': {'fs_name': 'fs1'}}]},
    {'filesystems': [{'mdsmap': {'fs_name': 'fs1', 'info': []}}]},
])
def test__FSmaps_RaisesWithoutMDSmap(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
    fsDump: object,
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, 'Run')
    MUT.Ceph.Run.return_value = fsDump

    with pytest.raises(
        RuntimeError, match='Ceph file system dump has no MDS map.',
    ): cephCluster.ceph._FSmaps()


@pytest.mark.parametrize('info', [
    {'gid_1': {'name': 'fs1-a'}},
    {'gid_1': {'state': 'up:active'}},
    {'gid_1': None},
])
def test_GetMDSstates_RaisesWithInvalidDaemonEntry(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
    info: dict[str, object],
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, 'Run')
    MUT.Ceph.Run.return_value = {'filesystems': [
        {'mdsmap': {'fs_name': 'fs1', 'info': info}},
    ]}

    with pytest.raises(
        RuntimeError, match='Ceph file system dump has no MDS map.',
    ): cephCluster.ceph.GetMDSstates('fs1')


@pytest.fixture(name='mdsLayer')
def MDSlayer(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    daemonFactory = mock.Mock()
    nodeFactory = mock.Mock()
    monkeypatch.setattr(MUT, 'MDSDaemon', daemonFactory)
    monkeypatch.setattr(MUT, 'Node', nodeFactory)
    return SimpleNamespace(
        daemonFactory=daemonFactory, nodeFactory=nodeFactory,
    )


def MDSmap(
    states: dict[str, str],
    gids: dict[str, str] | None = None,
) -> dict[str, tuple[str, str]]:
    '''Build a `GetMDSmap()` result; GIDs default to `gid_<name>`.'''
    gids = gids or {}
    return {
        name: (state, gids.get(name, f'gid_{name}'))
        for (name, state) in states.items()
    }


def MDScontext() -> mock.Mock:
    ceph = mock.Mock()
    ceph.GetODFfsName.return_value = 'fs'
    ceph.GetMDSmap.return_value = MDSmap({
        'fs-a': 'up:active', 'fs-b': 'up:standby-replay',
    })
    return ceph


def MDSdaemons(
    mdsLayer: SimpleNamespace,
    pods: dict[str, tuple[str, str]],
) -> None:
    def Make(name: str, ceph: object) -> SimpleNamespace:
        return SimpleNamespace(
            name=name, ceph=ceph, Pod=mock.Mock(return_value=pods[name]),
        )
    mdsLayer.daemonFactory.side_effect = Make


def test_Init_BindsToTheCephCluster() -> None:
    ceph = mock.Mock()

    layer = MUT.MDS(ceph)

    assert (layer.ceph is ceph)


@pytest.mark.parametrize(
    'field', ['fsName', 'active', 'standby', 'activeNode', 'stbyNode'],
)
def test_Discover_ReturnsFrozenRolesOfTheODFfileSystem(
    mdsLayer: SimpleNamespace,
    field: str,
) -> None:
    ceph = MDScontext()
    MDSdaemons(mdsLayer, {
        'fs-a': ('mds-a-pod', 'worker-a'),
        'fs-b': ('mds-b-pod', 'worker-b'),
    })
    nodes = {'worker-a': mock.Mock(), 'worker-b': mock.Mock()}
    mdsLayer.nodeFactory.side_effect = lambda name, oc: nodes[name]

    context = MUT.MDS(ceph).Discover()
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
    assert (mdsLayer.nodeFactory.call_args_list == [
        mock.call('worker-a', oc=ceph.oc),
        mock.call('worker-b', oc=ceph.oc),
    ])
    ceph.GetODFfsName.assert_called_once_with()
    assert (ceph.GetMDSmap.call_args_list == [
        mock.call('fs'), mock.call('fs'),
    ])
    original = getattr(context, field)
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(context, field, object())
    assert (getattr(context, field) is original)


def test_Discover_UsesConfiguredFileSystem(
    mdsLayer: SimpleNamespace,
) -> None:
    ceph = MDScontext()
    MDSdaemons(mdsLayer, {
        'fs-a': ('mds-a-pod', 'worker-a'),
        'fs-b': ('mds-b-pod', 'worker-b'),
    })
    mdsLayer.nodeFactory.side_effect = lambda name, oc: mock.Mock()

    context = MUT.MDS(ceph).Discover(fsName='fs2')
    assert (context.fsName == 'fs2')
    ceph.GetODFfsName.assert_not_called()
    # Both the discovery read and the revalidation read must use `fs2`.
    assert (ceph.GetMDSmap.call_args_list == [
        mock.call('fs2'), mock.call('fs2'),
    ])


def test_Discover_RequeriesCurrentRolesOnEveryCall(
    mdsLayer: SimpleNamespace,
) -> None:
    ceph = MDScontext()
    ceph.GetMDSmap.side_effect = [
        MDSmap({'fs-a': 'up:active', 'fs-b': 'up:standby-replay'}),
        MDSmap({'fs-a': 'up:active', 'fs-b': 'up:standby-replay'}),
        MDSmap({'fs-b': 'up:active', 'fs-a': 'up:standby-replay'}),
        MDSmap({'fs-b': 'up:active', 'fs-a': 'up:standby-replay'}),
    ]
    MDSdaemons(mdsLayer, {
        'fs-a': ('mds-a-pod', 'worker-a'),
        'fs-b': ('mds-b-pod', 'worker-b'),
    })
    mdsLayer.nodeFactory.side_effect = lambda name, oc: mock.Mock()

    # One layer, two discoveries: the second must see the new roles even if
    # the first result were cached on the instance.
    layer = MUT.MDS(ceph)
    first = layer.Discover()
    second = layer.Discover()
    assert (first.active.name == 'fs-a')
    assert (first.standby.name == 'fs-b')
    assert (second.active.name == 'fs-b')
    assert (second.standby.name == 'fs-a')
    assert (ceph.GetODFfsName.call_count == 2)
    assert (ceph.GetMDSmap.call_count == 4)


def test_Discover_RaisesWhenDaemonPodChangesDuringDiscovery(
    mdsLayer: SimpleNamespace,
) -> None:
    # A daemon that restarted into an identical name-to-state map still has
    # a different Pod: the revalidation re-reads the selected Pods as well.
    ceph = MDScontext()

    def Make(name: str, ceph: object) -> SimpleNamespace:
        podLookups = {
            'fs-a': [('mds-a-pod', 'worker-a'), ('mds-a-pod2', 'worker-c')],
            'fs-b': [('mds-b-pod', 'worker-b'), ('mds-b-pod', 'worker-b')],
        }[name]
        return SimpleNamespace(
            name=name, ceph=ceph, Pod=mock.Mock(side_effect=podLookups),
        )
    mdsLayer.daemonFactory.side_effect = Make
    mdsLayer.nodeFactory.side_effect = lambda name, oc: mock.Mock()

    with pytest.raises(RuntimeError) as raised:
        MUT.MDS(ceph).Discover()
    assert (str(raised.value) == (
        'Prerequisites not met: the Pod of MDS `fs-a` changed during '
        'discovery; discover again.'
    ))


@pytest.mark.parametrize('finalMap', [
    # The roles switched.
    MDSmap({'fs-b': 'up:active', 'fs-a': 'up:standby-replay'}),
    # The standby restarted (e.g. on another Node) and regained the same
    # role before the final read: same name-to-state map, new GID.
    MDSmap(
        {'fs-a': 'up:active', 'fs-b': 'up:standby-replay'},
        gids={'fs-b': 'gid_fs-b-restarted'},
    ),
])
def test_Discover_RaisesWhenRolesOrInstancesChangeDuringDiscovery(
    mdsLayer: SimpleNamespace,
    finalMap: dict[str, tuple[str, str]],
) -> None:
    ceph = MDScontext()
    ceph.GetMDSmap.side_effect = [
        MDSmap({'fs-a': 'up:active', 'fs-b': 'up:standby-replay'}),
        finalMap,
    ]
    MDSdaemons(mdsLayer, {
        'fs-a': ('mds-a-pod', 'worker-a'),
        'fs-b': ('mds-b-pod', 'worker-b'),
    })
    mdsLayer.nodeFactory.side_effect = lambda name, oc: mock.Mock()

    with pytest.raises(RuntimeError) as raised:
        MUT.MDS(ceph).Discover()
    assert (str(raised.value) == (
        'Prerequisites not met: the MDS roles or daemon instances of `fs` '
        'changed during discovery; discover again.'
    ))
    assert (ceph.GetMDSmap.call_count == 2)


def test_Discover_RaisesWithoutODFfileSystem() -> None:
    ceph = MDScontext()
    ceph.GetODFfsName.side_effect = RuntimeError('no owned fs')

    with pytest.raises(RuntimeError) as raised:
        MUT.MDS(ceph).Discover()
    assert (str(raised.value) == 'Prerequisites not met: no owned fs')
    assert (raised.value.__cause__ is None)
    ceph.GetMDSmap.assert_not_called()


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
def test_Discover_RaisesWithoutOneActiveAndOneStandbyReplay(
    mdsLayer: SimpleNamespace,
    states: dict[str, str],
) -> None:
    ceph = MDScontext()
    ceph.GetMDSmap.return_value = MDSmap(states)

    with pytest.raises(RuntimeError) as raised:
        MUT.MDS(ceph).Discover()
    assert (str(raised.value) == (
        'Prerequisites not met: expected one active and one '
        f'standby-replay MDS in `fs`, got: {states}.'
    ))
    mdsLayer.daemonFactory.assert_not_called()


@pytest.mark.parametrize('missing', ['fs-a', 'fs-b'])
def test_Discover_RaisesWithoutRunningPod(
    mdsLayer: SimpleNamespace,
    missing: str,
) -> None:
    ceph = MDScontext()
    pods = {'fs-a': ('mds-a-pod', 'worker-a'), 'fs-b': ('mds-b-pod', 'w-b')}
    pods[missing] = ('', '')
    MDSdaemons(mdsLayer, pods)

    with pytest.raises(
        RuntimeError,
        match=f'Prerequisites not met: no Running Pod for MDS `{missing}`.',
    ): MUT.MDS(ceph).Discover()
    nodeCalls = 1 if (missing == 'fs-b') else 0
    assert (mdsLayer.nodeFactory.call_count == nodeCalls)


def test_Fail_RunsCephMDSfail(caplog: pytest.LogCaptureFixture) -> None:
    ceph = mock.Mock()
    caplog.set_level('INFO')

    assert (MUT.MDS(ceph).Fail('fs1-a') is None)
    ceph.Run.assert_called_once_with('mds', 'fail', 'fs1-a')
    assert ('Sent MDS fail for daemon `fs1-a`.' in caplog.text)


@pytest.fixture(name='mdsDaemon')
def MDSdaemon() -> SimpleNamespace:
    oc = mock.create_autospec(OCcli, instance=True)
    ceph = mock.create_autospec(MUT.Ceph, instance=True)
    ceph.oc = oc
    ceph.namespace = 'odf-ns'
    return SimpleNamespace(
        daemon=MUT.MDSDaemon('fs1-b', ceph), ceph=ceph, oc=oc,
    )


def DaemonPodItem(
    name: str,
    node: str | None = 'worker-a',
    phase: str = 'Running',
    deleting: bool = False,
) -> dict:
    pod = {'metadata': {'name': name}, 'status': {'phase': phase}}
    if deleting: pod['metadata']['deletionTimestamp'] = '2026-11-18T00:00:00Z'
    if (node is not None): pod['spec'] = {'nodeName': node}
    return pod


def test_Init_StoresNameAndCluster(mdsDaemon: SimpleNamespace) -> None:
    assert (mdsDaemon.daemon.name == 'fs1-b')
    assert (mdsDaemon.daemon.ceph is mdsDaemon.ceph)


@pytest.mark.parametrize(('pods', 'exclNode', 'expected'), [
    ([], None, ('', '')),
    ([DaemonPodItem('mds-b-1')], None, ('mds-b-1', 'worker-a')),
    ([DaemonPodItem('mds-b-1')], 'worker-a', ('', '')),
    (
        [DaemonPodItem('mds-b-1'), DaemonPodItem('mds-b-2', node='worker-c')],
        'worker-a',
        ('mds-b-2', 'worker-c'),
    ),
    (
        [DaemonPodItem('mds-b-1', deleting=True), DaemonPodItem('mds-b-2')],
        None,
        ('mds-b-2', 'worker-a'),
    ),
    (
        [DaemonPodItem('mds-b-1', phase='Pending'), DaemonPodItem('mds-b-2')],
        None,
        ('mds-b-2', 'worker-a'),
    ),
    (
        [DaemonPodItem('mds-b-1', node=None), DaemonPodItem('mds-b-2')],
        None,
        ('mds-b-2', 'worker-a'),
    ),
    (
        [DaemonPodItem('mds-b-1', node=''), DaemonPodItem('mds-b-2')],
        None,
        ('mds-b-2', 'worker-a'),
    ),
    ([{'metadata': {'name': 'mds-b-1'}}], None, ('', '')),
])
def test_Pod_ReturnsFirstRunningPodOutsideExcludedNode(
    mdsDaemon: SimpleNamespace,
    pods: list[dict],
    exclNode: str | None,
    expected: tuple[str, str],
) -> None:
    mdsDaemon.oc.Run.return_value = CommandResult(
        True, json.dumps({'items': pods}),
    )
    result = (
        mdsDaemon.daemon.Pod() if (exclNode is None)
        else mdsDaemon.daemon.Pod(exclNode=exclNode)
    )

    assert (result == expected)
    mdsDaemon.oc.Run.assert_called_once_with(
        'get',
        'Pod',
        n='odf-ns',
        l='app=rook-ceph-mds,ceph_daemon_id=fs1-b',
        o='json',
    )


@pytest.mark.parametrize('result', [
    CommandResult(False, json.dumps({'items': [DaemonPodItem('mds-b-1')]})),
    CommandResult(True, 'not JSON'),
    CommandResult(True, json.dumps({})),
    CommandResult(True, json.dumps([])),
])
def test_Pod_ReturnsEmptyOnLookupFailure(
    mdsDaemon: SimpleNamespace,
    result: CmdRes,
) -> None:
    mdsDaemon.oc.Run.return_value = result

    assert (mdsDaemon.daemon.Pod() == ('', ''))


@pytest.mark.parametrize('cmdTO', [None, 30])
def test_FDstat_ProbesMatchingProcessOnHost(
    mdsDaemon: SimpleNamespace,
    cmdTO: int | None,
) -> None:
    node = mock.create_autospec(Node, instance=True)
    node.RunOnHost.return_value = CommandResult(True, '57416 1024 1024\n')
    result = (
        mdsDaemon.daemon.FDstat(node) if (cmdTO is None)
        else mdsDaemon.daemon.FDstat(node, cmdTO=cmdTO)
    )

    assert (result == (57416, 1024, 1024))
    matchCmd = 'grep -Fqx -e --id=fs1-b -e fs1-b'
    assert (node.RunOnHost.call_args == mock.call(
        rf'''
function ProcStartTime () {{
    sed -e 's/.*) //' "/proc/${{1}}/stat" 2>/dev/null |
        awk '{{print $20}}'
}}
for p in $(pgrep -x ceph-mds); do
    {{
        tr '\0' '\n' 0< "/proc/${{p}}/cmdline" 2>/dev/null | {matchCmd}
    }} || continue
    start="$(ProcStartTime "${{p}}")"
    [ -n "${{start}}" ] || continue
    fds="$(
        set -o pipefail
        find "/proc/${{p}}/fd" -mindepth 1 -maxdepth 1 -print0 2>/dev/null |
        tr -dc '\0' |
        wc -c
    )" || continue
    ((fds)) || continue
    lim="$(
        awk '/^Max open files/ {{print $4}}' "/proc/${{p}}/limits" 2>/dev/null
    )" || continue
    [ -n "${{lim}}" ] || continue
    {{
        tr '\0' '\n' 0< "/proc/${{p}}/cmdline" 2>/dev/null | {matchCmd}
    }} || continue
    [ "$(ProcStartTime "${{p}}")" = "${{start}}" ] || continue
    echo "${{p}} ${{fds}} ${{lim}}"
done
        '''.strip(),
        cmdTO=60 if (cmdTO is None) else cmdTO,
    ))


def test_FDstat_ReturnsNoneWhenHostLookupFails(
    mdsDaemon: SimpleNamespace,
) -> None:
    # No `machine-config-daemon` Pod on the Node, e.g. while the MCO
    # replaces it: the sample failed, not the test.
    node = mock.create_autospec(Node, instance=True)
    node.RunOnHost.side_effect = RuntimeError(
        'No `machine-config-daemon` Pod found on Node `worker-a`.'
    )

    assert (mdsDaemon.daemon.FDstat(node) is None)


def test_FDstat_QuotesDaemonName(mdsDaemon: SimpleNamespace) -> None:
    node = mock.create_autospec(Node, instance=True)
    node.RunOnHost.return_value = CommandResult(False)
    MUT.MDSDaemon("fs'1 b", mdsDaemon.ceph).FDstat(node)

    assert (
        """grep -Fqx -e '--id=fs'"'"'1 b' -e 'fs'"'"'1 b'""" in
        node.RunOnHost.call_args.args[0]
    )


@pytest.mark.parametrize('result', [
    CommandResult(False, '7 35 1024\n'),
    CommandResult(True, ''),
    CommandResult(True, None),
    CommandResult(True, ' \n'),
    CommandResult(True, '7 35 1024\n8 36 1024\n'),
    CommandResult(True, '35 1024\n'),
    CommandResult(True, '7 35 1024 9\n'),
    CommandResult(True, '7 x 1024\n'),
])
def test_FDstat_ReturnsNoneWithoutSingleValidProcess(
    mdsDaemon: SimpleNamespace,
    result: CmdRes,
) -> None:
    node = mock.create_autospec(Node, instance=True)
    node.RunOnHost.return_value = result

    assert (mdsDaemon.daemon.FDstat(node) is None)


@pytest.mark.parametrize(('stdout', 'expected'), [
    ('', 0),
    (None, 0),
    ('ok\nok\n', 0),
    (
        'accept: (24) Too many open files\nok\n'
        'AdminSocket: EMFILE\nTOO MANY OPEN FILES\n',
        3,
    ),
    ('EMFILE: (24) Too many open files\n', 1),
])
@pytest.mark.parametrize('since', [None, '30s'])
def test_LogCount_CountsMatchingLinesCaseInsensitive(
    mdsDaemon: SimpleNamespace,
    stdout: str | None,
    expected: int,
    since: str | None,
) -> None:
    mdsDaemon.oc.Run.return_value = CommandResult(True, stdout)
    pattern = 'too many open files|EMFILE'
    result = (
        mdsDaemon.daemon.LogCount('mds-b-1', pattern) if (since is None)
        else mdsDaemon.daemon.LogCount('mds-b-1', pattern, since=since)
    )

    assert (result == expected)
    mdsDaemon.oc.Run.assert_called_once_with(
        'logs',
        'mds-b-1',
        n='odf-ns',
        c='mds',
        since='60s' if (since is None) else since,
        spPars={'timeout': 60},
    )


def test_LogCount_ReturnsMinusOneOnLookupFailure(
    mdsDaemon: SimpleNamespace,
) -> None:
    mdsDaemon.oc.Run.return_value = CommandResult(False, 'EMFILE\n')

    assert (mdsDaemon.daemon.LogCount('mds-b-1', 'EMFILE') == -1)


@pytest.mark.parametrize(('stdout', 'expected'), [
    ('failure\n', 1),
    ('failure', 1),
    ('a\nb\n', 2),
    ('a\nb', 2),
    ('', 0),
    ('\n', 1),
    # One LF-terminated record with a U+2028 inside is still one record.
    ('EMFILE EMFILE\n', 1),
])
def test_LogCount_CountsOnlyRealLines(
    mdsDaemon: SimpleNamespace,
    stdout: str,
    expected: int,
) -> None:
    mdsDaemon.oc.Run.return_value = CommandResult(True, stdout)

    assert (mdsDaemon.daemon.LogCount('mds-b-1', '.*') == expected)
