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

import os
import uuid
import time
import logging
import concurrent.futures

from sdcm.loader import CassandraHarryStressExporter
from sdcm.prometheus import nemesis_metrics_obj
from sdcm.sct_events.loaders import CassandraHarryEvent, CASSANDRA_HARRY_ERROR_EVENTS_PATTERNS
from sdcm.utils.common import FileFollowerThread, generate_random_string
from sdcm.stress_thread import format_stress_cmd_error


LOGGER = logging.getLogger(__name__)


class CassandraHarryStressEventsPublisher(FileFollowerThread):
    def __init__(self, node, harry_log_filename):
        super().__init__()
        self.harry_log_filename = harry_log_filename
        self.node = str(node)

    def run(self):
        while not self.stopped():
            exists = os.path.isfile(self.harry_log_filename)
            if not exists:
                time.sleep(0.5)
                continue

            for line_number, line in enumerate(self.follow_file(self.harry_log_filename)):
                if self.stopped():
                    break

                for pattern, event in CASSANDRA_HARRY_ERROR_EVENTS_PATTERNS:
                    if pattern.search(line):
                        event.add_info(node=self.node, line=line, line_number=line_number).publish()


#  pylint: disable=too-many-instance-attributes
class CassandraHarryThread:
    #  pylint: disable=too-many-arguments
    def __init__(self, stress_cmd, loader_set, timeout, node_list=None, round_robin=False, use_single_loader=False,
                 stop_test_on_failure=False, stress_num=1, credentials=None):
        self.stress_cmd = stress_cmd
        if credentials and "username=" not in self.stress_cmd:
            self.stress_cmd += " -username {} -password {}".format(*credentials)
        self.loader_set = loader_set
        self.timeout = timeout
        self.node_list = node_list or []
        self.round_robin = round_robin
        self.use_single_loader = use_single_loader
        self.stop_test_on_failure = stop_test_on_failure
        self.stress_num = stress_num

        self.executor = None
        self.results_futures = []
        self.shell_marker = generate_random_string(20)
        self.max_workers = 0

    def get_stress_results_bench(self):
        ret = []
        LOGGER.debug('Wait for stress threads results')
        results = [
            future.result()
            for future in concurrent.futures.as_completed(
                self.results_futures, timeout=self.timeout
            )
        ]

        for _, result in results:
            if not result:
                # Silently skip if stress command throwed error, since it was already reported in _run_stress
                continue
            output = result.stdout + result.stderr
            try:
                lines = output.splitlines()
                if node_cs_res := self._parse_harry_summary(lines):
                    ret.append(node_cs_res)
            except Exception as exc:  # pylint: disable=broad-except
                CassandraHarryEvent.error(node="",
                                          stress_cmd=self.stress_cmd,
                                          errors=[f"Failed to proccess stress summary due to {exc}"]).publish()

        return ret

    def verify_results(self):
        harry_summary = []
        errors = []

        LOGGER.debug('Wait for stress threads results')
        results = [
            future.result()
            for future in concurrent.futures.as_completed(
                self.results_futures, timeout=self.timeout
            )
        ]

        for node, result in results:
            if not result:
                # Silently skip if stress command throwed error, since it was already reported in _run_stress
                continue
            output = result.stdout + result.stderr

            lines = output.splitlines()
            if node_cs_res := self._parse_harry_summary(lines):
                harry_summary.append(node_cs_res)

            for line in lines:
                if 'java.io.IOException' in line:
                    errors += [f'{node}: {line.strip()}']

        return harry_summary, errors

    def run(self):
        if self.round_robin:
            loaders = [self.loader_set.get_loader()]
        else:
            loaders = (
                [self.loader_set.nodes[0]]
                if self.use_single_loader
                else self.loader_set.nodes
            )

        LOGGER.debug(f"Round-Robin through loaders, Selected loader is {loaders} ")

        self.max_workers = (os.cpu_count() or 1) * 5
        LOGGER.debug("Starting %d cassandra-harry Worker threads", self.max_workers)
        # pylint: disable=consider-using-with
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers)

        for loader_idx, loader in enumerate(loaders):
            self.results_futures += [self.executor.submit(self._run_stress_harry,
                                                          *(loader, loader_idx, self.stress_cmd, self.node_list))]
            time.sleep(60)

        return self

    def _run_stress_harry(self, node, loader_idx, stress_cmd, node_list):

        CassandraHarryEvent.start(node=node, stress_cmd=stress_cmd).publish()
        os.makedirs(node.logdir, exist_ok=True)

        log_file_name = os.path.join(node.logdir, f'cassandra-harry-l{loader_idx}-{uuid.uuid4()}.log')
        # Select first seed node to send the scylla-harry cmds
        ip = node_list[0].cql_ip_address

        with CassandraHarryStressExporter(instance_name=node.cql_ip_address,
                                          metrics=nemesis_metrics_obj(),
                                          stress_operation='write',
                                          stress_log_filename=log_file_name,
                                          loader_idx=loader_idx), \
                CassandraHarryStressEventsPublisher(node=node, harry_log_filename=log_file_name):
            result = None
            try:
                result = node.remoter.run(
                    cmd=f"{stress_cmd} -node {ip}",
                    timeout=self.timeout,
                    log_file=log_file_name)
            except Exception as exc:  # pylint: disable=broad-except
                errors_str = format_stress_cmd_error(exc)
                if "timeout" in errors_str:
                    event_type = CassandraHarryEvent.timeout
                elif self.stop_test_on_failure:
                    event_type = CassandraHarryEvent.failure
                else:
                    event_type = CassandraHarryEvent.error
                event_type(
                    node=node,
                    stress_cmd=stress_cmd,
                    log_file_name=log_file_name,
                    errors=[errors_str, ],
                ).publish()
            else:
                CassandraHarryEvent.finish(node=node, stress_cmd=stress_cmd, log_file_name=log_file_name).publish()

        return node, result

    @staticmethod
    def _parse_harry_summary(lines):    # pylint: disable=too-many-branches
        """
        Currently we only check if it succeeds or not.
        A succeed sample:

        INFO  [pool-6-thread-1] instance_id_IS_UNDEFINED 2021-01-19 13:46:49,835 Runner.java:255 - Completed
        """
        return {
            'status': 'completed'
            if any('Runner.java:255 - Completed' in line for line in lines)
            else 'failed'
        }
