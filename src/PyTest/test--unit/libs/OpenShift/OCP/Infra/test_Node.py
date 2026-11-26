from types import SimpleNamespace
from unittest import mock

import pytest

import libs.OpenShift.OCP.Infra.Node as MUT
from libs.OpenShift.OCP.CLI.OC import OCcli
from libs.Utils.CmdExec import CmdRes


@pytest.fixture(name='nodeBoundaries', autouse=True)
def NodeBoundaries(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    ocFactory = mock.Mock()
    monkeypatch.setattr(MUT, 'OCcli', ocFactory)
    return SimpleNamespace(ocFactory=ocFactory)


@pytest.fixture(name='workerNode')
def WorkerNode() -> SimpleNamespace:
    oc = mock.create_autospec(OCcli, instance=True)
    return SimpleNamespace(node=MUT.Node('worker-a', oc=oc), oc=oc)


def CommandResult(success: bool, stdout: str = '', stderr: str = '') -> CmdRes:
    return CmdRes(stdout, stderr, 0 if success else 1, success)


def test_Init_UsesSuppliedClient(
    workerNode: SimpleNamespace,
    nodeBoundaries: SimpleNamespace,
) -> None:
    assert (workerNode.node.name == 'worker-a')
    assert (workerNode.node.oc is workerNode.oc)
    nodeBoundaries.ocFactory.assert_not_called()


def test_Init_CreatesDefaultClient(nodeBoundaries: SimpleNamespace) -> None:
    node = MUT.Node('worker-b')

    nodeBoundaries.ocFactory.assert_called_once_with()
    assert (node.oc is nodeBoundaries.ocFactory.return_value)


@pytest.mark.parametrize('cmdTO', [None, 30])
def test_RunOnHost_ExecsThroughMachineConfigDaemon(
    workerNode: SimpleNamespace,
    cmdTO: int | None,
) -> None:
    executed = CommandResult(True, 'host output')
    workerNode.oc.Run.side_effect = [
        CommandResult(True, ' machine-config-daemon-x1\n'),
        executed,
    ]
    result = (
        workerNode.node.RunOnHost('df /var') if (cmdTO is None)
        else workerNode.node.RunOnHost('df /var', cmdTO=cmdTO)
    )

    assert (result is executed)
    assert (workerNode.oc.Run.call_args_list == [
        mock.call(
            'get',
            'Pod',
            n='openshift-machine-config-operator',
            l='k8s-app=machine-config-daemon',
            field_selector='spec.nodeName=worker-a',
            o='jsonpath={.items[0].metadata.name}',
        ),
        mock.call(
            'exec',
            'machine-config-daemon-x1',
            '--', 'chroot', '/rootfs', 'sh', '-c', 'df /var',
            n='openshift-machine-config-operator',
            c='machine-config-daemon',
            tryMax=1,
            spPars={'timeout': 60 if (cmdTO is None) else cmdTO},
        ),
    ])


@pytest.mark.parametrize(('success', 'stdout'), [
    (False, 'machine-config-daemon-x1'),
    (True, ''),
    (True, ' \n'),
])
def test_RunOnHost_RaisesWithoutMachineConfigDaemon(
    workerNode: SimpleNamespace,
    success: bool,
    stdout: str,
) -> None:
    workerNode.oc.Run.return_value = CommandResult(success, stdout)

    with pytest.raises(
        RuntimeError,
        match='No `machine-config-daemon` Pod found on Node `worker-a`.',
    ): workerNode.node.RunOnHost('df /var')
    workerNode.oc.Run.assert_called_once()


@pytest.mark.parametrize('cmdTO', [0, -1, 1.5, '60', True, None])
def test_RunOnHost_RejectsInvalidTimeout(
    workerNode: SimpleNamespace,
    cmdTO: object,
) -> None:
    with pytest.raises(
        ValueError, match='The `cmdTO` must be a positive integer.',
    ): workerNode.node.RunOnHost('df /var', cmdTO=cmdTO)
    workerNode.oc.Run.assert_not_called()


@pytest.mark.parametrize('timerMarker', [
    'Running timer as unit: run-u123.timer',
    'Running as unit: run-r123.service',
])
@pytest.mark.parametrize('timeout', [None, '15m'])
@pytest.mark.parametrize('debugSuccess', [False, True])
def test_TempStopKubelet_ArmsTimerThenWaitsForNotReadyWhenReady(
    workerNode: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
    timerMarker: str,
    timeout: str | None,
    debugSuccess: bool,
) -> None:
    # The `oc debug` exit code is ignored: its stream is expected to break
    # when the kubelet stops; its output must show the created debug Pod and
    # the armed host timer, in either `systemd-run` spelling.
    workerNode.oc.Run.side_effect = [
        CommandResult(True, 'True\n'),
        CommandResult(True, '2026-11-24T10:00:00Z\n'),
        CommandResult(
            debugSuccess, '',
            f'Starting pod/worker-a-debug-abc12 ... {timerMarker} '
            '... connection refused',
        ),
        CommandResult(True),
    ]
    caplog.set_level('INFO')
    result = (
        workerNode.node.TempStopKubelet(40) if (timeout is None)
        else workerNode.node.TempStopKubelet(40, timeout=timeout)
    )

    assert (result is True)
    assert (workerNode.oc.Run.call_args_list == [
        mock.call(
            'get',
            'Node/worker-a',
            o='jsonpath={.status.conditions[?(@.type=="Ready")].status}',
        ),
        mock.call(
            'get',
            'Node/worker-a',
            o=(
                'jsonpath={.status.conditions[?(@.type=="Ready")]'
                '.lastTransitionTime}'
            ),
        ),
        mock.call(
            'debug',
            'Node/worker-a',
            '--', 'chroot', '/host', 'sh', '-c',
            (
                'systemd-run --on-active=40min systemctl start kubelet &&\n'
                'systemctl stop kubelet'
            ),
            tryMax=1,
            spPars={'timeout': 90},
        ),
        mock.call(
            'wait',
            'Node/worker-a',
            **{'for': 'condition=Ready=Unknown'},
            timeout='10m' if (timeout is None) else timeout,
        ),
    ])
    assert ('Stopped kubelet on Node `worker-a` for 40 min.' in caplog.text)


@pytest.mark.parametrize('debugResult', [
    CommandResult(False),
    CommandResult(False, '', 'Error from server: Timeout'),
    CommandResult(True, 'Starting something else'),
    CommandResult(
        True, '',
        'Starting pod/worker-a-debug-abc12 ... container never started',
    ),
])
def test_TempStopKubelet_FailsWhenStopCommandNeverRan(
    workerNode: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
    debugResult: CmdRes,
) -> None:
    # The `Starting pod/` banner is printed before the debug container
    # starts: without the `Running as unit:` timer marker, the stop command
    # never provably reached the host.
    workerNode.oc.Run.side_effect = [
        CommandResult(True, 'True\n'),
        CommandResult(True, '2026-11-24T10:00:00Z\n'),
        debugResult,
    ]

    assert (workerNode.node.TempStopKubelet(40) is False)
    assert (len(workerNode.oc.Run.call_args_list) == 3)
    assert ('Could not run the stop command on Node `worker-a`.' in caplog.text)
    assert ('Stopped kubelet' not in caplog.text)


@pytest.mark.parametrize('exitStat', [-2, -3])
def test_TempStopKubelet_ReturnsFalseWhenStopIsUnconfirmed(
    workerNode: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
    exitStat: int,
) -> None:
    # The output of an abnormally terminated `oc debug` is discarded:
    # without the timer marker there is no proof the restart timer was
    # armed, so a confirmed stop is not reported even if the Node goes
    # `NotReady` for an unrelated reason.
    workerNode.oc.Run.side_effect = [
        CommandResult(True, 'True\n'),
        CommandResult(True, '2026-11-24T10:00:00Z\n'),
        CmdRes(
            '', "Command '['oc']' timed out after 90 seconds",
            exitStat, False,
        ),
        CommandResult(True),
    ]

    assert (workerNode.node.TempStopKubelet(40) is False)
    assert (len(workerNode.oc.Run.call_args_list) == 3)
    assert (
        'Stop command result on Node `worker-a` is unconfirmed: the '
        '`oc debug` output was lost. The kubelet may be stopped; if the '
        'timer was armed, it restarts after 40 min.' in caplog.text
    )
    assert ('Stopped kubelet' not in caplog.text)


@pytest.mark.parametrize(('waitSuccess', 'transitionAfter', 'expected'), [
    (True, '2026-11-24T10:00:00Z\n', True),
    (False, '2026-11-24T10:05:00Z\n', True),
    (False, '2026-11-24T10:00:00Z\n', False),
])
def test_TempStopKubelet_WaitsAfterTheStopCommandRan(
    workerNode: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
    waitSuccess: bool,
    transitionAfter: str,
    expected: bool,
) -> None:
    # The stop command provably ran (banner + timer marker): the Node
    # condition wait decides. A wait that starts after the Node already
    # recovered still detects the outage: the `NotReady` period changed the
    # `Ready` condition transition time read before the stop.
    caplog.set_level('INFO')
    workerNode.oc.Run.side_effect = [
        CommandResult(True, 'True\n'),
        CommandResult(True, '2026-11-24T10:00:00Z\n'),
        CommandResult(
            False, '',
            'Starting pod/worker-a-debug-abc12 ... Running timer as unit: '
            'run-u123.timer ... connection refused',
        ),
        CommandResult(waitSuccess),
        CommandResult(True, transitionAfter),
    ]

    assert (workerNode.node.TempStopKubelet(40) is expected)
    assert (
        len(workerNode.oc.Run.call_args_list) == (4 if waitSuccess else 5)
    )
    assert ('Could not run the stop command' not in caplog.text)
    assert (
        ('Stopped kubelet on Node `worker-a` for 40 min.' in caplog.text) is
        expected
    )


@pytest.mark.parametrize('waitSuccess', [False, True])
def test_TempStopKubelet_ReturnsFalseWithoutTransitionBaseline(
    workerNode: SimpleNamespace,
    waitSuccess: bool,
) -> None:
    # Without the pre-stop transition time, a failed wait cannot fall back
    # to the changed-transition check.
    workerNode.oc.Run.side_effect = [
        CommandResult(True, 'True\n'),
        CommandResult(False, ''),
        CommandResult(
            False, '',
            'Starting pod/worker-a-debug-abc12 ... Running timer as unit: '
            'run-u123.timer ... connection refused',
        ),
        CommandResult(waitSuccess),
    ]

    assert (workerNode.node.TempStopKubelet(40) is waitSuccess)
    assert (len(workerNode.oc.Run.call_args_list) == 4)


def test_TempStopKubelet_ReturnsFalseWhenTransitionRecheckFails(
    workerNode: SimpleNamespace,
) -> None:
    workerNode.oc.Run.side_effect = [
        CommandResult(True, 'True\n'),
        CommandResult(True, '2026-11-24T10:00:00Z\n'),
        CommandResult(
            False, '',
            'Starting pod/worker-a-debug-abc12 ... Running timer as unit: '
            'run-u123.timer ... connection refused',
        ),
        CommandResult(False),
        CommandResult(False, ''),
    ]

    assert (workerNode.node.TempStopKubelet(40) is False)
    assert (len(workerNode.oc.Run.call_args_list) == 5)


@pytest.mark.parametrize(('success', 'stdout'), [
    (False, 'True'),
    (True, 'False'),
    (True, 'Unknown'),
    (True, ''),
])
def test_TempStopKubelet_ReturnsFalseWithoutStopWhenNotReady(
    workerNode: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
    success: bool,
    stdout: str,
) -> None:
    workerNode.oc.Run.return_value = CommandResult(success, stdout)

    assert (workerNode.node.TempStopKubelet(40) is False)
    workerNode.oc.Run.assert_called_once()
    assert ('Node `worker-a` is not Ready before the stop.' in caplog.text)
    assert ('Stopped kubelet' not in caplog.text)


@pytest.mark.parametrize('downMin', [0, -1, 1.5, '40', True, None])
def test_TempStopKubelet_RejectsInvalidDuration(
    workerNode: SimpleNamespace,
    downMin: object,
) -> None:
    with pytest.raises(
        ValueError, match='The `downMin` must be a positive integer.',
    ): workerNode.node.TempStopKubelet(downMin)
    workerNode.oc.Run.assert_not_called()


@pytest.mark.parametrize('timeout', [None, '15m'])
@pytest.mark.parametrize('waitSuccess', [False, True])
def test_WaitReady_WaitsForReadyCondition(
    workerNode: SimpleNamespace,
    timeout: str | None,
    waitSuccess: bool,
) -> None:
    workerNode.oc.Run.return_value = CommandResult(waitSuccess)
    result = (
        workerNode.node.WaitReady() if (timeout is None)
        else workerNode.node.WaitReady(timeout=timeout)
    )

    assert (result is waitSuccess)
    workerNode.oc.Run.assert_called_once_with(
        'wait',
        'Node/worker-a',
        **{'for': 'condition=Ready'},
        timeout='10m' if (timeout is None) else timeout,
    )
