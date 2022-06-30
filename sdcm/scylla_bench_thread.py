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
# Copyright (c) 2020 ScyllaDB

import os
import re
import uuid
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum

from sdcm.loader import ScyllaBenchStressExporter
from sdcm.prometheus import nemesis_metrics_obj
from sdcm.sct_events import Severity
from sdcm.sct_events.loaders import ScyllaBenchEvent, SCYLLA_BENCH_ERROR_EVENTS_PATTERNS
from sdcm.utils.common import FileFollowerThread, generate_random_string, convert_metric_to_ms
from sdcm.stress_thread import format_stress_cmd_error
from sdcm.wait import wait_for


LOGGER = logging.getLogger(__name__)


class ScyllaBenchModes(Enum):
    WRITE = "write"
    READ = "read"
    COUNTER_UPDATE = "counter_update"
    COUNTER_READ = "counter_read"
    SCAN = "scan"


class ScyllaBenchWorkloads(Enum):
    UNIFORM = "uniform"
    TIMESERIES = "timeseries"
    SEQUENTIAL = "sequential"


class ScyllaBenchStressEventsPublisher(FileFollowerThread):
    def __init__(self, node, sb_log_filename, event_id=None):
        super().__init__()
        self.sb_log_filename = sb_log_filename
        self.node = str(node)
        self.event_id = event_id

    def run(self):
        while not self.stopped():
            exists = os.path.isfile(self.sb_log_filename)
            if not exists:
                time.sleep(0.5)
                continue

            for line_number, line in enumerate(self.follow_file(self.sb_log_filename)):
                if self.stopped():
                    break

                for pattern, event in SCYLLA_BENCH_ERROR_EVENTS_PATTERNS:
                    if self.event_id:
                        # Connect the event to the stress load
                        event.event_id = self.event_id

                    if pattern.search(line):
                        event.add_info(node=self.node, line=line, line_number=line_number).publish()


class ScyllaBenchThread:  # pylint: disable=too-many-instance-attributes
    _SB_STATS_MAPPING = {
        # Mapping for scylla-bench statistic and configuration keys to db stats keys
        'Mode': 'Mode',
        'Workload': 'Workload',
        'Timeout': 'Timeout',
        'Consistency level': 'Consistency level',
        'Partition count': 'Partition count',
        'Clustering rows': 'Clustering rows',
        'Page size': 'Page size',
        'Concurrency': 'Concurrency',
        'Connections': 'Connections',
        'Maximum rate': 'Maximum rate',
        'Client compression': 'Client compression',
        'Clustering row size': 'Clustering row size',
        'Rows per request': 'Rows per request',
        'Total rows': 'Total rows',
        'max': 'latency max',
        '99.9th': 'latency 99.9th percentile',
        '99th': 'latency 99th percentile',
        '95th': 'latency 95th percentile',
        '90th': '90th',
        'median': 'latency median',
        'Operations/s': 'op rate',
        'Rows/s': 'row rate',
        'Total ops': 'Total partitions',
        'Time (avg)': 'Total operation time',
    }

    # pylint: disable=too-many-arguments
    def __init__(self, stress_cmd, loader_set, timeout, node_list=None, round_robin=False, use_single_loader=False,
                 stop_test_on_failure=False, stress_num=1, credentials=None):
        if not node_list:
            node_list = []
        self.loader_set = loader_set
        self.stress_cmd = stress_cmd
        self.timeout = timeout
        self.use_single_loader = use_single_loader
        self.round_robin = round_robin
        self.node_list = node_list
        self.stress_num = stress_num
        if credentials and 'username=' not in self.stress_cmd:
            self.stress_cmd += " -username {} -password {}".format(*credentials)
        self.stress_cmd += ' -error-at-row-limit 1000'  # make it fail after having 1000 errors at row
        self.stop_test_on_failure = stop_test_on_failure

        self.executor = None
        self.results_futures = []
        self.shell_marker = generate_random_string(20)
        self.max_workers = 0
        # Find stress mode:
        #    "scylla-bench -workload=sequential -mode=write -replication-factor=3 -partition-count=100"
        #    "scylla-bench -workload=uniform -mode=read -replication-factor=3 -partition-count=100"
        self.sb_mode: ScyllaBenchModes = ScyllaBenchModes(
            re.search(r"-mode=(.+?) ", stress_cmd)[1]
        )

        self.sb_workload: ScyllaBenchWorkloads = ScyllaBenchWorkloads(
            re.search(r"-workload=(.+?) ", stress_cmd)[1]
        )

    def verify_results(self):
        sb_summary = []
        errors = []

        LOGGER.debug('Wait for stress threads results')
        results = [
            future.result()
            for future in as_completed(self.results_futures, timeout=self.timeout)
        ]

        for _, result in results:
            if not result:
                # Silently skip if stress command threw an error, since it was already reported in _run_stress
                continue
            output = result.stdout + result.stderr

            lines = output.splitlines()
            if node_cs_res := self._parse_bench_summary(lines):
                sb_summary.append(node_cs_res)

        return sb_summary, errors

    def _run_stress_bench(self, node, loader_idx, stress_cmd, node_list):
        if self.sb_mode == ScyllaBenchModes.WRITE and self.sb_workload == ScyllaBenchWorkloads.TIMESERIES:
            node.parent_cluster.sb_write_timeseries_ts = write_timestamp = time.time_ns()
            LOGGER.debug("Set start-time: %s", write_timestamp)
            stress_cmd = re.sub(r"SET_WRITE_TIMESTAMP", f"{write_timestamp}", stress_cmd)
            LOGGER.debug("Replaced stress command: %s", stress_cmd)

        elif self.sb_mode == ScyllaBenchModes.READ and self.sb_workload == ScyllaBenchWorkloads.TIMESERIES:
            write_timestamp = wait_for(lambda: node.parent_cluster.sb_write_timeseries_ts,
                                       step=5,
                                       timeout=30,
                                       text='Waiting for "scylla-bench -workload=timeseries -mode=write" been started, to pick up timestamp'
                                       )
            LOGGER.debug("Found write timestamp %s", write_timestamp)
            stress_cmd = re.sub(r"GET_WRITE_TIMESTAMP", f"{write_timestamp}", stress_cmd)
            LOGGER.debug("replaced stress command %s", stress_cmd)
        else:
            LOGGER.debug("Scylla bench command: %s", stress_cmd)

        os.makedirs(node.logdir, exist_ok=True)

        log_file_name = os.path.join(node.logdir, f'scylla-bench-l{loader_idx}-{uuid.uuid4()}.log')
        # Select first seed node to send the scylla-bench cmds
        ips = node_list[0].cql_ip_address

        with ScyllaBenchStressExporter(instance_name=node.cql_ip_address,
                                           metrics=nemesis_metrics_obj(),
                                           stress_operation=self.sb_mode,
                                           stress_log_filename=log_file_name,
                                           loader_idx=loader_idx), ScyllaBenchStressEventsPublisher(node=node, sb_log_filename=log_file_name) as publisher, ScyllaBenchEvent(node=node, stress_cmd=stress_cmd,
                                     log_file_name=log_file_name) as scylla_bench_event:
            publisher.event_id = scylla_bench_event.event_id
            result = None
            try:
                result = node.remoter.run(
                    cmd="/$HOME/go/bin/{name} -nodes {ips}".format(name=stress_cmd.strip(), ips=ips),
                    timeout=self.timeout,
                    log_file=log_file_name)
            except Exception as exc:  # pylint: disable=broad-except
                errors_str = format_stress_cmd_error(exc)
                if (
                    "truncate: seastar::rpc::timeout_error" in errors_str
                    or not self.stop_test_on_failure
                ):
                    scylla_bench_event.severity = Severity.ERROR
                else:
                    scylla_bench_event.severity = Severity.CRITICAL
                scylla_bench_event.add_error([errors_str])

        return node, result

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

        for loader in loaders:
            if not loader.is_scylla_bench_installed:
                loader.install_scylla_bench()

        self.max_workers = (os.cpu_count() or 1) * 5
        LOGGER.debug("Starting %d scylla-bench Worker threads", self.max_workers)
        self.executor = ThreadPoolExecutor(max_workers=self.max_workers)  # pylint: disable=consider-using-with

        for loader_idx, loader in enumerate(loaders):
            self.results_futures += [self.executor.submit(self._run_stress_bench,
                                                          *(loader, loader_idx, self.stress_cmd, self.node_list))]
            time.sleep(60)

        return self

    @classmethod
    def _parse_bench_summary(cls, lines):
        """
        Parsing bench results, only parse the summary results.
        Collect results of all nodes and return a dictionaries' list,
        the new structure data will be easy to parse, compare, display or save.
        """
        results = {'keyspace_idx': None, 'stdev gc time(ms)': None, 'Total errors': None,
                   'total gc count': None, 'loader_idx': None, 'total gc time (s)': None,
                   'total gc mb': 0, 'cpu_idx': None, 'avg gc time(ms)': None, 'latency mean': None}

        for line in lines:
            line.strip()
            # Parse load params
            # pylint: disable=too-many-boolean-expressions
            if line.startswith('Results'):
                continue
            if 'c-o fixed latency' in line:
                # Ignore C-O Fixed latencies
                #
                # c-o fixed latency :
                #   max:        5.668863ms
                #   99.9th:	    5.537791ms
                #   99th:       3.440639ms
                #   95th:       3.342335ms
                break

            split = line.split(':', maxsplit=1)
            if len(split) < 2:
                continue
            key = split[0].strip()
            value = ' '.join(split[1].split())
            if target_key := cls._SB_STATS_MAPPING.get(key):
                value = int(value) if value.isdecimal() else convert_metric_to_ms(value)
                results[target_key] = value
            else:
                LOGGER.debug('unknown result key found: `%s` with value `%s`', key, value)
        row_rate = results.get('row rate')
        if row_rate is not None:
            results['partition rate'] = row_rate
        return results
