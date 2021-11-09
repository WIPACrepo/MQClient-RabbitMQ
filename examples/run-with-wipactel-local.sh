#!/bin/bash

if [[ `basename "$PWD"` != "MQClient-RabbitMQ" && $PWD != "/home/circleci/project" ]] ; then
	echo "ERROR: Run from 'MQClient-RabbitMQ/' (not '$PWD')"
	exit 1
fi

export WIPACTEL_EXPORT_STDOUT=${WIPACTEL_EXPORT_STDOUT:="TRUE"}
export OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4318/v1/traces"
export WIPACTEL_SERVICE_NAME_PREFIX=mqclient-rabbitmq

pip install tox
tox --notest -vv
. .tox/py/bin/activate

docker run -d -p 8084:5672 deadtrickster/rabbitmq_prometheus

python examples/worker.py --address "guest:guest@localhost:8084/%2F" &
python examples/server.py --address "guest:guest@localhost:8084/%2F"