"""Run integration tests for RabbitMQ backend."""

import logging

from mqclient.abstract_backend_tests import integrate_backend_interface, integrate_queue
from mqclient.abstract_backend_tests.utils import (  # pytest.fixture # noqa: F401 # pylint: disable=W0611
    queue_name,
)
from mqclient_rabbitmq.rabbitmq import Backend

logging.getLogger().setLevel(logging.DEBUG)
logging.getLogger("flake8").setLevel(logging.WARNING)
logging.getLogger("pika").setLevel(logging.WARNING)


class TestRabbitMQQueue(integrate_queue.PubSubQueue):
    """Run PubSubQueue integration tests with RabbitMQ backend."""

    backend = Backend()


class TestRabbitMQBackend(integrate_backend_interface.PubSubBackendInterface):
    """Run PubSubBackendInterface integration tests with RabbitMQ backend."""

    backend = Backend()
