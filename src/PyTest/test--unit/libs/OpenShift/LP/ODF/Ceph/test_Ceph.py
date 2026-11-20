import json
from types import SimpleNamespace
from unittest import mock

import pytest

import libs.OpenShift.LP.ODF.Ceph.Ceph as MUT
from libs.OpenShift.OCP.CLI.OC import OCcli
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


def CommandResult(success: bool, stdout: str = '', stderr: str = '') -> CmdRes:
    return CmdRes(stdout, stderr, 0 if success else 1, success)


def StgClusterResult(spec: dict | None = None) -> CmdRes:
    stgCluster = {
        'metadata': {'name': 'ocs-storagecluster', 'uid': 'sc-uid-1'},
    }
    if (spec is not None): stgCluster['spec'] = spec
    return CommandResult(True, json.dumps({'items': [stgCluster]}))


def PodItem(
    name: str,
    ready: str | None = 'True',
    deleting: bool = False,
) -> dict:
    pod = {'metadata': {'name': name}}
    if deleting: pod['metadata']['deletionTimestamp'] = '2026-11-18T00:00:00Z'
    if (ready is not None):
        pod['status'] = {'conditions': [
            {'type': 'PodScheduled', 'status': 'True'},
            {'type': 'Ready', 'status': ready},
        ]}
    return pod


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

    assert (result is True)
    expTO = '10m' if (timeout is None) else timeout
    assert (cephCluster.oc.Run.call_args_list == [
        mock.call(
            'patch',
            'StorageCluster/ocs-storagecluster',
            n='odf-ns',
            type='merge',
            p='{"spec": {"enableCephTools": true}}',
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

    assert (cephCluster.ceph.EnableTools() is False)
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
    MUT.Ceph.DisableTools.assert_called_once_with(cephCluster.ceph, True)
    assert (
        ('Could not disable Ceph toolbox: patch failed' in caplog.text) is
        disableFails
    )


def test_DisableTools_SkipsWhenNotEnabledByCaller(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, '_StorageCluster')

    assert (cephCluster.ceph.DisableTools(False) is None)
    MUT.Ceph._StorageCluster.assert_not_called()
    cephCluster.oc.Run.assert_not_called()


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
    cephCluster.ceph.DisableTools(True)

    cephCluster.oc.Run.assert_called_once_with(
        'patch',
        'StorageCluster/ocs-storagecluster',
        n='odf-ns',
        type='merge',
        p='{"spec": {"enableCephTools": false}}',
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
    ): cephCluster.ceph.DisableTools(True)


@pytest.mark.parametrize(('pods', 'expected'), [
    ([], ''),
    ([PodItem('tools-1')], 'tools-1'),
    ([PodItem('tools-1', deleting=True), PodItem('tools-2')], 'tools-2'),
    ([PodItem('tools-1', ready='False'), PodItem('tools-2')], 'tools-2'),
    ([PodItem('tools-1', ready=None), PodItem('tools-2')], 'tools-2'),
    ([PodItem('tools-1', ready='False')], ''),
])
@pytest.mark.parametrize('cmdTO', [None, 30])
def test__ToolsPod_ReturnsFirstReadyPod(
    cephCluster: SimpleNamespace,
    pods: list[dict],
    expected: str,
    cmdTO: int | None,
) -> None:
    cephCluster.oc.Run.return_value = CommandResult(
        True, json.dumps({'items': pods}),
    )
    result = (
        cephCluster.ceph._ToolsPod() if (cmdTO is None)
        else cephCluster.ceph._ToolsPod(cmdTO=cmdTO)
    )

    assert (result == expected)
    cephCluster.oc.Run.assert_called_once_with(
        'get',
        'Pod',
        n='odf-ns',
        l='app=rook-ceph-tools',
        o='json',
        spPars={'timeout': 60 if (cmdTO is None) else cmdTO},
    )


@pytest.mark.parametrize('result', [
    CommandResult(False, json.dumps({'items': [PodItem('tools-1')]})),
    CommandResult(True, 'not JSON'),
    CommandResult(True, json.dumps({})),
    CommandResult(True, json.dumps([])),
])
def test__ToolsPod_ReturnsEmptyOnLookupFailure(
    cephCluster: SimpleNamespace,
    result: CmdRes,
) -> None:
    cephCluster.oc.Run.return_value = result

    assert (cephCluster.ceph._ToolsPod() == '')


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
        'gid_1': {'name': 'fs1-a', 'state': 'up:active'},
        'gid_2': {'name': 'fs1-b', 'state': 'up:standby-replay'},
    }}},
    {'mdsmap': {'fs_name': 'fs0', 'info': {}}},
    {'mdsmap': {'fs_name': 'fs2', 'info': {
        'gid_3': {'name': 'fs2-a', 'state': 'up:replay'},
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


def test_ODFfsName_ReturnsCephFilesystemOwnedByStorageCluster(
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
        cephCluster.ceph.ODFfsName() == 'ocs-storagecluster-cephfilesystem'
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
def test_ODFfsName_RaisesWithoutExactlyOneOwnedCephFilesystem(
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

    with pytest.raises(RuntimeError) as raised: cephCluster.ceph.ODFfsName()
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
def test_MDSstates_MapsDaemonNamesToStatesOfOneFileSystem(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
    fsName: str,
    states: dict[str, str],
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, 'Run')
    MUT.Ceph.Run.return_value = FS_DUMP

    assert (cephCluster.ceph.MDSstates(fsName) == states)
    MUT.Ceph.Run.assert_called_once_with(cephCluster.ceph, 'fs', 'dump')


def test_MDSstates_RaisesForUnknownFileSystem(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, 'Run')
    MUT.Ceph.Run.return_value = FS_DUMP

    with pytest.raises(
        RuntimeError, match='No CephFS file system `fs9`.',
    ): cephCluster.ceph.MDSstates('fs9')


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
def test_MDSstates_RaisesWithInvalidDaemonEntry(
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
    ): cephCluster.ceph.MDSstates('fs1')


def test_FailMDS_RunsCephMDSfail(
    monkeypatch: pytest.MonkeyPatch,
    cephCluster: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    Utils.AutoPatch(monkeypatch, MUT.Ceph, 'Run')
    caplog.set_level('INFO')

    assert (cephCluster.ceph.FailMDS('fs1-a') is None)
    MUT.Ceph.Run.assert_called_once_with(
        cephCluster.ceph, 'mds', 'fail', 'fs1-a',
    )
    assert ('Sent MDS fail for daemon `fs1-a`.' in caplog.text)
