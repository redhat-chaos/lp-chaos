'''
ODF fixture for Ceph scenarios

Provide the ODF Ceph cluster of the Cluster Under Test (CUT), with the Ceph
toolbox enabled, to a test.
'''
from collections.abc import Callable, Generator
import dataclasses
import logging
import os
import sys
from typing import Any

import pytest

from libs.CustomTypes import NamedDict
from libs.OpenShift.LP.ODF.Ceph import Ceph


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
class ODFstorage:
    '''
    The storage cluster of the CUT.

    Can be sub-classed to add fields. Sub-classes must also be decorated with
    `@dataclass(frozen=True)`.
    '''
    ceph: Ceph
    # mcg: MCG       # Future expansion.


@dataclasses.dataclass(frozen=True)
class FxtObjODF:
    '''
    Fixture Object.

    Can be sub-classed to add fields. Sub-classes must also be decorated with
    `@dataclass(frozen=True)`.
    '''
    cli: NamedDict
    ns: str
    fxtInf: NamedDict
    testPars: NamedDict
    storage: ODFstorage


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
            (default: `'~/.kube/config'`)). A single file: it is passed as
            the `--kubeconfig` option, which does not merge
            colon-separated lists.
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
    the yielded `storage.ceph.MDS.Discover()`.
    Teardown disables the Ceph toolbox only if setup enabled it, and only on
    the same `StorageCluster`: a recreated `StorageCluster` of the same name
    is not touched. The fixture does not restore any other cluster state
    changed by the test.
    The fixture assumes exclusive use of the CUT's `StorageCluster`: the
    toolbox lifecycle is not coordinated between concurrent fixture runs.

    :yield:
        A `FxtObjODF` where:
          - cli:
            A `NamedDict` with:
              - oc:         A `OCcli` instance.
          - ns:         ODF Namespace.
          - fxtInf:     The resolved fixture settings `NamedDict` (see Fixture
                        Configuration for details).
          - testPars:   A `NamedDict` of test-specific parameters.
          - storage:
            An `ODFstorage` with:
              - ceph:
                The `Ceph` cluster wrapper of the CUT, bound to this
                fixture's `cli.oc`: the Ceph health (`ceph.Health()`), the
                CephFS MDS layer (`ceph.MDS`, e.g. `ceph.MDS.Discover()` and
                `ceph.MDS.Fail()`), and the other Ceph operations.

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
    toolsOwner = None
    try:
        toolsOwner = ceph.EnableTools(timeout=cephCfg.timeout.toolsReady)
        if cephCfg.chkHealth:
            (status, checks) = ceph.Health()
            if (status != 'HEALTH_OK'):
                raise RuntimeError(
                    f'Ceph is not `HEALTH_OK`: `{status}` {sorted(checks)}.'
                )
        yield FxtObjODF(
            cli=NamedDict(oc=oc),
            ns=fxtCfg.ns,
            fxtInf=fxtCfg.fxtInf,
            testPars=fxtCfg.testPars,
            storage=ODFstorage(ceph=ceph),
        )
    finally:
        primExc = sys.exc_info()[1]
        try: ceph.DisableTools(toolsOwner)
        except Exception as error:
            logs.error('Could not disable Ceph toolbox: %s', error)
            if (primExc is None): raise
        logs.info('The `fxtODF` fixture teardown complete.')
