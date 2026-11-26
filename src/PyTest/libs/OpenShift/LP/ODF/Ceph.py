'''ODF Ceph cluster and CephFS MDS wrappers.'''
import dataclasses
import json
import logging
import re
import shlex
from typing import Any, Optional

from libs.OpenShift.OCP.CLI.OC import OCcli
from libs.OpenShift.OCP.Infra.Node import Node


logs = logging.getLogger(__name__)


class Ceph:
    '''
    Wrapper around the ODF Ceph cluster.

    The `ceph` commands are not run locally. They run inside a Ready Ceph
    toolbox Pod (`rook-ceph-tools`) via `oc exec`. ODF creates the toolbox Pod
    when `.spec.enableCephTools` of the `StorageCluster` is `true` (see
    `EnableTools()`). The `MDS` attribute holds the cluster's CephFS MDS
    layer wrapper.
    '''

    def __init__(
        self,
        ns: str = 'openshift-storage',
        oc: Optional[OCcli] = None,
    ) -> None:
        '''
        :param ns:
            ODF Namespace (default: `'openshift-storage'`).
        :param oc:
            Optional `OCcli` instance. If `None`, a default instance is created
            with `ns` pre-configured.
        '''
        self.namespace = ns
        self.oc = oc or OCcli(n=ns)
        self.MDS = MDS(self)

    def _StorageCluster(self) -> tuple[str, str, bool]:
        '''
        Query the `StorageCluster` via `oc get`.

        :return:
            A `tuple` where:
              - name:       The `StorageCluster` name.
              - uid:        The `StorageCluster` UID.
              - toolsOn:    The `.spec.enableCephTools` value.

        :raises RuntimeError:
            The lookup failed or found no `StorageCluster`.
        '''
        result = self.oc.Run(
            'get',
            'StorageCluster',
            n=self.namespace,
            o='json',
        )
        try:
            if not result.success: raise ValueError()
            stgCluster = json.loads(result.stdout)['items'][0]
            name = stgCluster['metadata']['name']
            uid = stgCluster['metadata'].get('uid', '')
        except (IndexError, KeyError, TypeError, ValueError):
            raise RuntimeError(
                f'No `StorageCluster` found in Namespace `{self.namespace}`.'
            ) from None
        toolsOn = stgCluster.get('spec', {}).get('enableCephTools')
        return (name, uid, toolsOn is True)

    def _SetToolsEnabled(self, name: str, uid: str, enabled: bool) -> None:
        '''
        Patch `.spec.enableCephTools` of the `StorageCluster`, as a JSON
        Patch guarded by a `test` operation on the `StorageCluster` UID: a
        `StorageCluster` replaced under the same name after the lookup
        fails the patch instead of being changed.

        :param name:        The `StorageCluster` name.
        :param uid:         The `StorageCluster` UID.
        :param enabled:     The new toolbox state.

        :raises RuntimeError:   The patch failed.
        '''
        action = 'enable' if enabled else 'disable'
        if not self.oc.Run(
            'patch',
            f'StorageCluster/{name}',
            n=self.namespace,
            type='json',
            p=json.dumps([
                {'op': 'test', 'path': '/metadata/uid', 'value': uid},
                {
                    'op': 'add', 'path': '/spec/enableCephTools',
                    'value': enabled,
                },
            ]),
        ).success:
            raise RuntimeError(
                f'Failed to {action} Ceph toolbox on '
                f'`StorageCluster/{name}`.'
            )

    def EnableTools(self, timeout: str = '10m') -> Optional[tuple[str, str]]:
        '''
        Enable the Ceph toolbox, if not already enabled, and wait for it to be
        available.

        :param timeout:
            Maximum time to wait for each of toolbox Deployment creation and
            availability (default: `10m`).

        :return:
            The `(name, uid)` of the `StorageCluster` the toolbox was enabled
            on, or `None` if it was already enabled. Pass it to
            `DisableTools()` to restore the original state: the disable is
            skipped if that `StorageCluster` was replaced meanwhile, so
            cleanup cannot touch a cluster this call did not change.

        :raises RuntimeError:
            The `StorageCluster` lookup or patch failed, or the toolbox is not
            available within the given time. In the latter case, a toolbox
            enabled by this call is disabled again (best effort).
        '''
        (name, uid, toolsOn) = self._StorageCluster()
        # Any exit other than success, from the enable patch or the waits,
        # including an exception or an interrupt (e.g. Ctrl-C while the
        # patch is in flight, so its result is unknown), rolls back a toolbox
        # this call may have enabled: the caller gets no owner to clean up.
        # The rollback re-reads the `StorageCluster`, so it leaves a
        # replacement, or a flag the patch never set, untouched.
        try:
            if not toolsOn:
                self._SetToolsEnabled(name, uid, True)
                logs.info(
                    'Enabled Ceph toolbox on `StorageCluster/%s`.', name,
                )
            for waitCond in ('create', 'condition=Available'):
                if not self.oc.Run(
                    'wait',
                    'Deployment/rook-ceph-tools',
                    n=self.namespace,
                    **{'for': waitCond}, timeout=timeout,
                ).success:
                    raise RuntimeError(
                        'Ceph toolbox is not available within '
                        f'`{timeout}` (`{waitCond}`).'
                    )
        except BaseException:
            if not toolsOn:
                try: self.DisableTools((name, uid))
                except RuntimeError as error:
                    logs.error('Could not disable Ceph toolbox: %s', error)
            raise
        return None if toolsOn else (name, uid)

    def DisableTools(self, owner: Optional[tuple[str, str]]) -> None:
        '''
        Disable the Ceph toolbox, only if it was enabled by `EnableTools()`,
        and only on the same `StorageCluster`.

        :param owner:
            The `EnableTools()` result. Nothing is done if it is `None`, if
            the `StorageCluster` it names was replaced since (a recreated
            `StorageCluster` of the same name is not this call's to change),
            or if the toolbox is already disabled.

        :raises RuntimeError:
            The `StorageCluster` lookup or patch failed.
        '''
        if (owner is None): return
        (name, uid, toolsOn) = self._StorageCluster()
        if ((name, uid) != owner):
            logs.warning(
                'Not disabling Ceph toolbox: `StorageCluster/%s` was '
                'replaced since the enable.', name,
            )
            return
        if not toolsOn: return
        self._SetToolsEnabled(name, uid, False)
        logs.info('Disabled Ceph toolbox on `StorageCluster/%s`.', name)

    def _ToolsPod(self, cmdTO: int = 60) -> str:
        '''
        Find a Ready toolbox Pod on a Ready Node.

        A Pod on a `NotReady` Node is not usable: with the Node's kubelet
        down, the Pod's conditions are frozen at their last state, so it can
        still report `Ready` while `oc exec` into it cannot run. Such a Pod
        is skipped.

        :param cmdTO:
            Timeout (in sec.) for each of the Pod and Node lookups
            (default: `60`).

        :return:
            The Pod name, or an empty string if none is usable or a lookup
            failed.
        '''
        result = self.oc.Run(
            'get',
            'Pod',
            n=self.namespace,
            l='app=rook-ceph-tools',
            o='json',
            spPars={'timeout': cmdTO},
        )
        try:
            if not result.success: raise ValueError()
            pods = json.loads(result.stdout)['items']
        except (KeyError, TypeError, ValueError): return ''
        candidates = [
            pod for pod in pods
            if (pod['metadata'].get('deletionTimestamp') is None) and any(
                (cond.get('type') == 'Ready') and
                (cond.get('status') == 'True')
                for cond in pod.get('status', {}).get('conditions', [])
            )
        ]
        if not candidates: return ''
        nodes = self.oc.Run(
            'get',
            'Node',
            o='json',
            spPars={'timeout': cmdTO},
        )
        try:
            if not nodes.success: raise ValueError()
            readyNodes = {
                node['metadata']['name']
                for node in json.loads(nodes.stdout)['items']
                if any(
                    (cond.get('type') == 'Ready') and
                    (cond.get('status') == 'True')
                    for cond in node.get('status', {}).get('conditions', [])
                )
            }
        except (KeyError, TypeError, ValueError): return ''
        for pod in candidates:
            if (pod.get('spec', {}).get('nodeName', '') in readyNodes):
                return pod['metadata']['name']
        return ''

    def Run(self, *args: str, cmdTO: int = 60) -> Any:
        '''
        Run a `ceph` command in a Ready toolbox Pod, with JSON output:
        `oc exec <pod> -- ceph <args...> --format json`.

        :param args:    The `ceph` arguments (e.g. `'fs', 'dump'`).
        :param cmdTO:
            Timeout (in sec.) for each of the toolbox Pod lookup and the
            command (default: `60`).

        :return:
            The parsed JSON output, or `None` if the command printed nothing.

        :raises ValueError:     The `cmdTO` is invalid.
        :raises RuntimeError:
            No Ready toolbox Pod, the command failed, or its output is not
            valid JSON.
        '''
        if (
            not isinstance(cmdTO, int) or
            isinstance(cmdTO, bool) or
            (cmdTO <= 0)
        ): raise ValueError('The `cmdTO` must be a positive integer.')
        if not (toolsPod := self._ToolsPod(cmdTO)):
            raise RuntimeError('No Ready Ceph toolbox Pod found.')
        cephCmd = ' '.join(args)
        result = self.oc.Run(
            'exec',
            toolsPod,
            '--', 'ceph', *args, '--format', 'json',
            n=self.namespace,
            spPars={'timeout': cmdTO},
        )
        if not result.success:
            raise RuntimeError(f'Ceph command `{cephCmd}` failed.')
        if not (result.stdout or '').strip(): return None
        try: return json.loads(result.stdout)
        except ValueError:
            raise RuntimeError(
                f'Ceph command `{cephCmd}` returned invalid JSON.'
            ) from None

    def Health(self) -> tuple[str, set[str]]:
        '''
        Query the Ceph health: `ceph health`.

        :return:
            A `tuple` where:
              - status: Overall status (e.g. `'HEALTH_OK'`).
              - checks: Active health check codes (e.g. `{'FS_DEGRADED'}`).
                        Muted checks are excluded, as in `status`.

        :raises RuntimeError:
            The command failed, or returned no valid status or checks.
        '''
        health = self.Run('health')
        try:
            status = health['status']
            if not isinstance(status, str): raise TypeError()
            return (status, {
                code
                for (code, check) in health.get('checks', {}).items()
                if not check.get('muted')
            })
        except (AttributeError, KeyError, TypeError):
            raise RuntimeError(
                'Ceph health has no valid status or checks.'
            ) from None

    def _FSmaps(self) -> dict[str, dict[str, Any]]:
        '''
        Query the MDS maps of all CephFS file systems: `ceph fs dump`.

        :return:                A `dict` of file system name to its MDS map.

        :raises RuntimeError:   The command failed or returned no MDS map.
        '''
        fsDump = self.Run('fs', 'dump')
        try:
            fsMaps = {
                fs['mdsmap']['fs_name']: fs['mdsmap']
                for fs in fsDump['filesystems']
            }
            if not all(isinstance(m['info'], dict) for m in fsMaps.values()):
                raise TypeError()
            return fsMaps
        except (AttributeError, KeyError, TypeError):
            raise RuntimeError(
                'Ceph file system dump has no MDS map.'
            ) from None

    def GetODFfsName(self) -> str:
        '''
        Find the CephFS file system created by ODF: the `CephFilesystem`
        resource owned by the `StorageCluster` (Rook names the Ceph file
        system after it). The owner reference must match the
        `StorageCluster` name and UID, so a recreated `StorageCluster` of
        the same name does not own file systems of the old one.

        :return:                The file system name.

        :raises RuntimeError:
            The lookup failed, or not exactly one `CephFilesystem` is owned by
            the `StorageCluster`.
        '''
        (scName, scUID, _) = self._StorageCluster()
        result = self.oc.Run(
            'get',
            'CephFilesystem',
            n=self.namespace,
            o='json',
        )
        try:
            if not result.success: raise ValueError()
            owned = [
                cfs['metadata']['name']
                for cfs in json.loads(result.stdout)['items']
                if any(
                    (ref.get('kind') == 'StorageCluster') and
                    (ref.get('name') == scName) and
                    (ref.get('uid') == scUID)
                    for ref in cfs['metadata'].get('ownerReferences', [])
                )
            ]
        except (AttributeError, KeyError, TypeError, ValueError):
            owned = None
        if not (owned and (len(owned) == 1)):
            raise RuntimeError(
                'Expected one `CephFilesystem` owned by `StorageCluster` '
                f'`{scName}`, got: {owned}.'
            )
        return owned[0]

    def GetMDSmap(self, fsName: str) -> dict[str, tuple[str, str]]:
        '''
        Query the MDS daemons of one CephFS file system, with their states
        and daemon instance identities.

        :param fsName:  The file system name.

        :return:
            A `dict` of MDS daemon name to a `tuple` of `(state, gid)`, where
            `gid` is the daemon's Ceph instance ID as a `str`: the `gid`
            field of its MDS map entry (a JSON number), or the map key when
            the entry has no `gid` field. The monitor assigns a new GID each
            time a daemon starts, so a restarted daemon has a different `gid`
            even when it regains the same name and state.

        :raises RuntimeError:
            The command failed, returned no MDS map, or has no file system
            `fsName`.
        '''
        if ((fsMap := self._FSmaps().get(fsName)) is None):
            raise RuntimeError(f'No CephFS file system `{fsName}`.')
        try:
            return {
                mds['name']: (mds['state'], str(mds.get('gid', gid)))
                for (gid, mds) in fsMap['info'].items()
            }
        except (AttributeError, KeyError, TypeError):
            raise RuntimeError(
                'Ceph file system dump has no MDS map.'
            ) from None

    def GetMDSstates(self, fsName: str) -> dict[str, str]:
        '''
        Query the MDS daemon states of one CephFS file system.

        :param fsName:  The file system name.

        :return:
            A `dict` of MDS daemon name to its state (e.g.
            `{'<fs>-a': 'up:active', '<fs>-b': 'up:standby-replay'}`).

        :raises RuntimeError:
            The command failed, returned no MDS map, or has no file system
            `fsName`.
        '''
        return {
            name: state
            for (name, (state, _)) in self.GetMDSmap(fsName).items()
        }


class MDS:
    '''
    The CephFS MDS layer of one ODF Ceph cluster.

    Holds the MDS operations bound to one `Ceph` cluster. Available as the
    `MDS` attribute of a `Ceph` instance, e.g. `ceph.MDS.Discover()`.
    '''

    def __init__(self, ceph: Ceph) -> None:
        '''
        :param ceph:    The `Ceph` cluster of the MDS daemons.
        '''
        self.ceph = ceph

    def Discover(self, fsName: str = '') -> 'MDSdiscovery':
        '''
        Discover the active and standby-replay MDS daemons of one CephFS file
        system, and their Pods' Nodes.

        Exposed as the bound `fxtODF.storage.ceph.MDS.Discover(...)` operation
        (see `FxtODF()`), which supplies the Ceph context. The discovery is
        read-only: it runs only when requested, changes nothing, and has no
        teardown. Each call queries the current MDS roles; its result is
        a snapshot (`MDSdiscovery`), never a cached mapping. The snapshot is
        not atomic: it combines separate Ceph and Kubernetes reads, and a
        change after the final read is outside it. A caller about to act on
        a daemon's Node should revalidate the target right before acting,
        e.g. with `daemon.Pod()`.

        :param fsName:
            The CephFS file system name (default: `''`, the file system created
            by ODF, i.e. the `CephFilesystem` owned by the `StorageCluster`).

        :return:
            An `MDSdiscovery` where:
              - fsName:     The CephFS file system name.
              - active:     The `up:active` `MDSDaemon`.
              - standby:    The `up:standby-replay` `MDSDaemon`.
              - activeNode: The `Node` running the active MDS Pod.
              - stbyNode:   The `Node` running the standby-replay MDS Pod.

        :raises RuntimeError:   A discovery prerequisite is not met: the file
            system does not have exactly one `up:active` and one
            `up:standby-replay` MDS (the ODF default topology), each with a
            `Running` Pod, or ODF owns no `CephFilesystem`, or the MDS roles,
            daemon instances, or selected Pods changed during the discovery.
        '''
        try: fsName = fsName or self.ceph.GetODFfsName()
        except RuntimeError as error:
            raise RuntimeError(f'Prerequisites not met: {error}') from None
        mdsMap = self.ceph.GetMDSmap(fsName)
        states = {name: state for (name, (state, _)) in mdsMap.items()}
        roles = {
            role: [name for (name, state) in states.items() if (state == role)]
            for role in ('up:active', 'up:standby-replay')
        }
        if any((len(names) != 1) for names in roles.values()):
            raise RuntimeError(
                'Prerequisites not met: expected one active and one '
                f'standby-replay MDS in `{fsName}`, got: {states}.'
            )
        active = MDSDaemon(roles['up:active'][0], self.ceph)
        standby = MDSDaemon(roles['up:standby-replay'][0], self.ceph)
        pods = {}
        nodes = {}
        for daemon in (active, standby):
            (pod, nodeName) = daemon.Pod()
            if not pod:
                raise RuntimeError(
                    f'Prerequisites not met: no Running Pod for MDS '
                    f'`{daemon.name}`.'
                )
            pods[daemon.name] = (pod, nodeName)
            nodes[daemon.name] = Node(nodeName, oc=self.ceph.oc)
        # The lookups above take time; re-validate the selected Pods and
        # then the MDS map, so the snapshot never pairs a pre-failover role
        # with a post-failover Pod placement. A role switch does not move
        # Pods, so the MDS map is read last; it compares the daemon
        # instance GIDs as well as the roles, so a daemon that restarted
        # (on any Node) and regained the same role before this read is
        # still detected. The Ceph and Kubernetes reads are separate, not
        # atomic: a change after the final read is outside the snapshot.
        for daemon in (active, standby):
            if (daemon.Pod() != pods[daemon.name]):
                raise RuntimeError(
                    f'Prerequisites not met: the Pod of MDS `{daemon.name}` '
                    'changed during discovery; discover again.'
                )
        if (self.ceph.GetMDSmap(fsName) != mdsMap):
            raise RuntimeError(
                'Prerequisites not met: the MDS roles or daemon instances of '
                f'`{fsName}` changed during discovery; discover again.'
            )
        return MDSdiscovery(
            fsName=fsName,
            active=active,
            standby=standby,
            activeNode=nodes[active.name],
            stbyNode=nodes[standby.name],
        )

    def Fail(self, name: str) -> None:
        '''
        Mark an MDS daemon as failed, to trigger a failover:
        `ceph mds fail <name>`.

        The command completing does not prove the daemon was failed: Ceph
        can accept it as a no-op, e.g. report on stderr that the daemon is
        gone, and still exit successfully.

        :param name:    MDS daemon name.

        :raises RuntimeError:   The command failed.
        '''
        self.ceph.Run('mds', 'fail', name)
        logs.info('Sent MDS fail for daemon `%s`.', name)


class MDSDaemon:
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
            one matching process was found or the host command failed.
        '''
        matchCmd = (
            rf'''
grep -Fqx -e {shlex.quote(f'--id={self.name}')} -e {shlex.quote(self.name)}
            '''.strip()
        )
        # The sample must belong to this daemon alone: the `cmdline` is
        # matched before and after the FD listing, and the process start
        # time (`/proc/<pid>/stat` field 22) is compared across the sample,
        # so a PID that ended or was reused mid-sample is skipped, and so is
        # a process whose `/proc` FD listing or limits cannot be read. The
        # FD count comes from a NUL-separated listing with its exit status
        # checked, so it stays correct for any file name and a partial
        # listing is rejected. The reported `pid` is the host process: it
        # stays the same across in-process respawns, and changes when the
        # process is restarted.
        try:
            result = node.RunOnHost(
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
                cmdTO=cmdTO,
            )
        except RuntimeError:
            # No `machine-config-daemon` Pod on the Node, e.g. while the MCO
            # replaces it: the sample failed, like any host command failure.
            return None
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
        # Split on the LF record delimiter only: `str.splitlines()` also
        # splits on Unicode line boundaries (e.g. U+2028) inside one record.
        # An unterminated final record counts; the empty tail after a final
        # LF is not a record, so it is dropped (a pattern such as `.*` would
        # otherwise match it).
        records = (result.stdout or '').split('\n')
        if (records[-1] == ''): records.pop()
        return sum(1 for record in records if regex.search(record))


@dataclasses.dataclass(frozen=True)
class MDSdiscovery:
    '''
    The result of one CephFS MDS discovery (`MDS.Discover()`).

    A snapshot: it reflects the MDS roles at discovery time only. Discover
    again to see the current roles, e.g. after a failover.

    Can be sub-classed to add fields. Sub-classes must also be decorated with
    `@dataclass(frozen=True)`.
    '''
    fsName: str
    active: MDSDaemon
    standby: MDSDaemon
    activeNode: Node
    stbyNode: Node
