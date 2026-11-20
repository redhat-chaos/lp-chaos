'''ODF Ceph MDS daemon wrapper with Pod lookup and process observation.'''
import json
import re
import shlex
from typing import Optional

from libs.OpenShift.LP.ODF.Ceph.Ceph import Ceph
from libs.OpenShift.OCP.Infra.Node import Node


class MDS:
    '''
    Wrapper around one Ceph MDS daemon of ODF.

    Pods are matched by the Rook `ceph_daemon_id` label, so a replacement Pod
    of the same daemon is found as well.
    '''

    def __init__(self, name: str, ceph: Ceph) -> None:
        '''
        :param name:    MDS daemon name (e.g. `'<fs>-b'`).
        :param ceph:    The `Ceph` instance of the daemon's cluster.
        '''
        self.name = name
        self.ceph = ceph

    def Pod(self, exclNode: str = '') -> tuple[str, str]:
        '''
        Find a `Running` Pod of this daemon that is not being deleted.

        :param exclNode:
            Skip Pods on this Node (default: `''`, skip none). Use it to find
            the replacement Pod, while the original Pod's Node is `NotReady`.

        :return:
            A `tuple` of `(podName, nodeName)`, or empty strings if none found.
        '''
        result = self.ceph.oc.Run(
            'get',
            'Pod',
            n=self.ceph.namespace,
            l=f'app=rook-ceph-mds,ceph_daemon_id={self.name}',
            o='json',
        )
        try:
            if not result.success: raise ValueError()
            pods = json.loads(result.stdout)['items']
        except (KeyError, TypeError, ValueError): return ('', '')
        for pod in pods:
            nodeName = pod.get('spec', {}).get('nodeName', '')
            if (
                (pod['metadata'].get('deletionTimestamp') is not None) or
                (pod.get('status', {}).get('phase') != 'Running') or
                not nodeName or
                (nodeName == exclNode)
            ): continue
            return (pod['metadata']['name'], nodeName)
        return ('', '')

    def FDstat(
        self,
        node: Node,
        cmdTO: int = 60,
    ) -> Optional[tuple[int, int, int]]:
        '''
        Count the open File Descriptors (FDs) of this daemon's `ceph-mds`
        process, on its Node host via `Node.RunOnHost()`.

        :param node:    The `Node` running the daemon's Pod.
        :param cmdTO:   Timeout (in sec.) for the host command (default: `60`).

        :return:
            A `tuple` of `(pid, fdCount, fdLimit)`, or `None` if not exactly
            one matching process was found or the host command failed. A
            process whose `/proc` FD listing fails, or whose cmdline no
            longer matches after the sample, is skipped: its PID ended or
            was reused, so the sample would not be this daemon's. The
            host PID stays the same across in-process respawns, and changes
            when the process is restarted.
        '''
        matchCmd = (
            f'grep -Fqx -e {shlex.quote(f"--id={self.name}")} '
            f'-e {shlex.quote(self.name)}'
        )
        result = node.RunOnHost(
            (
                'for p in $(pgrep -x ceph-mds); do '
                f'tr "\\0" "\\n" < "/proc/${{p}}/cmdline" | {matchCmd} '
                '|| continue; '
                'fds=$(ls "/proc/${p}/fd") || continue; '
                'lim=$(awk \'/^Max open files/ {print $4}\' '
                '"/proc/${p}/limits"); '
                f'tr "\\0" "\\n" < "/proc/${{p}}/cmdline" | {matchCmd} '
                '|| continue; '
                'echo "${p} $(echo ${fds} | wc -w) ${lim}"; '
                'done'
            ),
            cmdTO=cmdTO,
        )
        if not result.success: return None
        lines = (result.stdout or '').split('\n')
        lines = [line for line in lines if line.strip()]
        try:
            if (len(lines) != 1): raise ValueError()
            (pid, fdCount, fdLimit) = (int(val) for val in lines[0].split())
        except ValueError: return None
        return (pid, fdCount, fdLimit)

    def LogCount(self, pod: str, pattern: str, since: str = '60s') -> int:
        '''
        Count this daemon's recent `mds` container log lines matching a
        pattern (case-insensitive): `oc logs <pod> -c mds --since=<since>`.

        :param pod:     Pod name, as returned by `Pod()`.
        :param pattern: Regular expression to search for.
        :param since:   Log time window (default: `60s`).

        :return:        Number of matching lines, or `-1` if the log lookup
                        failed.
        '''
        result = self.ceph.oc.Run(
            'logs',
            pod,
            n=self.ceph.namespace,
            c='mds',
            since=since,
            spPars={'timeout': 60},
        )
        if not result.success: return -1
        regex = re.compile(pattern, re.IGNORECASE)
        return sum(
            1 for line in (result.stdout or '').splitlines()
            if regex.search(line)
        )
