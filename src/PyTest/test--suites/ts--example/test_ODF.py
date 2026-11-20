import logging
from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from fixtures.ODF import FxtObjODF


logs = logging.getLogger()


class TestODF():
    '''
    Non-disruptive ODF Ceph fixture examples: read-only queries only, no fault
    injection.
    '''

    @pytest.mark.odf
    def test_CephHealth(self, fxtODF: 'FxtObjODF') -> None:
        '''Check Ceph `HEALTH_OK` via the fixture-enabled Ceph toolbox.'''
        (status, checks) = fxtODF.storage.ceph.Health()
        logs.info('Ceph health: `%s` %s.', status, sorted(checks))
        assert (status == 'HEALTH_OK'), f'`{status}` {sorted(checks)}'

    @pytest.mark.odf
    def test_MDSdiscovery(self, fxtODF: 'FxtObjODF') -> None:
        '''Check the discovered MDS daemon roles, Pods, and Nodes.'''
        mds = fxtODF.storage.ceph.MDS.Discover()
        states = fxtODF.ceph.MDSstates(mds.fsName)
        logs.info('File system `%s` MDS states: %s.', mds.fsName, states)
        assert (states.get(mds.active.name) == 'up:active')
        assert (states.get(mds.standby.name) == 'up:standby-replay')
        for (role, daemon, node) in (
            ('active', mds.active, mds.activeNode),
            ('standby-replay', mds.standby, mds.stbyNode),
        ):
            (pod, nodeName) = daemon.Pod()
            logs.info(
                'The %s MDS `%s`: Pod `%s` on Node `%s`.',
                role, daemon.name, pod, nodeName,
            )
            assert pod, f'No Running Pod for MDS `{daemon.name}`.'
            assert (nodeName == node.name)
