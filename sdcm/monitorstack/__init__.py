import logging
import os
import tarfile
import zipfile
import tempfile
import json
import datetime
import time
from textwrap import dedent
from pathlib import Path

import requests
import yaml

from sdcm.remote import LocalCmdRunner
from sdcm.utils.common import list_logs_by_test_id, S3Storage, remove_files, get_free_port
from sdcm.utils.decorators import retrying


LOGGER = logging.getLogger(name='monitoringstack')

GRAFANA_DOCKER_NAME = "agraf"
PROMETHEUS_DOCKER_NAME = "aprom"
ALERT_DOCKER_NAME = "aalert"

GRAFANA_DOCKER_PORT = get_free_port(ports_to_try=(3000, 0, ))
ALERT_DOCKER_PORT = get_free_port(ports_to_try=(9093, 0, ))
PROMETHEUS_DOCKER_PORT = get_free_port(ports_to_try=(9090, 0, ))
COMMAND_TIMEOUT = 1800


class ErrorUploadSCTDashboard(Exception):
    pass


class ErrorUploadAnnotations(Exception):
    pass


def restore_monitoring_stack(test_id, date_time=None):  # pylint: disable=too-many-return-statements
    if not is_docker_available():
        return False

    arch = get_monitoring_stack_archive(test_id, date_time)
    if not arch:
        return False
    # Arch element structure:
    #     {
    #         "file_path": log_file,
    #         "type": log_type,
    #         "link": link to archive,
    #         "date": date of create
    #     }

    LOGGER.info('Restoring monitoring stack from archive %s', arch['file_path'])
    monitoring_stack_base_dir = tempfile.mkdtemp()
    LOGGER.info(
        f"Download file {arch['link']} to directory {monitoring_stack_base_dir}"
    )

    downloaded_monitoring_archive = S3Storage().download_file(arch['link'],
                                                              dst_dir=monitoring_stack_base_dir)
    monitoring_data_arch = extract_monitoring_data_archive(downloaded_monitoring_archive,
                                                           monitoring_stack_base_dir)
    monitoring_stack_arch = extract_monitoring_stack_archive(downloaded_monitoring_archive,
                                                             monitoring_stack_base_dir)

    if not monitoring_data_arch:
        LOGGER.error("No prometheus snapshot were found in arch %s", arch['file_path'])
        return False
    if not monitoring_stack_arch:
        LOGGER.error("No monitoring stack archive were found in arch %s", arch['file_path'])
        return False

    monitoring_data_dir = create_monitoring_data_dir(monitoring_stack_base_dir, monitoring_data_arch)
    monitoring_stack_dir = create_monitoring_stack_dir(monitoring_stack_base_dir, monitoring_stack_arch)

    if not monitoring_stack_dir or not monitoring_data_dir:
        LOGGER.error('Creating monitoring stack directories failed:\ndata_dir: %s; stack_dir: %s',
                     monitoring_data_dir, monitoring_stack_dir)
    _, scylla_version = get_monitoring_stack_scylla_version(monitoring_stack_dir)

    status = run_monitoring_stack_containers(monitoring_stack_dir, monitoring_data_dir, scylla_version)
    if not status:
        return False

    status = restore_grafana_dashboards_and_annotations(monitoring_stack_dir)
    if not status:
        return False

    status = verify_monitoring_stack()
    if not status:
        remove_files(monitoring_stack_base_dir)
        return False

    LOGGER.info("Monitoring stack is running")
    return True


def get_monitoring_stack_archive(test_id, date_time):
    """Return monitor_set archive file info

    choose from list of archives files stored on S3 for test_run with test_id
    only monitor-set archives sorted by date_time of archive creation
    if date_time profided return archive file info for found files,
    otherwise return latest archive file info
    archive file info:
        {
         "file_path": log_file,
         "type": log_type,
         "link": link to archive,
         "date": date of create
        }

    :param test_id: test-run test-id
    :type test_id: str
    :param date_time: datetime from column eturned by hydra investigate show-logs <TEST_ID>
    :type date_time: str
    :returns: file info data
    :rtype: {dict}
    """
    arch = None

    stored_files_by_test_id = list_logs_by_test_id(test_id)
    monitoring_stack_archives = [arch for arch in stored_files_by_test_id if 'monitor-set' in arch['type']]
    if not monitoring_stack_archives:
        LOGGER.warning('No any archive for monitoring stack')
        return False

    if date_time:
        if found_archives := [
            archive
            for archive in monitoring_stack_archives
            if archive['date']
            == datetime.datetime.strptime(date_time, "%Y%m%d_%H%M%S")
        ]:
            arch = found_archives[-1]
        else:
            LOGGER.warning('Monitoring stack archive for date %s was not found', date_time)
    else:
        arch = monitoring_stack_archives[-1]

    return arch


def create_monitoring_data_dir(base_dir, archive):
    monitoring_data_base_dir = os.path.join(base_dir, 'monitoring_data_dir')
    cmd = dedent("""
        mkdir -p {data_dir}
        cd {data_dir}
        cp {archive} ./
        tar -xvf {archive_name}
        chmod -R 777 {data_dir}
        """.format(data_dir=monitoring_data_base_dir,
                   archive=archive,
                   archive_name=os.path.basename(archive)))
    result = LocalCmdRunner().run(cmd, timeout=COMMAND_TIMEOUT, ignore_status=True)
    if result.exited > 0:
        LOGGER.error("Error during extracting prometheus snapshot. Switch to next archive")
        return False
    return get_monitoring_data_dir(monitoring_data_base_dir)


def create_monitoring_stack_dir(base_dir, archive):
    cmd = dedent("""
        cd {data_dir}
        cp {archive} ./
        tar -xvf {archive_name}
        chmod -R 777 {data_dir}
        """.format(data_dir=base_dir,
                   archive_name=os.path.basename(archive),
                   archive=archive))

    result = LocalCmdRunner().run(cmd, timeout=COMMAND_TIMEOUT, ignore_status=True)
    if result.exited > 0:
        LOGGER.error("Error during extracting monitoring stack")
        return False

    return get_monitoring_stack_dir(base_dir)


def extract_monitoring_stack_archive(downloaded_monitor_set_archive, monitoring_stack_base_dir):

    return extract_file_from_archive('monitoring_data_stack',
                                     downloaded_monitor_set_archive,
                                     monitoring_stack_base_dir)


def extract_monitoring_data_archive(downloaded_monitor_set_archive, monitoring_stack_base_dir):

    return extract_file_from_archive('prometheus_data',
                                     downloaded_monitor_set_archive,
                                     monitoring_stack_base_dir)


def extract_file_from_archive(pattern, archive, extract_dir):
    if archive.endswith('.zip'):
        return extract_file_from_zip_archive(pattern, archive, extract_dir)
    if archive.endswith('.tar.gz'):
        return extract_file_from_tar_archive(pattern, archive, extract_dir)
    raise ValueError(f"Not supported archive type{archive.split('.')[-1]}")


def extract_file_from_zip_archive(pattern, archive, extract_dir):
    found_file = None
    with zipfile.ZipFile(archive) as zfile:
        for name in zfile.namelist():
            if pattern in name:
                target_dir = os.path.join(extract_dir, name)
                if is_path_outside_of_dir(target_dir, extract_dir):
                    LOGGER.warning('Skipping %s file it leads to outside of the target dir', name)
                    continue
                zfile.extract(name, extract_dir)
                found_file = target_dir
                break
    return found_file


def is_path_outside_of_dir(path, base) -> bool:
    real_base = os.path.realpath(base)
    return os.path.commonpath((os.path.realpath(path), real_base)) != real_base


def extract_file_from_tar_archive(pattern, archive, extract_dir):
    found_file = None
    with tarfile.open(archive) as tar_file:
        for name in tar_file.getnames():
            if pattern in name:
                target_dir = os.path.join(extract_dir, name)
                if is_path_outside_of_dir(target_dir, extract_dir):
                    LOGGER.warning('Skipping %s file it leads to outside of the target dir', name)
                    continue
                tar_file.extract(name, extract_dir)
                found_file = target_dir
                break
    return found_file


def get_monitoring_data_dir(base_dir):
    monitoring_data_dirs = [d for d in os.listdir(base_dir)
                            if os.path.isdir(os.path.join(base_dir, d))]
    if not monitoring_data_dirs:
        LOGGER.error("%s is empty. No data found", base_dir)
        return False
    return os.path.join(base_dir, monitoring_data_dirs[0])


def get_monitoring_stack_dir(base_dir):
    monitoring_stack_dir = [d for d in os.listdir(base_dir) if 'scylla-monitoring' in d]
    if not monitoring_stack_dir:
        LOGGER.error("%s is empty. No data found", base_dir)
        return False
    return os.path.join(base_dir, monitoring_stack_dir[0])


def get_monitoring_stack_scylla_version(monitoring_stack_dir):
    try:
        with open(os.path.join(monitoring_stack_dir, 'monitor_version'), encoding="utf-8") as f:  # pylint: disable=invalid-name
            versions = f.read().strip()
        monitoring_version, scylla_version = versions.split(':')
        if not requests.head(f'https://github.com/scylladb/scylla-monitoring/tree/{monitoring_version}'
                             f'/grafana/build/ver_{scylla_version}').ok:
            scylla_version = 'master'

        return monitoring_version, scylla_version
    except Exception:  # pylint: disable=broad-except
        return 'branch-3.0', 'master'


def restore_grafana_dashboards_and_annotations(monitoring_dockers_dir):
    status = []
    try:
        status.extend(
            (
                restore_sct_dashboards(monitoring_dockers_dir),
                restore_annotations_data(monitoring_dockers_dir),
            )
        )

    except Exception as details:  # pylint: disable=broad-except
        LOGGER.error("Error during uploading sct monitoring data %s", details)
        status.append(False)

    return all(status)


def run_monitoring_stack_containers(monitoring_stack_dir, monitoring_data_dir, scylla_version):
    try:
        start_dockers(monitoring_stack_dir, monitoring_data_dir, scylla_version)
        return True
    except Exception as details:  # pylint: disable=broad-except
        LOGGER.error("Dockers are not started. Error: %s", details)
        return False


@retrying(n=3, sleep_time=20, message='Uploading sct dashboard')
def restore_sct_dashboards(monitoring_dockers_dir):
    sct_dashboard_file_name = "*scylla-dash-per-server-nemesis.*.json"
    sct_dashboard_files = list((Path(monitoring_dockers_dir) / 'sct_monitoring_addons').glob(sct_dashboard_file_name))

    if not sct_dashboard_files or not os.path.exists(sct_dashboard_files[0]):
        LOGGER.warning('There is no dashboard %s. defaults to master dashboard', sct_dashboard_file_name)
        sct_dashboard_file_name = "scylla-dash-per-server-nemesis.master.json"
        sct_dashboard_files = [Path(__file__).parent.parent.parent / 'data_dir' / sct_dashboard_file_name]

    dashboard_url = f'http://localhost:{GRAFANA_DOCKER_PORT}/api/dashboards/db'
    sct_dashboard_file = sct_dashboard_files[0]
    with open(sct_dashboard_file, encoding="utf-8") as f:  # pylint: disable=invalid-name
        dashboard_config = json.load(f)
        # NOTE: remove value from the 'dashboard.id' field to avoid following error:
        #
        #       Error message {"message":"Dashboard not found","status":"not-found"}
        #
        #       Creating dashboard from scratch we should not have 'id', because Grafana will try
        #       to 'update' existing one which is absent in such a case.
        if dashboard_config.get('dashboard', {}).get('id'):
            dashboard_config['dashboard']['id'] = None

    try:
        res = requests.post(dashboard_url,
                            data=json.dumps(dashboard_config),
                            headers={'Content-Type': 'application/json'})

        if res.status_code != 200:
            LOGGER.info('Error uploading dashboard %s. Error message %s', sct_dashboard_file, res.text)
            raise ErrorUploadSCTDashboard(
                f'Error uploading dashboard {sct_dashboard_file}. Error message {res.text}'
            )

        LOGGER.info('Dashboard %s loaded successfully', sct_dashboard_file)
        return True
    except Exception as details:  # pylint: disable=broad-except
        LOGGER.error('Error uploading dashboard %s. Error message %s', sct_dashboard_file, details)
        raise


@retrying(n=3, sleep_time=20, message='Uploading annotations data')
def restore_annotations_data(monitoring_stack_dir):
    annotations_file = os.path.join(monitoring_stack_dir,
                                    'sct_monitoring_addons',
                                    'annotations.json')

    if not os.path.exists(annotations_file):
        LOGGER.info('There is no annotations file.Skip loading annotations')
        return False
    try:
        with open(annotations_file, encoding="utf-8") as f:  # pylint: disable=invalid-name
            annotations = json.load(f)

        annotations_url = f"http://localhost:{GRAFANA_DOCKER_PORT}/api/annotations"
        for an in annotations:  # pylint: disable=invalid-name
            res = requests.post(annotations_url, data=json.dumps(an), headers={'Content-Type': 'application/json'})
            if res.status_code != 200:
                LOGGER.info('Error during uploading annotation %s. Error message %s', an, res.text)
                raise ErrorUploadAnnotations(
                    f'Error during uploading annotation {an}. Error message {res.text}'
                )

        LOGGER.info('Annotations loaded successfully')
        return True
    except Exception as details:  # pylint: disable=broad-except
        LOGGER.error("Error during annotations data upload %s", details)
        raise


@retrying(n=3, sleep_time=5, message='Start docker containers')
def start_dockers(monitoring_dockers_dir, monitoring_stack_data_dir, scylla_version):  # pylint: disable=unused-argument
    graf_port = GRAFANA_DOCKER_PORT
    alert_port = ALERT_DOCKER_PORT
    prom_port = PROMETHEUS_DOCKER_PORT
    lr = LocalCmdRunner()  # pylint: disable=invalid-name
    lr.run('cd {monitoring_dockers_dir}; ./kill-all.sh -g {graf_port} -m {alert_port} -p {prom_port}'.format(**locals()),
           ignore_status=True, verbose=False)

    # clear scylla nodes from configuration
    servers_yaml = Path(monitoring_dockers_dir) / 'config' / 'scylla_servers.yml'
    servers_yaml.write_text("- targets: []")

    # clear SCT scrape configurations
    prom_tmpl_file = Path(monitoring_dockers_dir) / 'prometheus' / 'prometheus.yml.template'
    templ_yaml = yaml.safe_load(prom_tmpl_file.read_text())

    def remove_sct_metrics(metric):
        return '_metrics' not in metric['job_name']

    templ_yaml["scrape_configs"] = list(filter(remove_sct_metrics, templ_yaml["scrape_configs"]))
    prom_tmpl_file.write_text(yaml.safe_dump(templ_yaml))

    cmd = dedent("""cd {monitoring_dockers_dir};
            echo "" > UA.sh
            ./start-all.sh \
            $(grep -q -- --no-renderer ./start-all.sh && echo "--no-renderer")  \
            $(grep -q -- --no-loki ./start-all.sh && echo "--no-loki")  \
            -g {graf_port} -m {alert_port} -p {prom_port} \
            -s {monitoring_dockers_dir}/config/scylla_servers.yml \
            -d {monitoring_stack_data_dir} -v {scylla_version} \
            -b '-storage.tsdb.retention.time=100y' \
            -c 'GF_USERS_DEFAULT_THEME=dark'""".format(**locals()))
    res = lr.run(cmd)
    if res.ok:
        LOGGER.info("Docker containers for monitoring stack are started")


def is_docker_available():
    LOGGER.info("Checking that docker is available...")
    result = LocalCmdRunner().run('docker ps', ignore_status=True, verbose=False)
    if result.ok:
        LOGGER.info('Docker is available')
        return True
    else:
        LOGGER.warning('Docker is not available on your computer. Please install docker software before continue')
        return False


def verify_monitoring_stack():
    checked_statuses = [verify_dockers_are_running()]
    checked_statuses.append(verify_grafana_is_available())
    checked_statuses.append(verify_prometheus_is_available())
    return all(checked_statuses)


def verify_dockers_are_running():
    result = LocalCmdRunner().run("docker ps --format '{{.Names}}'", ignore_status=True)  # pylint: disable=invalid-name
    docker_names = result.stdout.strip().split()
    if (
        result.ok
        and docker_names
        and f"{GRAFANA_DOCKER_NAME}-{GRAFANA_DOCKER_PORT}" in docker_names
        and f"{PROMETHEUS_DOCKER_NAME}-{PROMETHEUS_DOCKER_PORT}"
        in docker_names
    ):
        LOGGER.info("Monitoring stack docker containers are running.\n%s", result.stdout)
        return True
    LOGGER.error("Monitoring stack containers are not running\nStdout:\n%s\nstderr:%s", result.stdout, result.stderr)
    return False


def verify_grafana_is_available():
    # pylint: disable=import-outside-toplevel
    from sdcm.logcollector import GrafanaEntity, MonitoringStack
    grafana_statuses = []
    for dashboard in GrafanaEntity.base_grafana_dashboards:
        try:
            LOGGER.info("Check dashboard %s", dashboard.title)
            result = MonitoringStack.get_dashboard_by_title(grafana_ip="localhost",
                                                            port=GRAFANA_DOCKER_PORT,
                                                            title=dashboard.title)
            grafana_statuses.append(result)
            LOGGER.info(f"Dashboard {dashboard.title} is available")
        except Exception as details:  # pylint: disable=broad-except
            LOGGER.error("Dashboard %s is not available. Error: %s", dashboard.title, details)
            grafana_statuses.append(False)

    result = any(grafana_statuses)

    if not result:
        LOGGER.error("None of the expected dashboards are available.")

    return result


def verify_prometheus_is_available():
    """Get result from prometheus for latest 10 minutes

    Validate that request to Prometheus container is not failed
    :returns: True if request is successful, False otherwise
    :rtype: {bool}
    """

    # pylint: disable=import-outside-toplevel
    from sdcm.db_stats import PrometheusDBStats

    time_end = time.time()
    time_start = time_end - 600
    try:
        LOGGER.info("Send request to Prometheus")
        prom_client = PrometheusDBStats("localhost", port=PROMETHEUS_DOCKER_PORT)
        prom_client.get_throughput(time_start, time_end)
        LOGGER.info("Prometheus is up")
        return True
    except Exception as details:  # pylint: disable=broad-except
        LOGGER.error("Error requesting prometheus %s", details)
        return False


def get_monitoring_stack_services():
    return [{"service": "Grafana service", "name": GRAFANA_DOCKER_NAME, "port": GRAFANA_DOCKER_PORT},
            {"service": "Prometheus service", "name": PROMETHEUS_DOCKER_NAME, "port": PROMETHEUS_DOCKER_PORT},
            {"service": "Alert service", "name": ALERT_DOCKER_NAME, "port": ALERT_DOCKER_PORT}]


def kill_running_monitoring_stack_services():
    lr = LocalCmdRunner()  # pylint: disable=invalid-name
    for docker in get_monitoring_stack_services():
        LOGGER.info("Killing %s", docker['service'])
        lr.run('docker rm -f {name}-{port}'.format(name=docker['name'], port=docker['port']),
               ignore_status=True)
