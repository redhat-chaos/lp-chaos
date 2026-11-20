'''
ODF fixture for Ceph scenarios

Provide the ODF Ceph cluster of the Cluster Under Test (CUT), with the Ceph
toolbox enabled, and its CephFS MDS discovery operation to a test.
'''
from collections.abc import Callable, Generator
import dataclasses
import functools
import logging
import os
import sys
from typing import Any

import pytest

from libs.CustomTypes import NamedDict
from libs.OpenShift.LP.ODF.Ceph.Ceph import Ceph
from libs.OpenShift.LP.ODF.Ceph.MDS import MDS
from libs.OpenShift.OCP.CLI.OC import OCcli
from libs.OpenShift.OCP.Infra.Node import Node


logs = logging.getLogger()
_DEF__FXT_CFG = NamedDict(
    kCfg=(
        os.environ.get('KUBECONFIG') or
        os.path.expanduser('~/.kube/config')
    ),
    # The `--kubeconfig` option will be overridden by the above `.kCfg`.
    cliCfg=NamedDict(
        ocCfg={},
    ),
    ns='openshift-storage',
    fxtInf=NamedDict(
        cephCfg=NamedDict(
            chkHealth=True,
            timeout=NamedDict(
                toolsReady='10m',
            ),
        ),
    ),
    testPars=NamedDict(),
)


@dataclasses.dataclass(frozen=True)
class FxtObjODF:
    '''
    Fixture Object.

    Can be sub-classed to add fields. Sub-classes must also be decorated with
    `@dataclass(frozen=True)`.
    '''
    ceph: Ceph
    storage: NamedDict
    fxtInf: NamedDict
    testPars: NamedDict
    cli: NamedDict
    ns: str


@pytest.fixture(name='fxtODF', scope='function')
def FxtODF(
    request: Any,
    fxtOCcli: Callable[..., Any],
) -> Generator[FxtObjODF, None, None]:
    '''
    Provide the ODF `Ceph` cluster wrapper and test parameters to a test.

    This is the complete Fixture Configuration reference (see
    `docs/guide--odf.md` for concise usage).
    Configure via indirect parametrize with `dict` and nested `NamedDict`:
        @pytest.mark.parametrize(
            'fxtODF,',
            [({
                'testPars': NamedDict(pollInt=30),
            },)],
            indirect=True,
        )

        Fixture Configuration:
          - kCfg:
            The `KUBECONFIG` file (default: `KUBECONFIG` Env. Var. value
            (default: `'~/.kube/config'`)).
          - cliCfg:
            Configuration options for CLIs:
              - ocCfg:  Configuration options for `OCcli` (default: `{}`).
          - ns:
            ODF Namespace (default: `'openshift-storage'`).
          - fxtInf:
            Fixture settings:
              - cephCfg:
                Ceph configurations.
                  - chkHealth:
                    Require Ceph `HEALTH_OK` at setup (default: `True`).
                  - timeout:
                      - toolsReady:
                        Ceph toolbox availability timeout (default: `'10m'`).
          - testPars:
            Parameters to be passed to test (default: `NamedDict()`).

    Setup enables the Ceph toolbox, if not already enabled, then checks the
    Ceph health. A setup failure is reported as a test error.
    Setup does not query the CephFS MDS topology; MDS discovery is lazy, via
    the yielded `storage` namespace.
    Teardown disables the Ceph toolbox only if setup enabled it. The fixture
    does not restore any other cluster state changed by the test.

    :yield:
        A `FxtObjODF` where:
          - ceph:       A `Ceph` instance.
          - storage:
            A `NamedDict` of storage operations, bound to this fixture's Ceph
            context (the same `ceph` and `cli.oc`):
              - ceph:
                A `NamedDict` with:
                  - Health:     Query the Ceph health; the bound
                                `ceph.Health()`.
                  - MDS:
                    A `NamedDict` with:
                      - Discover: Discover the CephFS MDS daemons of one file
                                  system, e.g. `Discover(fsName='')`; see
                                  `DiscoverMDS()`.
          - fxtInf:     The resolved fixture settings `NamedDict` (see Fixture
                        Configuration for details).
          - testPars:   A `NamedDict` of test-specific parameters.
          - cli:
            A `NamedDict` with:
              - oc:         A `OCcli` instance.
          - ns:         ODF Namespace.

    :raises RuntimeError:
        The Ceph toolbox is not available, or Ceph is not `HEALTH_OK` while
        `fxtInf.cephCfg.chkHealth` is set.
    '''
    fxtCfg = _DEF__FXT_CFG | NamedDict(**getattr(request, 'param', {}))
    cephCfg = fxtCfg.fxtInf.cephCfg
    oc = fxtOCcli(
        ns=fxtCfg.ns,
        **{**fxtCfg.cliCfg.ocCfg, 'kubeconfig': fxtCfg.kCfg},
    )
    ceph = Ceph(fxtCfg.ns, oc=oc)
    toolsOn = False
    try:
        toolsOn = ceph.EnableTools(timeout=cephCfg.timeout.toolsReady)
        if cephCfg.chkHealth:
            (status, checks) = ceph.Health()
            if (status != 'HEALTH_OK'):
                raise RuntimeError(
                    f'Ceph is not `HEALTH_OK`: `{status}` {sorted(checks)}.'
                )
        # The `storage` namespace exposes the storage operations bound to
        # this fixture's Ceph context.
        yield FxtObjODF(
            ceph=ceph,
            storage=NamedDict(
                ceph=NamedDict(
                    Health=ceph.Health,
                    MDS=NamedDict(
                        Discover=functools.partial(
                            DiscoverMDS, ceph=ceph, oc=oc,
                        ),
                    ),
                ),
            ),
            fxtInf=fxtCfg.fxtInf,
            testPars=fxtCfg.testPars,
            cli=NamedDict(oc=oc),
            ns=fxtCfg.ns,
        )
    finally:
        primExc = sys.exc_info()[1]
        try: ceph.DisableTools(toolsOn)
        except Exception as error:
            logs.error('Could not disable Ceph toolbox: %s', error)
            if (primExc is None): raise
        logs.info('The `fxtODF` fixture teardown complete.')


@dataclasses.dataclass(frozen=True)
class MDSdiscovery:
    '''
    The result of one CephFS MDS discovery (`DiscoverMDS()`).

    A snapshot: it reflects the MDS roles at discovery time only. Discover
    again to see the current roles, e.g. after a failover.

    Can be sub-classed to add fields. Sub-classes must also be decorated with
    `@dataclass(frozen=True)`.
    '''
    fsName: str
    active: MDS
    standby: MDS
    activeNode: Node
    stbyNode: Node


def DiscoverMDS(
    fsName: str = '',
    *,
    ceph: Ceph,
    oc: OCcli,
) -> MDSdiscovery:
    '''
    Discover the active and standby-replay MDS daemons of one CephFS file
    system, and their Pods' Nodes.

    Exposed as the bound `fxtODF.storage.ceph.MDS.Discover(...)` operation
    (see `FxtODF()`), which supplies the Ceph and CLI context. The discovery
    is lazy and read-only: it runs only when requested, changes nothing, and
    has no teardown. Each call queries the current MDS roles; its result is
    a snapshot (`MDSdiscovery`), never a cached mapping.

    :param fsName:
        The CephFS file system name (default: `''`, the file system created
        by ODF, i.e. the `CephFilesystem` owned by the `StorageCluster`).
    :param ceph:     The `Ceph` cluster wrapper.
    :param oc:       The `OCcli` instance of the same context.

    :return:
        An `MDSdiscovery` where:
          - fsName:     The CephFS file system name.
          - active:     The `up:active` `MDS`.
          - standby:    The `up:standby-replay` `MDS`.
          - activeNode: The `Node` running the active MDS Pod.
          - stbyNode:   The `Node` running the standby-replay MDS Pod.

    :raises RuntimeError:   A discovery prerequisite is not met: the file
        system does not have exactly one `up:active` and one
        `up:standby-replay` MDS (the ODF default topology), each with a
        `Running` Pod, or ODF owns no `CephFilesystem`, or the MDS roles
        changed during the discovery.
    '''
    try: fsName = fsName or ceph.ODFfsName()
    except RuntimeError as error:
        raise RuntimeError(f'Prerequisites not met: {error}') from None
    states = ceph.MDSstates(fsName)
    roles = {
        role: [name for (name, state) in states.items() if (state == role)]
        for role in ('up:active', 'up:standby-replay')
    }
    if any((len(names) != 1) for names in roles.values()):
        raise RuntimeError(
            'Prerequisites not met: expected one active and one '
            f'standby-replay MDS in `{fsName}`, got: {states}.'
        )
    active = MDS(roles['up:active'][0], ceph)
    standby = MDS(roles['up:standby-replay'][0], ceph)
    nodes = {}
    for mds in (active, standby):
        (pod, nodeName) = mds.Pod()
        if not pod:
            raise RuntimeError(
                f'Prerequisites not met: no Running Pod for MDS `{mds.name}`.'
            )
        nodes[mds.name] = Node(nodeName, oc=oc)
    # The Pod lookups above take time; re-validate the map so the snapshot
    # never pairs a pre-failover role with a post-failover Pod placement.
    if (ceph.MDSstates(fsName) != states):
        raise RuntimeError(
            f'Prerequisites not met: MDS roles of `{fsName}` changed during '
            'discovery; discover again.'
        )
    return MDSdiscovery(
        fsName=fsName,
        active=active,
        standby=standby,
        activeNode=nodes[active.name],
        stbyNode=nodes[standby.name],
    )
