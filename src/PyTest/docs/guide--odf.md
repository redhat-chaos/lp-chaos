# ODF Ceph Fixtures

Use `fxtODF` to work with the ODF Ceph cluster of the Cluster Under Test (CUT) and its CephFS MDS daemons. The fixture does not install
ODF. Ceph commands run inside the Ceph toolbox Pod via `oc exec`, not locally.


## Minimal Usage

```python
from fixtures.ODF import FxtObjODF


def test_MDSroles(fxtODF: FxtObjODF) -> None:
    mds = fxtODF.storage.ceph.MDS.Discover()
    states = fxtODF.ceph.MDSstates(mds.fsName)
    assert (states.get(mds.active.name) == 'up:active')
```

The `fxtODF.storage` namespace exposes the storage operations bound to the fixture's Ceph context: `storage.ceph.Health()` and
`storage.ceph.MDS.Discover()`. The raw wrappers stay available as `fxtODF.ceph` and `fxtODF.cli.oc`. This usage example only reads cluster
state; the [ODF examples](../test--suites/ts--example/test_ODF.py) are the non-disruptive live checks. See the complete
[fixture configuration and parametrization](../fixtures/ODF.py), the [`Ceph`](../libs/OpenShift/LP/ODF/Ceph/Ceph.py) and
[`MDS`](../libs/OpenShift/LP/ODF/Ceph/MDS.py) wrappers, and the [`Node`](../libs/OpenShift/OCP/Infra/Node.py) wrapper for kubelet and host access.


## Configuration

- Use an OCP CUT with ODF Internal mode installed and cluster-admin access. `ns` defaults to `openshift-storage`.
- Setup enables the Ceph toolbox (`StorageCluster` `.spec.enableCephTools`) if it is not already enabled, and waits up to
  `fxtInf.cephCfg.timeout.toolsReady` (default: `10m`) for it. If the toolbox does not become available, setup disables it again.
- With `fxtInf.cephCfg.chkHealth` (default: `True`), setup requires Ceph `HEALTH_OK`. Muted Ceph health checks are ignored.
- `Discover(fsName=...)` uses one CephFS file system: the `fsName` operation argument, or else the one created by ODF (the `CephFilesystem`
  owned by the `StorageCluster`, e.g. `ocs-storagecluster-cephfilesystem`). It requires exactly one `up:active` and one `up:standby-replay`
  MDS there (the ODF default topology), each with a `Running` Pod. It returns both as `MDS` wrappers, and their Pods' Nodes as `Node`
  wrappers.
- MDS discovery is lazy: requesting `fxtODF` or querying Ceph health does not require the MDS topology; it is checked only when
  `Discover()` is called. Each call queries the current roles, so the result is a snapshot of that moment, e.g. re-discover after a failover.
- A setup failure, such as an unavailable toolbox or unhealthy Ceph, is reported as a test error. A discovery failure, such as missing MDS roles,
  raises `RuntimeError` from `Discover()` in the test body and is reported as a test failure.
- Configure `fxtODF` via indirect parametrization with `ns`, `kCfg`, `cliCfg`, `testPars`, and nested `NamedDict` settings.


## Future Expansion Model

The `storage` namespace is the extension point for storage operations. `ceph` may gain MCG siblings with provider-specific backing-store
adapters, e.g. `mcgAWSs3`, `mcgAzureBlob` (Azure Blob object storage) and `mcgGCS`. These names are extension points only; none is
implemented yet, and the current Ceph toolbox prerequisite stays explicit.


## Node Access and Kubelet Stop

`Node.RunOnHost()` runs host commands through the Node's `machine-config-daemon` Pod; no SSH access is needed. `Node.StopKubelet(downMin)` requires a `Ready`
Node, arms a host `systemd` timer, then stops the kubelet. The timer is the only way the kubelet comes back, after `downMin` minutes, because the Node is
unreachable through `oc` meanwhile. The `oc debug` Pod used for the stop is removed by Kubernetes after the Node is `Ready` again.


## Cleanup

`fxtODF` teardown disables the Ceph toolbox only if its setup enabled it. Check that afterwards, outside the test, e.g.
`oc get StorageCluster -n openshift-storage -o jsonpath='{.items[0].spec.enableCephTools}'`: an assertion in the test body runs before teardown.
`Discover()` changes nothing, so it has no teardown. The fixture does not restore cluster state changed by the test.

Kubelet stops and MDS failovers disrupt the whole storage cluster. Use a disposable CUT only.
