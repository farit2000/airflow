#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
import unittest
from datetime import timedelta
from unittest import mock

import pytest

from airflow.exceptions import AirflowException
from airflow.jobs.backfill_job import BackfillJob
from airflow.models import DagBag
from airflow.utils import timezone
from tests.test_utils.config import conf_vars

try:
    from distributed import LocalCluster

    # utility functions imported from the dask testing suite to instantiate a test
    # cluster for tls tests
    from distributed.utils_test import cluster as dask_testing_cluster, get_cert, tls_security

    from airflow.executors.dask_executor import DaskExecutor

    skip_tls_tests = False
except ImportError:
    skip_tls_tests = True

DEFAULT_DATE = timezone.datetime(2017, 1, 1)
SUCCESS_COMMAND = ['airflow', 'tasks', 'run', '--help']
FAIL_COMMAND = ['airflow', 'tasks', 'run', 'false']


class TestBaseDask(unittest.TestCase):
    def assert_tasks_on_executor(self, executor, timeout_executor=120):

        # start the executor
        executor.start()

        executor.execute_async(key='success', command=SUCCESS_COMMAND)
        executor.execute_async(key='fail', command=FAIL_COMMAND)

        success_future = next(k for k, v in executor.futures.items() if v == 'success')
        fail_future = next(k for k, v in executor.futures.items() if v == 'fail')

        # wait for the futures to execute, with a timeout
        timeout = timezone.utcnow() + timedelta(seconds=timeout_executor)
        while not (success_future.done() and fail_future.done()):
            if timezone.utcnow() > timeout:
                raise ValueError(
                    'The futures should have finished; there is probably '
                    'an error communicating with the Dask cluster.'
                )

        # both tasks should have finished
        assert success_future.done()
        assert fail_future.done()

        # check task exceptions
        assert success_future.exception() is None
        assert fail_future.exception() is not None


class TestDaskExecutor(TestBaseDask):
    def setUp(self):
        self.dagbag = DagBag(include_examples=True)
        self.cluster = LocalCluster()

    def test_dask_executor_functions(self):
        executor = DaskExecutor(cluster_address=self.cluster.scheduler_address)
        self.assert_tasks_on_executor(executor, timeout_executor=120)

    def test_backfill_integration(self):
        """
        Test that DaskExecutor can be used to backfill example dags
        """
        dag = self.dagbag.get_dag('example_bash_operator')

        job = BackfillJob(
            dag=dag,
            start_date=DEFAULT_DATE,
            end_date=DEFAULT_DATE,
            ignore_first_depends_on_past=True,
            executor=DaskExecutor(cluster_address=self.cluster.scheduler_address),
        )
        job.run()

    def tearDown(self):
        self.cluster.close(timeout=5)


@pytest.mark.skipif(
    skip_tls_tests, reason="The tests are skipped because distributed framework could not be imported"
)
class TestDaskExecutorTLS(TestBaseDask):
    def setUp(self):
        self.dagbag = DagBag(include_examples=True)

    @conf_vars(
        {
            ('dask', 'tls_ca'): get_cert('tls-ca-cert.pem'),
            ('dask', 'tls_cert'): get_cert('tls-key-cert.pem'),
            ('dask', 'tls_key'): get_cert('tls-key.pem'),
        }
    )
    def test_tls(self):
        # These use test certs that ship with dask/distributed and should not be
        #  used in production
        with dask_testing_cluster(
            worker_kwargs={'security': tls_security(), "protocol": "tls"},
            scheduler_kwargs={'security': tls_security(), "protocol": "tls"},
        ) as (cluster, _):

            executor = DaskExecutor(cluster_address=cluster['address'])

            self.assert_tasks_on_executor(executor, timeout_executor=120)

            executor.end()
            # close the executor, the cluster context manager expects all listeners
            # and tasks to have completed.
            executor.client.close()

    @mock.patch('airflow.executors.dask_executor.DaskExecutor.sync')
    @mock.patch('airflow.executors.base_executor.BaseExecutor.trigger_tasks')
    @mock.patch('airflow.executors.base_executor.Stats.gauge')
    def test_gauge_executor_metrics(self, mock_stats_gauge, mock_trigger_tasks, mock_sync):
        executor = DaskExecutor()
        executor.heartbeat()
        calls = [
            mock.call('executor.open_slots', mock.ANY),
            mock.call('executor.queued_tasks', mock.ANY),
            mock.call('executor.running_tasks', mock.ANY),
        ]
        mock_stats_gauge.assert_has_calls(calls)


class TestDaskExecutorQueue(unittest.TestCase):
    def test_dask_queues_no_resources(self):
        self.cluster = LocalCluster()
        executor = DaskExecutor(cluster_address=self.cluster.scheduler_address)
        executor.start()

        with self.assertRaises(AirflowException):
            executor.execute_async(key='success', command=SUCCESS_COMMAND, queue='queue1')

    def test_dask_queues_not_available(self):
        self.cluster = LocalCluster(resources={'queue1': 1})
        executor = DaskExecutor(cluster_address=self.cluster.scheduler_address)
        executor.start()

        with self.assertRaises(AirflowException):
            # resource 'queue2' doesn't exist on cluster
            executor.execute_async(key='success', command=SUCCESS_COMMAND, queue='queue2')

    def test_dask_queues(self):
        self.cluster = LocalCluster(resources={'queue1': 1})
        executor = DaskExecutor(cluster_address=self.cluster.scheduler_address)
        executor.start()

        executor.execute_async(key='success', command=SUCCESS_COMMAND, queue='queue1')
        success_future = next(k for k, v in executor.futures.items() if v == 'success')

        # wait for the futures to execute, with a timeout
        timeout = timezone.utcnow() + timedelta(seconds=30)
        while not success_future.done():
            if timezone.utcnow() > timeout:
                raise ValueError(
                    'The futures should have finished; there is probably '
                    'an error communicating with the Dask cluster.'
                )

        assert success_future.done()
        assert success_future.exception() is None

    def test_dask_queues_no_queue_specified(self):
        self.cluster = LocalCluster(resources={'queue1': 1})
        executor = DaskExecutor(cluster_address=self.cluster.scheduler_address)
        executor.start()

        # no queue specified for executing task
        executor.execute_async(key='success', command=SUCCESS_COMMAND)
        success_future = next(k for k, v in executor.futures.items() if v == 'success')

        # wait for the futures to execute, with a timeout
        timeout = timezone.utcnow() + timedelta(seconds=30)
        while not success_future.done():
            if timezone.utcnow() > timeout:
                raise ValueError(
                    'The futures should have finished; there is probably '
                    'an error communicating with the Dask cluster.'
                )

        assert success_future.done()
        assert success_future.exception() is None

    def tearDown(self):
        self.cluster.close(timeout=5)
