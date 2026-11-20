import logging
import math
import sys
import time
from typing import TYPE_CHECKING

import pytest

from libs.CustomTypes import NamedDict
from libs.OpenShift.OCP.Infra.Node import Node


if TYPE_CHECKING:
    from fixtures.ODF import FxtObjODF, MDSdiscovery


logs = logging.getLogger()


def _ObserveReplacementMDS(
    fxtODF: 'FxtObjODF',
    mds: 'MDSdiscovery',
    upAt: float,
) -> list[str]:
    '''
    Observe the replacement standby-replay MDS while the kubelet is stopped.

    :param fxtODF:  The `fxtODF` fixture object.
    :param mds:     The `Discover()` result of the test.
    :param upAt:    The `time.monotonic()` value when the kubelet is back.

    :return:        The FD leak and `EMFILE` findings.
    '''
    (oc, pars) = (fxtODF.cli.oc, fxtODF.testPars)
    (standby, stbyNode) = (mds.standby, mds.stbyNode)
    findings = []

    # Observe the replacement MDS until the FD limit is hit and `EMFILE`
    # is logged, or until the kubelet is back. FDs are tracked per
    # process (PID): a restarted process starts a new count, and the FD
    # limit check uses the current process only. The first log read covers
    # the time since the observation start, each later read the time since
    # the last successful read.
    (fdTrk, emfile, limAt) = ({}, 0, None)
    (podSeen, logRead, logAt) = (False, False, time.monotonic())
    while (time.monotonic() < upAt):
        time.sleep(pars.pollInt)
        (pod, nodeName) = standby.Pod(exclNode=stbyNode.name)
        if not pod: continue
        podSeen = True
        node = Node(nodeName, oc=oc)
        try: fdStat = standby.FDstat(node)
        except RuntimeError as error:
            logs.warning('Could not read MDS FDs: %s', error)
            fdStat = None
        if (fdStat is not None):
            (pid, fdCount, fdLim) = fdStat
            (fdFirst, fdMax, _) = fdTrk.get(pid, (fdCount, fdCount, fdLim))
            fdTrk[pid] = (fdFirst, max(fdMax, fdCount), fdLim)
        readAt = time.monotonic()
        count = standby.LogCount(
            pod,
            'too many open files|EMFILE',
            since=f'{math.ceil(readAt - logAt)}s',
        )
        if (count >= 0):
            (emfile, logRead, logAt) = (emfile + count, True, readAt)
        logs.info(
            'MDS `%s` Pod `%s`: FDs %s, EMFILE lines %d.',
            standby.name, pod,
            f'{fdCount}/{fdLim}' if (fdStat is not None) else 'unknown',
            emfile,
        )
        try:
            var = node.RunOnHost("df -P /var | awk 'NR == 2 {print $5}'")
            varPct = var.stdout.strip().rstrip('%') if var.success else ''
        except RuntimeError as error:
            logs.warning('Could not read `/var` usage: %s', error)
            varPct = ''
        if (varPct.isdigit() and (int(varPct) > pars.varMaxPct)):
            logs.warning('Node `%s` `/var` is almost full.', nodeName)
            findings.append(
                f'Observation incomplete: Node `{nodeName}` `/var` usage '
                f'{varPct}% is above {pars.varMaxPct}%'
            )
            break
        if ((fdStat is not None) and fdLim and (fdCount >= fdLim)):
            if ((limAt is None) or (limAt[0] != pid)):
                limAt = (pid, time.monotonic())
            if (emfile or (time.monotonic() - limAt[1] >= pars.emfileWait)):
                break
    assert podSeen, (
        f'Prerequisites not met: replacement MDS `{standby.name}` Pod '
        'never ran (no schedulable storage Node in a free zone?).'
    )
    assert fdTrk, (
        f'Prerequisites not met: FD count of MDS `{standby.name}` was '
        'never read.'
    )
    assert logRead, (
        f'Prerequisites not met: log of MDS `{standby.name}` was never '
        'read.'
    )
    for (pid, (fdFirst, fdMax, fdLim)) in fdTrk.items():
        if (fdMax - fdFirst > pars.fdGrowMax):
            findings.append(
                f'MDS `{standby.name}` (PID {pid}) FD leak: {fdFirst} -> '
                f'{fdMax} (limit {fdLim})'
            )
    if emfile:
        findings.append(
            f'MDS `{standby.name}` logged {emfile} EMFILE lines'
        )
    return findings


def _FailoverAfterKubelet(
    fxtODF: 'FxtObjODF',
    mds: 'MDSdiscovery',
    upAt: float,
) -> list[str]:
    '''
    Once the kubelet and the MDS roles are back, fail the active MDS, and
    check that the standby-replay MDS takes over.

    :param fxtODF:  The `fxtODF` fixture object.
    :param mds:     The `Discover()` result of the test.
    :param upAt:    The `time.monotonic()` value when the kubelet is back.

    :return:        The Node, MDS role and failover findings.
    '''
    (ceph, pars) = (fxtODF.ceph, fxtODF.testPars)
    (standby, stbyNode) = (mds.standby, mds.stbyNode)
    findings = []

    # Wait for the kubelet timer and the Node, then for the MDS roles: one
    # active MDS, and the standby-replay MDS back in its role.
    if ((remTO := upAt - time.monotonic()) > 0):
        logs.info('Waiting %d sec. for kubelet.', remTO)
        time.sleep(remTO)
    active = ''
    if not stbyNode.WaitReady():
        findings.append(
            f'Node `{stbyNode.name}` is not Ready after the kubelet timer'
        )
    else:
        time.sleep(pars.settleSec)
        deadline = time.monotonic() + pars.rolesTO
        while True:
            try: states = ceph.MDSstates(mds.fsName)
            except RuntimeError as error:
                logs.warning('Could not query Ceph: %s', error)
                states = {}
            actives = [
                name for (name, state) in states.items()
                if (state == 'up:active')
            ]
            if (
                (states.get(standby.name) == 'up:standby-replay') and
                (len(actives) == 1)
            ):
                active = actives[0]
                break
            if (time.monotonic() >= deadline):
                findings.append(
                    f'MDS `{standby.name}` is not back as standby-replay '
                    f'{pars.rolesTO} sec. after the kubelet (states: '
                    f'{states})'
                )
                break
            time.sleep(pars.pollInt)

    # Failover: the standby-replay MDS must take over the active rank.
    if active:
        try: ceph.FailMDS(active)
        except RuntimeError as error:
            findings.append(f'Could not fail MDS `{active}`: {error}')
            return findings
        deadline = time.monotonic() + pars.failoverTO
        while True:
            time.sleep(pars.pollInt)
            try:
                (_, checks) = ceph.Health()
                state = ceph.MDSstates(mds.fsName).get(
                    standby.name, '',
                )
            except RuntimeError as error:
                logs.warning('Could not query Ceph: %s', error)
                (checks, state) = ({'UNKNOWN'}, '')
            if (('FS_DEGRADED' not in checks) and (state == 'up:active')):
                break
            if (time.monotonic() >= deadline):
                findings.append(
                    f'MDS `{standby.name}` did not take over '
                    f'{pars.failoverTO} sec. after failing MDS '
                    f'`{active}` (state: `{state}`, checks: '
                    f'{sorted(checks)})'
                )
                break

    return findings


def _RecoverKubeletStop(
    fxtODF: 'FxtObjODF',
    mds: 'MDSdiscovery',
    upAt: float,
) -> list[str]:
    '''
    Recover from the kubelet stop: wait for the kubelet timer and the Node to
    be `Ready`, delete the Pods of the original standby-replay daemon,
    whatever its current role, so that Rook recreates them as fresh
    processes, then wait up to `testPars.healthTO` sec. for Ceph `HEALTH_OK`.

    :param fxtODF:  The `fxtODF` fixture object.
    :param mds:     The `Discover()` result of the test.
    :param upAt:    The `time.monotonic()` value when the kubelet is back.

    :return:        The recovery failures.
    '''
    (ceph, oc) = (fxtODF.ceph, fxtODF.cli.oc)
    (standby, stbyNode) = (mds.standby, mds.stbyNode)
    healthTO = fxtODF.testPars.healthTO
    clnFail = []
    logs.info('Recovering from the kubelet stop on Node `%s`.', stbyNode.name)
    if ((remTO := (upAt - time.monotonic())) > 0):
        logs.info(
            'Waiting %d sec. for kubelet on Node `%s`.',
            remTO, stbyNode.name,
        )
        time.sleep(remTO)
    if not stbyNode.WaitReady():
        clnFail.append(f'Node `{stbyNode.name}` is not Ready')
    if not oc.Run(
        'delete',
        'Pod',
        n=fxtODF.ns,
        l=f'app=rook-ceph-mds,ceph_daemon_id={standby.name}',
        ignore_not_found=True, timeout='5m', wait='true',
    ).success:
        clnFail.append(f'Could not delete MDS `{standby.name}` Pods')
    else: logs.info('Deleted the Pods of MDS `%s`.', standby.name)
    startAt = time.monotonic()
    deadline = startAt + healthTO
    while True:
        try: (status, _) = ceph.Health()
        except RuntimeError: status = ''
        if (status == 'HEALTH_OK'):
            logs.info(
                'Ceph is `HEALTH_OK` after %d sec.',
                time.monotonic() - startAt,
            )
            break
        if ((remTO := (deadline - time.monotonic())) <= 0):
            clnFail.append(
                f'Ceph is not `HEALTH_OK` after {healthTO} sec.'
            )
            break
        time.sleep(min(30, remTO))
    return clnFail


class TestMDS():
    '''ODF CephFS MDS resiliency scenarios.'''

    @pytest.mark.odf__mds
    @pytest.mark.parametrize(
        'fxtODF,',
        [({
            'testPars': NamedDict(
                downMin=40,         # Kubelet stop duration (min.).
                pollInt=30,         # Poll interval (sec.).
                fdGrowMax=100,      # Max. allowed MDS FD growth.
                emfileWait=120,     # Wait for EMFILE once FD limit is hit.
                varMaxPct=85,       # Stop polling above this `/var` usage.
                settleSec=150,      # Min. settle time after kubelet is back.
                rolesTO=600,        # Max. wait for the MDS roles to return.
                failoverTO=300,     # Max. CephFS degraded time on failover.
                healthTO=1800,      # Max. wait for `HEALTH_OK` at recovery.
            ),
        },)],
        indirect=True,
    )
    def test_StandbyReplayFDleakAfterKubeletStop(
        self,
        fxtODF: 'FxtObjODF',
    ) -> None:
        '''
        Stop the kubelet on the standby-replay MDS Node. Rook starts a
        replacement standby-replay Pod while the original MDS keeps running,
        so two daemons share one name. Check the replacement MDS for a File
        Descriptor (FD) leak and `EMFILE`. Once the MDS roles are back, fail
        the active MDS and check that the standby-replay MDS takes over.
        Recover from the kubelet stop in any case.
        '''
        (pars, mds) = (
            fxtODF.testPars, fxtODF.storage.ceph.MDS.Discover(),
        )
        (standby, stbyNode) = (mds.standby, mds.stbyNode)

        # Re-check the roles and placement right before the stop: the
        # `Discover()` values are a discovery-time snapshot, and stopping the
        # kubelet of the active MDS Node would test the wrong scenario.
        states = fxtODF.ceph.MDSstates(mds.fsName)
        (_, nodeName) = standby.Pod()
        assert (
            (states.get(standby.name) == 'up:standby-replay') and
            (nodeName == stbyNode.name)
        ), (
            f'Prerequisites not met: MDS `{standby.name}` is not the '
            f'standby-replay MDS on Node `{stbyNode.name}` anymore (states: '
            f'{states}, Node: `{nodeName}`).'
        )
        assert stbyNode.WaitReady(timeout='1m'), (
            f'Prerequisites not met: Node `{stbyNode.name}` is not Ready.'
        )

        # Trigger. The kubelet comes back by its timer only (at `upAt`). The
        # recovery runs from here on, even if the stop fails part way.
        upAt = time.monotonic() + (pars.downMin * 60)
        try:
            assert stbyNode.StopKubelet(pars.downMin), (
                f'Could not stop the kubelet on Node `{stbyNode.name}`.'
            )
            findings = _ObserveReplacementMDS(fxtODF, mds, upAt)
            findings += _FailoverAfterKubelet(fxtODF, mds, upAt)
        finally:
            # Report recovery failures with the findings, or log them if the
            # test already failed, so that they never hide the test result.
            if (clnFail := _RecoverKubeletStop(fxtODF, mds, upAt)):
                clnErr = 'Kubelet stop recovery failed: ' + '; '.join(clnFail)
                if (sys.exc_info()[1] is None): findings.append(clnErr)
                else: logs.error('%s; preserving primary exception.', clnErr)

        assert not findings, '; '.join(findings)
