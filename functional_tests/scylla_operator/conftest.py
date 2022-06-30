#!/usr/bin/env python

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See LICENSE for more details.
#
# Copyright (c) 2021 ScyllaDB

import logging
import os
import traceback
import contextlib

from typing import Optional

import pytest

from functional_tests.scylla_operator.libs.auxiliary import ScyllaOperatorFunctionalClusterTester, sct_abs_path
from sdcm.cluster_k8s import ScyllaPodCluster
from sdcm.utils import version_utils


TESTER: Optional[ScyllaOperatorFunctionalClusterTester] = None
LOGGER = logging.getLogger(__name__)


# NOTE: fixtures which have "fixture_" prefix can be used without it.
#       it is added to avoid following pylint error:
#
#           W0621: Redefining name %r from outer scope (line %s)
#
#       Which appears when we define and use fixtures in one single module like we do here.


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):  # pylint: disable=unused-argument
    # Populate test result to test function instance
    outcome = yield
    rep = outcome.get_result()
    if rep.skipped:
        item._test_result = ('SKIPPED', rep.longrepr[2])  # pylint: disable=protected-access
        TESTER.update_test_status(item.nodeid, *item._test_result)  # pylint: disable=protected-access
    elif rep.passed:
        item._test_result = ('SUCCESS', None)  # pylint: disable=protected-access
    else:
        item._test_result = ('FAILED', str(rep.longrepr))  # pylint: disable=protected-access


@pytest.fixture(autouse=True, name="harvest_test_results")
def fixture_harvest_test_results(request, tester: ScyllaOperatorFunctionalClusterTester):
    # Pickup test results at the end of the test and submit it to the tester

    def publish_test_result():
        tester.update_test_status(
            request.node.nodeid,
            *request.node._test_result)  # pylint: disable=protected-access

    request.addfinalizer(publish_test_result)


@pytest.fixture(autouse=True, scope='package', name="tester")
def fixture_tester() -> ScyllaOperatorFunctionalClusterTester:
    global TESTER  # pylint: disable=global-statement
    os.chdir(sct_abs_path())
    tester_inst = ScyllaOperatorFunctionalClusterTester()
    TESTER = tester_inst  # putting tester global, so we can report skipped test (one with mark.skip)
    tester_inst.setUpClass()
    tester_inst.setUp()
    yield tester_inst
    with contextlib.suppress(Exception):
        tester_inst.tearDown()
    with contextlib.suppress(Exception):
        tester_inst.tearDownClass()


@pytest.fixture(autouse=True, scope="function")
def change_test_dir(request):
    os.chdir(sct_abs_path())
    yield
    os.chdir(request.config.invocation_dir)


@pytest.fixture(autouse=True)
def skip_if_node_termination_method_not_supported(request, db_cluster: ScyllaPodCluster) -> None:
    marker = request.node.get_closest_marker('requires_node_termination_support')
    if marker and marker.args:
        supported_methods = getattr(db_cluster, 'node_terminate_methods', [])
        for terminate_method in marker.args:
            if terminate_method not in supported_methods:
                pytest.skip(f'cluster {type(db_cluster).__name__} does not support {terminate_method} '
                            'node termination method')


@pytest.fixture(autouse=True)
def skip_if_scylla_manager_required_and_absent(request, tester: ScyllaOperatorFunctionalClusterTester) -> None:
    if request.node.get_closest_marker('requires_mgmt') and not tester.params.get('use_mgmt'):
        pytest.skip('test requires scylla manager to exist')


@pytest.fixture(name="db_cluster")
def fixture_db_cluster(tester: ScyllaOperatorFunctionalClusterTester):
    if not tester.healthy_flag:
        pytest.skip('cluster is not healthy, skipping rest of the tests')

    with tester.db_cluster.scylla_config_map() as scylla_config_map:
        original_scylla_config_map = scylla_config_map
    original_scylla_cluster_spec = tester.db_cluster.get_scylla_cluster_plain_value('/spec')

    yield tester.db_cluster

    if tester.healthy_flag:
        _bring_cluster_back_to_original_state(
            tester,
            config_map=original_scylla_config_map,
            original_scylla_cluster_spec=original_scylla_cluster_spec
        )


def _bring_cluster_back_to_original_state(
        tester: ScyllaOperatorFunctionalClusterTester,
        config_map: dict,
        original_scylla_cluster_spec: dict
):
    restart = False
    db_cluster = tester.db_cluster
    try:
        # Restore cluster spec, there is one problem though:
        #  s-o does not support rack removal, so if we see an extra rack we need to remove members form it
        #  but keep it in the cluster spec
        # Other alternative is to redeploy cluster, which is expensive and we will do so only if it is found needed

        original_rack_specs = original_scylla_cluster_spec.get('datacenter', {}).get('racks', [])
        current_cluster_spec = db_cluster.get_scylla_cluster_plain_value('/spec')
        current_rack_specs = current_cluster_spec.get('datacenter', {}).get('racks', [])
        if len(original_rack_specs) < len(current_rack_specs):
            # A new racks with 0 members in them to the original cluster specification
            # At this point original_rack_specs is more like cluster spec we want to have
            new_racks = current_rack_specs[len(original_rack_specs):]
            for rack in new_racks:
                rack['members'] = 0
            original_rack_specs.extend(new_racks)

        # Restore config-map scylla-config
        with db_cluster.scylla_config_map() as recover_config_map:
            if recover_config_map != config_map:
                recover_config_map.clear()
                recover_config_map.update(config_map)
                restart = True

        # NOTE: ignore 'forceRedeploymentReason' field always to avoid redundant restarts
        original_scylla_cluster_spec.pop("forceRedeploymentReason", None)
        current_cluster_spec.pop("forceRedeploymentReason", None)
        if original_scylla_cluster_spec != current_cluster_spec:
            # If cluster spec we currently have is not equal to what we want replace it.
            # It will cause scylla pods rollout restart on the operator level.
            # WARNING: if number of nodes differs than we will have incorrect data
            #          in "db_cluster.nodes". For the moment all changes to node number must
            #          go though 'add_nodes' and 'decommision' methods only.
            db_cluster.replace_scylla_cluster_value('/spec', original_scylla_cluster_spec)
            db_cluster.wait_sts_rollout_restart(len(db_cluster.nodes))
            restart = False

        if restart:
            db_cluster.restart_scylla()
        db_cluster.wait_for_nodes_up_and_normal(
            nodes=db_cluster.nodes, verification_node=db_cluster.nodes[0])
    except Exception as exc:  # pylint: disable=broad-except
        tester.healthy_flag = False
        pytest.fail("Failed to bring cluster nodes back to original state due to :\n" +
                    "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))


@pytest.fixture(name="cassandra_rackdc_properties")
def fixture_cassandra_rackdc_properties(db_cluster: ScyllaPodCluster):
    return db_cluster.remote_cassandra_rackdc_properties


@pytest.fixture(name="scylla_yaml")
def fixture_scylla_yaml(db_cluster: ScyllaPodCluster):
    return db_cluster.remote_scylla_yaml


# NOTE: Reason: https://github.com/scylladb/scylla/issues/9543
@pytest.fixture(autouse=True)
def skip_if_scylla_not_semver(request, tester: ScyllaOperatorFunctionalClusterTester) -> None:
    if request.node.get_closest_marker("restart_is_used") and not version_utils.SEMVER_REGEX.match(
            tester.db_cluster.params.get("scylla_version")):
        pytest.skip(
            "test calls 'restart_scylla()' DB method that won't have any effect using non-semver Scylla")
