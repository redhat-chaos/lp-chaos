import json
from types import SimpleNamespace
from unittest import mock

import pytest

import libs.OpenShift.LP.ODF.Ceph.MDS as MUT
from libs.OpenShift.LP.ODF.Ceph.Ceph import Ceph
from libs.OpenShift.OCP.CLI.OC import OCcli
from libs.OpenShift.OCP.Infra.Node import Node
from libs.Utils.CmdExec import CmdRes


@pytest.fixture(name='mdsDaemon')
def MDSdaemon() -> SimpleNamespace:
    oc = mock.create_autospec(OCcli, instance=True)
    ceph = mock.create_autospec(Ceph, instance=True)
    ceph.oc = oc
    ceph.namespace = 'odf-ns'
    return SimpleNamespace(mds=MUT.MDS('fs1-b', ceph), ceph=ceph, oc=oc)


def CommandResult(success: bool, stdout: str = '', stderr: str = '') -> CmdRes:
    return CmdRes(stdout, stderr, 0 if success else 1, success)


def PodItem(
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
    assert (mdsDaemon.mds.name == 'fs1-b')
    assert (mdsDaemon.mds.ceph is mdsDaemon.ceph)


@pytest.mark.parametrize(('pods', 'exclNode', 'expected'), [
    ([], None, ('', '')),
    ([PodItem('mds-b-1')], None, ('mds-b-1', 'worker-a')),
    ([PodItem('mds-b-1')], 'worker-a', ('', '')),
    (
        [PodItem('mds-b-1'), PodItem('mds-b-2', node='worker-c')],
        'worker-a',
        ('mds-b-2', 'worker-c'),
    ),
    (
        [PodItem('mds-b-1', deleting=True), PodItem('mds-b-2')],
        None,
        ('mds-b-2', 'worker-a'),
    ),
    (
        [PodItem('mds-b-1', phase='Pending'), PodItem('mds-b-2')],
        None,
        ('mds-b-2', 'worker-a'),
    ),
    (
        [PodItem('mds-b-1', node=None), PodItem('mds-b-2')],
        None,
        ('mds-b-2', 'worker-a'),
    ),
    (
        [PodItem('mds-b-1', node=''), PodItem('mds-b-2')],
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
        mdsDaemon.mds.Pod() if (exclNode is None)
        else mdsDaemon.mds.Pod(exclNode=exclNode)
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
    CommandResult(False, json.dumps({'items': [PodItem('mds-b-1')]})),
    CommandResult(True, 'not JSON'),
    CommandResult(True, json.dumps({})),
    CommandResult(True, json.dumps([])),
])
def test_Pod_ReturnsEmptyOnLookupFailure(
    mdsDaemon: SimpleNamespace,
    result: CmdRes,
) -> None:
    mdsDaemon.oc.Run.return_value = result

    assert (mdsDaemon.mds.Pod() == ('', ''))


@pytest.mark.parametrize('cmdTO', [None, 30])
def test_FDstat_ProbesMatchingProcessOnHost(
    mdsDaemon: SimpleNamespace,
    cmdTO: int | None,
) -> None:
    node = mock.create_autospec(Node, instance=True)
    node.RunOnHost.return_value = CommandResult(True, '57416 1024 1024\n')
    result = (
        mdsDaemon.mds.FDstat(node) if (cmdTO is None)
        else mdsDaemon.mds.FDstat(node, cmdTO=cmdTO)
    )

    assert (result == (57416, 1024, 1024))
    node.RunOnHost.assert_called_once_with(
        'for p in $(pgrep -x ceph-mds); do '
        'tr "\\0" "\\n" < "/proc/${p}/cmdline" | '
        'grep -Fqx -e --id=fs1-b -e fs1-b || continue; '
        'fds=$(ls "/proc/${p}/fd") || continue; '
        'lim=$(awk \'/^Max open files/ {print $4}\' '
        '"/proc/${p}/limits"); '
        'tr "\\0" "\\n" < "/proc/${p}/cmdline" | '
        'grep -Fqx -e --id=fs1-b -e fs1-b || continue; '
        'echo "${p} $(echo ${fds} | wc -w) ${lim}"; '
        'done',
        cmdTO=60 if (cmdTO is None) else cmdTO,
    )


def test_FDstat_QuotesDaemonName(mdsDaemon: SimpleNamespace) -> None:
    node = mock.create_autospec(Node, instance=True)
    node.RunOnHost.return_value = CommandResult(False)
    MUT.MDS("fs'1 b", mdsDaemon.ceph).FDstat(node)

    assert (
        """grep -Fqx -e '--id=fs'"'"'1 b' -e 'fs'"'"'1 b' || continue"""
        in node.RunOnHost.call_args.args[0]
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

    assert (mdsDaemon.mds.FDstat(node) is None)


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
        mdsDaemon.mds.LogCount('mds-b-1', pattern) if (since is None)
        else mdsDaemon.mds.LogCount('mds-b-1', pattern, since=since)
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

    assert (mdsDaemon.mds.LogCount('mds-b-1', 'EMFILE') == -1)


@pytest.mark.parametrize(('stdout', 'expected'), [
    ('failure\n', 1),
    ('failure', 1),
    ('a\nb\n', 2),
    ('', 0),
])
def test_LogCount_CountsOnlyRealLines(
    mdsDaemon: SimpleNamespace,
    stdout: str,
    expected: int,
) -> None:
    mdsDaemon.oc.Run.return_value = CommandResult(True, stdout)

    assert (mdsDaemon.mds.LogCount('mds-b-1', '.*') == expected)
