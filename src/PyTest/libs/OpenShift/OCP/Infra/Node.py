'''OpenShift Node wrapper with host access and kubelet control.'''
import logging
from typing import Optional

from libs.OpenShift.OCP.CLI.OC import OCcli
from libs.Utils.CmdExec import CmdRes


logs = logging.getLogger(__name__)
_MCO__NS = 'openshift-machine-config-operator'


class Node:
    '''
    Wrapper around an OpenShift `Node` resource.

    Host commands run through the Node's `machine-config-daemon` Pod
    (privileged, host root at `/rootfs`).
    '''

    def __init__(self, name: str, oc: Optional[OCcli] = None) -> None:
        '''
        :param name:
            Node resource name.
        :param oc:
            Optional `OCcli` instance. If `None`, a default instance is
            created.
        '''
        self.name = name
        self.oc = oc or OCcli()

    def RunOnHost(self, cmd: str, cmdTO: int = 60) -> CmdRes:
        '''
        Run a shell command on the Node host via its `machine-config-daemon`
        Pod: `oc exec <pod> -c machine-config-daemon -- chroot /rootfs sh -c`.

        The Node's kubelet must be running.

        :param cmd:     Shell command to run on the host.
        :param cmdTO:   Timeout (in sec.) for the command (default: `60`).

        :return:        The `CmdRes` of the `oc exec` command.

        :raises ValueError:     The `cmdTO` is invalid.
        :raises RuntimeError:   No `machine-config-daemon` Pod on this Node.
        '''
        if (
            not isinstance(cmdTO, int) or
            isinstance(cmdTO, bool) or
            (cmdTO <= 0)
        ): raise ValueError('The `cmdTO` must be a positive integer.')
        found = self.oc.Run(
            'get',
            'Pod',
            n=_MCO__NS,
            l='k8s-app=machine-config-daemon',
            field_selector=f'spec.nodeName={self.name}',
            o='jsonpath={.items[0].metadata.name}',
        )
        if not (found.success and (mcdPod := found.stdout.strip())):
            raise RuntimeError(
                f'No `machine-config-daemon` Pod found on Node `{self.name}`.'
            )
        return self.oc.Run(
            'exec',
            mcdPod,
            '--', 'chroot', '/rootfs', 'sh', '-c', cmd,
            n=_MCO__NS,
            c='machine-config-daemon',
            spPars={'timeout': cmdTO},
        )

    def StopKubelet(self, downMin: int, timeout: str = '10m') -> bool:
        '''
        Stop the Node's kubelet, and arm a host `systemd` timer that starts it
        again after `downMin` minutes.

        The timer is armed before the kubelet stops, so it fires even though
        the Node is unreachable through `oc` meanwhile. The Node must be
        `Ready` first, so that becoming `NotReady` after a started stop
        command is strong evidence of this stop. The `oc debug` session is
        expected to fail by losing its connection once the kubelet stops; a
        session that never started is reported as a failed stop, and a
        command timeout leaves the session state unknown, decided by the
        Node condition wait.

        :param downMin:
            Minutes the kubelet stays stopped.
        :param timeout:
            Maximum time to wait for the Node to become `NotReady`
            (default: `10m`).

        :return:
            `True` if the Node was `Ready`, the `oc debug` session started,
            and the Node becomes `NotReady` within the given time.

        :raises ValueError: The `downMin` is invalid.
        '''
        if (
            not isinstance(downMin, int) or
            isinstance(downMin, bool) or
            (downMin <= 0)
        ): raise ValueError('The `downMin` must be a positive integer.')
        ready = self.oc.Run(
            'get',
            f'Node/{self.name}',
            o='jsonpath={.status.conditions[?(@.type=="Ready")].status}',
        )
        if not (ready.success and (ready.stdout.strip() == 'True')):
            logs.warning('Node `%s` is not Ready before the stop.', self.name)
            return False
        # The `oc debug` session is expected to fail: it loses the
        # connection once the kubelet stops. `oc` itself prints
        # `Starting pod/` once its debug Pod is Running on the Node;
        # without it, the stop command never reached the host, and any Node
        # condition change is unrelated to this stop. That output is only
        # trustworthy when the process exited (`exitStat >= 0`): on a
        # subprocess exception, such as the 90 sec. timeout, `CmdExec.Run`
        # discards the captured output, so the session state stays unknown
        # and the condition wait below decides. The stop command is not
        # idempotent: it arms a host `systemd` timer and stops the kubelet,
        # so a retry by the executor's default would arm a second timer;
        # exactly one attempt is enforced with `tryMax=1`.
        debug = self.oc.Run(
            'debug',
            f'Node/{self.name}',
            '--', 'chroot', '/host', 'sh', '-c',
            (
                f'systemd-run --on-active={downMin}min '
                'systemctl start kubelet && systemctl stop kubelet'
            ),
            tryMax=1,
            spPars={'timeout': 90},
        )
        debugOut = (debug.stdout or '') + (debug.stderr or '')
        if (('Starting pod/' not in debugOut) and (debug.exitStat >= 0)):
            logs.warning(
                'Could not start a debug Pod on Node `%s`.', self.name,
            )
            return False
        if not self.oc.Run(
            'wait',
            f'Node/{self.name}',
            **{'for': 'condition=Ready=Unknown'}, timeout=timeout,
        ).success: return False
        logs.info(
            'Stopped kubelet on Node `%s` for %d min.', self.name, downMin,
        )
        return True

    def WaitReady(self, timeout: str = '10m') -> bool:
        '''
        Wait for the Node to become `Ready`.

        :param timeout: Maximum time to wait (default: `10m`).

        :return:        `True` if the Node is `Ready` within the given time.
        '''
        return self.oc.Run(
            'wait',
            f'Node/{self.name}',
            **{'for': 'condition=Ready'}, timeout=timeout,
        ).success
