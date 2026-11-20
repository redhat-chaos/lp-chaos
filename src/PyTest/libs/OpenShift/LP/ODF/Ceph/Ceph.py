'''ODF Ceph cluster wrapper with toolbox management and `ceph` access.'''
import json
import logging
from typing import Any, Optional

from libs.OpenShift.OCP.CLI.OC import OCcli


logs = logging.getLogger(__name__)


class Ceph:
    '''
    Wrapper around the ODF Ceph cluster.

    The `ceph` commands are not run locally. They run inside a Ready Ceph
    toolbox Pod (`rook-ceph-tools`) via `oc exec`. ODF creates the toolbox Pod
    when `.spec.enableCephTools` of the `StorageCluster` is `true` (see
    `EnableTools()`).
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

    def EnableTools(self, timeout: str = '10m') -> bool:
        '''
        Enable the Ceph toolbox, if not already enabled, and wait for it to be
        available.

        :param timeout:
            Maximum time to wait for each of toolbox Deployment creation and
            availability (default: `10m`).

        :return:
            `True` if this call enabled the toolbox, `False` if it was already
            enabled. Pass it to `DisableTools()` to restore the original state.

        :raises RuntimeError:
            The `StorageCluster` lookup or patch failed, or the toolbox is not
            available within the given time. In the latter case, a toolbox
            enabled by this call is disabled again (best effort).
        '''
        (name, _, toolsOn) = self._StorageCluster()
        if not toolsOn:
            if not self.oc.Run(
                'patch',
                f'StorageCluster/{name}',
                n=self.namespace,
                type='merge',
                p=json.dumps({'spec': {'enableCephTools': True}}),
            ).success:
                raise RuntimeError(
                    'Failed to enable Ceph toolbox on '
                    f'`StorageCluster/{name}`.'
                )
            logs.info('Enabled Ceph toolbox on `StorageCluster/%s`.', name)
        for waitCond in ('create', 'condition=Available'):
            if not self.oc.Run(
                'wait',
                'Deployment/rook-ceph-tools',
                n=self.namespace,
                **{'for': waitCond}, timeout=timeout,
            ).success:
                if not toolsOn:
                    try: self.DisableTools(True)
                    except RuntimeError as error:
                        logs.error('Could not disable Ceph toolbox: %s', error)
                raise RuntimeError(
                    'Ceph toolbox is not available within '
                    f'`{timeout}` (`{waitCond}`).'
                )
        return not toolsOn

    def DisableTools(self, enabled: bool) -> None:
        '''
        Disable the Ceph toolbox, only if it was enabled by `EnableTools()`.

        :param enabled:
            The `EnableTools()` result. Nothing is done if it is `False`.

        :raises RuntimeError:
            The `StorageCluster` lookup or patch failed.
        '''
        if not enabled: return
        (name, _, _) = self._StorageCluster()
        if not self.oc.Run(
            'patch',
            f'StorageCluster/{name}',
            n=self.namespace,
            type='merge',
            p=json.dumps({'spec': {'enableCephTools': False}}),
        ).success:
            raise RuntimeError(
                f'Failed to disable Ceph toolbox on `StorageCluster/{name}`.'
            )
        logs.info('Disabled Ceph toolbox on `StorageCluster/%s`.', name)

    def _ToolsPod(self, cmdTO: int = 60) -> str:
        '''
        Find a Ready toolbox Pod. A Pod on a `NotReady` Node is not Ready.

        :param cmdTO:   Timeout (in sec.) for the lookup (default: `60`).

        :return:
            The Pod name, or an empty string if none is Ready or the lookup
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
        for pod in pods:
            if (pod['metadata'].get('deletionTimestamp') is not None): continue
            if any(
                (cond.get('type') == 'Ready') and
                (cond.get('status') == 'True')
                for cond in pod.get('status', {}).get('conditions', [])
            ): return pod['metadata']['name']
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
            return (health['status'], {
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

    def ODFfsName(self) -> str:
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

    def MDSstates(self, fsName: str) -> dict[str, str]:
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
        if ((fsMap := self._FSmaps().get(fsName)) is None):
            raise RuntimeError(f'No CephFS file system `{fsName}`.')
        try:
            return {
                mds['name']: mds['state'] for mds in fsMap['info'].values()
            }
        except (KeyError, TypeError):
            raise RuntimeError(
                'Ceph file system dump has no MDS map.'
            ) from None

    def FailMDS(self, name: str) -> None:
        '''
        Mark an MDS daemon as failed, to trigger a failover:
        `ceph mds fail <name>`.

        The command completing does not prove the daemon was failed: Ceph
        can accept it as a no-op, e.g. report on stderr that the daemon is
        gone, and still exit successfully.

        :param name:    MDS daemon name.

        :raises RuntimeError:   The command failed.
        '''
        self.Run('mds', 'fail', name)
        logs.info('Sent MDS fail for daemon `%s`.', name)
