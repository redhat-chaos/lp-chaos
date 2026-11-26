# ODF Ceph Fixtures

Use `fxtODF` to work with the ODF Ceph cluster of the Cluster Under Test (CUT) and its CephFS MDS daemons. The fixture does not install
ODF. Ceph commands run inside the Ceph toolbox Pod via `oc exec`, not locally.


## Minimal Usage

```python
from fixtures.ODF import FxtObjODF


def test_MDSroles(fxtODF: FxtObjODF) -> None:
    mds = fxtODF.storage.ceph.MDS.Discover()
    assert (mds.active.name != mds.standby.name)
```

The discovery result is a snapshot: assert on it directly, as above — a second live query could observe a post-discovery failover and fail the
assertion without a discovery problem. The `fxtODF.storage` holds the `ODFstorage` of the CUT: `storage.ceph` is the fixture's `Ceph` instance,
with `storage.ceph.Health()`, `storage.ceph.MDS.Discover()`, and the other Ceph operations. The `OCcli` stays available as `fxtODF.cli.oc`. This
usage example only reads cluster state; the [ODF examples](../test--suites/ts--example/test_ODF.py) are the non-disruptive live checks. See the
complete [fixture configuration and parametrization](../fixtures/ODF.py), the [`Ceph`/`MDS`/`MDSDaemon`](../libs/OpenShift/LP/ODF/Ceph.py)
wrappers, and the [`Node`](../libs/OpenShift/OCP/Infra/Node.py) wrapper for kubelet and host access.


## Configuration

- Use an OCP CUT with ODF Internal mode installed and cluster-admin access. `ns` defaults to `openshift-storage`.
- The `oc` client must support `oc wait --for=create` (OCP 4.18+); the `ocp-test` image client qualifies.
- `kCfg` must be a single kubeconfig file: it is passed as the `--kubeconfig` option, which does not merge colon-separated `KUBECONFIG` lists.
- Setup enables the Ceph toolbox (`StorageCluster` `.spec.enableCephTools`) if it is not already enabled, and waits up to
  `fxtInf.cephCfg.timeout.toolsReady` (default: `10m`) for each of the toolbox Deployment creation and its availability — not for both combined.
  If the toolbox does not become available, setup attempts to disable it again, best effort, and only a toolbox it enabled itself: a pre-enabled
  toolbox, or a failed rollback patch, is left enabled.
- With `fxtInf.cephCfg.chkHealth` (default: `True`), setup requires Ceph `HEALTH_OK`. Muted Ceph health checks are ignored.
- `Discover(fsName=...)` uses one CephFS file system: the `fsName` operation argument, or else the one created by ODF (the `CephFilesystem`
  owned by the `StorageCluster`, e.g. `ocs-storagecluster-cephfilesystem`). It requires exactly one `up:active` and one `up:standby-replay`
  MDS there (the ODF default topology), each with a `Running` Pod. It returns both as `MDSDaemon` wrappers, and their Pods' Nodes as `Node`
  wrappers.
- MDS discovery is lazy: requesting `fxtODF` or querying Ceph health does not require the MDS topology; it is checked only when
  `Discover()` is called. Each call queries the current roles, so the result is a snapshot of that moment, e.g. re-discover after a failover.
- A setup failure, such as an unavailable toolbox or unhealthy Ceph, is reported as a test error. A discovery failure, such as missing MDS roles,
  raises `RuntimeError` from `Discover()` in the test body and is reported as a test failure.
- Configure `fxtODF` via indirect parametrization with `ns`, `kCfg`, `cliCfg`, `testPars`, and nested `NamedDict` settings.


## Future Expansion Model

The `ODFstorage` is the extension point for storage operations. `ceph` may gain an `mcg` sibling holding the MCG operations, with
provider-specific backing-store adapters, e.g. `mcgAWSs3`, `mcgAzureBlob` (Azure Blob object storage) and `mcgGCS`. These names are extension
points only; none is implemented yet, and the current Ceph toolbox prerequisite stays explicit.


## Node Access and Kubelet Stop

`Node.RunOnHost()` runs host commands through the Node's `machine-config-daemon` Pod; no SSH access is needed. `Node.TempStopKubelet(downMin)` requires a
`Ready` Node, arms a host `systemd` timer, then stops the kubelet. The timer is the only way the kubelet comes back, after `downMin` minutes, because the
Node is unreachable through `oc` meanwhile. The `oc debug` Pod used for the stop is removed by Kubernetes after the Node is `Ready` again.


## Cleanup

`fxtODF` teardown disables the Ceph toolbox only if its setup enabled it, and only on the same `StorageCluster`: a recreated `StorageCluster` of
the same name is not touched. Check that afterwards, outside the test, e.g.
`oc get StorageCluster -n openshift-storage -o jsonpath='{.items[0].spec.enableCephTools}'`: an assertion in the test body runs before teardown.
`Discover()` changes nothing, so it has no teardown. The fixture does not restore cluster state changed by the test.

Kubelet stops and MDS failovers disrupt the whole storage cluster. Use a disposable CUT only, and use one `StorageCluster` exclusively per run:
the toolbox lifecycle is not coordinated between concurrent fixture runs.
