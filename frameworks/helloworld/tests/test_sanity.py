import json
import logging
import re
from typing import Any, Dict, Iterator, List

import pytest
import retrying

import sdk_cmd
import sdk_install
import sdk_marathon
import sdk_metrics
import sdk_plan
import sdk_tasks
import sdk_upgrade
import sdk_utils

from tests import config


log = logging.getLogger(__name__)
foldered_name = sdk_utils.get_foldered_name(config.SERVICE_NAME)


@pytest.fixture(scope="module", autouse=True)
def configure_package(configure_security: None) -> Iterator[Dict[str, Any]]:
    try:
        service_options = {"service": {"name": foldered_name}}
        sdk_install.uninstall(config.PACKAGE_NAME, foldered_name)
        sdk_upgrade.test_upgrade(
            config.PACKAGE_NAME,
            foldered_name,
            config.DEFAULT_TASK_COUNT,
            additional_options=service_options,
        )

        yield {"package_name": config.PACKAGE_NAME, **service_options}
    finally:
        sdk_install.uninstall(config.PACKAGE_NAME, foldered_name)


@pytest.mark.sanity
@pytest.mark.smoke
@pytest.mark.dcos_min_version("1.9")
def test_metrics_cli_for_scheduler_metrics(configure_package: Dict[str, Any]) -> None:
    scheduler_task_prefix = sdk_marathon.get_scheduler_task_prefix(
        configure_package["service"]["name"]
    )
    scheduler_task_id = sdk_tasks.get_task_ids("marathon", scheduler_task_prefix).pop()
    scheduler_metrics = sdk_metrics.wait_for_metrics_from_cli(scheduler_task_id, timeout_seconds=60)

    assert scheduler_metrics, "Expecting a non-empty set of metrics"


@pytest.mark.sanity
@pytest.mark.smoke
@pytest.mark.dcos_min_version("1.9")
def test_metrics_for_task_metrics(configure_package: Dict[str, Any]) -> None:

    def write_metric_to_statsd_counter(metric_name: str, value: int) -> None:
        """
        Write a metric with the specified value to statsd.

        This is done by echoing the statsd string through ncat to the statsd host an port.
        """
        metric_echo = 'echo \\"{}:{}|c\\"'.format(metric_name, value)
        ncat_command = 'ncat -w 1 -u \\$STATSD_UDP_HOST \\$STATSD_UDP_PORT'
        pipe = " | "

        bash_command = sdk_cmd.get_bash_command(
            metric_echo + pipe + ncat_command,
            environment=None,
        )
        sdk_cmd.service_task_exec(configure_package["service"]["name"], "hello-0-server", bash_command)

    metric_name = "test.metrics.CamelCaseMetric"
    write_metric_to_statsd_counter(metric_name, 1)

    def expected_metrics_exist(emitted_metrics: List[str]) -> bool:
        return sdk_metrics.check_metrics_presence(emitted_metrics, [metric_name])

    sdk_metrics.wait_for_service_metrics(
        configure_package["package_name"],
        configure_package["service"]["name"],
        "hello-0",
        "hello-0-server",
        timeout=5 * 60,
        expected_metrics_callback=expected_metrics_exist,
    )


@pytest.mark.sanity
@pytest.mark.smoke
def test_bump_hello_cpus() -> None:
    hello_ids = sdk_tasks.get_task_ids(foldered_name, "hello")
    log.info("hello ids: " + str(hello_ids))

    updated_cpus = config.bump_hello_cpus(foldered_name)

    sdk_tasks.check_tasks_updated(foldered_name, "hello", hello_ids)
    sdk_plan.wait_for_completed_deployment(foldered_name)

    all_tasks = sdk_tasks.get_service_tasks(foldered_name, task_prefix="hello")
    running_tasks = [t for t in all_tasks if t.state == "TASK_RUNNING"]
    assert len(running_tasks) == config.hello_task_count(foldered_name)
    for t in running_tasks:
        assert config.close_enough(t.resources["cpus"], updated_cpus)


@pytest.mark.sanity
@pytest.mark.smoke
def test_bump_world_cpus() -> None:
    original_world_ids = sdk_tasks.get_task_ids(foldered_name, "world")
    log.info("world ids: " + str(original_world_ids))

    updated_cpus = config.bump_world_cpus(foldered_name)

    sdk_tasks.check_tasks_updated(foldered_name, "world", original_world_ids)
    sdk_plan.wait_for_completed_deployment(foldered_name)

    all_tasks = sdk_tasks.get_service_tasks(foldered_name, task_prefix="world")
    running_tasks = [t for t in all_tasks if t.state == "TASK_RUNNING"]
    assert len(running_tasks) == config.world_task_count(foldered_name)
    for t in running_tasks:
        assert config.close_enough(t.resources["cpus"], updated_cpus)


@pytest.mark.sanity
@pytest.mark.smoke
def test_increase_decrease_world_nodes() -> None:
    original_hello_ids = sdk_tasks.get_task_ids(foldered_name, "hello")
    original_world_ids = sdk_tasks.get_task_ids(foldered_name, "world")
    log.info("world ids: " + str(original_world_ids))

    # add 2 world nodes
    sdk_marathon.bump_task_count_config(foldered_name, "WORLD_COUNT", 2)

    # autodetects the correct number of nodes and waits for them to deploy:
    config.check_running(foldered_name)
    sdk_plan.wait_for_completed_deployment(foldered_name)

    sdk_tasks.check_tasks_not_updated(foldered_name, "world", original_world_ids)

    # check 2 world tasks added:
    assert 2 + len(original_world_ids) == len(sdk_tasks.get_task_ids(foldered_name, "world"))

    # subtract 2 world nodes
    sdk_marathon.bump_task_count_config(foldered_name, "WORLD_COUNT", -2)

    # autodetects the correct number of nodes and waits for them to deploy:
    config.check_running(foldered_name)
    sdk_plan.wait_for_completed_deployment(foldered_name)

    # wait for the decommission plan for this subtraction to be complete
    sdk_plan.wait_for_completed_plan(foldered_name, "decommission")

    # check that the total task count is back to original
    sdk_tasks.check_running(
        foldered_name, len(original_hello_ids) + len(original_world_ids), allow_more=False
    )
    # check that original tasks weren't affected/relaunched in the process
    sdk_tasks.check_tasks_not_updated(foldered_name, "hello", original_hello_ids)
    sdk_tasks.check_tasks_not_updated(foldered_name, "world", original_world_ids)

    # check that the world tasks are back to their prior state (also without changing task ids)
    assert original_world_ids == sdk_tasks.get_task_ids(foldered_name, "world")


@pytest.mark.sanity
def test_pod_list() -> None:
    rc, stdout, _ = sdk_cmd.svc_cli(config.PACKAGE_NAME, foldered_name, "pod list")
    assert rc == 0, "Pod list failed"
    jsonobj = json.loads(stdout)
    assert len(jsonobj) == config.configured_task_count(foldered_name)
    # expect: X instances of 'hello-#' followed by Y instances of 'world-#',
    # in alphanumerical order
    first_world = -1
    for i in range(len(jsonobj)):
        entry = jsonobj[i]
        if first_world < 0:
            if entry.startswith("world-"):
                first_world = i
        if first_world == -1:
            assert jsonobj[i] == "hello-{}".format(i)
        else:
            assert jsonobj[i] == "world-{}".format(i - first_world)


@pytest.mark.sanity
def test_pod_status_all() -> None:
    # /test/integration/hello-world => test.integration.hello-world
    sanitized_name = sdk_utils.get_task_id_service_name(foldered_name)
    rc, stdout, _ = sdk_cmd.svc_cli(config.PACKAGE_NAME, foldered_name, "pod status --json")
    assert rc == 0, "Pod status failed"
    jsonobj = json.loads(stdout)
    assert jsonobj["service"] == foldered_name
    for pod in jsonobj["pods"]:
        assert re.match("(hello|world)", pod["name"])
        for instance in pod["instances"]:
            assert re.match("(hello|world)-[0-9]+", instance["name"])
            for task in instance["tasks"]:
                assert len(task) == 3
                assert re.match(
                    sanitized_name + "__(hello|world)-[0-9]+-server__[0-9a-f-]+", task["id"]
                )
                assert re.match("(hello|world)-[0-9]+-server", task["name"])
                assert task["status"] == "RUNNING"


@pytest.mark.sanity
def test_pod_status_one() -> None:
    # /test/integration/hello-world => test.integration.hello-world
    sanitized_name = sdk_utils.get_task_id_service_name(foldered_name)
    rc, stdout, _ = sdk_cmd.svc_cli(config.PACKAGE_NAME, foldered_name, "pod status --json hello-0")
    assert rc == 0, "Pod status failed"
    jsonobj = json.loads(stdout)
    assert jsonobj["name"] == "hello-0"
    assert len(jsonobj["tasks"]) == 1
    task = jsonobj["tasks"][0]
    assert len(task) == 3
    assert re.match(sanitized_name + "__hello-0-server__[0-9a-f-]+", task["id"])
    assert task["name"] == "hello-0-server"
    assert task["status"] == "RUNNING"


@pytest.mark.sanity
def test_pod_info() -> None:
    rc, stdout, _ = sdk_cmd.svc_cli(config.PACKAGE_NAME, foldered_name, "pod info world-1")
    assert rc == 0, "Pod info failed"
    jsonobj = json.loads(stdout)
    assert len(jsonobj) == 1
    task = jsonobj[0]
    assert len(task) == 2
    assert task["info"]["name"] == "world-1-server"
    assert task["info"]["taskId"]["value"] == task["status"]["taskId"]["value"]
    assert task["status"]["state"] == "TASK_RUNNING"


@pytest.mark.sanity
def test_state_properties_get() -> None:
    rc, stdout, _ = sdk_cmd.svc_cli(config.PACKAGE_NAME, foldered_name, "debug state properties")
    assert rc == 0, "State properties failed"
    jsonobj = json.loads(stdout)
    # Just check that some expected properties are present. The following may also be present:
    # - "suppressed": Depends on internal scheduler state at the time of the query.
    # - "world-[2,3]-server:task-status": Leftovers from an earlier expansion to 4 world tasks.
    #     In theory, the SDK could clean these up as part of the decommission operation, but they
    #     won't hurt or affect the service, and are arguably useful in terms of leaving behind some
    #     evidence of pods that had existed prior to a decommission operation.
    for required in [
        "hello-0-server:task-status",
        "last-completed-update-type",
        "world-0-server:task-status",
        "world-1-server:task-status",
    ]:
        assert required in jsonobj
    # also check that the returned list was in alphabetical order:
    list_sorted = list(jsonobj)  # copy
    list_sorted.sort()
    assert list_sorted == jsonobj


@pytest.mark.sanity
def test_help_cli() -> None:
    sdk_cmd.svc_cli(config.PACKAGE_NAME, foldered_name, "help")


@pytest.mark.sanity
def test_config_cli() -> None:
    rc, stdout, _ = sdk_cmd.svc_cli(config.PACKAGE_NAME, foldered_name, "debug config list")
    assert rc == 0, "Config list fetch failed"
    configs = json.loads(stdout)
    assert len(configs) >= 1  # refrain from breaking this test if earlier tests did a config update

    rc, _, _ = sdk_cmd.svc_cli(
        config.PACKAGE_NAME,
        foldered_name,
        "debug config show {}".format(configs[0]),
        print_output=False,  # noisy output
    )
    assert rc == 0
    _check_json_output(foldered_name, "debug config target")
    _check_json_output(foldered_name, "debug config target_id")


@pytest.mark.sanity
@pytest.mark.smoke  # include in smoke: verify that cluster is healthy after earlier service config changes
def test_plan_cli() -> None:
    plan_name = "deploy"
    phase_name = "world"
    _check_json_output(foldered_name, "plan list")
    rc, _, _ = sdk_cmd.svc_cli(config.PACKAGE_NAME, foldered_name, "plan show {}".format(plan_name))
    assert rc == 0
    _check_json_output(foldered_name, "plan show --json {}".format(plan_name))
    _check_json_output(foldered_name, "plan show {} --json".format(plan_name))

    # trigger a restart so that the plan is in a non-complete state.
    # the 'interrupt' command will fail if the plan is already complete:
    rc, _, _ = sdk_cmd.svc_cli(
        config.PACKAGE_NAME, foldered_name, "plan force-restart {}".format(plan_name)
    )
    assert rc == 0
    rc, _, _ = sdk_cmd.svc_cli(
        config.PACKAGE_NAME, foldered_name, "plan interrupt {} {}".format(plan_name, phase_name)
    )
    assert rc == 0
    rc, _, _ = sdk_cmd.svc_cli(
        config.PACKAGE_NAME, foldered_name, "plan continue {} {}".format(plan_name, phase_name)
    )
    assert rc == 0

    # now wait for plan to finish before continuing to other tests:
    assert sdk_plan.wait_for_completed_plan(foldered_name, plan_name)


@pytest.mark.sanity
def test_state_cli() -> None:
    _check_json_output(foldered_name, "debug state framework_id")
    _check_json_output(foldered_name, "debug state properties")


@pytest.mark.sanity
def test_state_refresh_disable_cache() -> None:
    """Disables caching via a scheduler envvar"""
    config.check_running(foldered_name)
    task_ids = sdk_tasks.get_task_ids(foldered_name, "")

    # caching enabled by default:
    rc, stdout, _ = sdk_cmd.svc_cli(config.PACKAGE_NAME, foldered_name, "debug state refresh_cache")
    assert rc == 0, "Refresh cache failed"
    assert "Received cmd: refresh" in stdout

    marathon_config = sdk_marathon.get_config(foldered_name)
    marathon_config["env"]["DISABLE_STATE_CACHE"] = "any-text-here"
    sdk_marathon.update_app(marathon_config)

    sdk_plan.wait_for_completed_deployment(foldered_name)
    sdk_tasks.check_tasks_not_updated(foldered_name, "", task_ids)

    # caching disabled, refresh_cache should fail with a 409 error (eventually, once scheduler is up):
    @retrying.retry(wait_fixed=1000, stop_max_delay=120 * 1000, retry_on_result=lambda res: not res)
    def check_cache_refresh_fails_409conflict() -> bool:
        rc, stdout, stderr = sdk_cmd.svc_cli(
            config.PACKAGE_NAME, foldered_name, "debug state refresh_cache"
        )
        return rc != 0 and stdout == "" and "failed: 409 Conflict" in stderr

    check_cache_refresh_fails_409conflict()

    marathon_config = sdk_marathon.get_config(foldered_name)
    del marathon_config["env"]["DISABLE_STATE_CACHE"]
    sdk_marathon.update_app(marathon_config)

    sdk_plan.wait_for_completed_deployment(foldered_name)
    sdk_tasks.check_tasks_not_updated(foldered_name, "", task_ids)

    # caching reenabled, refresh_cache should succeed (eventually, once scheduler is up):
    @retrying.retry(wait_fixed=1000, stop_max_delay=120 * 1000, retry_on_result=lambda res: not res)
    def check_cache_refresh() -> str:
        rc, stdout, _ = sdk_cmd.svc_cli(
            config.PACKAGE_NAME, foldered_name, "debug state refresh_cache"
        )
        assert rc == 0, "Refresh cache failed"
        return stdout

    stdout = check_cache_refresh()
    assert "Received cmd: refresh" in stdout


@pytest.mark.sanity
def test_lock() -> None:
    """This test verifies that a second scheduler fails to startup when
    an existing scheduler is running.  Without locking, the scheduler
    would fail during registration, but after writing its config to ZK.
    So in order to verify that the scheduler fails immediately, we ensure
    that the ZK config state is unmodified."""

    def get_zk_node_data(node_name: str) -> Any:
        return sdk_cmd.cluster_request(
            "GET", "/exhibitor/exhibitor/v1/explorer/node-data?key={}".format(node_name)
        ).json()

    # Get ZK state from running framework
    zk_path = "{}/ConfigTarget".format(sdk_utils.get_zk_path(foldered_name))
    zk_config_old = get_zk_node_data(zk_path)

    # Get marathon app
    marathon_config = sdk_marathon.get_config(foldered_name)
    old_timestamp = marathon_config.get("lastTaskFailure", {}).get("timestamp", None)

    # Scale to 2 instances
    labels = marathon_config["labels"]
    original_labels = labels.copy()
    labels.pop("MARATHON_SINGLE_INSTANCE_APP")
    sdk_marathon.update_app(marathon_config)
    marathon_config["instances"] = 2
    sdk_marathon.update_app(marathon_config, wait_for_completed_deployment=False)

    @retrying.retry(wait_fixed=1000, stop_max_delay=120 * 1000, retry_on_result=lambda res: not res)
    def wait_for_second_scheduler_to_fail() -> bool:
        timestamp = (
            sdk_marathon.get_config(foldered_name).get("lastTaskFailure", {}).get("timestamp", None)
        )
        return bool(timestamp != old_timestamp)

    wait_for_second_scheduler_to_fail()

    # Verify ZK is unchanged
    zk_config_new = get_zk_node_data(zk_path)
    assert zk_config_old == zk_config_new

    # In order to prevent the second scheduler instance from obtaining a lock, we undo the "scale-up" operation
    marathon_config["instances"] = 1
    marathon_config["labels"] = original_labels
    sdk_marathon.update_app(marathon_config, force=True)


@pytest.mark.sanity
def test_tmp_directory_created() -> None:
    code, stdout, stderr = sdk_cmd.service_task_exec(
        config.SERVICE_NAME, "hello-0-server", "echo bar > /tmp/bar && cat tmp/bar | grep bar"
    )
    assert code > 0


def _check_json_output(svc_name: str, cmd: str) -> None:
    rc, stdout, _ = sdk_cmd.svc_cli(config.PACKAGE_NAME, svc_name, cmd)
    assert rc == 0, "Command failed: {}".format(cmd)
    # Check that stdout is valid json:
    json.loads(stdout)
