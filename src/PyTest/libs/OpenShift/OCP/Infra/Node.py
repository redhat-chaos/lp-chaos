'''OpenShift Node wrapper with host access and kubelet control.'''
import logging
from typing import Optional

from libs.OpenShift.OCP.CLI.OC import OCcli
from libs.Utils.CmdExec import CmdRes


logs = logging.getLogger(__name__)


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

        The Node's kubelet must be running. The `machine-config-daemon` Pod is
        used on purpose: it is a privileged DaemonSet Pod with the host root
        at `/rootfs`, present exactly once on every Node, so no extra Pod is
        spawned per command. This suits short, repeated host commands, e.g.
        FD sampling. The command runs exactly once, without an executor
        retry, so it may be non-idempotent. It is still an operator's
        internal Pod: it can be briefly replaced while the MCO rolls out a
        new machine config, and it is unreachable once the Node's kubelet is
        down. A command that stops the kubelet must run via `oc debug`
        instead (see `TempStopKubelet()`).

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
        nsMCO = 'openshift-machine-config-operator'
        found = self.oc.Run(
            'get',
            'Pod',
            n=nsMCO,
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
            n=nsMCO,
            c='machine-config-daemon',
            tryMax=1,
            spPars={'timeout': cmdTO},
        )

    def TempStopKubelet(self, downMin: int, timeout: str = '10m') -> bool:
        '''
        Stop the Node's kubelet, and arm a host `systemd` timer that starts it
        again after `downMin` minutes.

        The timer is armed before the kubelet stops, so it fires even though
        the Node is unreachable through `oc` meanwhile. The Node must be
        `Ready` first, so that becoming `NotReady` after a started stop
        command is strong evidence of this stop. The stop command runs
        through `oc debug`, not `Node.RunOnHost()`: the ephemeral debug Pod
        is independent of the Node's `machine-config-daemon` Pod. Its
        output proves the command reached the host even though the
        connection breaks when the kubelet stops: `oc` prints `Starting
        pod/` once the debug Pod is created, and `systemd-run` prints
        `Running timer as unit:` once the host timer is armed (`Running as
        unit:` on systemd before v239). The banner is printed before the
        debug container starts, so both markers are required, and without
        them a `NotReady` Node is not attributed to this stop: the result is
        only confirmed when the command's output proves the timer was
        armed, because a stop without its timer would never restart the
        kubelet.

        :param downMin:
            Minutes the kubelet stays stopped.
        :param timeout:
            Maximum time to wait for the Node to become `NotReady`
            (default: `10m`).

        :return:
            `True` if the Node was `Ready`, the stop command provably ran,
            and the Node becomes `NotReady` within the given time; a
            `NotReady` period that already ended before the wait could
            observe it is still detected through a changed `Ready`
            condition transition time. `False` whenever that proof or the
            `NotReady` period is missing: an unconfirmed stop is never
            reported as a confirmed one.

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
        # Read the `Ready` condition transition time before the stop: if the
        # Node recovers before the condition wait below starts, the wait
        # misses the `NotReady` period (a past transition never fires it),
        # but a changed transition time still proves it happened.
        transition = self.oc.Run(
            'get',
            f'Node/{self.name}',
            o=(
                'jsonpath={.status.conditions[?(@.type=="Ready")]'
                '.lastTransitionTime}'
            ),
        )
        transitionBefore = (
            transition.stdout.strip() if transition.success else ''
        )
        # The `oc debug` session is expected to fail: it loses the
        # connection once the kubelet stops. `oc` prints `Starting pod/`
        # once its debug Pod is created, and `systemd-run` prints `Running
        # timer as unit:` once the host timer is armed (`Running as unit:`
        # on systemd before v239); the banner is printed before the debug
        # container starts, so both markers are required to prove the
        # command ran, and without them any Node condition change is
        # unrelated to this stop. The output of an abnormally terminated
        # process (a subprocess exception, such as the 90 sec. timeout) is
        # discarded by `CmdExec.Run`: without the markers the stop stays
        # unconfirmed — a `NotReady` Node alone cannot be attributed to
        # this stop, and a stop without its timer armed would never restart
        # the kubelet. The stop command is not idempotent: it arms a host
        # `systemd` timer and stops the kubelet; exactly one attempt is
        # enforced with `tryMax=1`.
        debug = self.oc.Run(
            'debug',
            f'Node/{self.name}',
            '--', 'chroot', '/host', 'sh', '-c',
            rf'''
systemd-run --on-active={downMin}min systemctl start kubelet &&
systemctl stop kubelet
            '''.strip(),
            tryMax=1,
            spPars={'timeout': 90},
        )
        debugOut = (debug.stdout or '') + (debug.stderr or '')
        timerArmed = (
            ('Running as unit:' in debugOut) or
            ('Running timer as unit:' in debugOut)
        )
        if (debug.exitStat < 0):
            # Deliberate trade-off: the command may have run (kubelet
            # stopped, timer armed) with only its output lost, e.g. when
            # `oc debug` outlives the 90 sec. timeout. Without proof of the
            # armed timer the stop is not confirmed; if the timer was armed,
            # it still restarts the kubelet.
            logs.warning(
                'Stop command result on Node `%s` is unconfirmed: the '
                '`oc debug` output was lost. The kubelet may be stopped; if '
                'the timer was armed, it restarts after %d min.',
                self.name, downMin,
            )
            return False
        if (('Starting pod/' not in debugOut) or (not timerArmed)):
            logs.warning(
                'Could not run the stop command on Node `%s`.', self.name,
            )
            return False
        if not self.oc.Run(
            'wait',
            f'Node/{self.name}',
            **{'for': 'condition=Ready=Unknown'}, timeout=timeout,
        ).success:
            # The wait starts only after the `oc debug` command returns: a
            # `NotReady` period that already ended by then is invisible to
            # it. A changed transition time proves the Node was `NotReady`
            # since the stop started, so the stop is still confirmed.
            if (not transitionBefore): return False
            transition = self.oc.Run(
                'get',
                f'Node/{self.name}',
                o=(
                    'jsonpath={.status.conditions[?(@.type=="Ready")]'
                    '.lastTransitionTime}'
                ),
            )
            if (
                (not transition.success) or
                (transition.stdout.strip() == transitionBefore)
            ): return False
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
